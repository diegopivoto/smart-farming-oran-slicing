#undef NDEBUG
#include "un3gpp_proxy_core.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#define RUN "0123456789abcdef0123456789abcdef"
static const uint64_t T0 = 1793660000000000000ULL;
static const uint64_t MS = 1000000ULL;

static bool parse(const char *text, un3gpp_request_t *req)
{
  return un3gpp_parse_request(text, strlen(text), req);
}

static void test_requests(void)
{
  un3gpp_request_t r;
  assert(parse("sf=0.1;op=un3gpp_ul;action_id=77;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=30000;issued_at_ns=5", &r));
  assert(r.kind == UN3GPP_REQ_UL && r.action_id == 77 && r.ue == 1 && r.mcs == 9 && r.rb == 66);
  assert(r.run_id[0] == 0x01 && r.ttl_ms == 30000 && r.issued_at_ns == 5);
  assert(parse("sf=0.1;op=un3gpp_release;action_id=78;run_id=" RUN ";ue=1;ttl_ms=1000;issued_at_ns=5", &r));
  assert(r.kind == UN3GPP_REQ_RELEASE && r.action_id == 78);

  const char *bad[] = {
      "sf=0.1;op=un3gpp_ul;action_id=1;run_id=" RUN ";ue=1;mcs=18;rb=66;ttl_ms=1;issued_at_ns=1",
      "sf=0.1;op=un3gpp_ul;action_id=1;run_id=" RUN ";ue=1;mcs=9;rb=65;ttl_ms=1;issued_at_ns=1",
      "sf=0.1;op=un3gpp_ul;action_id=1;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=1",
      "sf=0.1;op=un3gpp_ul;action_id=0;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=1;issued_at_ns=1",
      "sf=0.1;op=un3gpp_ul;action_id=1;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=0;issued_at_ns=1",
      "sf=0.1;op=un3gpp_ul;action_id=1;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=1;issued_at_ns=1;x=1",
      "sf=0.1;op=prb_quota;action_id=1",
      "sf=0.2;op=un3gpp_ul",
      "",
  };
  for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
    if (parse(bad[i], &r)) {
      fprintf(stderr, "accepted #%zu %s\n", i, bad[i]);
      assert(0);
    }
    assert(r.kind == UN3GPP_REQ_INVALID && r.error[0] != '\0');
  }
}

static const char *STATUS =
    "ts_ns=1793660001000000000\nlast_seq=101\nlast_status=CONFIRMED\nlast_status_ns=1793660000900000000\n"
    "resends=0\ntarget_ue=1\ntarget_mcs=9\ntarget_rb=66\nmac_throughput_mbps=12.500\n"
    "ue.1.override=1\nue.1.override_mcs=9\nue.1.committed_mcs=9\nue.1.committed_first_rb=0\n"
    "ue.1.committed_number_rb=66\nue.1.observed_mcs=9\nue.1.observed_modulation=6\nue.1.observed_rb_max=66\n"
    "ue.1.data_pdus=180\nue.1.la_mcs=11\nue.1.ul_snr_db=24.10\nue.1.ul_ber=-1.000e+00\n"
    "capacity_bytes.mcs9=9407,18815,28223,37631\n";

static void test_status_parse(void)
{
  un3gpp_status_t s;
  un3gpp_parse_status(STATUS, &s);
  assert(s.available && s.last_seq == 101 && strcmp(s.last_status, "CONFIRMED") == 0);
  assert(s.mac_throughput_mbps > 12.4 && s.mac_throughput_mbps < 12.6);
  const un3gpp_ue_view_t *u = &s.ues[0];
  assert(u->present && u->ue == 1 && u->override_active == 1 && u->committed_mcs == 9 && u->committed_rb == 66);
  assert(u->observed_mcs == 9 && u->observed_rb_max == 66 && u->la_mcs == 11 && u->data_pdus == 180);
  assert(u->ul_snr_db > 24.0 && u->ul_snr_db < 24.2);
  un3gpp_status_t empty;
  un3gpp_parse_status("", &empty);
  assert(!empty.available);
}

