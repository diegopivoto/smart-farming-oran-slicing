#include "kpm_encoder.h"

#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

typedef struct {
  char *out;
  size_t cap;
  size_t len;
  bool ok;
} json_buffer_t;

static const char *const desired_measurements[] = {
    "DRB.PdcpSduVolumeDL",
    "DRB.RlcSduDelayDl",
    "DRB.UEThpDl",
    "RRU.PrbTotDl",
    "CARR.PDSCHMCSDist",
    "L1M.SS-RSRP",
    "MR.NRScSSSINR",
};

static bool terminated_within(const char *value, size_t cap)
{
  return value != NULL && memchr(value, '\0', cap) != NULL;
}

static void append_bytes(json_buffer_t *buffer, const char *value, size_t length)
{
  if (!buffer->ok || length > buffer->cap - buffer->len - 1U) {
    buffer->ok = false;
    return;
  }
  memcpy(buffer->out + buffer->len, value, length);
  buffer->len += length;
  buffer->out[buffer->len] = '\0';
}

static void append_literal(json_buffer_t *buffer, const char *value)
{
  append_bytes(buffer, value, strlen(value));
}

static void append_format(json_buffer_t *buffer, const char *format, ...)
{
  if (!buffer->ok) {
    return;
  }
  va_list arguments;
  va_start(arguments, format);
  const size_t remaining = buffer->cap - buffer->len;
  const int result = vsnprintf(buffer->out + buffer->len, remaining, format, arguments);
  va_end(arguments);
  if (result < 0 || (size_t)result >= remaining) {
    buffer->ok = false;
    return;
  }
  buffer->len += (size_t)result;
}

static void append_json_string(json_buffer_t *buffer, const char *value)
{
  static const char hex[] = "0123456789abcdef";
  append_literal(buffer, "\"");
  for (const unsigned char *cursor = (const unsigned char *)value;
       buffer->ok && *cursor != '\0'; ++cursor) {
    switch (*cursor) {
      case '"':
        append_literal(buffer, "\\\"");
        break;
      case '\\':
        append_literal(buffer, "\\\\");
        break;
      case '\b':
        append_literal(buffer, "\\b");
        break;
      case '\f':
        append_literal(buffer, "\\f");
        break;
      case '\n':
        append_literal(buffer, "\\n");
        break;
      case '\r':
        append_literal(buffer, "\\r");
        break;
      case '\t':
        append_literal(buffer, "\\t");
        break;
      default:
        if (*cursor < 0x20U) {
          char escaped[7] = {'\\', 'u', '0', '0', hex[*cursor >> 4U], hex[*cursor & 0x0fU], '\0'};
          append_literal(buffer, escaped);
        } else {
          append_bytes(buffer, (const char *)cursor, 1U);
        }
        break;
    }
  }
  append_literal(buffer, "\"");
}

static bool valid_uuid(const char *value)
{
  if (!terminated_within(value, SF_KPM_EPOCH_CAP) || strlen(value) != 36U) {
    return false;
  }
  for (size_t i = 0; i < 36U; ++i) {
    const bool hyphen = i == 8U || i == 13U || i == 18U || i == 23U;
    const bool digit = value[i] >= '0' && value[i] <= '9';
    const bool lower = value[i] >= 'a' && value[i] <= 'f';
    const bool upper = value[i] >= 'A' && value[i] <= 'F';
    if ((hyphen && value[i] != '-') || (!hyphen && !digit && !lower && !upper)) {
      return false;
    }
  }
  return true;
}

bool sf_kpm_is_supported(const char *name)
{
  if (name == NULL) {
    return false;
  }
  const size_t count = sizeof(desired_measurements) / sizeof(desired_measurements[0]);
  for (size_t i = 0; i < count; ++i) {
    if (strcmp(name, desired_measurements[i]) == 0) {
      return true;
    }
  }
  return false;
}

