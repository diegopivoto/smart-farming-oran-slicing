"""Fail-closed compatibility contract for OAI and FlexRIC builds."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EXPECTED_OAI_COMMIT = "f8f769592a7030be88ede4bb5ca66fa1ca6a80e0"
EXPECTED_FLEXRIC_COMMIT = "d7a71285f3720fbc1487e9a86309eec5fb61a408"
EXPECTED_E2AP = "E2AP_V2"
EXPECTED_KPM = "KPM_V2_03"
EXPECTED_SM_DIR = Path("/usr/local/lib/flexric")


class FrozenModel(BaseModel):
    """Immutable compatibility value object."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class E2BuildManifest(FrozenModel):
    """Observed identity of one compiled E2 component."""

    component: Literal["oai", "flexric"]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    e2ap: str = Field(min_length=1)
    kpm: str = Field(min_length=1)
    sm_dir: Path
    binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submodule_initialized: bool
    # Set only for an explicitly allowed patch branch: the pinned commit it descends from.
    base_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @property
    def pinned_commit(self) -> str:
        """Commit that must match the pinned wire contract (the base of a patch branch)."""

        return self.base_commit if self.base_commit is not None else self.commit

    @field_validator("sm_dir")
    @classmethod
    def require_absolute_sm_dir(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("sm_dir must be absolute")
        return value


class CompatibilityMismatch(FrozenModel):
    """One expected-versus-observed preflight failure."""

    field: str
    expected: str
    observed: str


class CompatibilityReport(FrozenModel):
    """Complete preflight decision; callers must require `compatible`."""

    compatible: bool
    mismatches: tuple[CompatibilityMismatch, ...]


def verify_compatible(
    oai: E2BuildManifest,
    ric: E2BuildManifest,
) -> CompatibilityReport:
    """Compare observed builds against the pinned, common wire contract."""

    mismatches: list[CompatibilityMismatch] = []

    def check(field: str, expected: object, observed: object) -> None:
        expected_text = _render(expected)
        observed_text = _render(observed)
        if expected_text != observed_text:
            mismatches.append(
                CompatibilityMismatch(
                    field=field,
                    expected=expected_text,
                    observed=observed_text,
                )
            )

    check("oai.component", "oai", oai.component)
    check("flexric.component", "flexric", ric.component)
    check("oai.commit", EXPECTED_OAI_COMMIT, oai.pinned_commit)
    check("flexric.commit", EXPECTED_FLEXRIC_COMMIT, ric.commit)
    check("flexric.base_commit", None, ric.base_commit)
    check("oai.e2ap", EXPECTED_E2AP, oai.e2ap)
    check("flexric.e2ap", EXPECTED_E2AP, ric.e2ap)
    check("oai.kpm", EXPECTED_KPM, oai.kpm)
    check("flexric.kpm", EXPECTED_KPM, ric.kpm)
    check("oai.sm_dir", EXPECTED_SM_DIR, oai.sm_dir)
    check("flexric.sm_dir", EXPECTED_SM_DIR, ric.sm_dir)
    check("oai.submodule_initialized", True, oai.submodule_initialized)
    check("flexric.submodule_initialized", True, ric.submodule_initialized)
    return CompatibilityReport(compatible=not mismatches, mismatches=tuple(mismatches))


def _render(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)
