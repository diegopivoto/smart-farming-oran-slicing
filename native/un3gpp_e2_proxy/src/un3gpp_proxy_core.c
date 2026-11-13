#include "un3gpp_proxy_core.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool fail(un3gpp_request_t *out, const char *fmt, ...)
{
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(out->error, sizeof(out->error), fmt, ap);
  va_end(ap);
  out->kind = UN3GPP_REQ_INVALID;
  return false;
}

static bool parse_dec(const char *s, uint64_t max, uint64_t *out)
{
  if (*s == '\0')
    return false;
  uint64_t v = 0;
  for (; *s; s++) {
    if (*s < '0' || *s > '9')
      return false;
    const uint64_t d = (uint64_t)(*s - '0');
    if (v > (max - d) / 10)
      return false;
    v = v * 10 + d;
  }
  *out = v;
  return true;
}

static int hex_digit(char c)
{
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

enum { K_ACTION, K_RUN, K_UE, K_MCS, K_RB, K_TTL, K_ISSUED, K_END };
static const char *keys[K_END] = {"action_id", "run_id", "ue", "mcs", "rb", "ttl_ms", "issued_at_ns"};

bool un3gpp_parse_request(const char *text, size_t len, un3gpp_request_t *out)
{
  memset(out, 0, sizeof(*out));
  if (text == NULL || len == 0)
    return fail(out, "empty request");
  const size_t n = strnlen(text, len);
  if (n >= UN3GPP_MAX_LABEL)
    return fail(out, "request too long");
  char buf[UN3GPP_MAX_LABEL];
  memcpy(buf, text, n);
  buf[n] = '\0';

  unsigned seen = 0, index = 0;
  un3gpp_req_kind_t kind = UN3GPP_REQ_INVALID;
  char *save = NULL;
  for (char *pair = strtok_r(buf, ";", &save); pair; pair = strtok_r(NULL, ";", &save), index++) {
    char *eq = strchr(pair, '=');
    if (eq == NULL)
      return fail(out, "pair without '='");
    *eq = '\0';
    const char *key = pair, *value = eq + 1;
    if (index == 0) {
      if (strcmp(key, "sf") != 0 || strcmp(value, "0.1") != 0)
        return fail(out, "first pair must be sf=0.1");
      continue;
    }
    if (index == 1) {
      if (strcmp(key, "op") != 0)
        return fail(out, "second pair must be op");
      if (strcmp(value, "un3gpp_ul") == 0) kind = UN3GPP_REQ_UL;
      else if (strcmp(value, "un3gpp_release") == 0) kind = UN3GPP_REQ_RELEASE;
      else return fail(out, "unknown op %.24s", value);
      continue;
    }
    int k = -1;
    for (int i = 0; i < K_END; i++)
      if (strcmp(key, keys[i]) == 0) k = i;
    if (k < 0 || (kind == UN3GPP_REQ_RELEASE && (k == K_MCS || k == K_RB)))
      return fail(out, "unexpected key %.24s", key);
    if (seen & (1u << k))
      return fail(out, "duplicate key %s", keys[k]);
    uint64_t v = 0;
    bool ok = true;
    switch (k) {
      case K_ACTION: ok = parse_dec(value, UINT64_MAX, &v) && v > 0; out->action_id = v; break;
      case K_UE: ok = parse_dec(value, 15, &v); out->ue = (uint8_t)v; break;
      case K_MCS: ok = parse_dec(value, 17, &v); out->mcs = (uint8_t)v; break;
      case K_RB:
        ok = parse_dec(value, 255, &v) && (v == 33 || v == 66 || v == 99 || v == 132);
        out->rb = (uint8_t)v;
        break;
      case K_TTL: ok = parse_dec(value, UN3GPP_MAX_TTL_MS, &v) && v > 0; out->ttl_ms = (uint32_t)v; break;
      case K_ISSUED: ok = parse_dec(value, UINT64_MAX, &v); out->issued_at_ns = v; break;
      case K_RUN:
        ok = strlen(value) == 32;
        for (int i = 0; ok && i < 16; i++) {
          const int hi = hex_digit(value[2 * i]), lo = hex_digit(value[2 * i + 1]);
          ok = hi >= 0 && lo >= 0;
          out->run_id[i] = (uint8_t)((hi << 4) | lo);
        }
        break;
      default: ok = false;
    }
    if (!ok)
      return fail(out, "invalid value for %s", keys[k]);
    seen |= 1u << k;
  }
  if (kind == UN3GPP_REQ_INVALID)
    return fail(out, "missing op");
  unsigned required = (1u << K_ACTION) | (1u << K_RUN) | (1u << K_UE) | (1u << K_TTL) | (1u << K_ISSUED);
  if (kind == UN3GPP_REQ_UL)
    required |= (1u << K_MCS) | (1u << K_RB);
  if ((seen & required) != required)
    return fail(out, "missing keys");
  out->kind = kind;
  return true;
}

void un3gpp_parse_status(const char *text, un3gpp_status_t *out)
{
  memset(out, 0, sizeof(*out));
  if (text == NULL || *text == '\0')
    return;
  const char *line = text;
  while (*line) {
    const char *end = strchr(line, '\n');
    const size_t len = end ? (size_t)(end - line) : strlen(line);
    char buf[256];
    if (len > 0 && len < sizeof(buf)) {
      memcpy(buf, line, len);
      buf[len] = '\0';
      char *eq = strchr(buf, '=');
      if (eq) {
        *eq = '\0';
        const char *k = buf, *v = eq + 1;
        if (strcmp(k, "ts_ns") == 0) out->ts_ns = strtoull(v, NULL, 10), out->available = true;
        else if (strcmp(k, "last_seq") == 0) out->last_seq = (uint32_t)strtoul(v, NULL, 10);
        else if (strcmp(k, "last_status") == 0) snprintf(out->last_status, sizeof(out->last_status), "%s", v);
        else if (strcmp(k, "last_status_ns") == 0) out->last_status_ns = strtoull(v, NULL, 10);
        else if (strcmp(k, "resends") == 0) out->resends = atoi(v);
        else if (strcmp(k, "mac_throughput_mbps") == 0) out->mac_throughput_mbps = strtod(v, NULL);
        else if (strncmp(k, "ue.", 3) == 0) {
          char *dot = NULL;
          const long ue = strtol(k + 3, &dot, 10);
          if (dot && *dot == '.' && ue >= 0 && ue < 16) {
            un3gpp_ue_view_t *u = NULL;
            for (int i = 0; i < UN3GPP_MAX_UES && !u; i++)
              if (out->ues[i].present && out->ues[i].ue == ue) u = &out->ues[i];
            for (int i = 0; i < UN3GPP_MAX_UES && !u; i++)
              if (!out->ues[i].present) {
                u = &out->ues[i];
                u->present = true;
                u->ue = (uint8_t)ue;
                u->observed_mcs = u->la_mcs = -1;
              }
            const char *f = dot + 1;
            if (u) {
              if (strcmp(f, "override") == 0) u->override_active = atoi(v);
              else if (strcmp(f, "committed_mcs") == 0) u->committed_mcs = atoi(v);
              else if (strcmp(f, "committed_first_rb") == 0) u->committed_first_rb = atoi(v);
              else if (strcmp(f, "committed_number_rb") == 0) u->committed_rb = atoi(v);
              else if (strcmp(f, "observed_mcs") == 0) u->observed_mcs = atoi(v);
              else if (strcmp(f, "observed_rb_max") == 0) u->observed_rb_max = atoi(v);
              else if (strcmp(f, "la_mcs") == 0) u->la_mcs = atoi(v);
              else if (strcmp(f, "data_pdus") == 0) u->data_pdus = (unsigned)strtoul(v, NULL, 10);
              else if (strcmp(f, "ul_snr_db") == 0) u->ul_snr_db = strtod(v, NULL);
            }
          }
        }
      }
    }
    if (!end)
      break;
    line = end + 1;
  }
}

void un3gpp_proxy_init(un3gpp_proxy_t *proxy, uint32_t first_seq)
{
  memset(proxy, 0, sizeof(*proxy));
  proxy->next_seq = first_seq ? first_seq : 1;
  proxy->last_index = -1;
}

static uint64_t fingerprint(const un3gpp_request_t *r)
{
  uint64_t h = 0xcbf29ce484222325ULL;
  const uint8_t fields[] = {(uint8_t)r->kind, r->ue, r->mcs, r->rb};
  const void *parts[] = {fields, r->run_id, &r->ttl_ms, &r->issued_at_ns};
  const size_t sizes[] = {sizeof(fields), sizeof(r->run_id), sizeof(r->ttl_ms), sizeof(r->issued_at_ns)};
  for (int p = 0; p < 4; p++)
    for (size_t i = 0; i < sizes[p]; i++) {
      h ^= ((const uint8_t *)parts[p])[i];
      h *= 0x100000001b3ULL;
    }
  return h;
}

static un3gpp_record_t *push(un3gpp_proxy_t *p)
{
  un3gpp_record_t *r = &p->history[p->next];
  memset(r, 0, sizeof(*r));
  p->last_index = (int)p->next;
  p->last_duplicate = false;
  p->next = (p->next + 1) % UN3GPP_HISTORY;
  if (p->len < UN3GPP_HISTORY)
    p->len++;
  return r;
}

un3gpp_decision_t un3gpp_proxy_decide(un3gpp_proxy_t *p, const un3gpp_request_t *req, uint64_t now_ns,
                                      un3gpp_record_t **record)
{
  const uint64_t fp = fingerprint(req);
  for (unsigned i = 0; i < p->len; i++) {
    un3gpp_record_t *h = &p->history[i];
    if (h->action_id != req->action_id || h->fingerprint == 0)   /* fingerprint 0: a CONFLICT report */
      continue;
    if (h->fingerprint == fp) {
      *record = h;
      p->last_index = (int)i;
      p->last_duplicate = true;
      p->outcome_version++;
      return UN3GPP_REPLAY;
    }
    un3gpp_record_t *c = push(p);
    *c = (un3gpp_record_t){.action_id = req->action_id, .fingerprint = 0, .ue = req->ue, .mcs = req->mcs,
                           .rb = req->rb, .release = req->kind == UN3GPP_REQ_RELEASE, .at_ns = now_ns};
    snprintf(c->status, sizeof(c->status), "CONFLICT");
    p->outcome_version++;
    *record = c;
    return UN3GPP_REJECT;
  }
  un3gpp_record_t *r = push(p);
  *r = (un3gpp_record_t){.action_id = req->action_id, .fingerprint = fp, .ue = req->ue, .mcs = req->mcs,
                         .rb = req->rb, .release = req->kind == UN3GPP_REQ_RELEASE, .at_ns = now_ns};
  p->outcome_version++;
  *record = r;
  const uint64_t ttl_ns = (uint64_t)req->ttl_ms * 1000000ULL;
  if (req->issued_at_ns > now_ns + UN3GPP_CLOCK_SKEW_NS ||
      (now_ns > req->issued_at_ns && now_ns - req->issued_at_ns >= ttl_ns + UN3GPP_CLOCK_SKEW_NS)) {
    snprintf(r->status, sizeof(r->status), "REJECTED_STALE");
    return UN3GPP_REJECT;
  }
  r->mac_seq = p->next_seq++;
  if (p->next_seq == 0)
    p->next_seq = 1;
  snprintf(r->status, sizeof(r->status), "SENT");
  return UN3GPP_SEND;
}

bool un3gpp_proxy_sync(un3gpp_proxy_t *p, const un3gpp_status_t *status, uint64_t now_ns)
{
  if (!status->available || status->last_seq == 0)
    return false;
  for (unsigned i = 0; i < p->len; i++) {
    un3gpp_record_t *r = &p->history[i];
    if (r->mac_seq != status->last_seq || status->last_status[0] == '\0')
      continue;
    if (strcmp(r->status, status->last_status) == 0)
      return false;
    snprintf(r->status, sizeof(r->status), "%s", status->last_status);
    r->at_ns = now_ns;
    p->outcome_version++;
    return true;
  }
  return false;
}

const un3gpp_record_t *un3gpp_proxy_latest(const un3gpp_proxy_t *p, bool *duplicate)
{
  if (duplicate != NULL)
    *duplicate = p->last_duplicate;
  if (p->last_index < 0)
    return NULL;
  return &p->history[p->last_index];
}

size_t un3gpp_mac_datagram(const un3gpp_record_t *r, uint8_t out[8])
{
  out[0] = r->release ? 5 : 4;
  memcpy(&out[1], &r->mac_seq, sizeof(r->mac_seq)); /* little endian on x86, as the MAC expects */
  out[5] = r->ue;
  if (r->release)
    return 6;
  out[6] = r->mcs;
  out[7] = r->rb;
  return 8;
}

static int finish(char *buf, size_t cap, int w)
{
  if (w < 0 || (size_t)w >= cap) {
    if (cap) buf[0] = '\0';
    return -1;
  }
  return w;
}

int un3gpp_format_outcome(char *buf, size_t cap, const un3gpp_record_t *r, bool duplicate)
{
  return finish(buf, cap, snprintf(buf, cap,
      "sf=0.1;kind=un3gpp_outcome;action_id=%" PRIu64 ";mac_seq=%" PRIu32 ";status=%s;dup=%d;ue=%u;mcs=%u;rb=%u;at_ns=%" PRIu64,
      r->action_id, r->mac_seq, r->status, duplicate ? 1 : 0, r->ue, r->mcs, r->rb, r->at_ns));
}

int un3gpp_format_ue(char *buf, size_t cap, const un3gpp_ue_view_t *u, const un3gpp_status_t *s, uint64_t now_ns)
{
  const uint64_t age_ms = now_ns > s->ts_ns ? (now_ns - s->ts_ns) / 1000000ULL : 0;
  return finish(buf, cap, snprintf(buf, cap,
      "sf=0.1;kind=un3gpp_ue;ue=%u;override=%d;mcs_cmd=%d;rb_first=%d;rb_cmd=%d;mcs_obs=%d;rb_obs=%d;la_mcs=%d;"
      "snr_db=%.2f;pdus=%u;mac_mbps=%.3f;age_ms=%" PRIu64,
      u->ue, u->override_active, u->committed_mcs, u->committed_first_rb, u->committed_rb, u->observed_mcs,
      u->observed_rb_max, u->la_mcs, u->ul_snr_db, u->data_pdus, s->mac_throughput_mbps, age_ms));
}
