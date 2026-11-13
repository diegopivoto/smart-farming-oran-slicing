"""Experimental E2SM-SF/0.1 radio actions and readback for the OAI DL PRB quota.

The profile is carried as ``key=value;...`` text inside DL slice labels of
FlexRIC's custom SLICE service model. It is not O-RAN E2SM-RC nor a
standardized slicing service model; see ``schemas/e2sm-sf-v0.1.md``.
"""

from enum import StrEnum
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field, field_validator

from smartfarm.contracts.service import FrozenModel, RadioBounds, Snssai

PROFILE_VERSION = "0.1"
EXPERIMENTAL_PROFILE_URN = "urn:inatel:smartfarm:e2sm-sf:0.1"
MAX_TTL_MS = 30_000
MAX_LABEL_BYTES = 511
UINT64_MAX = 2**64 - 1


class E2smSfCodecError(ValueError):
    """Raised when an E2SM-SF label cannot be encoded or decoded."""


class QuotaStatus(StrEnum):
    """Outcome status names, identical to the gNB-side enumeration."""

    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED_RANGE = "REJECTED_RANGE"
    REJECTED_TTL = "REJECTED_TTL"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_TARGET = "REJECTED_TARGET"
    REJECTED_RUN = "REJECTED_RUN"
    CONFLICT = "CONFLICT"
    TABLE_FULL = "TABLE_FULL"
    ROLLBACK_UNKNOWN = "ROLLBACK_UNKNOWN"
    ROLLBACK_TOKEN = "ROLLBACK_TOKEN"
    ROLLBACK_SUPERSEDED = "ROLLBACK_SUPERSEDED"
    REJECTED_MALFORMED = "REJECTED_MALFORMED"


def _sd_int(snssai: Snssai) -> int:
    return int(snssai.sd, 16)


def _snssai(sst: int, sd: int) -> Snssai:
    return Snssai(sst=sst, sd=f"{sd:06x}")


class PrbQuotaAction(FrozenModel):
    """Cap new DL transmissions of a slice (optionally one RNTI) to a PRB quota."""

    OP: ClassVar[str] = "prb_quota"

    action_id: int = Field(ge=1, le=UINT64_MAX)
    run_id: UUID
    snssai: Snssai
    rnti: int = Field(default=0, ge=0, le=0xFFFF)
    prb_quota: int
    ttl_ms: int = Field(ge=1, le=MAX_TTL_MS)
    issued_at_ns: int = Field(ge=0, le=UINT64_MAX)

    @field_validator("prb_quota")
    @classmethod
    def _quota_in_mask(cls, value: int) -> int:
        if value not in RadioBounds.DL_PRB_ACTIONS:
            raise ValueError(f"prb_quota must be one of {sorted(RadioBounds.DL_PRB_ACTIONS)}")
        return value

    def to_label(self) -> str:
        return _join(
            ("sf", PROFILE_VERSION),
            ("op", self.OP),
            ("action_id", str(self.action_id)),
            ("run_id", self.run_id.hex),
            ("sst", str(self.snssai.sst)),
            ("sd", f"{_sd_int(self.snssai):06x}"),
            ("rnti", str(self.rnti)),
            ("prb", str(self.prb_quota)),
            ("ttl_ms", str(self.ttl_ms)),
            ("issued_at_ns", str(self.issued_at_ns)),
        )


class QuotaRollback(FrozenModel):
    """Undo a previously applied quota action, authenticated by its rollback token."""

    OP: ClassVar[str] = "rollback"

    action_id: int = Field(ge=1, le=UINT64_MAX)
    run_id: UUID
    target_action_id: int = Field(ge=1, le=UINT64_MAX)
    rollback_token: int = Field(ge=0, le=UINT64_MAX)
    ttl_ms: int = Field(ge=1, le=MAX_TTL_MS)
    issued_at_ns: int = Field(ge=0, le=UINT64_MAX)

    def to_label(self) -> str:
        return _join(
            ("sf", PROFILE_VERSION),
            ("op", self.OP),
            ("action_id", str(self.action_id)),
            ("run_id", self.run_id.hex),
            ("target_action_id", str(self.target_action_id)),
            ("token", f"{self.rollback_token:016x}"),
            ("ttl_ms", str(self.ttl_ms)),
            ("issued_at_ns", str(self.issued_at_ns)),
        )


