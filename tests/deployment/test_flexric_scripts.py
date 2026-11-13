import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


BUILD_SCRIPT = Path("deployment/flexric/build_flexric.sh")
VERIFY_SCRIPT = Path("deployment/flexric/verify_install.py")
PINNED_FLEXRIC = "d7a71285f3720fbc1487e9a86309eec5fb61a408"


def _load_verifier() -> ModuleType:
    specification = importlib.util.spec_from_file_location("flexric_verifier", VERIFY_SCRIPT)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_artifact_labels_are_relative_to_the_evidence_root(tmp_path: Path) -> None:
    verifier = _load_verifier()
    build = tmp_path / "build"

    assert verifier._artifact_label(build / "src/lib.a", build) == "src/lib.a"
    with pytest.raises(verifier.VerificationError, match="outside build root"):
        verifier._artifact_label(tmp_path / "untracked/file", build)


def test_build_script_is_fail_fast_pinned_and_never_rewrites_git_history() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert PINNED_FLEXRIC in script
    assert 'git -C "$oai_dir" submodule update --init -- openair2/E2AP/flexric' in script
    assert "-DE2AP_VERSION=E2AP_V2" in script
    assert "-DKPM_VERSION=KPM_V2_03" in script
    assert "-DUNIT_TEST=FALSE" in script
    assert '-DCMAKE_C_COMPILER="$cc"' in script
    assert '-DCMAKE_CXX_COMPILER="$cxx"' in script
    assert "-DNEAR_RIC_INSTALL=TRUE" in script
    assert "-DXAPP_C_INSTALL=FALSE" in script
    assert 'examples/xApp/c/monitor/xapp_kpm_moni"' in script
    assert 'stage_root/usr/local/bin/xapp_kpm_moni"' in script
    assert 'stage_root/usr/local/lib/flexric/libe42_xapp.a"' in script
    assert 'stage_root/usr/local/lib/flexric/libe42_xapp_shared.so"' in script
    assert 'stage_root/usr/local/include/flexric/e42_xapp_api.h"' in script
    assert 'stage_root/usr/local/etc/flexric/flexric.conf"' in script
    assert "-DCMAKE_INSTALL_LIBDIR=lib" in script
    assert "cmake --fresh" in script
    assert "git checkout" not in script
    assert "git reset" not in script
    assert "sudo" not in script
    assert 'project_python="$project_root/.venv/bin/python"' in script
    assert '"$project_python" "$project_root/deployment/flexric/verify_install.py"' in script


def test_build_script_requires_explicit_source_and_build_directory() -> None:
    help_result = subprocess.run(
        [str(BUILD_SCRIPT), "--help"], capture_output=True, check=False, text=True
    )
    missing_result = subprocess.run(
        [str(BUILD_SCRIPT)], capture_output=True, check=False, text=True
    )

    assert help_result.returncode == 0
    assert "--oai" in help_result.stdout
    assert "--build-dir" in help_result.stdout
    assert "--prefix" in help_result.stdout
    assert "--cc" in help_result.stdout
    assert "--install" in help_result.stdout
    assert missing_result.returncode != 0


def test_install_is_opt_in_and_requires_an_explicit_absolute_prefix() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'install_requested="false"' in script
    assert 'if [[ "$install_requested" == "true" ]]' in script
    assert 'if [[ -z "$prefix" || "$prefix" != /* || "$prefix" == "/" ]]' in script
    assert 'cmake --install "$build_dir" --prefix "$prefix"' in script


def test_verifier_is_a_non_mutating_python_command() -> None:
    script = VERIFY_SCRIPT.read_text(encoding="utf-8")

    assert "--source" in script
    assert "--build-dir" in script
    assert "--stage-root" in script
    assert "EXPECTED_FLEXRIC_COMMIT" in script
    assert "subprocess.run" not in script
    assert 'staged_bin_dir / "nearRT-RIC"' in script
    assert 'staged_bin_dir / "xapp_kpm_moni"' in script


def test_flexric_config_binds_only_the_management_address() -> None:
    config = Path("deployment/flexric/flexric.conf").read_text(encoding="utf-8")

    assert "[NEAR-RIC]" in config
    assert "NEAR_RIC_IP = 192.168.56.101" in config
    assert "[XAPP]" in config
    assert "DB_DIR = /var/lib/smartfarm-flexric/" in config
    assert "e2_port" not in config
