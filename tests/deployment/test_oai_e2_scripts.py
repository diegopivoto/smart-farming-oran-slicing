import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from smartfarm.e2.compatibility import (
    EXPECTED_E2AP,
    EXPECTED_FLEXRIC_COMMIT,
    EXPECTED_KPM,
    EXPECTED_OAI_COMMIT,
    EXPECTED_SM_DIR,
    E2BuildManifest,
)


BUILD_SCRIPT = Path("deployment/oai/build_oai_e2.sh")
RENDER_SCRIPT = Path("deployment/oai/render_oai_e2_config.py")
PREFLIGHT_SCRIPT = Path("deployment/oai/preflight_oai_e2.py")


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _valid_tree(tmp_path: Path) -> dict[str, Any]:
    source = tmp_path / "oai"
    git_dir = source / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(EXPECTED_OAI_COMMIT + "\n", encoding="utf-8")
    flexric_git = source / "openair2/E2AP/flexric/.git"
    flexric_git.parent.mkdir(parents=True)
    flexric_git.write_text("gitdir: unused-for-presence-check\n", encoding="utf-8")

    build_root = tmp_path / "oai-e2"
    cmake_build = build_root / "cmake_targets/ran_build/build"
    cmake_build.mkdir(parents=True)
    (cmake_build / "CMakeCache.txt").write_text(
        "\n".join(
            (
                "E2_AGENT:STRING=ON",
                f"E2AP_VERSION:STRING={EXPECTED_E2AP}",
                f"KPM_VERSION:STRING={EXPECTED_KPM}",
                "OAI_USRP:BOOL=ON",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    binary = cmake_build / "nr-softmodem"
    binary.write_bytes(b"e2-enabled-binary")
    binary.chmod(0o755)

    runtime_config = build_root / "runtime/gnb.conf"
    runtime_config.parent.mkdir(parents=True)
    runtime_config.write_text(
        """gNBs = ({
  servingCellConfigCommon = ({
    dl_carrierBandwidth = 106;
    ul_carrierBandwidth = 106;
  });
});
e2_agent = {
  near_ric_ip_addr = "192.168.56.101";
  # The active lab config follows OAI's shipped syntax without a semicolon here.
  sm_dir = "/usr/local/lib/flexric/"
};
""",
        encoding="utf-8",
    )

    staged_sm_dir = tmp_path / "flexric-stage/usr/local/lib/flexric"
    staged_sm_dir.mkdir(parents=True)
    for name in ("gtp", "kpm", "mac", "pdcp", "rc", "rlc", "slice", "tc"):
        (staged_sm_dir / f"lib{name}_sm.so").write_bytes(name.encode())

    baseline_binary = tmp_path / "baseline-nr-softmodem"
    baseline_binary.write_bytes(b"e2-disabled-binary")
    flexric_manifest = tmp_path / "flexric-build-manifest.json"
    flexric_manifest.write_text(
        E2BuildManifest(
            component="flexric",
            commit=EXPECTED_FLEXRIC_COMMIT,
            e2ap=EXPECTED_E2AP,
            kpm=EXPECTED_KPM,
            sm_dir=EXPECTED_SM_DIR,
            binary_sha256="b" * 64,
            submodule_initialized=True,
        ).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )
    return {
        "source": source,
        "build_root": build_root,
        "runtime_config": runtime_config,
        "staged_sm_dir": staged_sm_dir,
        "baseline_binary": baseline_binary,
        "flexric_manifest_path": flexric_manifest,
    }


def test_renderer_changes_only_the_e2_management_address(tmp_path: Path) -> None:
    renderer = _load_module("oai_renderer", RENDER_SCRIPT)
    source = tmp_path / "active.conf"
    output = tmp_path / "runtime.conf"
    original = """dl_carrierBandwidth = 106;
e2_agent = {
  near_ric_ip_addr = "127.0.0.1";
  sm_dir = "/usr/local/lib/flexric/";
};
"""
    source.write_text(original, encoding="utf-8")

    renderer.render(source, output, "192.168.56.101")

    assert source.read_text(encoding="utf-8") == original
    assert output.read_text(encoding="utf-8") == original.replace(
        'near_ric_ip_addr = "127.0.0.1"',
        'near_ric_ip_addr = "192.168.56.101"',
    )


@pytest.mark.parametrize(
    ("variable", "observed"),
    (
        ("E2_AGENT", "OFF"),
        ("E2AP_VERSION", "E2AP_V3"),
        ("KPM_VERSION", "KPM_V3_00"),
        ("OAI_USRP", "OFF"),
    ),
)
def test_preflight_rejects_incompatible_cmake_cache(
    tmp_path: Path, variable: str, observed: str
) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    cache_path = tree["build_root"] / "cmake_targets/ran_build/build/CMakeCache.txt"
    cache = cache_path.read_text(encoding="utf-8")
    cache_path.write_text(
        cache.replace(f"{variable}:STRING=ON", f"{variable}:STRING={observed}")
        .replace(f"{variable}:BOOL=ON", f"{variable}:BOOL={observed}")
        .replace(f"{variable}:STRING={EXPECTED_E2AP}", f"{variable}:STRING={observed}")
        .replace(f"{variable}:STRING={EXPECTED_KPM}", f"{variable}:STRING={observed}"),
        encoding="utf-8",
    )

    with pytest.raises(preflight.PreflightError, match=variable):
        preflight.verify(**tree)


@pytest.mark.parametrize("field", ("near_ric_ip_addr", "sm_dir"))
def test_preflight_rejects_missing_e2_config_field(tmp_path: Path, field: str) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    config_path = tree["runtime_config"]
    config = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        "\n".join(line for line in config.splitlines() if field not in line) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(preflight.PreflightError, match=field):
        preflight.verify(**tree)


def test_preflight_rejects_empty_service_model_directory(tmp_path: Path) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    for service_model in tree["staged_sm_dir"].iterdir():
        service_model.unlink()

    with pytest.raises(preflight.PreflightError, match="service model"):
        preflight.verify(**tree)


def test_preflight_rejects_binary_equal_to_e2_off_baseline(tmp_path: Path) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    binary = tree["build_root"] / "cmake_targets/ran_build/build/nr-softmodem"
    tree["baseline_binary"].write_bytes(binary.read_bytes())

    with pytest.raises(preflight.PreflightError, match="baseline E2-OFF"):
        preflight.verify(**tree)


def test_preflight_rejects_non_106_prb_runtime_config(tmp_path: Path) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    config_path = tree["runtime_config"]
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "dl_carrierBandwidth = 106", "dl_carrierBandwidth = 52"
        ),
        encoding="utf-8",
    )

    with pytest.raises(preflight.PreflightError, match="106 PRB"):
        preflight.verify(**tree)


def test_preflight_rejects_flexric_wire_version_mismatch(tmp_path: Path) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)
    manifest_path = tree["flexric_manifest_path"]
    flexric_manifest = E2BuildManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    ).model_copy(update={"e2ap": "E2AP_V3"})
    manifest_path.write_text(flexric_manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

    with pytest.raises(preflight.PreflightError, match="compatibility mismatch"):
        preflight.verify(**tree)


def test_preflight_emits_compatible_manifest_and_checksums(tmp_path: Path) -> None:
    preflight = _load_module("oai_preflight", PREFLIGHT_SCRIPT)
    tree = _valid_tree(tmp_path)

    manifest = preflight.verify(**tree)

    assert manifest.component == "oai"
    assert manifest.commit == EXPECTED_OAI_COMMIT
    assert manifest.e2ap == EXPECTED_E2AP
    assert manifest.kpm == EXPECTED_KPM
    assert manifest.sm_dir == EXPECTED_SM_DIR
    assert (tree["build_root"] / "oai-e2-build-manifest.json").is_file()
    assert (tree["build_root"] / "oai-e2-checksums.sha256").is_file()


def test_build_script_is_isolated_pinned_and_non_mutating() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert EXPECTED_OAI_COMMIT in script
    assert "cmake --fresh" in script
    assert "-DE2_AGENT=ON" in script
    assert "-DE2AP_VERSION=E2AP_V2" in script
    assert "-DKPM_VERSION=KPM_V2_03" in script
    assert "-DOAI_USRP=ON" in script
    assert "nr-softmodem" in script
    assert "oai_usrpdevif" in script
    assert "render_oai_e2_config.py" in script
    assert "preflight_oai_e2.py" in script
    assert "git checkout" not in script
    assert "git reset" not in script
    assert "sudo" not in script
    assert "systemctl" not in script


def _git(repo: Path, *args: str) -> str:
    import subprocess

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    return subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def test_patch_branch_must_descend_from_pin_and_be_clean(tmp_path: Path) -> None:
    preflight = _load_module("preflight_patch", PREFLIGHT_SCRIPT)
    repo = tmp_path / "oai"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.c").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.c")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.c").write_text("patched\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "patch")
    head = _git(repo, "rev-parse", "HEAD")

    preflight.EXPECTED_OAI_COMMIT = base
    assert preflight.verify_patch_branch(repo) == head

    (repo / "a.c").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="uncommitted"):
        preflight.verify_patch_branch(repo)

    _git(repo, "checkout", "-q", "a.c")
    preflight.EXPECTED_OAI_COMMIT = "f" * 40
    with pytest.raises(preflight.PreflightError, match="does not descend"):
        preflight.verify_patch_branch(repo)
