#!/usr/bin/env python3
"""Fail-closed preflight for an isolated OAI E2 build and runtime config."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from smartfarm.e2.compatibility import (  # noqa: E402
    EXPECTED_E2AP,
    EXPECTED_KPM,
    EXPECTED_OAI_COMMIT,
    EXPECTED_SM_DIR,
    E2BuildManifest,
    verify_compatible,
)


class PreflightError(RuntimeError):
    """Raised when any deployment precondition is not proven."""


EXPECTED_SERVICE_MODELS = ("gtp", "kpm", "mac", "pdcp", "rc", "rlc", "slice", "tc")
EXPECTED_NEAR_RIC_IP = "192.168.56.101"
E2_BLOCK = re.compile(r"(?ms)^[ \t]*e2_agent[ \t]*=[ \t]*\{(?P<body>.*?)^[ \t]*\};")


def sha256(path: Path) -> str:
    """Hash one artifact without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_git_head(source: Path) -> str:
    """Resolve a detached or referenced worktree HEAD without invoking Git."""

    git_entry = source / ".git"
    if git_entry.is_file():
        marker = git_entry.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            raise PreflightError(f"invalid gitdir marker: {git_entry}")
        git_dir = Path(marker.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = (source / git_dir).resolve()
    elif git_entry.is_dir():
        git_dir = git_entry
    else:
        raise PreflightError(f"missing Git metadata: {git_entry}")

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
    raise PreflightError(f"unresolved Git reference: {reference}")


def _cache_values(cache_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([^#/:=]+):[^=]+=(.*)", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _require_cache(cache: dict[str, str], variable: str, expected: str) -> None:
    observed = cache.get(variable, "<missing>")
    if observed != expected:
        raise PreflightError(f"{variable}: expected {expected}, observed {observed}")


def _field(block: str, name: str) -> str:
    pattern = re.compile(
        rf'(?m)^[ \t]*{re.escape(name)}[ \t]*=[ \t]*"([^"\n]+)"' r"[ \t]*;?[ \t]*(?:#[^\n]*)?$"
    )
    values: list[str] = pattern.findall(block)
    if len(values) != 1:
        raise PreflightError(f"expected one {name} field, found {len(values)}")
    return values[0]


def _verify_runtime_config(runtime_config: Path) -> None:
    config = runtime_config.read_text(encoding="utf-8")
    blocks = E2_BLOCK.findall(config)
    if len(blocks) != 1:
        raise PreflightError(f"expected one e2_agent block, found {len(blocks)}")
    near_ric_ip = _field(blocks[0], "near_ric_ip_addr")
    if near_ric_ip != EXPECTED_NEAR_RIC_IP:
        raise PreflightError(
            f"near_ric_ip_addr: expected {EXPECTED_NEAR_RIC_IP}, observed {near_ric_ip}"
        )
    sm_dir = Path(_field(blocks[0], "sm_dir"))
    if sm_dir != EXPECTED_SM_DIR:
        raise PreflightError(f"sm_dir: expected {EXPECTED_SM_DIR}, observed {sm_dir}")

    for direction in ("dl", "ul"):
        pattern = re.compile(rf"(?m)^[ \t]*{direction}_carrierBandwidth[ \t]*=[ \t]*(\d+)[ \t]*;")
        values = pattern.findall(config)
        if values != ["106"]:
            raise PreflightError(
                f"runtime config must contain one {direction} 106 PRB carrier, observed {values}"
            )


def _verify_service_models(staged_sm_dir: Path) -> list[Path]:
    service_models = [staged_sm_dir / f"lib{name}_sm.so" for name in EXPECTED_SERVICE_MODELS]
    missing = [path for path in service_models if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise PreflightError(f"missing or empty service model libraries: {missing}")
    return service_models


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed git argv
        ("git", "-C", str(source), *args), capture_output=True, text=True, check=False
    )


def verify_patch_branch(source: Path) -> str:
    """Accept a clean branch descending from the pinned commit; return its HEAD."""

    head = _git(source, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise PreflightError(f"cannot resolve OAI HEAD: {head.stderr.strip()}")
    commit = head.stdout.strip()
    if _git(source, "merge-base", "--is-ancestor", EXPECTED_OAI_COMMIT, commit).returncode != 0:
        raise PreflightError(f"OAI HEAD {commit} does not descend from {EXPECTED_OAI_COMMIT}")
    dirty = _git(source, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise PreflightError("OAI patch branch has uncommitted changes to tracked files")
    return commit


def verify(
    source: Path,
    build_root: Path,
    runtime_config: Path,
    staged_sm_dir: Path,
    baseline_binary: Path,
    flexric_manifest_path: Path,
    allow_patches: bool = False,
) -> E2BuildManifest:
    """Verify every offline deployment input and seal the OAI build evidence."""

    base_commit: str | None = None
    if allow_patches:
        commit = verify_patch_branch(source)
        if commit != EXPECTED_OAI_COMMIT:
            base_commit = EXPECTED_OAI_COMMIT
    else:
        commit = read_git_head(source)
        if commit != EXPECTED_OAI_COMMIT:
            raise PreflightError(
                f"OAI commit mismatch: expected {EXPECTED_OAI_COMMIT}, observed {commit}"
            )
    submodule_initialized = (source / "openair2/E2AP/flexric/.git").exists()
    if not submodule_initialized:
        raise PreflightError("FlexRIC submodule is not initialized in the OAI source")

    cmake_build = build_root / "cmake_targets/ran_build/build"
    cache_path = cmake_build / "CMakeCache.txt"
    cache = _cache_values(cache_path)
    for variable, expected in (
        ("E2_AGENT", "ON"),
        ("E2AP_VERSION", EXPECTED_E2AP),
        ("KPM_VERSION", EXPECTED_KPM),
        ("OAI_USRP", "ON"),
    ):
        _require_cache(cache, variable, expected)

    binary = cmake_build / "nr-softmodem"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise PreflightError(f"missing executable nr-softmodem: {binary}")
    binary_hash = sha256(binary)
    baseline_hash = sha256(baseline_binary)
    if binary_hash == baseline_hash:
        raise PreflightError("E2 build binary is identical to the baseline E2-OFF binary")

    _verify_runtime_config(runtime_config)
    service_models = _verify_service_models(staged_sm_dir)
    manifest = E2BuildManifest(
        component="oai",
        commit=commit,
        e2ap=cache["E2AP_VERSION"],
        kpm=cache["KPM_VERSION"],
        sm_dir=EXPECTED_SM_DIR,
        binary_sha256=binary_hash,
        submodule_initialized=submodule_initialized,
        base_commit=base_commit,
    )
    try:
        flexric_manifest = E2BuildManifest.model_validate_json(
            flexric_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        raise PreflightError(f"invalid FlexRIC build manifest: {error}") from error
    compatibility = verify_compatible(manifest, flexric_manifest)
    if not compatibility.compatible:
        details = ", ".join(
            f"{item.field}={item.observed} (expected {item.expected})"
            for item in compatibility.mismatches
        )
        raise PreflightError(f"OAI/FlexRIC compatibility mismatch: {details}")

    manifest_path = build_root / "oai-e2-build-manifest.json"
    _atomic_json(manifest_path, manifest.model_dump(mode="json"))
    evidence = {
        "schema_version": 1,
        "baseline_e2_off_sha256": baseline_hash,
        "binary_e2_on_sha256": binary_hash,
        "runtime_config_sha256": sha256(runtime_config),
        "cmake_cache_sha256": sha256(cache_path),
        "flexric_manifest_sha256": sha256(flexric_manifest_path),
        "service_model_sha256": {path.name: sha256(path) for path in sorted(service_models)},
        "compatibility": compatibility.model_dump(mode="json"),
    }
    _atomic_json(build_root / "oai-e2-preflight.json", evidence)
    checksums = [
        f"{sha256(path)}  {path.relative_to(build_root).as_posix()}"
        for path in sorted((binary, cache_path, runtime_config))
    ]
    _atomic_text(build_root / "oai-e2-checksums.sha256", "\n".join(checksums) + "\n")
    return manifest


def _atomic_json(path: Path, document: object) -> None:
    _atomic_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--staged-sm-dir", type=Path, required=True)
    parser.add_argument("--baseline-binary", type=Path, required=True)
    parser.add_argument("--flexric-manifest", type=Path, required=True)
    parser.add_argument(
        "--allow-patches",
        action="store_true",
        help="accept a clean branch descending from the pinned OAI commit",
    )
    arguments = parser.parse_args()
    manifest = verify(
        source=arguments.source.resolve(),
        build_root=arguments.build_root.resolve(),
        runtime_config=arguments.runtime_config.resolve(),
        staged_sm_dir=arguments.staged_sm_dir.resolve(),
        baseline_binary=arguments.baseline_binary.resolve(),
        flexric_manifest_path=arguments.flexric_manifest.resolve(),
        allow_patches=arguments.allow_patches,
    )
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
