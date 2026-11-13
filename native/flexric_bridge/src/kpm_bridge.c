#define _POSIX_C_SOURCE 200809L

#include "kpm_encoder.h"
#include "kpm_slice_condition.h"

#include "xApp/e42_xapp_api.h"
#include "sm/kpm_sm/kpm_sm_v02.03/kpm_sm_id.h"

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define SF_JSON_CAPACITY (256U * 1024U)
#define SF_REPORT_PERIOD_MS 1000U
#define SF_SLICE_SST 1U
#define SF_NO_SUBSCRIPTION_SD UINT32_MAX

typedef struct {
  sf_kpm_queue_t queue;
  sf_kpm_counters_t counters;
  atomic_bool writer_running;
  atomic_uint_fast64_t next_sequence;
  char connection_epoch[SF_KPM_EPOCH_CAP];
  char e2_node_id[SF_KPM_NODE_ID_CAP];
  int output_fd;
} bridge_context_t;

static bridge_context_t bridge;

static void fail(const char *message)
{
  fprintf(stderr, "smartfarm_kpm_bridge: %s\n", message);
  exit(EXIT_FAILURE);
}

static void *checked_calloc(size_t count, size_t size)
{
  void *result = calloc(count, size);
  if (result == NULL) {
    fail("memory allocation failed");
  }
  return result;
}

static uint64_t realtime_ns(void)
{
  struct timespec timestamp = {0};
  if (clock_gettime(CLOCK_REALTIME, &timestamp) != 0) {
    return 0U;
  }
  return (uint64_t)timestamp.tv_sec * 1000000000ULL + (uint64_t)timestamp.tv_nsec;
}

static uint64_t monotonic_ns(void)
{
  struct timespec timestamp = {0};
  if (clock_gettime(CLOCK_MONOTONIC, &timestamp) != 0) {
    return 0U;
  }
  return (uint64_t)timestamp.tv_sec * 1000000000ULL + (uint64_t)timestamp.tv_nsec;
}

static void make_connection_epoch(char destination[SF_KPM_EPOCH_CAP])
{
  uint8_t bytes[16] = {0};
  const ssize_t received = getrandom(bytes, sizeof(bytes), 0);
  if (received != (ssize_t)sizeof(bytes)) {
    fail("getrandom could not create the connection epoch");
  }
  bytes[6] = (uint8_t)((bytes[6] & 0x0fU) | 0x40U);
  bytes[8] = (uint8_t)((bytes[8] & 0x3fU) | 0x80U);
  const int length = snprintf(
      destination,
      SF_KPM_EPOCH_CAP,
      "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
      bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
      bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
  );
  if (length != 36) {
    fail("connection epoch formatting failed");
  }
}

