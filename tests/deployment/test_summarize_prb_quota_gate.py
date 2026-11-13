import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path("deployment/scripts/summarize_prb_quota_gate.py")
S = 1_000_000_000


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("summarize_prb_quota_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def envelope(at_s: int, thp: float, prb: float, sd: int = 1) -> dict[str, object]:
    labels = {"subscription_sd": sd}
    return {
        "collected_at_ns": at_s * S,
        "ue_id": "gnb-ran:1",
        "measurements": [
            {"name": "DRB.UEThpDl", "value": thp, "labels": labels},
            {"name": "RRU.PrbTotDl", "value": prb, "labels": labels},
        ],
    }


def test_phase_statistics_exclude_settling_and_other_slices() -> None:
    module = load()
    gate = [
        {"type": "phase", "phase": "dwell-24", "event": "start", "prb": 24, "logged_at_ns": 100 * S},
        {"type": "outcome", "latency_ms": 12.0, "outcome": {"status": "APPLIED"}},
        {"type": "check", "passed": True, "message": "ok"},
        {"type": "phase", "phase": "dwell-24", "event": "end", "prb": 24, "logged_at_ns": 120 * S},
        {"type": "sentinel", "phase": "dwell-24", "passed": True},
        {"type": "gate", "event": "end", "passed": True},
    ]
    kpm = [envelope(102, 99_000, 100)]  # inside the 5 s settling window
    kpm += [envelope(t, 18_000, 23) for t in range(106, 121)]
    kpm += [envelope(110, 1, 1, sd=2)]  # another slice must not leak in
    report = module.summarize(gate, kpm, settle_s=5.0)

    phase = report["phases"][0]
    assert phase["ue_thp_dl_kbps"]["n"] == 15
    assert phase["ue_thp_dl_kbps"]["median"] == 18_000
    assert phase["prb_tot_dl_percent"]["median"] == 23
    assert phase["rlc_sdu_delay_dl_us"]["median"] is None  # missing stays null
    assert report["control"]["status_counts"] == {"APPLIED": 1}
    assert report["gate_passed"] is True
    assert "| dwell-24 | 24 |" in module.markdown(report)
