from pathlib import Path

import pytest


RUNBOOK = Path("docs/runbooks/flexric-oai-kpm.md")


@pytest.mark.skipif(not RUNBOOK.exists(), reason="internal lab runbook, not distributed")
def test_runbook_installs_only_service_model_shared_objects() -> None:
    document = RUNBOOK.read_text(encoding="utf-8")

    assert (
        ".build/flexric-e2ap-v2-kpm-v2.03/stage-root/usr/local/"
        "lib/flexric/lib*_sm.so" in document
    )
    assert (
        ".build/flexric-e2ap-v2-kpm-v2.03/stage-root/usr/local/" "lib/flexric/*.so" not in document
    )