static void test_decisions_and_sync(void)
{
  un3gpp_proxy_t p;
  un3gpp_proxy_init(&p, 100);
  un3gpp_request_t r;
  un3gpp_record_t *rec = NULL;
  assert(parse("sf=0.1;op=un3gpp_ul;action_id=7;run_id=" RUN ";ue=1;mcs=9;rb=66;ttl_ms=5000;issued_at_ns=1793660000000000000", &r));
  assert(un3gpp_proxy_decide(&p, &r, T0, &rec) == UN3GPP_SEND);
  assert(rec->mac_seq == 100 && strcmp(rec->status, "SENT") == 0);
  uint8_t d[8];
  assert(un3gpp_mac_datagram(rec, d) == 8 && d[0] == 4 && d[1] == 100 && d[5] == 1 && d[6] == 9 && d[7] == 66);

  /* duplicate: replay without resending, reported as dup; conflicting payload: rejected */
  assert(un3gpp_proxy_decide(&p, &r, T0 + MS, &rec) == UN3GPP_REPLAY);
  bool dup = false;
  assert(un3gpp_proxy_latest(&p, &dup) == rec && dup && rec->mac_seq == 100);
  un3gpp_request_t other = r;
  other.mcs = 3;
  assert(un3gpp_proxy_decide(&p, &other, T0 + MS, &rec) == UN3GPP_REJECT && strcmp(rec->status, "CONFLICT") == 0);

  /* staleness tolerates the PC1/PC2 clock skew (5 s) but nothing beyond it */
  un3gpp_request_t stale = r;
  stale.action_id = 8;                                  /* ttl 5 s + skew 5 s */
  assert(un3gpp_proxy_decide(&p, &stale, T0 + 10001 * MS, &rec) == UN3GPP_REJECT);
  assert(strcmp(rec->status, "REJECTED_STALE") == 0);
  un3gpp_request_t ahead = r;                           /* issuer clock 4 s ahead: accepted */
  ahead.action_id = 9;
  ahead.issued_at_ns = T0 + 4000 * MS;
  assert(un3gpp_proxy_decide(&p, &ahead, T0, &rec) == UN3GPP_SEND);
  un3gpp_request_t future = r;                          /* 6 s ahead: rejected */
  future.action_id = 10;
  future.issued_at_ns = T0 + 6000 * MS;
  assert(un3gpp_proxy_decide(&p, &future, T0, &rec) == UN3GPP_REJECT);

  /* MAC status for seq 100 updates action 7; a status of another seq does not */
  un3gpp_status_t s;
  un3gpp_parse_status(STATUS, &s);
  s.last_seq = 99;
  assert(!un3gpp_proxy_sync(&p, &s, T0 + 2000 * MS));
  s.last_seq = 100;
  assert(un3gpp_proxy_sync(&p, &s, T0 + 2000 * MS));
  const un3gpp_record_t *seven = NULL;
  for (unsigned i = 0; i < p.len; i++)
    if (p.history[i].mac_seq == 100) seven = &p.history[i];
  assert(seven && strcmp(seven->status, "CONFIRMED") == 0);
  assert(!un3gpp_proxy_sync(&p, &s, T0 + 3000 * MS)); /* unchanged */

  char label[UN3GPP_MAX_LABEL];
  assert(un3gpp_format_outcome(label, sizeof(label), seven, true) > 0);
  assert(strstr(label, "sf=0.1;kind=un3gpp_outcome;action_id=7;mac_seq=100;status=CONFIRMED;dup=1;ue=1;mcs=9;rb=66;at_ns=") == label);
  assert(un3gpp_format_ue(label, sizeof(label), &s.ues[0], &s, s.ts_ns + 300 * MS) > 0);
  assert(strstr(label, "sf=0.1;kind=un3gpp_ue;ue=1;override=1;mcs_cmd=9;rb_first=0;rb_cmd=66;mcs_obs=9;rb_obs=66;la_mcs=11;snr_db=24.10;pdus=180;mac_mbps=12.500;age_ms=300") == label);
}

int main(void)
{
  test_requests();
  test_status_parse();
  test_decisions_and_sync();
  printf("un3gpp_proxy_core_test: all tests passed\n");
  return 0;
}
