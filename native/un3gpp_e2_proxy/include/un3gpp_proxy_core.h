#ifndef SMARTFARM_UN3GPP_PROXY_CORE_H
#define SMARTFARM_UN3GPP_PROXY_CORE_H

/*
 * Core of the UN3GPP E2 research proxy (no FlexRIC dependency).
 *
 * E2SM-SF/0.1 requests for the UN3GPP BS (carried in SLICE SM labels, like the OAI profile):
 *   sf=0.1;op=un3gpp_ul;action_id=<u64>;run_id=<32hex>;ue=<u8>;mcs=<0..17>;rb=<33|66|99|132>;ttl_ms=<1..30000>;issued_at_ns=<u64>
 *   sf=0.1;op=un3gpp_release;action_id=<u64>;run_id=<32hex>;ue=<u8>;ttl_ms=<..>;issued_at_ns=<u64>
 * Readback labels published in SLICE indications:
 *   sf=0.1;kind=un3gpp_outcome;action_id=..;mac_seq=..;status=..;dup=0|1;ue=..;mcs=..;rb=..;at_ns=..
 *   sf=0.1;kind=un3gpp_ue;ue=..;override=..;mcs_cmd=..;rb_first=..;rb_cmd=..;mcs_obs=..;rb_obs=..;la_mcs=..;snr_db=..;pdus=..;mac_mbps=..;age_ms=..
 * The BS MAC is driven through its existing datagram socket (type 4/5) and observed through the status
 * file it rewrites once per second (see the MAC's SmartFarmUl.h).
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UN3GPP_MAX_LABEL 512
#define UN3GPP_MAX_UES 4
#define UN3GPP_HISTORY 64
#define UN3GPP_MAX_TTL_MS 30000U
/* PC1 (RIC/xApp) and PC2 (proxy) clocks are not synchronized during radio runs (start_bs.sh stops NTP;
 * measured offset -1.39 s on 2026-11-02): staleness checks allow this much skew in both directions. */
#define UN3GPP_CLOCK_SKEW_NS 5000000000ULL

typedef enum { UN3GPP_REQ_INVALID = 0, UN3GPP_REQ_UL, UN3GPP_REQ_RELEASE } un3gpp_req_kind_t;

typedef struct {
  un3gpp_req_kind_t kind;
  uint64_t action_id;
  uint8_t run_id[16];
  uint8_t ue;
  uint8_t mcs;
  uint8_t rb;
  uint32_t ttl_ms;
  uint64_t issued_at_ns;
  char error[96];
} un3gpp_request_t;

typedef struct {
  bool present;
  uint8_t ue;
  int override_active;
  int committed_mcs, committed_first_rb, committed_rb;
  int observed_mcs, observed_rb_max, la_mcs;
  unsigned data_pdus;
  double ul_snr_db;
} un3gpp_ue_view_t;

typedef struct {
  bool available;
  uint64_t ts_ns;
  uint32_t last_seq;
  char last_status[32];
  uint64_t last_status_ns;
  int resends;
  double mac_throughput_mbps;
  un3gpp_ue_view_t ues[UN3GPP_MAX_UES];
} un3gpp_status_t;

typedef struct {
  uint64_t action_id;
  uint64_t fingerprint;
  uint32_t mac_seq; /* 0 when the request never reached the MAC */
  uint8_t ue, mcs, rb;
  bool release;
  char status[32];
  uint64_t at_ns;
} un3gpp_record_t;

typedef struct {
  un3gpp_record_t history[UN3GPP_HISTORY];
  unsigned next, len;
  uint32_t next_seq;
  uint64_t outcome_version; /* bumped whenever a reported status changes */
  int last_index;           /* record touched by the latest request (-1 when none) */
  bool last_duplicate;      /* the latest request was a replay of an already handled action */
} un3gpp_proxy_t;

typedef enum {
  UN3GPP_SEND = 0,  /* forward to the MAC with the returned sequence */
  UN3GPP_REPLAY,    /* duplicate: report the stored outcome, do not resend */
  UN3GPP_REJECT     /* rejected locally: outcome recorded, nothing sent */
} un3gpp_decision_t;

bool un3gpp_parse_request(const char *text, size_t len, un3gpp_request_t *out);

/* Parses the MAC status file text (key=value lines). */
void un3gpp_parse_status(const char *text, un3gpp_status_t *out);

void un3gpp_proxy_init(un3gpp_proxy_t *proxy, uint32_t first_seq);

/* Idempotency/staleness decision for a decoded request; *record points at its history entry. */
un3gpp_decision_t un3gpp_proxy_decide(un3gpp_proxy_t *proxy, const un3gpp_request_t *req, uint64_t now_ns,
                                      un3gpp_record_t **record);

/* Updates SENT records from the MAC status; returns true when any reported status changed. */
bool un3gpp_proxy_sync(un3gpp_proxy_t *proxy, const un3gpp_status_t *status, uint64_t now_ns);

/* Record touched by the latest request (NULL when none); *duplicate tells whether it was a replay. */
const un3gpp_record_t *un3gpp_proxy_latest(const un3gpp_proxy_t *proxy, bool *duplicate);

/* Datagram for the MAC socket; returns its length. */
size_t un3gpp_mac_datagram(const un3gpp_record_t *record, uint8_t out[8]);

int un3gpp_format_outcome(char *buf, size_t cap, const un3gpp_record_t *record, bool duplicate);
int un3gpp_format_ue(char *buf, size_t cap, const un3gpp_ue_view_t *ue, const un3gpp_status_t *status,
                     uint64_t now_ns);

#endif