static int open_output(void)
{
  const char *socket_path = getenv("SMARTFARM_KPM_SOCKET");
  if (socket_path != NULL && socket_path[0] != '\0') {
    if (strlen(socket_path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
      fail("SMARTFARM_KPM_SOCKET is too long");
    }
    const int socket_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (socket_fd < 0) {
      fail("could not create the KPM Unix socket");
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1U);
    if (connect(socket_fd, (const struct sockaddr *)&address, sizeof(address)) != 0) {
      close(socket_fd);
      fail("could not connect to SMARTFARM_KPM_SOCKET");
    }
    return socket_fd;
  }

  const int output_fd = dup(STDOUT_FILENO);
  if (output_fd < 0) {
    fail("could not duplicate stdout for JSONL");
  }
  return output_fd;
}

static void redirect_library_stdout(void)
{
  if (fflush(stdout) != 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
    fail("could not isolate FlexRIC logs from JSONL stdout");
  }
}

static bool write_all(int descriptor, const char *data, size_t length)
{
  size_t offset = 0U;
  while (offset < length) {
    const ssize_t result = write(descriptor, data + offset, length - offset);
    if (result > 0) {
      offset += (size_t)result;
      continue;
    }
    if (result < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
  return true;
}

static void log_counters(uint64_t encoded, uint64_t encode_errors, uint64_t output_errors)
{
  fprintf(
      stderr,
      "smartfarm_kpm_bridge counters "
      "received=%" PRIuFAST64 " enqueued=%" PRIuFAST64 " dropped=%" PRIuFAST64
      " unknown=%" PRIuFAST64 " encoded=%" PRIu64 " encode_errors=%" PRIu64
      " output_errors=%" PRIu64 "\n",
      atomic_load_explicit(&bridge.counters.received, memory_order_relaxed),
      atomic_load_explicit(&bridge.counters.enqueued, memory_order_relaxed),
      atomic_load_explicit(&bridge.counters.dropped, memory_order_relaxed),
      atomic_load_explicit(&bridge.counters.unknown, memory_order_relaxed),
      encoded,
      encode_errors,
      output_errors
  );
}

static void *writer_main(void *unused)
{
  (void)unused;
  sf_kpm_sample_t sample = {0};
  char *json = checked_calloc(SF_JSON_CAPACITY, sizeof(char));
  uint64_t encoded = 0U;
  uint64_t encode_errors = 0U;
  uint64_t output_errors = 0U;
  uint64_t next_report = monotonic_ns() + 1000000000ULL;
  const struct timespec idle = {.tv_sec = 0, .tv_nsec = 1000000L};

  while (atomic_load_explicit(&bridge.writer_running, memory_order_acquire) ||
         sf_kpm_queue_size(&bridge.queue) > 0U) {
    if (sf_kpm_queue_pop(&bridge.queue, &sample)) {
      size_t written = 0U;
      if (!sf_kpm_encode(&sample, json, SF_JSON_CAPACITY, &written)) {
        ++encode_errors;
      } else if (!write_all(bridge.output_fd, json, written)) {
        ++output_errors;
      } else {
        ++encoded;
      }
    } else {
      (void)nanosleep(&idle, NULL);
    }

    const uint64_t now = monotonic_ns();
    if (now >= next_report) {
      log_counters(encoded, encode_errors, output_errors);
      next_report = now + 1000000000ULL;
    }
  }
  log_counters(encoded, encode_errors, output_errors);
  free(json);
  return NULL;
}

static bool copy_byte_array_text(char *destination, size_t capacity, byte_array_t source)
{
  if (destination == NULL || capacity == 0U || source.buf == NULL || source.len == 0U) {
    return false;
  }
  const size_t length = source.len < capacity - 1U ? source.len : capacity - 1U;
  memcpy(destination, source.buf, length);
  destination[length] = '\0';
  return length == source.len;
}

static const char *measurement_unit(const char *name)
{
  if (strcmp(name, "DRB.PdcpSduVolumeDL") == 0) {
    return "Mbit";
  }
  if (strcmp(name, "DRB.RlcSduDelayDl") == 0) {
    return "us";
  }
  if (strcmp(name, "DRB.UEThpDl") == 0) {
    return "kbit/s";
  }
  if (strcmp(name, "RRU.PrbTotDl") == 0) {
    return "percent";
  }
  if (strcmp(name, "CARR.PDSCHMCSDist") == 0) {
    return "PDSCH_RBs";
  }
  if (strcmp(name, "L1M.SS-RSRP") == 0) {
    return "dBm";
  }
  if (strcmp(name, "MR.NRScSSSINR") == 0) {
    return "dB";
  }
  return NULL;
}

static void add_integer_label(sf_kpm_measurement_t *measurement, const char *name, int64_t value)
{
  if (measurement->label_count >= SF_KPM_MAX_LABELS) {
    return;
  }
  sf_kpm_label_t *label = &measurement->labels[measurement->label_count++];
  (void)snprintf(label->name, sizeof(label->name), "%s", name);
  label->kind = SF_KPM_LABEL_INTEGER;
  label->integer = value;
}

static void add_boolean_label(sf_kpm_measurement_t *measurement, const char *name, bool value)
{
  if (measurement->label_count >= SF_KPM_MAX_LABELS) {
    return;
  }
  sf_kpm_label_t *label = &measurement->labels[measurement->label_count++];
  (void)snprintf(label->name, sizeof(label->name), "%s", name);
  label->kind = SF_KPM_LABEL_BOOLEAN;
  label->boolean = value;
}

static void copy_standard_labels(
    sf_kpm_measurement_t *measurement,
    const label_info_lst_t *source,
    const meas_data_lst_t *data,
    size_t granularity_index,
    uint32_t subscription_sd
)
{
  add_integer_label(measurement, "granularity_index", (int64_t)granularity_index);
  if (subscription_sd != SF_NO_SUBSCRIPTION_SD) {
    add_integer_label(measurement, "subscription_sst", SF_SLICE_SST);
    add_integer_label(measurement, "subscription_sd", subscription_sd);
  }
  if (data->incomplete_flag != NULL) {
    add_boolean_label(
        measurement,
        "incomplete",
        *data->incomplete_flag == TRUE_ENUM_VALUE
    );
  }
  if (source->plmn_id != NULL) {
    add_integer_label(measurement, "mcc", source->plmn_id->mcc);
    add_integer_label(measurement, "mnc", source->plmn_id->mnc);
    add_integer_label(measurement, "mnc_digit_length", source->plmn_id->mnc_digit_len);
  }
  if (source->sliceID != NULL) {
    add_integer_label(measurement, "sst", source->sliceID->sST);
    if (source->sliceID->sD != NULL) {
      add_integer_label(measurement, "sd", *source->sliceID->sD);
    }
  }
  if (source->fiveQI != NULL) {
    add_integer_label(measurement, "five_qi", *source->fiveQI);
  }
  if (source->qFI != NULL) {
    add_integer_label(measurement, "qfi", *source->qFI);
  }
  if (source->qCI != NULL) {
    add_integer_label(measurement, "qci", *source->qCI);
  }
  if (source->distBinX != NULL) {
    add_integer_label(measurement, "dist_bin_x", *source->distBinX);
  }
  if (source->distBinY != NULL) {
    add_integer_label(measurement, "dist_bin_y", *source->distBinY);
  }
  if (source->distBinZ != NULL) {
    add_integer_label(measurement, "dist_bin_z", *source->distBinZ);
  }
  if (source->ssbIndex != NULL) {
    add_integer_label(measurement, "ssb_index", *source->ssbIndex);
  }
  if (source->layerMU_MIMO != NULL) {
    add_integer_label(measurement, "mu_mimo_layer", *source->layerMU_MIMO);
  }
  if (source->mimoModeIndex != NULL) {
    add_integer_label(measurement, "mimo_mode_index", *source->mimoModeIndex);
  }
}

static bool format_ue_id(char destination[SF_KPM_UE_ID_CAP], const ue_id_e2sm_t *ue)
{
  int length = -1;
  switch (ue->type) {
    case GNB_UE_ID_E2SM:
      if (ue->gnb.ran_ue_id != NULL) {
        length = snprintf(destination, SF_KPM_UE_ID_CAP, "gnb-ran:%" PRIu64,
                          *ue->gnb.ran_ue_id);
      } else {
        length = snprintf(destination, SF_KPM_UE_ID_CAP, "gnb-amf-ngap:%" PRIu64,
                          ue->gnb.amf_ue_ngap_id);
      }
      break;
    case GNB_DU_UE_ID_E2SM:
      length = snprintf(destination, SF_KPM_UE_ID_CAP, "gnb-du-f1ap:%" PRIu32,
                        ue->gnb_du.gnb_cu_ue_f1ap);
      break;
    case GNB_CU_UP_UE_ID_E2SM:
      length = snprintf(destination, SF_KPM_UE_ID_CAP, "gnb-cu-up-e1ap:%" PRIu32,
                        ue->gnb_cu_up.gnb_cu_cp_ue_e1ap);
      break;
    default:
      length = snprintf(destination, SF_KPM_UE_ID_CAP, "e2sm-type:%d", (int)ue->type);
      break;
  }
  return length > 0 && (size_t)length < SF_KPM_UE_ID_CAP;
}

static void initialize_sample(
    sf_kpm_sample_t *sample,
    uint64_t collected_at_ns,
    const ue_id_e2sm_t *ue
)
{
  memset(sample, 0, sizeof(*sample));
  (void)snprintf(
      sample->connection_epoch,
      sizeof(sample->connection_epoch),
      "%s",
      bridge.connection_epoch
  );
  sample->collected_at_ns = collected_at_ns;
  (void)snprintf(sample->e2_node_id, sizeof(sample->e2_node_id), "%s", bridge.e2_node_id);
  if (ue != NULL) {
    sample->has_ue_id = format_ue_id(sample->ue_id, ue);
  }
}

static void submit_sample(sf_kpm_sample_t *sample)
{
  if (sample->measurement_count == 0U) {
    return;
  }
  sample->sequence = atomic_fetch_add_explicit(
      &bridge.next_sequence,
      1U,
      memory_order_relaxed
  );
  (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, sample);
}

static bool copy_measurement(
    sf_kpm_measurement_t *destination,
    const meas_info_format_1_lst_t *information,
    const meas_record_lst_t *record,
    const label_info_lst_t *label,
    const meas_data_lst_t *data,
    size_t granularity_index,
    uint32_t subscription_sd
)
{
  memset(destination, 0, sizeof(*destination));
  if (information->meas_type.type == NAME_MEAS_TYPE) {
    if (!copy_byte_array_text(
            destination->name,
            sizeof(destination->name),
            information->meas_type.name)) {
      (void)snprintf(destination->name, sizeof(destination->name), "TRUNCATED.NAME");
    }
  } else if (information->meas_type.type == ID_MEAS_TYPE) {
    (void)snprintf(
        destination->name,
        sizeof(destination->name),
        "measurement-id:%" PRIu16,
        information->meas_type.id
    );
  } else {
    return false;
  }
  destination->supported = sf_kpm_is_supported(destination->name);
  const char *unit = measurement_unit(destination->name);
  if (unit != NULL) {
    destination->has_unit = true;
    (void)snprintf(destination->unit, sizeof(destination->unit), "%s", unit);
  }
  switch (record->value) {
    case INTEGER_MEAS_VALUE:
      destination->kind = SF_KPM_INTEGER;
      destination->integer = record->int_val;
      break;
    case REAL_MEAS_VALUE:
      destination->kind = SF_KPM_REAL;
      destination->real = record->real_val;
      break;
    case NO_VALUE_MEAS_VALUE:
      destination->kind = SF_KPM_NO_VALUE;
      break;
    default:
      return false;
  }
  copy_standard_labels(destination, label, data, granularity_index, subscription_sd);
  return true;
}

static void extract_format_1(
    const kpm_ind_msg_format_1_t *message,
    uint64_t collected_at_ns,
    const ue_id_e2sm_t *ue,
    uint32_t subscription_sd
)
{
  if (message == NULL || message->meas_info_lst == NULL ||
      message->meas_info_lst_len == 0U || message->meas_data_lst == NULL) {
    (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
    return;
  }

  for (size_t data_index = 0; data_index < message->meas_data_lst_len; ++data_index) {
    const meas_data_lst_t *data = &message->meas_data_lst[data_index];
    sf_kpm_sample_t sample;
    initialize_sample(&sample, collected_at_ns, ue);
    size_t record_index = 0U;

    for (size_t info_index = 0; info_index < message->meas_info_lst_len; ++info_index) {
      const meas_info_format_1_lst_t *information = &message->meas_info_lst[info_index];
      for (size_t label_index = 0; label_index < information->label_info_lst_len;
           ++label_index) {
        if (record_index >= data->meas_record_len) {
          submit_sample(&sample);
          (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
          return;
        }
        if (sample.measurement_count == SF_KPM_MAX_MEASUREMENTS) {
          submit_sample(&sample);
          initialize_sample(&sample, collected_at_ns, ue);
        }
        sf_kpm_measurement_t *measurement =
            &sample.measurements[sample.measurement_count];
        if (copy_measurement(
            measurement,
            information,
            &data->meas_record_lst[record_index++],
            &information->label_info_lst[label_index],
            data,
            data_index,
            subscription_sd
        )) {
          ++sample.measurement_count;
        } else {
          (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
        }
      }
    }
    submit_sample(&sample);
    if (record_index != data->meas_record_len) {
      (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
    }
  }
}

static void handle_kpm_indication(const sm_ag_if_rd_t *read, uint32_t subscription_sd)
{
  if (read == NULL || read->type != INDICATION_MSG_AGENT_IF_ANS_V0 ||
      read->ind.type != KPM_STATS_V3_0) {
    (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
    return;
  }
  const kpm_ind_data_t *indication = &read->ind.kpm.ind;
  uint64_t collected_at_ns = realtime_ns();
  if (indication->hdr.type == FORMAT_1_INDICATION_HEADER) {
    const uint32_t start = indication->hdr.kpm_ric_ind_hdr_format_1.collectStartTime;
    if (start > 0U) {
      collected_at_ns = (uint64_t)start * 1000000000ULL;
    }
  }
  if (collected_at_ns == 0U) {
    (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
    return;
  }

  if (indication->msg.type == FORMAT_1_INDICATION_MESSAGE) {
    extract_format_1(&indication->msg.frm_1, collected_at_ns, NULL, subscription_sd);
    return;
  }
  if (indication->msg.type == FORMAT_3_INDICATION_MESSAGE) {
    const kpm_ind_msg_format_3_t *format_3 = &indication->msg.frm_3;
    for (size_t i = 0; i < format_3->ue_meas_report_lst_len; ++i) {
      extract_format_1(
          &format_3->meas_report_per_ue[i].ind_msg_format_1,
          collected_at_ns,
          &format_3->meas_report_per_ue[i].ue_meas_report_lst,
          subscription_sd
      );
    }
    return;
  }
  (void)sf_kpm_callback_enqueue(&bridge.queue, &bridge.counters, NULL);
}

/* FlexRIC callbacks carry no user data, so each slice subscription gets its own. */
static void kpm_callback_node(const sm_ag_if_rd_t *read)
{
  handle_kpm_indication(read, SF_NO_SUBSCRIPTION_SD);
}

static void kpm_callback_slice_0(const sm_ag_if_rd_t *read)
{
  uint32_t sd = SF_NO_SUBSCRIPTION_SD;
  (void)sf_kpm_project_slice_sd(0U, &sd);
  handle_kpm_indication(read, sd);
}

static void kpm_callback_slice_1(const sm_ag_if_rd_t *read)
{
  uint32_t sd = SF_NO_SUBSCRIPTION_SD;
  (void)sf_kpm_project_slice_sd(1U, &sd);
  handle_kpm_indication(read, sd);
}

static void kpm_callback_slice_2(const sm_ag_if_rd_t *read)
{
  uint32_t sd = SF_NO_SUBSCRIPTION_SD;
  (void)sf_kpm_project_slice_sd(2U, &sd);
  handle_kpm_indication(read, sd);
}

static void (*const slice_callbacks[])(const sm_ag_if_rd_t *) = {
    kpm_callback_slice_0,
    kpm_callback_slice_1,
    kpm_callback_slice_2,
};

static label_info_lst_t no_label(void)
{
  label_info_lst_t label = {0};
  label.noLabel = checked_calloc(1U, sizeof(*label.noLabel));
  *label.noLabel = TRUE_ENUM_VALUE;
  return label;
}

static label_info_lst_t distribution_label(uint16_t x, const uint16_t *y, const uint16_t *z)
{
  label_info_lst_t label = {0};
  label.distBinX = checked_calloc(1U, sizeof(*label.distBinX));
  *label.distBinX = x;
  if (y != NULL) {
    label.distBinY = checked_calloc(1U, sizeof(*label.distBinY));
    *label.distBinY = *y;
  }
  if (z != NULL) {
    label.distBinZ = checked_calloc(1U, sizeof(*label.distBinZ));
    *label.distBinZ = *z;
  }
  return label;
}

static void fill_requested_labels(meas_info_format_1_lst_t *measurement)
{
  char name[SF_KPM_NAME_CAP] = {0};
  (void)copy_byte_array_text(name, sizeof(name), measurement->meas_type.name);
  if (strcmp(name, "CARR.PDSCHMCSDist") == 0) {
    measurement->label_info_lst_len = 8U * 3U * 32U;
    measurement->label_info_lst = checked_calloc(
        measurement->label_info_lst_len,
        sizeof(*measurement->label_info_lst)
    );
    size_t index = 0U;
    for (uint16_t rank = 1U; rank <= 8U; ++rank) {
      for (uint16_t table = 1U; table <= 3U; ++table) {
        for (uint16_t mcs = 0U; mcs <= 31U; ++mcs) {
          measurement->label_info_lst[index++] = distribution_label(rank, &table, &mcs);
        }
      }
    }
  } else if (strcmp(name, "L1M.SS-RSRP") == 0 ||
             strcmp(name, "MR.NRScSSSINR") == 0) {
    measurement->label_info_lst_len = 128U;
    measurement->label_info_lst = checked_calloc(
        measurement->label_info_lst_len,
        sizeof(*measurement->label_info_lst)
    );
    for (uint16_t bin = 0U; bin <= 127U; ++bin) {
      measurement->label_info_lst[bin] = distribution_label(bin, NULL, NULL);
    }
  } else {
    measurement->label_info_lst_len = 1U;
    measurement->label_info_lst = checked_calloc(1U, sizeof(*measurement->label_info_lst));
    measurement->label_info_lst[0] = no_label();
  }
}

static size_t desired_advertised_count(const ric_report_style_item_t *report)
{
  size_t count = 0U;
  for (size_t i = 0; i < report->meas_info_for_action_lst_len; ++i) {
    char name[SF_KPM_NAME_CAP] = {0};
    if (copy_byte_array_text(
            name,
            sizeof(name),
            report->meas_info_for_action_lst[i].name) &&
        sf_kpm_is_supported(name)) {
      ++count;
    }
  }
  return count;
}

static kpm_act_def_format_1_t make_action_format_1(const ric_report_style_item_t *report)
{
  kpm_act_def_format_1_t action = {0};
  action.meas_info_lst_len = desired_advertised_count(report);
  action.meas_info_lst = checked_calloc(action.meas_info_lst_len, sizeof(*action.meas_info_lst));

  size_t destination = 0U;
  for (size_t i = 0; i < report->meas_info_for_action_lst_len; ++i) {
    char name[SF_KPM_NAME_CAP] = {0};
    if (!copy_byte_array_text(
            name,
            sizeof(name),
            report->meas_info_for_action_lst[i].name) ||
        !sf_kpm_is_supported(name)) {
      continue;
    }
    meas_info_format_1_lst_t *measurement = &action.meas_info_lst[destination++];
    measurement->meas_type.type = NAME_MEAS_TYPE;
    measurement->meas_type.name = copy_byte_array(report->meas_info_for_action_lst[i].name);
    fill_requested_labels(measurement);
  }
  action.gran_period_ms = SF_REPORT_PERIOD_MS;
  return action;
}

static test_info_lst_t make_snssai_condition(uint32_t sd)
{
  uint8_t encoded[4] = {0U};
  if (!sf_kpm_encode_snssai(SF_SLICE_SST, sd, encoded)) {
    fail("invalid project slice SD");
  }
  test_info_lst_t condition = {0};
  condition.test_cond_type = S_NSSAI_TEST_COND_TYPE;
  condition.S_NSSAI = TRUE_TEST_COND_TYPE;
  condition.test_cond = checked_calloc(1U, sizeof(*condition.test_cond));
  *condition.test_cond = EQUAL_TEST_COND;
  condition.test_cond_value = checked_calloc(1U, sizeof(*condition.test_cond_value));
  condition.test_cond_value->type = OCTET_STRING_TEST_COND_VALUE;
  condition.test_cond_value->octet_string_value = checked_calloc(
      1U,
      sizeof(*condition.test_cond_value->octet_string_value)
  );
  condition.test_cond_value->octet_string_value->len = sizeof(encoded);
  condition.test_cond_value->octet_string_value->buf =
      checked_calloc(sizeof(encoded), sizeof(uint8_t));
  memcpy(condition.test_cond_value->octet_string_value->buf, encoded, sizeof(encoded));
  return condition;
}

static bool make_subscription(
    const kpm_ran_function_def_t *definition,
    const ric_report_style_item_t *report,
    uint32_t sd,
    kpm_sub_data_t *subscription
)
{
  memset(subscription, 0, sizeof(*subscription));
  const size_t measurement_count = desired_advertised_count(report);
  if (measurement_count == 0U ||
      definition->ric_event_trigger_style_list == NULL ||
      definition->ric_event_trigger_style_list[0].format_type !=
          FORMAT_1_RIC_EVENT_TRIGGER) {
    return false;
  }
  subscription->ev_trg_def.type = FORMAT_1_RIC_EVENT_TRIGGER;
  subscription->ev_trg_def.kpm_ric_event_trigger_format_1.report_period_ms =
      SF_REPORT_PERIOD_MS;
  subscription->sz_ad = 1U;
  subscription->ad = checked_calloc(1U, sizeof(*subscription->ad));

  if (report->act_def_format_type == FORMAT_1_ACTION_DEFINITION) {
    subscription->ad[0].type = FORMAT_1_ACTION_DEFINITION;
    subscription->ad[0].frm_1 = make_action_format_1(report);
    return true;
  }
  if (report->act_def_format_type == FORMAT_4_ACTION_DEFINITION) {
    subscription->ad[0].type = FORMAT_4_ACTION_DEFINITION;
    subscription->ad[0].frm_4.matching_cond_lst_len = 1U;
    subscription->ad[0].frm_4.matching_cond_lst = checked_calloc(
        1U,
        sizeof(*subscription->ad[0].frm_4.matching_cond_lst)
    );
    subscription->ad[0].frm_4.matching_cond_lst[0].test_info_lst =
        make_snssai_condition(sd);
    subscription->ad[0].frm_4.action_def_format_1 = make_action_format_1(report);
    return true;
  }
  free_kpm_sub_data(subscription);
  memset(subscription, 0, sizeof(*subscription));
  return false;
}

static sm_ran_function_t *find_kpm_function(e2_node_connected_xapp_t *node)
{
  for (size_t i = 0; i < node->len_rf; ++i) {
    if (node->rf[i].id == SM_KPM_ID && node->rf[i].defn.type == KPM_RAN_FUNC_DEF_E) {
      return &node->rf[i];
    }
  }
  return NULL;
}

static uint32_t discovery_timeout_seconds(void)
{
  const char *configured = getenv("SMARTFARM_E2_DISCOVERY_TIMEOUT_S");
  if (configured == NULL || configured[0] == '\0') {
    return 600U;
  }
  char *end = NULL;
  errno = 0;
  const unsigned long parsed = strtoul(configured, &end, 10);
  if (errno != 0 || end == configured || *end != '\0' || parsed == 0U ||
      parsed > 86400U) {
    fail("SMARTFARM_E2_DISCOVERY_TIMEOUT_S must be in [1, 86400]");
  }
  return (uint32_t)parsed;
}

static bool format_node_id(
    char destination[SF_KPM_NODE_ID_CAP],
    const global_e2_node_id_t *node
)
{
  const int mnc_width = node->plmn.mnc_digit_len == 2U ? 2 : 3;
  int length;
  if (node->cu_du_id != NULL) {
    length = snprintf(
        destination,
        SF_KPM_NODE_ID_CAP,
        "e2:%d:%03" PRIu16 "-%0*" PRIu16 ":%" PRIu32 ":%" PRIu64,
        (int)node->type,
        node->plmn.mcc,
        mnc_width,
        node->plmn.mnc,
        node->nb_id.nb_id,
        *node->cu_du_id
    );
  } else {
    length = snprintf(
        destination,
        SF_KPM_NODE_ID_CAP,
        "e2:%d:%03" PRIu16 "-%0*" PRIu16 ":%" PRIu32,
        (int)node->type,
        node->plmn.mcc,
        mnc_width,
        node->plmn.mnc,
        node->nb_id.nb_id
    );
  }
  return length > 0 && (size_t)length < SF_KPM_NODE_ID_CAP;
}

int main(int argc, char **argv)
{
  (void)signal(SIGPIPE, SIG_IGN);
  bridge.output_fd = open_output();
  redirect_library_stdout();
  make_connection_epoch(bridge.connection_epoch);
  sf_kpm_queue_init(&bridge.queue);
  sf_kpm_counters_init(&bridge.counters);
  atomic_init(&bridge.writer_running, true);
  atomic_init(&bridge.next_sequence, 0U);

  const fr_args_t arguments = init_fr_args(argc, argv);
  init_xapp_api(&arguments);
  e2_node_arr_xapp_t nodes = {0};
  e2_node_connected_xapp_t *selected_node = NULL;
  sm_ran_function_t *kpm_function = NULL;
  size_t matching_nodes = 0U;
  const uint32_t discovery_timeout = discovery_timeout_seconds();
  const struct timespec discovery_delay = {.tv_sec = 1, .tv_nsec = 0};
  for (uint32_t waited = 0U; waited < discovery_timeout; ++waited) {
    nodes = e2_nodes_xapp_api();
    for (size_t i = 0; i < nodes.len; ++i) {
      sm_ran_function_t *candidate = find_kpm_function(&nodes.n[i]);
      if (candidate != NULL) {
        ++matching_nodes;
        if (selected_node == NULL) {
          selected_node = &nodes.n[i];
          kpm_function = candidate;
        }
      }
    }
    if (selected_node != NULL) {
      break;
    }
    free_e2_node_arr_xapp(&nodes);
    memset(&nodes, 0, sizeof(nodes));
    if (waited == 0U || (waited + 1U) % 10U == 0U) {
      fprintf(
          stderr,
          "smartfarm_kpm_bridge: waiting for an E2 node advertising KPM "
          "(%u/%u s)\n",
          waited + 1U,
          discovery_timeout
      );
    }
    (void)nanosleep(&discovery_delay, NULL);
  }
  if (selected_node == NULL || kpm_function == NULL) {
    free_e2_node_arr_xapp(&nodes);
    fail("E2 discovery timeout expired without a node advertising KPM");
  }
  if (matching_nodes > 1U) {
    fprintf(
        stderr,
        "smartfarm_kpm_bridge: %zu KPM nodes discovered; using the first to keep "
        "callback attribution deterministic\n",
        matching_nodes
    );
  }
  if (!format_node_id(bridge.e2_node_id, &selected_node->id)) {
    free_e2_node_arr_xapp(&nodes);
    fail("E2 node identity is too long");
  }

  pthread_t writer;
  if (pthread_create(&writer, NULL, writer_main, NULL) != 0) {
    free_e2_node_arr_xapp(&nodes);
    fail("could not start JSONL writer");
  }

  _Static_assert(
      sizeof(slice_callbacks) / sizeof(slice_callbacks[0]) == 3U,
      "one callback per project slice"
  );
  if (sf_kpm_project_slice_count() != sizeof(slice_callbacks) / sizeof(slice_callbacks[0])) {
    fail("project slice table and callback table disagree");
  }
  const size_t style_count = kpm_function->defn.kpm.sz_ric_report_style_list;
  const size_t max_handles = style_count * sf_kpm_project_slice_count();
  sm_ans_xapp_t *handles = checked_calloc(max_handles, sizeof(*handles));
  size_t handle_count = 0U;
  for (size_t i = 0; i < style_count; ++i) {
    ric_report_style_item_t *report = &kpm_function->defn.kpm.ric_report_style_list[i];
    const bool per_slice = report->act_def_format_type == FORMAT_4_ACTION_DEFINITION;
    const size_t variants = per_slice ? sf_kpm_project_slice_count() : 1U;
    for (size_t slice = 0; slice < variants; ++slice) {
      uint32_t sd = SF_NO_SUBSCRIPTION_SD;
      if (per_slice && !sf_kpm_project_slice_sd(slice, &sd)) {
        fail("project slice lookup failed");
      }
      kpm_sub_data_t subscription = {0};
      if (!make_subscription(&kpm_function->defn.kpm, report, sd, &subscription)) {
        continue;
      }
      const sm_ans_xapp_t answer = report_sm_xapp_api(
          &selected_node->id,
          SM_KPM_ID,
          &subscription,
          per_slice ? slice_callbacks[slice] : kpm_callback_node
      );
      free_kpm_sub_data(&subscription);
      if (!answer.success) {
        fprintf(stderr, "smartfarm_kpm_bridge: KPM subscription rejected\n");
        continue;
      }
      if (per_slice) {
        fprintf(
            stderr,
            "smartfarm_kpm_bridge: UE-level KPM subscribed for S-NSSAI sst=%u sd=0x%06" PRIx32 "\n",
            SF_SLICE_SST,
            sd
        );
      }
      handles[handle_count++] = answer;
    }
  }
  if (handle_count == 0U) {
    atomic_store_explicit(&bridge.writer_running, false, memory_order_release);
    (void)pthread_join(writer, NULL);
    free(handles);
    free_e2_node_arr_xapp(&nodes);
    fail("no compatible KPM report style could be subscribed");
  }

  fprintf(
      stderr,
      "smartfarm_kpm_bridge: node=%s epoch=%s subscriptions=%zu output=%s\n",
      bridge.e2_node_id,
      bridge.connection_epoch,
      handle_count,
      getenv("SMARTFARM_KPM_SOCKET") != NULL ? "unix-socket" : "stdout"
  );
  xapp_wait_end_api();

  for (size_t i = 0; i < handle_count; ++i) {
    rm_report_sm_xapp_api(handles[i].u.handle);
  }
  free(handles);
  atomic_store_explicit(&bridge.writer_running, false, memory_order_release);
  (void)pthread_join(writer, NULL);
  free_e2_node_arr_xapp(&nodes);
  while (!try_stop_xapp_api()) {
    const struct timespec retry = {.tv_sec = 0, .tv_nsec = 1000000L};
    (void)nanosleep(&retry, NULL);
  }
  close(bridge.output_fd);
  return EXIT_SUCCESS;
}