static bool valid_measurement(const sf_kpm_measurement_t *measurement)
{
  if (!terminated_within(measurement->name, sizeof(measurement->name)) ||
      measurement->name[0] == '\0' || measurement->label_count > SF_KPM_MAX_LABELS ||
      measurement->supported != sf_kpm_is_supported(measurement->name)) {
    return false;
  }
  if (measurement->kind != SF_KPM_INTEGER && measurement->kind != SF_KPM_REAL &&
      measurement->kind != SF_KPM_NO_VALUE) {
    return false;
  }
  if (measurement->kind == SF_KPM_REAL && !isfinite(measurement->real)) {
    return false;
  }
  if (measurement->has_unit &&
      (!terminated_within(measurement->unit, sizeof(measurement->unit)) ||
       measurement->unit[0] == '\0')) {
    return false;
  }
  for (size_t i = 0; i < measurement->label_count; ++i) {
    const sf_kpm_label_t *label = &measurement->labels[i];
    if (!terminated_within(label->name, sizeof(label->name)) || label->name[0] == '\0' ||
        label->kind > SF_KPM_LABEL_STRING) {
      return false;
    }
    if (label->kind == SF_KPM_LABEL_REAL && !isfinite(label->real)) {
      return false;
    }
    if (label->kind == SF_KPM_LABEL_STRING &&
        !terminated_within(label->string, sizeof(label->string))) {
      return false;
    }
  }
  return true;
}

static bool valid_sample(const sf_kpm_sample_t *sample)
{
  if (sample == NULL || !valid_uuid(sample->connection_epoch) ||
      !terminated_within(sample->e2_node_id, sizeof(sample->e2_node_id)) ||
      sample->e2_node_id[0] == '\0' || sample->collected_at_ns == 0U ||
      sample->measurement_count == 0U ||
      sample->measurement_count > SF_KPM_MAX_MEASUREMENTS) {
    return false;
  }
  if (sample->has_ue_id &&
      (!terminated_within(sample->ue_id, sizeof(sample->ue_id)) || sample->ue_id[0] == '\0')) {
    return false;
  }
  for (size_t i = 0; i < sample->measurement_count; ++i) {
    if (!valid_measurement(&sample->measurements[i])) {
      return false;
    }
  }
  return true;
}

static void append_label_value(json_buffer_t *buffer, const sf_kpm_label_t *label)
{
  switch (label->kind) {
    case SF_KPM_LABEL_INTEGER:
      append_format(buffer, "%" PRId64, label->integer);
      break;
    case SF_KPM_LABEL_REAL:
      append_format(buffer, "%.17g", label->real);
      break;
    case SF_KPM_LABEL_BOOLEAN:
      append_literal(buffer, label->boolean ? "true" : "false");
      break;
    case SF_KPM_LABEL_STRING:
      append_json_string(buffer, label->string);
      break;
  }
}

static void append_measurement(json_buffer_t *buffer, const sf_kpm_measurement_t *measurement)
{
  append_literal(buffer, "{\"name\":");
  append_json_string(buffer, measurement->name);
  switch (measurement->kind) {
    case SF_KPM_INTEGER:
      append_literal(buffer, ",\"kind\":\"integer\",\"value\":");
      append_format(buffer, "%" PRId64, measurement->integer);
      break;
    case SF_KPM_REAL:
      append_literal(buffer, ",\"kind\":\"real\",\"value\":");
      append_format(buffer, "%.17g", measurement->real);
      break;
    case SF_KPM_NO_VALUE:
      append_literal(buffer, ",\"kind\":\"no_value\",\"value\":null");
      break;
  }
  append_literal(buffer, ",\"unit\":");
  if (measurement->has_unit) {
    append_json_string(buffer, measurement->unit);
  } else {
    append_literal(buffer, "null");
  }
  append_literal(buffer, measurement->supported ? ",\"supported\":true,\"labels\":{"
                                                : ",\"supported\":false,\"labels\":{");
  for (size_t i = 0; i < measurement->label_count; ++i) {
    if (i > 0U) {
      append_literal(buffer, ",");
    }
    append_json_string(buffer, measurement->labels[i].name);
    append_literal(buffer, ":");
    append_label_value(buffer, &measurement->labels[i]);
  }
  append_literal(buffer, "}}");
}

