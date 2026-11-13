#define _POSIX_C_SOURCE 200809L

/*
 * Smart Farming E2SM-SF/0.1 control xApp.
 *
 * Bridges a local Unix socket to FlexRIC: each client line "CTRL <label>"
 * becomes an E2 RIC Control carrying the label inside a custom SLICE SM ADD
 * request, and every SLICE indication (the node readback) is streamed back as
 * one JSON line tagged with its node. Two E2 nodes are handled: the OAI gNB
 * (the node that also advertises KPM) and the UN3GPP BS research proxy; a
 * request whose op starts with "un3gpp_" goes to the latter, any other to the
 * gNB. Nodes are rediscovered every few seconds. The xApp does not interpret
 * the profile; validation lives in the Python client and the node adapters.
 * It is a separate process from the read-only KPM bridge.
 */

#include "sf_control.h"

#include "xApp/e42_xapp_api.h"
#include "xApp/db/db.h"
#include "xApp/endpoint_xapp.h"
#include "xApp/pending_event_xapp.h"
#include "sm/slice_sm/slice_sm_id.h"
#include "sm/kpm_sm/kpm_sm_v02.03/kpm_sm_id.h"

#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define SF_LINE_CAP 1024U
#define SF_JSON_CAP (64U * 1024U)
#define SF_JOURNAL_EVERY 5U
#define SF_MAX_NODES 4
#define SF_RESCAN_NS 3000000000ULL

typedef struct {
  bool used;
  global_e2_node_id_t id;
  char role[8];          /* "oai" or "un3gpp" */
  uint32_t nb_id;
  int handle;
} sf_node_t;

static sf_node_t nodes_tbl[SF_MAX_NODES];
static pthread_mutex_t nodes_lock = PTHREAD_MUTEX_INITIALIZER;

typedef struct {
  pthread_mutex_t lock;
  int client_fd;
  int journal_fd;
  char epoch[37];
  uint64_t last_outcome_hash;
  atomic_uint_fast64_t indications;
  atomic_uint_fast64_t controls;
} sf_xapp_t;

static sf_xapp_t xapp = {.lock = PTHREAD_MUTEX_INITIALIZER, .client_fd = -1, .journal_fd = -1};
static volatile sig_atomic_t stop_requested = 0;

/*
 * The pinned FlexRIC xApp SDK stores every indication in SQLite, and its SLICE
 * row writer copies each slice label into a 50-byte buffer
 * (sqlite3_wrapper.c, to_sql_string_slice_rb), which aborts the process
 * (FORTIFY) for E2SM-SF labels. The SDK offers no switch, so the link wraps
 * write_db_xapp (-Wl,--wrap=write_db_xapp): SLICE readback is persisted by
 * this xApp as JSONL instead, other indication types still reach the SDK.
 */
void __real_write_db_xapp(db_xapp_t *db, global_e2_node_id_t const *id, sm_ag_if_rd_t const *rd);
void __wrap_write_db_xapp(db_xapp_t *db, global_e2_node_id_t const *id, sm_ag_if_rd_t const *rd);

void __wrap_write_db_xapp(db_xapp_t *db, global_e2_node_id_t const *id, sm_ag_if_rd_t const *rd)
{
  if (rd != NULL && rd->type == INDICATION_MSG_AGENT_IF_ANS_V0 && rd->ind.type == SLICE_STATS_V0)
    return;
  __real_write_db_xapp(db, id, rd);
}

