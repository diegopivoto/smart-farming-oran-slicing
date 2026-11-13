import hashlib
from collections.abc import Sequence
from pathlib import PurePosixPath

from smartfarm.inventory.runner import CommandResult
from smartfarm.un3gpp.mac_audit import MacAuditCollector, parse_flags, parse_grep

FLAGS = """
#define ENABLE_MAC_TDD 0
#define ENABLE_UL_DYNAMIC_RECFG 0 // compiled out
#define ENABLE_MAC_ALWAYS_DCI 1
#define ENABLE_MAC_TDD 1
"""


class FakeRunner:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append(tuple(argv))
        joined = " ".join(argv)
        stdout = next((out for key, out in self.outputs.items() if key in joined), "")
        return CommandResult(host=host, argv=tuple(argv), exit_code=0, stdout=stdout, stderr="",
                             duration_ms=1, timed_out=False)


def test_flags_follow_preprocessor_last_definition() -> None:
    flags = parse_flags(FLAGS)
    assert flags["ENABLE_MAC_TDD"] == "1"
    assert flags["ENABLE_UL_DYNAMIC_RECFG"] == "0"
    assert flags["ENABLE_MAC_ALWAYS_DCI"] == "1"


def test_grep_lines_become_attributed_hits() -> None:
    hits = parse_grep(
        "a/MacController.cpp:1060:  dyn->setMcsUplink(mac, mcs);\nnoise\nb/X.h:7:void setUlReservation(a);\n",
        ("setMcsUplink", "setUlReservation"),
    )
    assert [(h.symbol, h.path, h.line) for h in hits] == [
        ("setMcsUplink", "a/MacController.cpp", 1060),
        ("setUlReservation", "b/X.h", 7),
    ]


def test_collector_records_identity_diff_hash_and_is_read_only() -> None:
    runner = FakeRunner({
        "branch --show-current": "infra-tvws-5g-bs\n",
        "rev-parse HEAD": "0e74c2e\n",
        "status --short": " M build/Default.csv\n",
        "diff --stat": " build/Default.csv | 15 ++\n",
        "-C /repo diff": "diff --git a/x b/x\n",
        "mac_feature_flags.h": FLAGS,
        "sha256sum": "abc123  /repo/build/coreL2.o\n",
        "pgrep": "2962 ./coreL2.o tun0\n",
    })
    audit = MacAuditCollector(runner).collect("smartfarm-pc2", PurePosixPath("/repo"))

    assert audit.branch == "infra-tvws-5g-bs" and audit.head == "0e74c2e"
    assert audit.status_short == (" M build/Default.csv",)
    assert audit.diff_sha256 == hashlib.sha256(b"diff --git a/x b/x\n").hexdigest()
    assert audit.feature_flags["ENABLE_MAC_ALWAYS_DCI"] == "1"
    assert audit.binary_sha256 == "abc123"
    assert audit.running_processes == ("2962 ./coreL2.o tun0",)
    mutating = {"rm", "mv", "cp", "kill", "make", "cmake", "systemctl", "tee"}
    assert not any(call[0] in mutating for call in runner.calls)
    assert all(call[:2] != ("git", "--no-pager") or call[4] in {"branch", "rev-parse", "status", "diff"}
               for call in runner.calls)