bool sf_kpm_encode(const sf_kpm_sample_t *sample, char *out, size_t cap, size_t *written)
{
  if (written == NULL) {
    return false;
  }
  *written = 0U;
  if (out == NULL || cap == 0U) {
    return false;
  }
  out[0] = '\0';
  if (!valid_sample(sample)) {
    return false;
  }

  json_buffer_t buffer = {.out = out, .cap = cap, .len = 0U, .ok = true};
  append_literal(&buffer, "{\"schema_version\":1,\"connection_epoch\":");
  append_json_string(&buffer, sample->connection_epoch);
  append_format(&buffer, ",\"sequence\":%" PRIu64 ",\"collected_at_ns\":%" PRIu64,
                sample->sequence, sample->collected_at_ns);
  append_literal(&buffer, ",\"e2_node_id\":");
  append_json_string(&buffer, sample->e2_node_id);
  append_literal(&buffer, ",\"ue_id\":");
  if (sample->has_ue_id) {
    append_json_string(&buffer, sample->ue_id);
  } else {
    append_literal(&buffer, "null");
  }
  append_literal(&buffer, ",\"measurements\":[");
  for (size_t i = 0; i < sample->measurement_count; ++i) {
    if (i > 0U) {
      append_literal(&buffer, ",");
    }
    append_measurement(&buffer, &sample->measurements[i]);
  }
  append_literal(&buffer, "]}\n");

  if (!buffer.ok) {
    out[0] = '\0';
    return false;
  }
  *written = buffer.len;
  return true;
}

void sf_kpm_queue_init(sf_kpm_queue_t *queue)
{
  if (queue == NULL) {
    return;
  }
  atomic_init(&queue->gate, false);
  queue->head = 0U;
  queue->tail = 0U;
  atomic_init(&queue->count, 0U);
}

bool sf_kpm_queue_push(sf_kpm_queue_t *queue, const sf_kpm_sample_t *sample)
{
  if (queue == NULL || sample == NULL ||
      atomic_exchange_explicit(&queue->gate, true, memory_order_acquire)) {
    return false;
  }
  const size_t count = atomic_load_explicit(&queue->count, memory_order_relaxed);
  if (count == SF_KPM_QUEUE_CAPACITY) {
    atomic_store_explicit(&queue->gate, false, memory_order_release);
    return false;
  }
  queue->slots[queue->tail] = *sample;
  queue->tail = (queue->tail + 1U) % SF_KPM_QUEUE_CAPACITY;
  atomic_fetch_add_explicit(&queue->count, 1U, memory_order_release);
  atomic_store_explicit(&queue->gate, false, memory_order_release);
  return true;
}

bool sf_kpm_queue_pop(sf_kpm_queue_t *queue, sf_kpm_sample_t *sample)
{
  if (queue == NULL || sample == NULL) {
    return false;
  }
  const size_t count = atomic_load_explicit(&queue->count, memory_order_acquire);
  if (count == 0U) {
    return false;
  }
  *sample = queue->slots[queue->head];
  queue->head = (queue->head + 1U) % SF_KPM_QUEUE_CAPACITY;
  atomic_fetch_sub_explicit(&queue->count, 1U, memory_order_release);
  return true;
}

size_t sf_kpm_queue_size(const sf_kpm_queue_t *queue)
{
  return queue == NULL ? 0U : atomic_load_explicit(&queue->count, memory_order_acquire);
}

void sf_kpm_counters_init(sf_kpm_counters_t *counters)
{
  if (counters == NULL) {
    return;
  }
  atomic_init(&counters->received, 0U);
  atomic_init(&counters->enqueued, 0U);
  atomic_init(&counters->dropped, 0U);
  atomic_init(&counters->unknown, 0U);
}

bool sf_kpm_callback_enqueue(
    sf_kpm_queue_t *queue,
    sf_kpm_counters_t *counters,
    const sf_kpm_sample_t *sample
)
{
  if (counters == NULL) {
    return false;
  }
  atomic_fetch_add_explicit(&counters->received, 1U, memory_order_relaxed);
  if (sample != NULL) {
    const size_t count = sample->measurement_count > SF_KPM_MAX_MEASUREMENTS
                             ? SF_KPM_MAX_MEASUREMENTS
                             : sample->measurement_count;
    for (size_t i = 0; i < count; ++i) {
      if (!sf_kpm_is_supported(sample->measurements[i].name)) {
        atomic_fetch_add_explicit(&counters->unknown, 1U, memory_order_relaxed);
      }
    }
  }
  if (!sf_kpm_queue_push(queue, sample)) {
    atomic_fetch_add_explicit(&counters->dropped, 1U, memory_order_relaxed);
    return false;
  }
  atomic_fetch_add_explicit(&counters->enqueued, 1U, memory_order_relaxed);
  return true;
}
