/*
 * UN3GPP E2 research proxy (PC2).
 *
 * A FlexRIC E2 agent that represents the experimental UN3GPP base station towards the near-RT RIC.
 * It is declared as a lab node (not an O-DU/O-CU) and carries the experimental E2SM-SF/0.1 profile in
 * the labels of FlexRIC's custom SLICE service model, exactly like the OAI gNB adapter:
 *   - RIC Control (SLICE ADD): "op=un3gpp_ul" / "op=un3gpp_release" labels become datagrams on the BS
 *     MAC's local socket (types 4/5);
 *   - RIC Indication (SLICE): the MAC status file is read back as un3gpp_outcome / un3gpp_ue labels.
 * Only the SLICE plugin must be in the plugin directory given with -p.
 *
 * Environment: SMARTFARM_UN3GPP_NB_ID (default 4001), SMARTFARM_MAC_SOCKET (/tmp/sync_response.sock),
 * SMARTFARM_MAC_STATUS (/tmp/smartfarm_ul_status).
 */
#define _GNU_SOURCE

#include "un3gpp_proxy_core.h"

#include "agent/e2_agent_api.h"
#include "sm/slice_sm/ie/slice_data_ie.h"
#include "util/time_now_us.h"
#include "test/rnd/fill_rnd_data_e2_setup_req.h"

#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define REPORT_PERIOD_NS 200000000ULL
#define PRINT_PERIOD_NS 5000000000ULL /* operator echo of the UE views on stderr */
#define SCHED "SF"

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static un3gpp_proxy_t proxy;
static uint64_t reported_version;
static uint64_t last_report_ns;
static uint64_t last_print_ns;
static const char *mac_socket = "/tmp/sync_response.sock";
static const char *mac_status = "/tmp/smartfarm_ul_status";
static volatile sig_atomic_t stop_requested;