/*
 * The pinned SDK sends the E42 RIC CONTROL REQUEST *before* registering its
 * pending event (msg_handler_xapp.c, e2ap_handle_e42_ric_control_request_xapp:
 * e2ap_send_bytes_xapp, then add_pending_event_xapp). The ACK is handled on
 * the SDK event-loop thread, and the RIC round trip is ~0.25-1 ms, so when
 * this (caller) thread is descheduled in that window the ACK handler extracts
 * a key that is not yet in the pending-event red-black tree. With NDEBUG the
 * "key not found" assert is gone and assoc_rb_tree_extract_node walks the
 * tree's dummy node (children NULL): SIGSEGV at address 0x18 in minimum()
 * (segfaults of 2026-11-03, docs/results/2026-11-03-xapp-segfault.md).
 *
 * The link wraps e2ap_send_bytes_xapp and add_pending_event (both cross-object
 * calls from msg_handler_xapp.c): while send_control() is inside
 * control_sm_xapp_api, the CONTROL bytes are held back and sent right after
 * the CONTROL pending event is in the tree, the same order the SDK already
 * uses for subscriptions. Any other send (event-loop thread, subscriptions)
 * passes through untouched. rm_pending_event_ev is wrapped as a last guard:
 * an ACK/FAILURE with no pending event (e.g. a duplicate) is logged and
 * dropped instead of corrupting the tree.
 */
static void fail(const char *message);
void __real_e2ap_send_bytes_xapp(e2ap_ep_xapp_t *ep, byte_array_t ba);
void __wrap_e2ap_send_bytes_xapp(e2ap_ep_xapp_t *ep, byte_array_t ba);
void __real_add_pending_event(pending_event_xapp_ds_t *p, int fd, pending_event_xapp_t *ev);
void __wrap_add_pending_event(pending_event_xapp_ds_t *p, int fd, pending_event_xapp_t *ev);
int *__real_rm_pending_event_ev(pending_event_xapp_ds_t *p, pending_event_xapp_t *ev);
int *__wrap_rm_pending_event_ev(pending_event_xapp_ds_t *p, pending_event_xapp_t *ev);

static _Thread_local bool ctrl_hold_armed = false;
static _Thread_local e2ap_ep_xapp_t *ctrl_held_ep = NULL;
static _Thread_local byte_array_t ctrl_held_ba;
static atomic_uint_fast64_t orphan_acks;

void __wrap_e2ap_send_bytes_xapp(e2ap_ep_xapp_t *ep, byte_array_t ba)
{
  if (ctrl_hold_armed && ctrl_held_ep == NULL) {
    ctrl_held_ep = ep; /* ba stays valid until the SDK handler returns */
    ctrl_held_ba = ba;
    return;
  }
  __real_e2ap_send_bytes_xapp(ep, ba);
}

void __wrap_add_pending_event(pending_event_xapp_ds_t *p, int fd, pending_event_xapp_t *ev)
{
  __real_add_pending_event(p, fd, ev);
  if (ctrl_held_ep != NULL && ev != NULL && ev->ev == E42_RIC_CONTROL_REQUEST_PENDING_EVENT) {
    e2ap_ep_xapp_t *ep = ctrl_held_ep;
    ctrl_held_ep = NULL;
    ctrl_hold_armed = false;
    __real_e2ap_send_bytes_xapp(ep, ctrl_held_ba);
  }
}

int *__wrap_rm_pending_event_ev(pending_event_xapp_ds_t *p, pending_event_xapp_t *ev)
{
  /* only the SDK event-loop thread removes pending events, so the event cannot vanish after this check */
  bool present = false;
  pthread_mutex_lock(&p->pend_mtx);
  assoc_rb_tree_t *right = &p->pending.right;
  for (void *it = assoc_rb_tree_front(right); !present && it != assoc_rb_tree_end(right);
       it = assoc_rb_tree_next(right, it))
    present = right->comp(ev, assoc_rb_tree_key(right, it)) == 0;
  pthread_mutex_unlock(&p->pend_mtx);
  if (present)
    return __real_rm_pending_event_ev(p, ev);

  const uint64_t n = atomic_fetch_add(&orphan_acks, 1) + 1;
  fprintf(stderr, "smartfarm_sf_control: response for pending event %d ric_req_id=%u with nothing pending, dropped (total %" PRIu64 ")\n",
          (int)ev->ev, (unsigned)ev->id.ric_req_id, n);
  int *fd = malloc(sizeof(*fd));
  if (fd == NULL)
    fail("memory exhausted");
  *fd = -1; /* the caller's epoll_ctl(DEL)/close(-1) fail with EBADF and are ignored (NDEBUG SDK) */
  return fd;
}

