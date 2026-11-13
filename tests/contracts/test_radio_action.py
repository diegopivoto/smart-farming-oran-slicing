from uuid import UUID

import pytest
from pydantic import ValidationError

from smartfarm.contracts.radio import (
    E2smSfCodecError,
    PrbQuotaAction,
    QuotaCounters,
    QuotaOutcome,
    QuotaRollback,
    QuotaRule,
    QuotaStatus,
    UeQuota,
    parse_readback,
    parse_request,
)
from smartfarm.contracts.service import Snssai

RUN = UUID("01234567-89ab-cdef-0123-456789abcdef")


def quota(**overrides: object) -> PrbQuotaAction:
    fields: dict[str, object] = {
        "action_id": 2**64 - 1,
        "run_id": RUN,
        "snssai": Snssai(sst=1, sd="000002"),
        "rnti": 17921,
        "prb_quota": 52,
        "ttl_ms": 30000,
        "issued_at_ns": 1793646912000000000,
    }
    fields.update(overrides)
    return PrbQuotaAction.model_validate(fields)


def test_quota_label_matches_gnb_codec_fixture() -> None:
    # same string decoded by sf_codec_test.c on the gNB side
    assert quota().to_label() == (
        "sf=0.1;op=prb_quota;action_id=18446744073709551615;run_id=0123456789abcdef0123456789abcdef"
        ";sst=1;sd=000002;rnti=17921;prb=52;ttl_ms=30000;issued_at_ns=1793646912000000000"
    )


def test_request_round_trip_preserves_every_field() -> None:
    action = quota()
    assert parse_request(action.to_label()) == action
    rollback = QuotaRollback(
        action_id=9,
        run_id=RUN,
        target_action_id=8,
        rollback_token=0xDEADBEEF,
        ttl_ms=5000,
        issued_at_ns=10,
    )
    assert rollback.to_label().endswith("target_action_id=8;token=00000000deadbeef;ttl_ms=5000;issued_at_ns=10")
    assert parse_request(rollback.to_label()) == rollback


@pytest.mark.parametrize("prb", [0, 23, 25, 107, 132])
def test_quota_outside_mask_is_rejected_before_e2(prb: int) -> None:
    with pytest.raises(ValidationError):
        quota(prb_quota=prb)


@pytest.mark.parametrize(
    "overrides",
    [{"ttl_ms": 0}, {"ttl_ms": 30001}, {"action_id": 0}, {"rnti": 65536}, {"action_id": 2**64}],
)
def test_invalid_action_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        quota(**overrides)


def test_malformed_sd_is_rejected() -> None:
    with pytest.raises(ValidationError):
        quota(snssai={"sst": 1, "sd": "0000001"})


@pytest.mark.parametrize(
    "label",
    [
        "op=prb_quota;sf=0.1",
        "sf=0.2;op=prb_quota",
        "sf=0.1;op=steal;action_id=1",
        "sf=0.1;op=prb_quota;action_id=1",
        "sf=0.1;op=rollback;action_id=9;run_id=0123456789abcdef0123456789abcdef;target_action_id=8;"
        "token=xyz;ttl_ms=5000;issued_at_ns=10",
        "sf=0.1;op=prb_quota;action_id=1;action_id=2",
    ],
)
def test_malformed_requests_raise(label: str) -> None:
    with pytest.raises(E2smSfCodecError):
        parse_request(label)


def test_outcome_readback_matches_gnb_formatter() -> None:
    outcome = parse_readback(
        "sf=0.1;kind=outcome;action_id=5;status=APPLIED;dup=1;sst=1;sd=000003;rnti=0;requested=24;"
        "applied=24;previous=106;token=0000000000abcdef;expires_at_ns=99;version=4;at_ns=1234"
    )
    assert isinstance(outcome, QuotaOutcome)
    assert outcome.status is QuotaStatus.APPLIED and outcome.duplicate and outcome.succeeded
    assert outcome.snssai == Snssai(sst=1, sd="000003")
    assert (outcome.requested_prb, outcome.applied_prb, outcome.previous_prb) == (24, 24, 106)
    assert outcome.rollback_token == 0xABCDEF


