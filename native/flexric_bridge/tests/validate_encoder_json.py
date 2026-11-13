"""Validate the native encoder output with the production Python wire contract."""

from pathlib import Path
import subprocess
import sys


def main() -> None:
    executable = Path(sys.argv[1])
    repository = Path(sys.argv[2])
    sys.path.insert(0, str(repository / "src"))

    from smartfarm.e2.kpm import decode_kpm_line

    completed = subprocess.run(
        [str(executable), "--emit-json"],
        check=True,
        capture_output=True,
    )
    envelope = decode_kpm_line(completed.stdout)
    assert envelope.e2_node_id == "gnb:001-01:42"
    assert envelope.ue_id == "gnb-amf-ngap:131"
    assert [measurement.name for measurement in envelope.measurements] == [
        "DRB.UEThpDl",
        "RRU.PrbTotDl",
        "DRB.RlcSduDelayDl",
    ]
    assert envelope.measurements[0].value == 55.25
    assert envelope.measurements[2].kind == "no_value"
    assert envelope.measurements[2].value is None


if __name__ == "__main__":
    main()