class Un3gppUlAction(FrozenModel):
    """Set the UL MCS and RB reservation (startRB=0) of one UE at the UN3GPP BS research proxy."""

    OP: ClassVar[str] = "un3gpp_ul"

    action_id: int = Field(ge=1, le=UINT64_MAX)
    run_id: UUID
    ue: int = Field(ge=0, le=15)
    mcs: int = Field(ge=RadioBounds.UL_MCS_MIN, le=RadioBounds.UL_MCS_MAX)
    number_rb: int
    ttl_ms: int = Field(ge=1, le=MAX_TTL_MS)
    issued_at_ns: int = Field(ge=0, le=UINT64_MAX)

    @field_validator("number_rb")
    @classmethod
    def _rb_in_mask(cls, value: int) -> int:
        if value not in RadioBounds.UL_RB_ACTIONS:
            raise ValueError(f"number_rb must be one of {sorted(RadioBounds.UL_RB_ACTIONS)}")
        return value

    def to_label(self) -> str:
        return _join(
            ("sf", PROFILE_VERSION), ("op", self.OP), ("action_id", str(self.action_id)),
            ("run_id", self.run_id.hex), ("ue", str(self.ue)), ("mcs", str(self.mcs)),
            ("rb", str(self.number_rb)), ("ttl_ms", str(self.ttl_ms)), ("issued_at_ns", str(self.issued_at_ns)),
        )


class Un3gppRelease(FrozenModel):
    """Hand the UL MCS of one UE back to the BS link adaptation (RB reservation unchanged)."""

    OP: ClassVar[str] = "un3gpp_release"

    action_id: int = Field(ge=1, le=UINT64_MAX)
    run_id: UUID
    ue: int = Field(ge=0, le=15)
    ttl_ms: int = Field(ge=1, le=MAX_TTL_MS)
    issued_at_ns: int = Field(ge=0, le=UINT64_MAX)

    def to_label(self) -> str:
        return _join(
            ("sf", PROFILE_VERSION), ("op", self.OP), ("action_id", str(self.action_id)),
            ("run_id", self.run_id.hex), ("ue", str(self.ue)), ("ttl_ms", str(self.ttl_ms)),
            ("issued_at_ns", str(self.issued_at_ns)),
        )


RadioAction = PrbQuotaAction | QuotaRollback | Un3gppUlAction | Un3gppRelease


