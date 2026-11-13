"""Read-only view of the O-RAN radio control in force, for the O-RAN card of the Smart Farm app.

Source: the JSONL journal of the E2 control xApp. `smartfarm_sf_control` appends every E2SM-SF SLICE indication of
both E2 nodes (OAI gNB and the UN3GPP BS proxy) to `<log dir>/sf-control.jsonl`
(deployment/scripts/run_sf_control_supervised.sh), with or without a control client. This module only reads the end
of that file: it never opens the xApp control socket (one client at a time, owned by the RL/calibration tools), never
sends an E2 action and never touches the radio. The demo itself does not run the RL agent, so the view reports what
is in force at each node and the latest E2 outcomes read back, whoever issued them (the DDQN agent or the calibration
tools through the xApp), or the RAN defaults when no E2 action is in force.

Per service slice (`view(sd)`):
  - uplink (UN3GPP BS, from `kind=un3gpp_ue`/`kind=un3gpp_outcome`): E2 override (commanded MCS/RBs) or the BS link
    adaptation, the MCS/RBs observed at the MAC, SNR, and the proxy verdict of the latest action
    (CONFIRMED/CONSISTENT/RELEASED, SENT/RESENT, REJECTED_*);
  - downlink (OAI gNB, from `kind=rule`/`kind=ue`/`kind=outcome` of the slice's SD): the PRB quota enforced for the
    phone in that slice (E2 rule) or none, and the gNB verdict of the latest action (APPLIED, REJECTED_*...);
  - history: the recent E2 outcomes of both nodes for the slice, newest first (the monitor remembers outcomes across
    reads; it reads only when polled, so an action issued and replaced between two polls more than a journal tail
    apart can be missed).
UN3GPP `at_ns` is stamped in the PC2 clock (kept within a few seconds of PC1 by sf_clock_slew.py), so its ages are
approximate. All texts are English. `view()` never raises: a missing or unreadable journal gives
`{"available": false, "detail": ...}`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smartfarm.contracts.radio import (
    UN3GPP_APPLIED,
    UN3GPP_PENDING,
    E2smSfCodecError,
    QuotaOutcome,
    QuotaRule,
    QuotaStatus,
    UeQuota,
    Un3gppOutcome,
    Un3gppUeView,
    parse_readback,
)

SCHEMA = "smartfarm.oran_view.v1"
DEFAULT_JOURNAL_DIR = "experiments/gates/20261102_192902-un3gpp-e2-proxy"  # run_sf_control_supervised.sh default
JOURNAL_NAME = "sf-control.jsonl"
TAIL_BYTES = 192 * 1024  # about 20 s of indications of both nodes (each node reports every ~200 ms)
CACHE_S = 1.0
STALE_S = 10.0  # a node without an indication for this long is flagged stale
DROP_S = 120.0  # ... and left out of the view after this long (xApp or node down)
HISTORY_SHOWN = 6
HISTORY_MAX_AGE_S = 900.0
HISTORY_KEPT = 64
OAI_OK = frozenset({QuotaStatus.APPLIED.value, QuotaStatus.ROLLED_BACK.value})


def default_journal(project: Path) -> Path:
    return project / os.environ.get("SF_CONTROL_LOG_DIR", DEFAULT_JOURNAL_DIR) / JOURNAL_NAME


def tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> list[bytes]:
    """Complete lines among the last `nbytes` of the file (a partial first line is dropped; a partial last line, still
    being written, fails JSON decoding and is skipped by the monitor)."""

    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - nbytes)
        fh.seek(start)
        data = fh.read(nbytes)
    lines = data.split(b"\n")
    return lines[1:] if start > 0 else lines


class JournalTail:
    """Callable source: the last lines of the xApp journal."""

    def __init__(self, path: Path, nbytes: int = TAIL_BYTES) -> None:
        self.path = path
        self.nbytes = nbytes

    def __call__(self) -> list[bytes]:
        return tail_lines(self.path, self.nbytes)


@dataclass(frozen=True, slots=True)
class _Indication:
    received_at_ns: int
    labels: tuple[str, ...]


def _age_s(now_ns: int, at_ns: int) -> float | None:
    return round(max(0.0, (now_ns - at_ns) / 1e9), 1) if at_ns > 0 else None


def _ul_value(o: Un3gppOutcome) -> str:
    if o.status == "RELEASED" or (o.mcs == 0 and o.number_rb == 0):
        return "BS link adaptation"
    return f"MCS {o.mcs} · {o.number_rb} RB"


def _ul_ok(status: str) -> bool | None:
    if status in UN3GPP_APPLIED:
        return True
    return None if status in UN3GPP_PENDING else False


class OranMonitor:
    """Builds the O-RAN view of one slice from the latest xApp readbacks. Thread-unsafe, used from the event loop."""

    def __init__(self, source: Callable[[], Iterable[bytes | str]], *, source_name: str = "xapp-journal",
                 clock_ns: Callable[[], int] = time.time_ns, cache_s: float = CACHE_S, stale_s: float = STALE_S,
                 drop_s: float = DROP_S, history_shown: int = HISTORY_SHOWN,
                 history_max_age_s: float = HISTORY_MAX_AGE_S) -> None:
        self._source = source
        self.source_name = source_name
        self._clock_ns = clock_ns
        self._cache_s = cache_s
        self._stale_s = stale_s
        self._drop_s = drop_s
        self._history_shown = history_shown
        self._history_max_age_s = history_max_age_s
        self._latest: dict[str, _Indication] = {}
        self._read_at_ns: int | None = None
        self._error: str | None = None
        # (node, action_id) -> latest outcome read back for it
        self._outcomes: dict[tuple[str, int], QuotaOutcome | Un3gppOutcome] = {}

    # ------------------------------------------------------------------------------------------------- reading

    def _refresh(self) -> None:
        now = self._clock_ns()
        if self._read_at_ns is not None and now - self._read_at_ns < self._cache_s * 1e9:
            return
        self._read_at_ns = now
        try:
            lines = list(self._source())
        except OSError as error:
            self._error = f"xApp journal not readable: {error.strerror or error}"
            return
        self._error = None
        parsed: set[str] = set()
        for raw in lines:
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "indication":
                continue
            node, labels, at = event.get("node"), event.get("labels"), event.get("received_at_ns")
            if node not in ("oai", "un3gpp") or not isinstance(labels, list) or not isinstance(at, int):
                continue
            labels_t = tuple(str(x) for x in labels)
            previous = self._latest.get(node)
            if previous is None or at >= previous.received_at_ns:
                self._latest[node] = _Indication(at, labels_t)
            for label in labels_t:
                if label in parsed or (";kind=outcome;" not in label and ";kind=un3gpp_outcome;" not in label):
                    continue
                parsed.add(label)
                try:
                    item = parse_readback(label)
                except E2smSfCodecError:
                    continue
                if isinstance(item, (QuotaOutcome, Un3gppOutcome)):
                    self._outcomes[(node, item.action_id)] = item  # file order: the latest status wins
        if len(self._outcomes) > HISTORY_KEPT:
            keep = sorted(self._outcomes.items(), key=lambda kv: kv[1].at_ns)[-HISTORY_KEPT:]
            self._outcomes = dict(keep)

    def _labels(self, node: str, now: int) -> tuple[list[Any], float] | None:
        ind = self._latest.get(node)
        if ind is None:
            return None
        age = (now - ind.received_at_ns) / 1e9
        if age > self._drop_s:
            return None
        items: list[Any] = []
        for label in ind.labels:
            try:
                items.append(parse_readback(label))
            except E2smSfCodecError:
                continue
        return items, round(max(0.0, age), 1)

    # ------------------------------------------------------------------------------------------------- views

    def _uplink(self, now: int) -> dict[str, Any] | None:
        got = self._labels("un3gpp", now)
        if got is None:
            return None
        items, age = got
        ue = next((x for x in items if isinstance(x, Un3gppUeView)), None)
        last = max((x for x in items if isinstance(x, Un3gppOutcome)), key=lambda o: o.at_ns, default=None)
        if ue is None and last is None:
            return None
        view: dict[str, Any] = {"node": "un3gpp", "age_s": age, "stale": age > self._stale_s}
        if ue is not None:
            override = ue.override
            view.update(
                mode="override" if override else "link-adaptation",
                by_e2=override,
                mcs=ue.mcs_commanded if override else None,
                rb=ue.rb_commanded if override else None,
                la_mcs=ue.la_mcs, mcs_observed=ue.mcs_observed, rb_observed=ue.rb_observed,
                snr_db=round(ue.snr_db, 1), mac_mbps=round(ue.mac_mbps, 2),
                label=(f"MCS {ue.mcs_commanded} · {ue.rb_commanded} RB" if override
                       else f"BS link adaptation (MCS {ue.la_mcs})"),
                readback=f"MAC uses MCS {ue.mcs_observed} · {ue.rb_observed} RB · SNR {ue.snr_db:.1f} dB",
            )
        if last is not None:
            view["last_action"] = {"action_id": last.action_id, "status": last.status, "ok": _ul_ok(last.status),
                                   "value": _ul_value(last), "age_s": _age_s(now, last.at_ns)}
        return view

    def _downlink(self, sd: str, now: int) -> dict[str, Any] | None:
        got = self._labels("oai", now)
        if got is None:
            return None
        items, age = got
        rule = next((x for x in items if isinstance(x, QuotaRule) and x.snssai.sd == sd), None)
        ue = next((x for x in items if isinstance(x, UeQuota) and x.snssai is not None and x.snssai.sd == sd), None)
        last = max((x for x in items if isinstance(x, QuotaOutcome) and x.snssai.sd == sd), key=lambda o: o.at_ns,
                   default=None)
        prb = ue.prb_quota if ue is not None else rule.prb_quota if rule is not None else None
        by_e2 = rule is not None or (ue is not None and ue.action_id != 0)
        view: dict[str, Any] = {"node": "oai", "sd": sd, "age_s": age, "stale": age > self._stale_s,
                                "prb_quota": prb, "by_e2": by_e2, "phone_in_slice": ue is not None}
        if prb is None:
            view["label"] = "No quota rule"
        else:
            view["label"] = f"{prb} PRB quota" if by_e2 else f"No quota · {prb} PRB"
        if rule is not None:
            view["expires_in_s"] = round((rule.expires_at_ns - now) / 1e9, 1)
        view["readback"] = (f"gNB enforces {ue.prb_quota} PRB for the phone in this slice" if ue is not None
                            else "phone not scheduled in this slice yet")
        if last is not None:
            view["last_action"] = {"action_id": last.action_id, "status": last.status.value,
                                   "ok": last.status.value in OAI_OK, "value": f"{last.requested_prb} PRB",
                                   "applied_prb": last.applied_prb, "previous_prb": last.previous_prb,
                                   "age_s": _age_s(now, last.at_ns)}
        return view

    def _history(self, sd: str, now: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (node, action_id), o in self._outcomes.items():
            age = _age_s(now, o.at_ns)
            if age is None or age > self._history_max_age_s:
                continue
            if isinstance(o, QuotaOutcome):
                if o.snssai.sd != sd:
                    continue
                rows.append({"node": node, "link": "downlink", "action_id": action_id, "status": o.status.value,
                             "ok": o.status.value in OAI_OK, "value": f"DL {o.requested_prb} PRB", "age_s": age,
                             "_at": o.at_ns})
            else:
                rows.append({"node": node, "link": "uplink", "action_id": action_id, "status": o.status,
                             "ok": _ul_ok(o.status), "value": f"UL {_ul_value(o)}", "age_s": age, "_at": o.at_ns})
        rows.sort(key=lambda r: r["_at"], reverse=True)
        for r in rows:
            del r["_at"]
        return rows[: self._history_shown]

    def view(self, sd: str) -> dict[str, Any]:
        """The O-RAN view for the slice `sd` (6 hex digits). Never raises."""

        try:
            self._refresh()
            now = self._clock_ns()
            if self._error is not None:
                return {"schema": SCHEMA, "available": False, "source": self.source_name, "detail": self._error}
            uplink, downlink = self._uplink(now), self._downlink(sd.lower(), now)
            if uplink is None and downlink is None:
                return {"schema": SCHEMA, "available": False, "source": self.source_name,
                        "detail": "no recent E2 readback in the xApp journal"}
            by_e2 = bool((uplink or {}).get("by_e2")) or bool((downlink or {}).get("by_e2"))
            return {
                "schema": SCHEMA,
                "available": True,
                "source": self.source_name,
                "sd": sd.lower(),
                # who set the radio configuration in force: an E2 action through the xApp, or the RAN's own defaults
                "controller": "e2-action" if by_e2 else "ran-default",
                "decided_by": "E2 action (O-RAN xApp)" if by_e2 else "RAN defaults",
                "agent_running": False,  # the demo orchestrator does not run the DDQN agent
                "uplink": uplink,
                "downlink": downlink,
                "history": self._history(sd.lower(), now),
                "updated_at": datetime.fromtimestamp(now / 1e9, UTC).isoformat(timespec="seconds"),
            }
        except Exception as error:  # noqa: BLE001 - the O-RAN card must never break the session view
            return {"schema": SCHEMA, "available": False, "source": self.source_name,
                    "detail": f"O-RAN view error: {error!r}"[:200]}