def test_other_readback_kinds() -> None:
    rule = parse_readback("sf=0.1;kind=rule;sst=1;sd=000001;rnti=0;prb=78;action_id=3;expires_at_ns=7;version=2")
    assert isinstance(rule, QuotaRule) and rule.prb_quota == 78
    counters = parse_readback(
        "sf=0.1;kind=counters;lookups=1;unmapped=2;expired=3;fallback=4;capped=5;bypass_retx=6;version=9"
    )
    assert isinstance(counters, QuotaCounters) and counters.bypass_retx == 6
    ue = parse_readback("sf=0.1;kind=ue;rnti=17921;sst=1;sd=000001;prb=52;action_id=3")
    assert isinstance(ue, UeQuota) and ue.snssai == Snssai(sst=1, sd="000001")
    unmapped = parse_readback("sf=0.1;kind=ue;rnti=17921;sst=0;sd=ffffff;prb=106;action_id=0")
    assert isinstance(unmapped, UeQuota) and unmapped.snssai is None


def test_rejected_outcome_is_not_success() -> None:
    outcome = parse_readback(
        "sf=0.1;kind=outcome;action_id=0;status=REJECTED_MALFORMED;dup=0;sst=0;sd=000000;rnti=0;"
        "requested=0;applied=0;previous=0;token=0000000000000000;expires_at_ns=0;version=0;at_ns=1"
    )
    assert isinstance(outcome, QuotaOutcome) and not outcome.succeeded


@pytest.mark.parametrize(
    "label",
    [
        "sf=0.1;kind=outcome;action_id=5",
        "sf=0.1;kind=rule;sst=1;sd=000001;rnti=0;prb=78;action_id=3;expires_at_ns=7;version=2;extra=1",
        "sf=0.1;kind=ue;rnti=-1;sst=1;sd=000001;prb=52;action_id=3",
        "sf=0.1;kind=outcome;action_id=5;status=NOPE;dup=0;sst=1;sd=000003;rnti=0;requested=24;"
        "applied=24;previous=106;token=0000000000abcdef;expires_at_ns=99;version=4;at_ns=1234",
        "sf=0.1;kind=mystery",
    ],
)
def test_malformed_readback_raises(label: str) -> None:
    with pytest.raises(E2smSfCodecError):
        parse_readback(label)


def test_un3gpp_label_matches_proxy_codec_fixture() -> None:
    from smartfarm.contracts.radio import Un3gppRelease, Un3gppUlAction

    action = Un3gppUlAction(action_id=77, run_id=RUN, ue=1, mcs=9, number_rb=66, ttl_ms=30000, issued_at_ns=5)
    # same string decoded by un3gpp_proxy_core_test.c
    assert action.to_label() == (
        "sf=0.1;op=un3gpp_ul;action_id=77;run_id=0123456789abcdef0123456789abcdef;ue=1;mcs=9;rb=66;"
        "ttl_ms=30000;issued_at_ns=5"
    )
    release = Un3gppRelease(action_id=78, run_id=RUN, ue=1, ttl_ms=1000, issued_at_ns=5)
    assert release.to_label().startswith("sf=0.1;op=un3gpp_release;action_id=78;")


@pytest.mark.parametrize("fields", [{"mcs": 18}, {"number_rb": 65}, {"number_rb": 131}, {"ue": 16}])
def test_un3gpp_action_mask(fields: dict[str, int]) -> None:
    from smartfarm.contracts.radio import Un3gppUlAction

    base = {"action_id": 1, "run_id": RUN, "ue": 1, "mcs": 9, "number_rb": 66, "ttl_ms": 30000, "issued_at_ns": 5}
    with pytest.raises(ValidationError):
        Un3gppUlAction.model_validate(base | fields)


def test_un3gpp_readback_kinds() -> None:
    from smartfarm.contracts.radio import Un3gppOutcome, Un3gppUeView

    outcome = parse_readback(
        "sf=0.1;kind=un3gpp_outcome;action_id=7;mac_seq=100;status=SENT;dup=0;ue=1;mcs=9;rb=66;at_ns=9"
    )
    assert isinstance(outcome, Un3gppOutcome) and not outcome.final and outcome.succeeded
    view = parse_readback(
        "sf=0.1;kind=un3gpp_ue;ue=1;override=1;mcs_cmd=9;rb_first=0;rb_cmd=66;mcs_obs=-1;rb_obs=66;la_mcs=11;"
        "snr_db=24.10;pdus=180;mac_mbps=12.500;age_ms=300"
    )
    assert isinstance(view, Un3gppUeView) and view.mcs_observed == -1 and view.snr_db == pytest.approx(24.1)