static void on_signal(int signo)
{
  (void)signo;
  stop_requested = 1;
}

static void fail(const char *message)
{
  fprintf(stderr, "smartfarm_sf_control: %s\n", message);
  exit(EXIT_FAILURE);
}

static uint64_t realtime_ns(void)
{
  struct timespec ts = {0};
  clock_gettime(CLOCK_REALTIME, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void make_epoch(char out[37])
{
  uint8_t b[16] = {0};
  if (getrandom(b, sizeof(b), 0) != (ssize_t)sizeof(b))
    fail("getrandom failed");
  b[6] = (uint8_t)((b[6] & 0x0fU) | 0x40U);
  b[8] = (uint8_t)((b[8] & 0x3fU) | 0x80U);
  snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
}

static bool write_all(int fd, const char *data, size_t len)
{
  size_t off = 0;
  while (off < len) {
    ssize_t n = write(fd, data + off, len - off);
    if (n > 0) {
      off += (size_t)n;
    } else if (n < 0 && errno == EINTR) {
      continue;
    } else {
      return false;
    }
  }
  return true;
}

/* Sends one JSON line to the connected client and, when asked, to the journal. */
static void emit(const char *line, size_t len, bool journal)
{
  pthread_mutex_lock(&xapp.lock);
  if (xapp.client_fd >= 0 && !write_all(xapp.client_fd, line, len)) {
    close(xapp.client_fd);
    xapp.client_fd = -1;
  }
  if (journal && xapp.journal_fd >= 0)
    (void)write_all(xapp.journal_fd, line, len);
  pthread_mutex_unlock(&xapp.lock);
}

static uint64_t fnv1a(uint64_t h, const char *data, size_t len)
{
  for (size_t i = 0; i < len; i++) {
    h ^= (uint8_t)data[i];
    h *= 0x100000001b3ULL;
  }
  return h;
}

static void sf_indication_node(sm_ag_if_rd_t const *rd, int slot)
{
  if (rd == NULL || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != SLICE_STATS_V0)
    return;
  const char *role = nodes_tbl[slot].role;
  const uint32_t nb_id = nodes_tbl[slot].nb_id;
  const uint64_t received_at_ns = realtime_ns();
  const ul_dl_slice_conf_t *dl = &rd->ind.slice.msg.slice_conf.dl;
  const uint64_t seq = atomic_fetch_add(&xapp.indications, 1);

  char *json = malloc(SF_JSON_CAP);
  if (json == NULL)
    return;
  sf_json_buffer_t buf = {.data = json, .cap = SF_JSON_CAP};
  sf_json_printf(&buf,
                 "{\"type\":\"indication\",\"epoch\":\"%s\",\"seq\":%" PRIu64 ",\"received_at_ns\":%" PRIu64
                 ",\"tstamp_us\":%" PRId64 ",\"node\":\"%s\",\"nb_id\":%" PRIu32 ",\"labels\":[",
                 xapp.epoch, seq, received_at_ns, rd->ind.slice.msg.tstamp, role, nb_id);
  uint64_t outcome_hash = 0xcbf29ce484222325ULL;
  size_t emitted = 0;
  for (uint32_t i = 0; i < dl->len_slices && dl->slices != NULL; i++) {
    const fr_slice_t *s = &dl->slices[i];
    if (!sf_is_profile_label(s->label, s->len_label))
      continue;
    if (sf_label_has_kind(s->label, s->len_label, "outcome") || sf_label_has_kind(s->label, s->len_label, "un3gpp_outcome"))
      outcome_hash = fnv1a(outcome_hash, s->label, s->len_label);
    if (emitted++ > 0)
      sf_json_printf(&buf, ",");
    sf_json_string(&buf, s->label, s->len_label);
  }
  sf_json_printf(&buf, "]}\n");
  if (!buf.truncated) {
    pthread_mutex_lock(&xapp.lock);
    const bool new_outcome = outcome_hash != xapp.last_outcome_hash;
    xapp.last_outcome_hash = outcome_hash;
    pthread_mutex_unlock(&xapp.lock);
    emit(buf.data, buf.len, new_outcome || seq % SF_JOURNAL_EVERY == 0);
  }
  free(json);
}

static void sf_cb0(sm_ag_if_rd_t const *rd) { sf_indication_node(rd, 0); }
static void sf_cb1(sm_ag_if_rd_t const *rd) { sf_indication_node(rd, 1); }
static void sf_cb2(sm_ag_if_rd_t const *rd) { sf_indication_node(rd, 2); }
static void sf_cb3(sm_ag_if_rd_t const *rd) { sf_indication_node(rd, 3); }
static void (*const node_callbacks[SF_MAX_NODES])(sm_ag_if_rd_t const *) = {sf_cb0, sf_cb1, sf_cb2, sf_cb3};

/* Subscribes to every new node that advertises the SLICE SM; returns how many nodes are known. */
static int refresh_nodes(void)
{
  e2_node_arr_xapp_t arr = e2_nodes_xapp_api();
  pthread_mutex_lock(&nodes_lock);
  for (size_t i = 0; i < arr.len; i++) {
    bool slice = false, kpm = false;
    for (size_t j = 0; j < arr.n[i].len_rf; j++) {
      slice = slice || arr.n[i].rf[j].id == SM_SLICE_ID;
      kpm = kpm || arr.n[i].rf[j].id == SM_KPM_ID;
    }
    const uint32_t nb_id = arr.n[i].id.nb_id.nb_id;
    bool known = false;
    for (int k = 0; k < SF_MAX_NODES; k++)
      known = known || (nodes_tbl[k].used && nodes_tbl[k].nb_id == nb_id);
    if (!slice || known)
      continue;
    for (int k = 0; k < SF_MAX_NODES; k++) {
      if (nodes_tbl[k].used)
        continue;
      sf_node_t *node = &nodes_tbl[k];
      node->id = cp_global_e2_node_id(&arr.n[i].id);
      node->nb_id = nb_id;
      snprintf(node->role, sizeof(node->role), "%s", kpm ? "oai" : "un3gpp");
      node->used = true;   /* before subscribing: indications may arrive at once */
      const sm_ans_xapp_t sub = report_sm_xapp_api(&node->id, SM_SLICE_ID, (void *)"10_ms", node_callbacks[k]);
      node->handle = sub.success ? sub.u.handle : -1;
      fprintf(stderr, "smartfarm_sf_control: node nb_id=%" PRIu32 " role=%s subscribed=%d\n", nb_id, node->role,
              sub.success);
      break;
    }
  }
  int count = 0;
  for (int k = 0; k < SF_MAX_NODES; k++)
    count += nodes_tbl[k].used;
  pthread_mutex_unlock(&nodes_lock);
  free_e2_node_arr_xapp(&arr);
  return count;
}

static global_e2_node_id_t *node_for_label(const char *label, size_t len, const char **role)
{
  const char marker[] = ";op=un3gpp_";
  const size_t m = sizeof(marker) - 1;
  bool un3gpp = false;
  for (size_t i = 0; !un3gpp && i + m <= len; i++)
    un3gpp = memcmp(label + i, marker, m) == 0;
  *role = un3gpp ? "un3gpp" : "oai";
  for (int k = 0; k < SF_MAX_NODES; k++)
    if (nodes_tbl[k].used && strcmp(nodes_tbl[k].role, *role) == 0)
      return &nodes_tbl[k].id;
  return NULL;
}

static void fill_text(char **dst, uint32_t *dst_len, const char *src, size_t len)
{
  *dst = malloc(len);
  if (*dst == NULL)
    fail("memory exhausted");
  memcpy(*dst, src, len);
  *dst_len = (uint32_t)len;
}

static bool send_control(global_e2_node_id_t *node, const char *label, size_t len)
{
  slice_ctrl_req_data_t req = {0};
  req.msg.type = SLICE_CTRL_SM_V0_ADD;
  ul_dl_slice_conf_t *dl = &req.msg.u.add_mod_slice.dl;
  ul_dl_slice_conf_t *ul = &req.msg.u.add_mod_slice.ul;
  fill_text(&dl->sched_name, &dl->len_sched_name, "SF", 2);
  fill_text(&ul->sched_name, &ul->len_sched_name, "SF", 2);
  dl->len_slices = 1;
  dl->slices = calloc(1, sizeof(*dl->slices));
  if (dl->slices == NULL)
    fail("memory exhausted");
  fr_slice_t *s = &dl->slices[0];
  fill_text(&s->label, &s->len_label, label, len);
  fill_text(&s->sched, &s->len_sched, "SF", 2);
  s->params.type = SLICE_ALG_SM_V0_STATIC;
  s->params.u.sta.pos_low = 0;
  s->params.u.sta.pos_high = 0;

  ctrl_hold_armed = true; /* see __wrap_e2ap_send_bytes_xapp */
  ctrl_held_ep = NULL;
  const sm_ans_xapp_t ans = control_sm_xapp_api(node, SM_SLICE_ID, &req);
  const bool held_unsent = ctrl_held_ep != NULL;
  ctrl_hold_armed = false;
  ctrl_held_ep = NULL;
  free_slice_ctrl_msg(&req.msg);
  if (held_unsent) {
    fprintf(stderr, "smartfarm_sf_control: CONTROL bytes held but no pending event registered (SDK changed?)\n");
    return false;
  }
  return ans.success;
}

static void handle_line(const char *line, size_t len)
{
  char json[SF_LINE_CAP * 2 + 256];
  sf_json_buffer_t buf = {.data = json, .cap = sizeof(json)};
  const uint64_t seq = atomic_fetch_add(&xapp.controls, 1);
  const char *label = NULL;
  size_t label_len = 0;
  const char *error = sf_parse_control_line(line, len, &label, &label_len);
  if (error != NULL) {
    sf_json_printf(&buf, "{\"type\":\"control\",\"epoch\":\"%s\",\"seq\":%" PRIu64 ",\"accepted\":false,\"error\":",
                   xapp.epoch, seq);
    sf_json_string(&buf, error, strlen(error));
    sf_json_printf(&buf, "}\n");
    emit(buf.data, buf.len, true);
    return;
  }
  const char *role = NULL;
  pthread_mutex_lock(&nodes_lock);
  global_e2_node_id_t *node = node_for_label(label, label_len, &role);
  pthread_mutex_unlock(&nodes_lock);
  if (node == NULL) {
    sf_json_printf(&buf, "{\"type\":\"control\",\"epoch\":\"%s\",\"seq\":%" PRIu64
                   ",\"accepted\":false,\"error\":\"no %s node connected\"}\n", xapp.epoch, seq, role);
    emit(buf.data, buf.len, true);
    return;
  }
  const uint64_t sent_at_ns = realtime_ns();
  const bool ok = send_control(node, label, label_len);
  const uint64_t returned_at_ns = realtime_ns();
  sf_json_printf(&buf,
                 "{\"type\":\"control\",\"epoch\":\"%s\",\"seq\":%" PRIu64 ",\"accepted\":true,\"e2_success\":%s"
                 ",\"node\":\"%s\",\"sent_at_ns\":%" PRIu64 ",\"returned_at_ns\":%" PRIu64 ",\"label\":",
                 xapp.epoch, seq, ok ? "true" : "false", role, sent_at_ns, returned_at_ns);
  sf_json_string(&buf, label, label_len);
  sf_json_printf(&buf, "}\n");
  emit(buf.data, buf.len, true);
}

static int open_listener(const char *path)
{
  struct sockaddr_un addr = {.sun_family = AF_UNIX};
  if (strlen(path) >= sizeof(addr.sun_path))
    fail("SMARTFARM_SF_SOCKET is too long");
  memcpy(addr.sun_path, path, strlen(path) + 1);
  (void)unlink(path);
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(fd, 1) != 0)
    fail("could not listen on SMARTFARM_SF_SOCKET");
  if (chmod(path, 0660) != 0)
    fail("could not restrict SMARTFARM_SF_SOCKET permissions");
  return fd;
}

static void serve_client(int fd)
{
  pthread_mutex_lock(&xapp.lock);
  xapp.client_fd = fd;
  pthread_mutex_unlock(&xapp.lock);
  fprintf(stderr, "smartfarm_sf_control: client connected\n");

  char line[SF_LINE_CAP];
  size_t used = 0;
  uint64_t last_scan = realtime_ns();
  while (!stop_requested) {
    struct pollfd p = {.fd = fd, .events = POLLIN};
    int r = poll(&p, 1, 500);
    if (realtime_ns() - last_scan >= SF_RESCAN_NS) {
      refresh_nodes();
      last_scan = realtime_ns();
    }
    if (r < 0 && errno == EINTR)
      continue;
    if (r <= 0)
      continue;
    ssize_t n = read(fd, line + used, sizeof(line) - used);
    if (n <= 0)
      break;
    used += (size_t)n;
    char *start = line;
    char *nl;
    while ((nl = memchr(start, '\n', used - (size_t)(start - line))) != NULL) {
      handle_line(start, (size_t)(nl - start));
      start = nl + 1;
    }
    used -= (size_t)(start - line);
    memmove(line, start, used);
    if (used == sizeof(line)) {
      handle_line("", 0); /* report an oversized line, then resynchronize */
      used = 0;
    }
  }
  pthread_mutex_lock(&xapp.lock);
  if (xapp.client_fd == fd) {
    close(fd);
    xapp.client_fd = -1;
  }
  pthread_mutex_unlock(&xapp.lock);
  fprintf(stderr, "smartfarm_sf_control: client disconnected\n");
}

int main(int argc, char **argv)
{
  const char *socket_path = getenv("SMARTFARM_SF_SOCKET");
  if (socket_path == NULL || socket_path[0] == '\0')
    fail("SMARTFARM_SF_SOCKET must name the control socket path");

  signal(SIGPIPE, SIG_IGN);
  struct sigaction sa = {.sa_handler = on_signal};
  sigaction(SIGINT, &sa, NULL);
  sigaction(SIGTERM, &sa, NULL);

  /* keep FlexRIC's own prints out of the JSONL journal stream */
  xapp.journal_fd = dup(STDOUT_FILENO);
  if (xapp.journal_fd < 0 || fflush(stdout) != 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0)
    fail("could not isolate FlexRIC logs from JSONL stdout");
  make_epoch(xapp.epoch);

  const fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args);

  for (int waited = 0; !stop_requested && refresh_nodes() == 0; waited++) {
    if (waited >= 1800)
      fail("no E2 node advertising the SLICE SM");
    if (waited % 10 == 0)
      fprintf(stderr, "smartfarm_sf_control: waiting for an E2 node with the SLICE SM\n");
    sleep(1);
  }

  const int listener = open_listener(socket_path);
  fprintf(stderr, "smartfarm_sf_control: epoch=%s socket=%s\n", xapp.epoch, socket_path);
  uint64_t last_scan = realtime_ns();
  while (!stop_requested) {
    struct pollfd p = {.fd = listener, .events = POLLIN};
    int r = poll(&p, 1, 500);
    if (realtime_ns() - last_scan >= SF_RESCAN_NS) {
      refresh_nodes();
      last_scan = realtime_ns();
    }
    if (r <= 0)
      continue;
    int fd = accept(listener, NULL, NULL);
    if (fd >= 0)
      serve_client(fd);
  }

  close(listener);
  (void)unlink(socket_path);
  for (int k = 0; k < SF_MAX_NODES; k++)
    if (nodes_tbl[k].used && nodes_tbl[k].handle >= 0)
      rm_report_sm_xapp_api(nodes_tbl[k].handle);
  const struct timespec retry = {.tv_sec = 0, .tv_nsec = 1000000L};
  while (!try_stop_xapp_api())
    (void)nanosleep(&retry, NULL);
  return EXIT_SUCCESS;
}
