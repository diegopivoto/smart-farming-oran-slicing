#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_OAI_COMMIT="f8f769592a7030be88ede4bb5ca66fa1ca6a80e0"
readonly EXPECTED_FLEXRIC_COMMIT="d7a71285f3720fbc1487e9a86309eec5fb61a408"

usage() {
  printf '%s\n' \
    "Usage: $0 --oai PATH --build-dir PATH [--cc COMPILER] [--cxx COMPILER] [--prefix ABSOLUTE_PATH --install] [--jobs N]" \
    "" \
    "Builds pinned FlexRIC into BUILD_DIR and stages its /usr/local layout." \
    "Nothing is copied to PREFIX unless --install is explicitly supplied." \
    "The script never escalates privileges; PREFIX must already be writable."
}

oai_dir=""
build_dir=""
prefix=""
jobs="$(nproc)"
cc="gcc-13"
cxx="g++-13"
install_requested="false"

while (($# > 0)); do
  case "$1" in
    --oai)
      oai_dir="${2:-}"
      shift 2
      ;;
    --build-dir)
      build_dir="${2:-}"
      shift 2
      ;;
    --prefix)
      prefix="${2:-}"
      shift 2
      ;;
    --jobs)
      jobs="${2:-}"
      shift 2
      ;;
    --cc)
      cc="${2:-}"
      shift 2
      ;;
    --cxx)
      cxx="${2:-}"
      shift 2
      ;;
    --install)
      install_requested="true"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$oai_dir" || -z "$build_dir" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
  printf '%s\n' "--jobs must be a positive integer" >&2
  exit 2
fi
if ! command -v "$cc" >/dev/null 2>&1; then
  printf 'Compiler not found: %s\n' "$cc" >&2
  exit 2
fi
cc="$(command -v "$cc")"
if ! command -v "$cxx" >/dev/null 2>&1; then
  printf 'C++ compiler not found: %s\n' "$cxx" >&2
  exit 2
fi
cxx="$(command -v "$cxx")"

oai_dir="$(realpath "$oai_dir")"
mkdir -p "$build_dir"
build_dir="$(realpath "$build_dir")"
readonly project_root="$(realpath "$(dirname "${BASH_SOURCE[0]}")/../..")"
readonly project_python="$project_root/.venv/bin/python"
readonly flexric_source="$oai_dir/openair2/E2AP/flexric"
readonly stage_root="$build_dir/stage-root"
readonly kpm_xapp="$build_dir/examples/xApp/c/monitor/xapp_kpm_moni"

if [[ ! -x "$project_python" ]]; then
  printf 'Project Python environment not found: %s\n' "$project_python" >&2
  exit 1
fi

if [[ "$(git -C "$oai_dir" rev-parse HEAD)" != "$EXPECTED_OAI_COMMIT" ]]; then
  printf '%s\n' "OAI checkout does not match pinned commit $EXPECTED_OAI_COMMIT" >&2
  exit 1
fi

git -C "$oai_dir" submodule update --init -- openair2/E2AP/flexric
if [[ "$(git -C "$flexric_source" rev-parse HEAD)" != "$EXPECTED_FLEXRIC_COMMIT" ]]; then
  printf '%s\n' "FlexRIC submodule does not match pinned commit $EXPECTED_FLEXRIC_COMMIT" >&2
  exit 1
fi

cmake --fresh \
  -S "$flexric_source" \
  -B "$build_dir" \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_C_COMPILER="$cc" \
  -DCMAKE_CXX_COMPILER="$cxx" \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DE2AP_VERSION=E2AP_V2 \
  -DKPM_VERSION=KPM_V2_03 \
  -DUNIT_TEST=FALSE \
  -DNEAR_RIC_INSTALL=TRUE \
  -DXAPP_C_INSTALL=FALSE
cmake --build "$build_dir" --parallel "$jobs"
DESTDIR="$stage_root" cmake --install "$build_dir"

# This pinned revision's XAPP_C_INSTALL list names a removed target and cannot
# configure. Stage the KPM xApp target that this project actually uses without
# patching the provenance-controlled upstream source.
cmake -E make_directory "$stage_root/usr/local/bin"
cmake -E copy "$kpm_xapp" "$stage_root/usr/local/bin/xapp_kpm_moni"
cmake -E make_directory "$stage_root/usr/local/lib/flexric"
cmake -E copy \
  "$build_dir/src/xApp/libe42_xapp.a" \
  "$stage_root/usr/local/lib/flexric/libe42_xapp.a"
cmake -E copy \
  "$build_dir/src/xApp/libe42_xapp_shared.so" \
  "$stage_root/usr/local/lib/flexric/libe42_xapp_shared.so"
cmake -E make_directory "$stage_root/usr/local/include/flexric"
cmake -E copy \
  "$flexric_source/src/xApp/e42_xapp_api.h" \
  "$stage_root/usr/local/include/flexric/e42_xapp_api.h"
cmake -E copy \
  "$project_root/deployment/flexric/flexric.conf" \
  "$stage_root/usr/local/etc/flexric/flexric.conf"

"$project_python" "$project_root/deployment/flexric/verify_install.py" \
  --source "$flexric_source" \
  --build-dir "$build_dir" \
  --stage-root "$stage_root"

if [[ "$install_requested" == "true" ]]; then
  if [[ -z "$prefix" || "$prefix" != /* || "$prefix" == "/" ]]; then
    printf '%s\n' "--install requires a non-root absolute --prefix" >&2
    exit 2
  fi
  cmake --install "$build_dir" --prefix "$prefix"
  cmake -E make_directory "$prefix/bin"
  cmake -E copy "$kpm_xapp" "$prefix/bin/xapp_kpm_moni"
  cmake -E make_directory "$prefix/lib/flexric"
  cmake -E copy "$build_dir/src/xApp/libe42_xapp.a" "$prefix/lib/flexric/libe42_xapp.a"
  cmake -E copy \
    "$build_dir/src/xApp/libe42_xapp_shared.so" \
    "$prefix/lib/flexric/libe42_xapp_shared.so"
  cmake -E make_directory "$prefix/include/flexric"
  cmake -E copy \
    "$flexric_source/src/xApp/e42_xapp_api.h" \
    "$prefix/include/flexric/e42_xapp_api.h"
  cmake -E copy \
    "$project_root/deployment/flexric/flexric.conf" \
    "$prefix/etc/flexric/flexric.conf"
fi
