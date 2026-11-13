#!/usr/bin/env python3
"""Verify a staged FlexRIC build and emit immutable build evidence."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from smartfarm.e2.compatibility import (  # noqa: E402
    EXPECTED_E2AP,
    EXPECTED_FLEXRIC_COMMIT,
    EXPECTED_KPM,
    EXPECTED_SM_DIR,
    E2BuildManifest,
)


class VerificationError(RuntimeError):
    """Raised when staged output does not match the pinned contract."""


def sha256(path: Path) -> str:
    """Hash one artifact without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_git_head(source: Path) -> str:
    """Resolve a detached or referenced HEAD without invoking Git."""

    git_entry = source / ".git"
    if git_entry.is_file():
        marker = git_entry.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            raise VerificationError(f"invalid gitdir marker: {git_entry}")
        git_dir = Path(marker.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = (source / git_dir).resolve()
    elif git_entry.is_dir():
        git_dir = git_entry
    else:
        raise VerificationError(f"missing Git metadata: {git_entry}")

    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    reference = head.removeprefix("ref: ")
    loose_ref = git_dir / reference
    if loose_ref.exists():
        return loose_ref.read_text(encoding="utf-8").strip()
    packed_refs = git_dir / "packed-refs"
    if packed_refs.exists():
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "^")):
                commit, name = line.split(" ", maxsplit=1)
                if name == reference:
                    return commit
    raise VerificationError(f"unresolved Git reference: {reference}")


def find_near_ric(build_dir: Path) -> Path:
    """Find the single built near-RT RIC executable."""

    candidates = [
        path
        for path in build_dir.rglob("nearRT-RIC")
        if path.is_file() and os.access(path, os.X_OK) and "stage-root" not in path.parts
    ]
    if len(candidates) != 1:
        raise VerificationError(f"expected one nearRT-RIC, found {len(candidates)}")
    return candidates[0]


def _artifact_label(path: Path, build_dir: Path) -> str:
    """Return a sha256sum-compatible path relative to the evidence root."""

    try:
        return path.relative_to(build_dir).as_posix()
    except ValueError as error:
        raise VerificationError(f"artifact is outside build root: {path}") from error


def verify(source: Path, build_dir: Path, stage_root: Path) -> E2BuildManifest:
    """Validate commit, CMake versions, RIC binary, and staged SM libraries."""

    commit = read_git_head(source)
    if commit != EXPECTED_FLEXRIC_COMMIT:
        raise VerificationError(
            f"FlexRIC commit mismatch: expected {EXPECTED_FLEXRIC_COMMIT}, observed {commit}"
        )

    cache = (build_dir / "CMakeCache.txt").read_text(encoding="utf-8")
    for variable, expected in (
        ("E2AP_VERSION", EXPECTED_E2AP),
        ("KPM_VERSION", EXPECTED_KPM),
        ("UNIT_TEST", "FALSE"),
        ("NEAR_RIC_INSTALL", "TRUE"),
        ("XAPP_C_INSTALL", "FALSE"),
    ):
        if f"{variable}:STRING={expected}" not in cache:
            raise VerificationError(f"CMake cache does not pin {variable}={expected}")

    find_near_ric(build_dir)
    staged_bin_dir = stage_root / "usr/local/bin"
    near_ric = staged_bin_dir / "nearRT-RIC"
    kpm_xapp = staged_bin_dir / "xapp_kpm_moni"
    for executable in (near_ric, kpm_xapp):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise VerificationError(f"missing staged executable: {executable}")

    staged_sm_dir = stage_root / EXPECTED_SM_DIR.relative_to("/")
    service_models = [
        staged_sm_dir / f"lib{name}_sm.so"
        for name in ("gtp", "kpm", "mac", "pdcp", "rc", "rlc", "slice", "tc")
    ]
    missing_service_models = [path for path in service_models if not path.is_file()]
    if missing_service_models:
        raise VerificationError(f"missing service model libraries: {missing_service_models}")
    sdk_libraries = [
        staged_sm_dir / "libe42_xapp.a",
        staged_sm_dir / "libe42_xapp_shared.so",
    ]
    sdk_header = stage_root / "usr/local/include/flexric/e42_xapp_api.h"
    for artifact in (*sdk_libraries, sdk_header):
        if not artifact.is_file():
            raise VerificationError(f"missing xApp SDK artifact: {artifact}")
    staged_config = stage_root / "usr/local/etc/flexric/flexric.conf"
    expected_config = PROJECT_ROOT / "deployment/flexric/flexric.conf"
    if not staged_config.is_file() or sha256(staged_config) != sha256(expected_config):
        raise VerificationError("staged FlexRIC configuration does not match project config")

    manifest = E2BuildManifest(
        component="flexric",
        commit=commit,
        e2ap=EXPECTED_E2AP,
        kpm=EXPECTED_KPM,
        sm_dir=EXPECTED_SM_DIR,
        binary_sha256=sha256(near_ric),
        submodule_initialized=True,
    )
    _atomic_json(build_dir / "flexric-build-manifest.json", manifest.model_dump(mode="json"))

    checksum_lines = [
        f"{sha256(path)}  {_artifact_label(path, build_dir)}"
        for path in sorted(
            [near_ric, kpm_xapp, *service_models, *sdk_libraries, sdk_header, staged_config]
        )
    ]
    _atomic_text(build_dir / "flexric-checksums.sha256", "\n".join(checksum_lines) + "\n")
    return manifest


def _atomic_json(path: Path, document: object) -> None:
    _atomic_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--stage-root", type=Path, required=True)
    arguments = parser.parse_args()

    manifest = verify(
        arguments.source.resolve(),
        arguments.build_dir.resolve(),
        arguments.stage_root.resolve(),
    )
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
