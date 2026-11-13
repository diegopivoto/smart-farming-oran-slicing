#!/usr/bin/env bash
set -euo pipefail

readonly EXPECTED_OAI_COMMIT="f8f769592a7030be88ede4bb5ca66fa1ca6a80e0"
readonly EXPECTED_FLEXRIC_COMMIT="d7a71285f3720fbc1487e9a86309eec5fb61a408"

usage() {
  printf '%s\n' \
    "Usage: $0 --source PATH --build-dir PATH --active-config FILE --baseline-binary FILE --flexric-build-dir PATH [--allow-patches] [--cc COMPILER] [--cxx COMPILER] [--jobs N]" \
    "" \
    "Builds an E2-enabled OAI nr-softmodem in an isolated output directory." \
    "It does not install, start, stop, or replace any live component."
}

source_dir=""
build_root=""
active_config=""
baseline_binary=""
flexric_build_dir=""
cc="gcc"
cxx="g++"
jobs="$(nproc)"
allow_patches=0

while (($# > 0)); do
  case "$1" in
    --source)
      source_dir="${2:-}"
      shift 2
      ;;
    --build-dir)
      build_root="${2:-}"
      shift 2
      ;;
    --active-config)
      active_config="${2:-}"
      shift 2
      ;;
    --baseline-binary)
      baseline_binary="${2:-}"
      shift 2
      ;;
    --flexric-build-dir)
      flexric_build_dir="${2:-}"
      shift 2
      ;;
    --allow-patches)
      allow_patches=1
      shift
      ;;
    --cc)
      cc="${2:-}"
      shift 2
      ;;
    --cxx)
      cxx="${2:-}"
      shift 2
      ;;
    --jobs)
      jobs="${2:-}"
      shift 2
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

if [[ -z "$source_dir" || -z "$build_root" || -z "$active_config" || \
      -z "$baseline_binary" || -z "$flexric_build_dir" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
  printf '%s\n' "--jobs must be a positive integer" >&2
  exit 2
fi
for compiler in "$cc" "$cxx"; do
  if ! command -v "$compiler" >/dev/null 2>&1; then
    printf 'Compiler not found: %s\n' "$compiler" >&2
    exit 2
  fi
done
cc="$(command -v "$cc")"
cxx="$(command -v "$cxx")"

source_dir="$(realpath "$source_dir")"
active_config="$(realpath "$active_config")"
baseline_binary="$(realpath "$baseline_binary")"
flexric_build_dir="$(realpath "$flexric_build_dir")"
mkdir -p "$build_root"
build_root="$(realpath "$build_root")"

readonly project_root="$(realpath "$(dirname "${BASH_SOURCE[0]}")/../..")"
readonly project_python="$project_root/.venv/bin/python"
readonly cmake_build="$build_root/cmake_targets/ran_build/build"
readonly runtime_config="$build_root/runtime/$(basename "$active_config")"
readonly staged_sm_dir="$flexric_build_dir/stage-root/usr/local/lib/flexric"
readonly flexric_manifest="$flexric_build_dir/flexric-build-manifest.json"
readonly flexric_source="$source_dir/openair2/E2AP/flexric"

if [[ ! -x "$project_python" ]]; then
  printf 'Project Python environment not found: %s\n' "$project_python" >&2
  exit 1
fi
if [[ "$allow_patches" -eq 1 ]]; then
  # Smart Farming patch branches must descend from the pinned commit and be clean.
  if ! git -C "$source_dir" merge-base --is-ancestor "$EXPECTED_OAI_COMMIT" HEAD; then
    printf '%s\n' "OAI HEAD does not descend from pinned commit $EXPECTED_OAI_COMMIT" >&2
    exit 1
  fi
  if [[ -n "$(git -C "$source_dir" status --porcelain --untracked-files=no)" ]]; then
    printf '%s\n' "OAI patch branch has uncommitted changes to tracked files" >&2
    exit 1
  fi
elif [[ "$(git -C "$source_dir" rev-parse HEAD)" != "$EXPECTED_OAI_COMMIT" ]]; then
  printf '%s\n' "OAI checkout does not match pinned commit $EXPECTED_OAI_COMMIT" >&2
  exit 1
fi
git -C "$source_dir" submodule update --init -- openair2/E2AP/flexric
if [[ "$(git -C "$flexric_source" rev-parse HEAD)" != "$EXPECTED_FLEXRIC_COMMIT" ]]; then
  printf '%s\n' "FlexRIC submodule does not match pinned commit $EXPECTED_FLEXRIC_COMMIT" >&2
  exit 1
fi

cmake --fresh \
  -S "$source_dir" \
  -B "$cmake_build" \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_C_COMPILER="$cc" \
  -DCMAKE_CXX_COMPILER="$cxx" \
  -DE2_AGENT=ON \
  -DE2AP_VERSION=E2AP_V2 \
  -DKPM_VERSION=KPM_V2_03 \
  -DOAI_USRP=ON

cmake --build "$cmake_build" --parallel "$jobs" --target \
  nr-softmodem \
  oai_usrpdevif \
  params_libconfig \
  coding \
  rfsimulator \
  dfts \
  params_yaml \
  vrtsim \
  rf_emulator

"$project_python" "$project_root/deployment/oai/render_oai_e2_config.py" \
  --source "$active_config" \
  --output "$runtime_config" \
  --near-ric-ip 192.168.56.101

preflight_args=()
if [[ "$allow_patches" -eq 1 ]]; then
  preflight_args+=(--allow-patches)
fi
"$project_python" "$project_root/deployment/oai/preflight_oai_e2.py" \
  --source "$source_dir" \
  --build-root "$build_root" \
  --runtime-config "$runtime_config" \
  --staged-sm-dir "$staged_sm_dir" \
  --baseline-binary "$baseline_binary" \
  --flexric-manifest "$flexric_manifest" \
  "${preflight_args[@]}"