class QuotaOutcome(FrozenModel):
    """gNB verdict for one action, as published in the SLICE indication readback."""

    action_id: int = Field(ge=0, le=UINT64_MAX)
    status: QuotaStatus
    duplicate: bool
    snssai: Snssai
    rnti: int = Field(ge=0, le=0xFFFF)
    requested_prb: int
    applied_prb: int
    previous_prb: int
    rollback_token: int = Field(ge=0, le=UINT64_MAX)
    expires_at_ns: int = Field(ge=0)
    table_version: int = Field(ge=0)
    at_ns: int = Field(ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status in (QuotaStatus.APPLIED, QuotaStatus.ROLLED_BACK)


UN3GPP_APPLIED = frozenset({"CONFIRMED", "CONSISTENT", "RELEASED"})
UN3GPP_PENDING = frozenset({"SENT", "RESENT"})


class Un3gppOutcome(FrozenModel):
    """UN3GPP proxy verdict for one action; SENT/RESENT are interim, the rest are final."""

    action_id: int = Field(ge=0, le=UINT64_MAX)
    mac_seq: int = Field(ge=0)
    status: str = Field(min_length=1)
    duplicate: bool
    ue: int = Field(ge=0, le=15)
    mcs: int = Field(ge=0, le=255)
    number_rb: int = Field(ge=0, le=255)
    at_ns: int = Field(ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status in UN3GPP_APPLIED or self.status in UN3GPP_PENDING

    @property
    def final(self) -> bool:
        return self.status not in UN3GPP_PENDING


class Un3gppUeView(FrozenModel):
    """Commanded versus observed UL configuration and radio quality of one UN3GPP UE."""

    ue: int = Field(ge=0, le=15)
    override: bool
    mcs_commanded: int
    rb_first: int
    rb_commanded: int
    mcs_observed: int
    rb_observed: int
    la_mcs: int
    snr_db: float
    data_pdus: int
    mac_mbps: float
    age_ms: int


RadioOutcome = QuotaOutcome | Un3gppOutcome


class QuotaRule(FrozenModel):
    """A live quota rule in the gNB table."""

    snssai: Snssai
    rnti: int = Field(ge=0, le=0xFFFF)
    prb_quota: int
    action_id: int = Field(ge=0, le=UINT64_MAX)
    expires_at_ns: int = Field(ge=0)
    table_version: int = Field(ge=0)


class QuotaCounters(FrozenModel):
    """Monotonic scheduler counters of the quota table."""

    lookups: int = Field(ge=0)
    unmapped: int = Field(ge=0)
    expired: int = Field(ge=0)
    fallback: int = Field(ge=0)
    capped: int = Field(ge=0)
    bypass_retx: int = Field(ge=0)
    table_version: int = Field(ge=0)


class UeQuota(FrozenModel):
    """Quota currently enforced for one connected UE (readback per RNTI)."""

    rnti: int = Field(ge=0, le=0xFFFF)
    snssai: Snssai | None
    prb_quota: int
    action_id: int = Field(ge=0, le=UINT64_MAX)


Readback = QuotaOutcome | QuotaRule | QuotaCounters | UeQuota | Un3gppOutcome | Un3gppUeView

_NO_SD = 0xFFFFFF


def _join(*pairs: tuple[str, str]) -> str:
    label = ";".join(f"{key}={value}" for key, value in pairs)
    if len(label.encode()) > MAX_LABEL_BYTES:
        raise E2smSfCodecError("label exceeds the E2SM-SF size limit")
    return label


def _split(label: str) -> tuple[str, dict[str, str]]:
    if len(label.encode()) > MAX_LABEL_BYTES:
        raise E2smSfCodecError("label exceeds the E2SM-SF size limit")
    pairs: list[tuple[str, str]] = []
    for item in label.split(";"):
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise E2smSfCodecError(f"malformed pair {item!r}")
        pairs.append((key, value))
    if len(pairs) < 2 or pairs[0] != ("sf", PROFILE_VERSION):
        raise E2smSfCodecError(f"label must start with sf={PROFILE_VERSION}")
    selector_key, selector = pairs[1]
    fields: dict[str, str] = {}
    for key, value in pairs[2:]:
        if key in fields:
            raise E2smSfCodecError(f"duplicate key {key}")
        fields[key] = value
    return f"{selector_key}={selector}", fields


def _take(fields: dict[str, str], expected: tuple[str, ...]) -> dict[str, str]:
    if set(fields) != set(expected):
        missing = sorted(set(expected) - set(fields))
        extra = sorted(set(fields) - set(expected))
        raise E2smSfCodecError(f"unexpected fields (missing={missing}, extra={extra})")
    return fields


def _dec(value: str) -> int:
    if not value.isdigit() or not value.isascii():
        raise E2smSfCodecError(f"not a decimal integer: {value!r}")
    return int(value)


def _hex(value: str, digits: int) -> int:
    if len(value) != digits or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise E2smSfCodecError(f"expected {digits} hex digits: {value!r}")
    return int(value, 16)


def parse_request(label: str) -> RadioAction:
    """Decode a request label (used for round-trip tests and audit tooling)."""

    selector, fields = _split(label)
    try:
        if selector == f"op={PrbQuotaAction.OP}":
            f = _take(fields, ("action_id", "run_id", "sst", "sd", "rnti", "prb", "ttl_ms", "issued_at_ns"))
            return PrbQuotaAction(
                action_id=_dec(f["action_id"]),
                run_id=UUID(hex=f"{_hex(f['run_id'], 32):032x}"),
                snssai=_snssai(_dec(f["sst"]), _hex(f["sd"], 6)),
                rnti=_dec(f["rnti"]),
                prb_quota=_dec(f["prb"]),
                ttl_ms=_dec(f["ttl_ms"]),
                issued_at_ns=_dec(f["issued_at_ns"]),
            )
        if selector == f"op={QuotaRollback.OP}":
            f = _take(fields, ("action_id", "run_id", "target_action_id", "token", "ttl_ms", "issued_at_ns"))
            return QuotaRollback(
                action_id=_dec(f["action_id"]),
                run_id=UUID(hex=f"{_hex(f['run_id'], 32):032x}"),
                target_action_id=_dec(f["target_action_id"]),
                rollback_token=_hex(f["token"], 16),
                ttl_ms=_dec(f["ttl_ms"]),
                issued_at_ns=_dec(f["issued_at_ns"]),
            )
    except ValueError as error:
        if isinstance(error, E2smSfCodecError):
            raise
        raise E2smSfCodecError(str(error)) from error
    raise E2smSfCodecError(f"unknown request selector {selector}")


def _signed(value: str) -> int:
    if value.startswith("-") and value[1:].isdigit():
        return -int(value[1:])
    return _dec(value)


def _float(value: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise E2smSfCodecError(f"not a number: {value!r}") from error


def parse_readback(label: str) -> Readback:
    """Decode one readback label published by the gNB in a SLICE indication."""

    selector, fields = _split(label)
    try:
        if selector == "kind=outcome":
            f = _take(
                fields,
                (
                    "action_id", "status", "dup", "sst", "sd", "rnti", "requested", "applied",
                    "previous", "token", "expires_at_ns", "version", "at_ns",
                ),
            )
            if f["dup"] not in ("0", "1"):
                raise E2smSfCodecError("dup must be 0 or 1")
            return QuotaOutcome(
                action_id=_dec(f["action_id"]),
                status=QuotaStatus(f["status"]),
                duplicate=f["dup"] == "1",
                snssai=_snssai(_dec(f["sst"]), _hex(f["sd"], 6)),
                rnti=_dec(f["rnti"]),
                requested_prb=_dec(f["requested"]),
                applied_prb=_dec(f["applied"]),
                previous_prb=_dec(f["previous"]),
                rollback_token=_hex(f["token"], 16),
                expires_at_ns=_dec(f["expires_at_ns"]),
                table_version=_dec(f["version"]),
                at_ns=_dec(f["at_ns"]),
            )
        if selector == "kind=rule":
            f = _take(fields, ("sst", "sd", "rnti", "prb", "action_id", "expires_at_ns", "version"))
            return QuotaRule(
                snssai=_snssai(_dec(f["sst"]), _hex(f["sd"], 6)),
                rnti=_dec(f["rnti"]),
                prb_quota=_dec(f["prb"]),
                action_id=_dec(f["action_id"]),
                expires_at_ns=_dec(f["expires_at_ns"]),
                table_version=_dec(f["version"]),
            )
        if selector == "kind=counters":
            f = _take(fields, ("lookups", "unmapped", "expired", "fallback", "capped", "bypass_retx", "version"))
            return QuotaCounters(
                lookups=_dec(f["lookups"]),
                unmapped=_dec(f["unmapped"]),
                expired=_dec(f["expired"]),
                fallback=_dec(f["fallback"]),
                capped=_dec(f["capped"]),
                bypass_retx=_dec(f["bypass_retx"]),
                table_version=_dec(f["version"]),
            )
        if selector == "kind=un3gpp_outcome":
            f = _take(fields, ("action_id", "mac_seq", "status", "dup", "ue", "mcs", "rb", "at_ns"))
            if f["dup"] not in ("0", "1"):
                raise E2smSfCodecError("dup must be 0 or 1")
            return Un3gppOutcome(
                action_id=_dec(f["action_id"]), mac_seq=_dec(f["mac_seq"]), status=f["status"],
                duplicate=f["dup"] == "1", ue=_dec(f["ue"]), mcs=_dec(f["mcs"]), number_rb=_dec(f["rb"]),
                at_ns=_dec(f["at_ns"]),
            )
        if selector == "kind=un3gpp_ue":
            f = _take(fields, ("ue", "override", "mcs_cmd", "rb_first", "rb_cmd", "mcs_obs", "rb_obs", "la_mcs",
                               "snr_db", "pdus", "mac_mbps", "age_ms"))
            return Un3gppUeView(
                ue=_dec(f["ue"]), override=f["override"] == "1", mcs_commanded=_signed(f["mcs_cmd"]),
                rb_first=_signed(f["rb_first"]), rb_commanded=_signed(f["rb_cmd"]),
                mcs_observed=_signed(f["mcs_obs"]), rb_observed=_signed(f["rb_obs"]), la_mcs=_signed(f["la_mcs"]),
                snr_db=_float(f["snr_db"]), data_pdus=_dec(f["pdus"]), mac_mbps=_float(f["mac_mbps"]),
                age_ms=_dec(f["age_ms"]),
            )
        if selector == "kind=ue":
            f = _take(fields, ("rnti", "sst", "sd", "prb", "action_id"))
            sst, sd = _dec(f["sst"]), _hex(f["sd"], 6)
            return UeQuota(
                rnti=_dec(f["rnti"]),
                snssai=None if sst == 0 and sd == _NO_SD else _snssai(sst, sd),
                prb_quota=_dec(f["prb"]),
                action_id=_dec(f["action_id"]),
            )
    except ValueError as error:
        if isinstance(error, E2smSfCodecError):
            raise
        raise E2smSfCodecError(str(error)) from error
    raise E2smSfCodecError(f"unknown readback selector {selector}")


def quota_status_literal(status: QuotaStatus) -> Literal["ok", "rejected"]:
    """Collapse a status into the binary verdict recorded per RL transition."""

    return "ok" if status in (QuotaStatus.APPLIED, QuotaStatus.ROLLED_BACK) else "rejected"
