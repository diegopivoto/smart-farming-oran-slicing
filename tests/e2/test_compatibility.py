from pathlib import Path
from typing import Literal

import yaml

from smartfarm.e2.compatibility import (
    EXPECTED_E2AP,
    EXPECTED_FLEXRIC_COMMIT,
    EXPECTED_KPM,
    EXPECTED_OAI_COMMIT,
    EXPECTED_SM_DIR,
    E2BuildManifest,
    verify_compatible,
)


def manifest(component: Literal["oai", "flexric"]) -> E2BuildManifest:
    return E2BuildManifest(
        component=component,
        commit=EXPECTED_OAI_COMMIT if component == "oai" else EXPECTED_FLEXRIC_COMMIT,
        e2ap=EXPECTED_E2AP,
        kpm=EXPECTED_KPM,
        sm_dir=EXPECTED_SM_DIR,
        binary_sha256="a" * 64 if component == "oai" else "b" * 64,
        submodule_initialized=True,
    )


def test_matching_oai_and_flexric_builds_are_compatible() -> None:
    report = verify_compatible(manifest("oai"), manifest("flexric"))

    assert report.compatible is True
    assert report.mismatches == ()


def test_every_compatibility_mismatch_is_structured() -> None:
    cases = (
        ("oai", {"commit": "0" * 40}, "oai.commit", EXPECTED_OAI_COMMIT, "0" * 40),
        (
            "flexric",
            {"commit": "1" * 40},
            "flexric.commit",
            EXPECTED_FLEXRIC_COMMIT,
            "1" * 40,
        ),
        ("oai", {"e2ap": "E2AP_V3"}, "oai.e2ap", EXPECTED_E2AP, "E2AP_V3"),
        ("flexric", {"kpm": "KPM_V3_00"}, "flexric.kpm", EXPECTED_KPM, "KPM_V3_00"),
        (
            "oai",
            {"sm_dir": Path("/tmp/wrong-sm")},
            "oai.sm_dir",
            str(EXPECTED_SM_DIR),
            "/tmp/wrong-sm",
        ),
        (
            "oai",
            {"submodule_initialized": False},
            "oai.submodule_initialized",
            "true",
            "false",
        ),
    )

    for component, update, field, expected, observed in cases:
        oai = manifest("oai")
        flexric = manifest("flexric")
        if component == "oai":
            oai = oai.model_copy(update=update)
        else:
            flexric = flexric.model_copy(update=update)

        report = verify_compatible(oai, flexric)

        assert report.compatible is False
        assert [(item.field, item.expected, item.observed) for item in report.mismatches] == [
            (field, expected, observed)
        ]


def test_component_order_is_checked_not_assumed() -> None:
    report = verify_compatible(manifest("flexric"), manifest("oai"))

    assert report.compatible is False
    assert {mismatch.field for mismatch in report.mismatches} >= {
        "oai.component",
        "flexric.component",
    }


def test_version_document_matches_code_contract() -> None:
    path = Path("deployment/flexric/versions.yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert document == {
        "schema_version": 1,
        "oai_commit": EXPECTED_OAI_COMMIT,
        "flexric_commit": EXPECTED_FLEXRIC_COMMIT,
        "e2ap": EXPECTED_E2AP,
        "kpm": EXPECTED_KPM,
        "sm_dir": str(EXPECTED_SM_DIR),
        "near_ric_ip_addr": "192.168.56.101",
    }


def test_patch_branch_is_compatible_through_its_pinned_base() -> None:
    patched = manifest("oai").model_copy(update={"commit": "c" * 40, "base_commit": EXPECTED_OAI_COMMIT})
    assert verify_compatible(patched, manifest("flexric")).compatible is True

    wrong_base = patched.model_copy(update={"base_commit": "d" * 40})
    report = verify_compatible(wrong_base, manifest("flexric"))
    assert not report.compatible
    assert report.mismatches[0].field == "oai.commit"


def test_flexric_patch_branch_is_not_accepted() -> None:
    patched = manifest("flexric").model_copy(update={"base_commit": EXPECTED_FLEXRIC_COMMIT})
    report = verify_compatible(manifest("oai"), patched)
    assert [m.field for m in report.mismatches] == ["flexric.base_commit"]