static uint64_t realtime_ns(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static bool send_to_mac(const uint8_t *datagram, size_t len)
{
  const int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
  if (fd < 0)
    return false;
  struct sockaddr_un addr = {.sun_family = AF_UNIX};
  snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", mac_socket);
  const bool ok = sendto(fd, datagram, len, 0, (struct sockaddr *)&addr, sizeof(addr)) == (ssize_t)len;
  close(fd);
  return ok;
}

static void read_status(un3gpp_status_t *status)
{
  char text[8192] = {0};
  FILE *f = fopen(mac_status, "r");
  if (f == NULL) {
    un3gpp_parse_status("", status);
    return;
  }
  const size_t n = fread(text, 1, sizeof(text) - 1, f);
  fclose(f);
  text[n] = '\0';
  un3gpp_parse_status(text, status);
}

static char *dup_text(const char *s, size_t len)
{
  char *d = malloc(len);
  if (d != NULL)
    memcpy(d, s, len);
  return d;
}

static void set_slice(fr_slice_t *s, uint32_t id, const char *label)
{
  memset(s, 0, sizeof(*s));
  s->id = id;
  s->len_label = strlen(label);
  s->label = dup_text(label, s->len_label);
  s->len_sched = sizeof(SCHED) - 1;
  s->sched = dup_text(SCHED, s->len_sched);
  s->params.type = SLICE_ALG_SM_V0_STATIC;   /* the plain SLICE encoder needs a concrete type */
}

/* ---- SLICE SM handlers ---- */

static sm_ag_if_ans_t write_ctrl_un3gpp(void const *data)
{
  sm_ag_if_ans_t ans = {.type = CTRL_OUTCOME_SM_AG_IF_ANS_V0};
  ans.ctrl_out.type = SLICE_AGENT_IF_CTRL_ANS_V0;
  ans.ctrl_out.slice.ans = SLICE_CTRL_OUT_ERROR;
  const slice_ctrl_req_data_t *req = data;
  char label[UN3GPP_MAX_LABEL] = {0};
  bool all_ok = req != NULL && req->msg.type == SLICE_CTRL_SM_V0_ADD && req->msg.u.add_mod_slice.dl.len_slices > 0;

  pthread_mutex_lock(&lock);
  for (uint32_t i = 0; all_ok && i < req->msg.u.add_mod_slice.dl.len_slices; i++) {
    const fr_slice_t *s = &req->msg.u.add_mod_slice.dl.slices[i];
    un3gpp_request_t parsed;
    un3gpp_record_t *record = NULL;
    if (!un3gpp_parse_request(s->label, s->len_label, &parsed)) {
      fprintf(stderr, "[UN3GPP proxy] rejected request: %s\n", parsed.error);
      all_ok = false;
      continue;
    }
    const un3gpp_decision_t decision = un3gpp_proxy_decide(&proxy, &parsed, realtime_ns(), &record);
    if (decision == UN3GPP_SEND) {
      uint8_t datagram[8];
      const size_t len = un3gpp_mac_datagram(record, datagram);
      if (!send_to_mac(datagram, len)) {
        snprintf(record->status, sizeof(record->status), "MAC_UNREACHABLE");
        all_ok = false;
      }
    } else if (decision == UN3GPP_REJECT) {
      all_ok = false;
    }
    un3gpp_format_outcome(label, sizeof(label), record, decision == UN3GPP_REPLAY);
    fprintf(stderr, "[UN3GPP proxy] %s\n", label);
  }
  pthread_mutex_unlock(&lock);

  ans.ctrl_out.slice.ans = all_ok ? SLICE_CTRL_OUT_OK : SLICE_CTRL_OUT_ERROR;
  const size_t len = strlen(label);
  if (len > 0 && (ans.ctrl_out.slice.diagnostic = dup_text(label, len)) != NULL)
    ans.ctrl_out.slice.len_diag = len;
  return ans;
}

static bool read_ind_un3gpp(void *data)
{
  slice_ind_data_t *ind = data;
  const uint64_t now = realtime_ns();
  un3gpp_status_t status;
  read_status(&status);

  pthread_mutex_lock(&lock);
  un3gpp_proxy_sync(&proxy, &status, now);
  const bool due = proxy.outcome_version != reported_version || now - last_report_ns >= REPORT_PERIOD_NS;
  if (!due) {
    pthread_mutex_unlock(&lock);
    return false;
  }
  reported_version = proxy.outcome_version;
  last_report_ns = now;

  /* at most 1 outcome + 4 UE views: far below the RIC 2 KB rendering and xApp SQLite limits */
  char labels[1 + UN3GPP_MAX_UES][UN3GPP_MAX_LABEL];
  uint32_t n = 0;
  bool duplicate = false;
  const un3gpp_record_t *latest = un3gpp_proxy_latest(&proxy, &duplicate);
  if (latest != NULL && un3gpp_format_outcome(labels[n], UN3GPP_MAX_LABEL, latest, duplicate) > 0)
    n++;
  pthread_mutex_unlock(&lock);
  for (int i = 0; i < UN3GPP_MAX_UES; i++)
    if (status.ues[i].present && un3gpp_format_ue(labels[n], UN3GPP_MAX_LABEL, &status.ues[i], &status, now) > 0)
      n++;
  if (n == 0)
    return false;
  if (now - last_print_ns >= PRINT_PERIOD_NS) {
    last_print_ns = now;
    for (uint32_t i = 0; i < n; i++)
      if (strstr(labels[i], "kind=un3gpp_ue") != NULL)
        fprintf(stderr, "[UN3GPP proxy] readback %s\n", labels[i]);
  }

  ul_dl_slice_conf_t *dl = &ind->msg.slice_conf.dl;
  dl->slices = calloc(n, sizeof(*dl->slices));
  if (dl->slices == NULL)
    return false;
  for (uint32_t i = 0; i < n; i++)
    set_slice(&dl->slices[i], i, labels[i]);
  dl->len_slices = n;
  dl->len_sched_name = sizeof(SCHED) - 1;
  dl->sched_name = dup_text(SCHED, dl->len_sched_name);
  ind->msg.tstamp = time_now_us();
  return true;
}

static void read_setup_un3gpp(void *data)
{
  (void)data; /* the SLICE SM agent fills its own RAN Function Definition */
}

static void read_setup_ran(void *data, const ngran_node_t node_type)
{
  (void)node_type;
  arr_node_component_config_add_t *dst = data;
  dst->len_cca = 1;
  dst->cca = calloc(1, sizeof(e2ap_node_component_config_add_t));
  if (dst->cca != NULL)
    dst->cca[0] = fill_ngap_e2ap_node_component_config_add();
}

static void on_signal(int signo)
{
  (void)signo;
  stop_requested = 1;
}

int main(int argc, char *argv[])
{
  const char *nb = getenv("SMARTFARM_UN3GPP_NB_ID");
  const int nb_id = nb != NULL ? atoi(nb) : 4001;
  if (getenv("SMARTFARM_MAC_SOCKET") != NULL) mac_socket = getenv("SMARTFARM_MAC_SOCKET");
  if (getenv("SMARTFARM_MAC_STATUS") != NULL) mac_status = getenv("SMARTFARM_MAC_STATUS");
  un3gpp_proxy_init(&proxy, (uint32_t)(realtime_ns() / 1000000ULL));

  sm_io_ag_ran_t io = {0};
  io.read_ind_tbl[SLICE_STATS_V0] = read_ind_un3gpp;
  io.read_setup_tbl[SLICE_AGENT_IF_E2_SETUP_ANS_V0] = read_setup_un3gpp;
  io.write_ctrl_tbl[SLICE_CTRL_REQ_V0] = write_ctrl_un3gpp;
  io.write_subs_tbl[SLICE_SUBS_V0] = NULL;
  io.read_setup_ran = read_setup_ran;

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  fr_args_t args = init_fr_args(argc, argv);
  fprintf(stderr, "[UN3GPP proxy] research proxy nb_id=%d mcc=001 mnc=01 mac_socket=%s status=%s\n", nb_id,
          mac_socket, mac_status);
  init_agent_api(1, 1, 2, nb_id, 0, ngran_gNB, io, &args);
  while (!stop_requested)
    sleep(1);
  stop_agent_api();
  return EXIT_SUCCESS;
}
