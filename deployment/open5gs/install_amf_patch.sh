#!/bin/bash
# Installs the Smart Farming Open5GS AMF (0001-amf-select-slice-by-dnn.patch on Open5GS v2.7.5) next to the
# apt-installed Open5GS 2.7.5, with one command. Idempotent: every step is skipped when it is already done.
#
# Not needed with the oran-tvws-5g packages: when Open5GS was installed from the .debs of the
# github.com/diegopivoto/open5gs release (version 2.7.5~noble+oran.tvws.5g<N>), /usr/bin/open5gs-amfd already
# carries the patch; the script detects it (package version or the "Smart Farming: DNN" string in the binary),
# reports "patched AMF from the oran-tvws-5g packages: nothing to do" and changes nothing.
#
#   deployment/open5gs/install_amf_patch.sh --dry-run     # reports what is missing or differs, changes nothing
#   deployment/open5gs/install_amf_patch.sh               # clone + patch + build + install + drop-in, NO restart
#   deployment/open5gs/install_amf_patch.sh --restart     # same, then restarts open5gs-amfd
#   deployment/open5gs/install_amf_patch.sh --rollback    # back to the apt AMF (removes the drop-in)
#
# Steps:
#   1. the apt Open5GS must be 2.7.5 (dpkg + open5gs-amfd -v); if it is the patched oran-tvws-5g build, stop here
#      (nothing to do); the build dependencies must be installed (the apt command is printed; they are installed
#      only with --install-deps)
#   2. source: when --src (default ~/src/open5gs-2.7.5-sf) is absent, git clone of github.com/diegopivoto/open5gs
#      branch oran-tvws-5g (private; v2.7.5 with the patch committed); if that clone fails (no access), or with
#      --from-upstream, the official open5gs tag v2.7.5. The patch is applied with git apply unless it is already
#      applied
#   3. meson setup build --prefix=/opt/open5gs-sf; ninja builds only the AMF (src/amf/open5gs-amfd)
#   4. installs the AMF and the Open5GS libraries it links from the build tree into /opt/open5gs-sf/{bin,lib}, as
#      install to *.new + mv -f (a plain cp onto the running binary fails with "Text file busy")
#   5. writes the systemd drop-in (LD_LIBRARY_PATH + ExecStart override) and runs systemctl daemon-reload
#   6. verifies: systemctl cat shows the override, ldd resolves every library, the binary prints its version, and
#      tells whether the running AMF is already the installed one
#
# Run it as the normal user: it builds in --src and uses sudo only for /opt/open5gs-sf, systemd and apt.
# The AMF is not restarted by default: a restart drops every N2 association. The OAI gNB re-associates by itself;
# the N3IWF on PC2 does not and must be restarted, followed by the N3IWUE on PC3.
set -euo pipefail

VERSION=2.7.5
REPO_URL=https://github.com/diegopivoto/open5gs.git      # private; branch $BRANCH = v$VERSION + the patch
BRANCH=oran-tvws-5g
UPSTREAM_URL=https://github.com/open5gs/open5gs.git     # fallback: tag v$VERSION + git apply of the patch
PKG_SUFFIX=+oran.tvws.5g                                # Debian version suffix of the patched packages
PATCH_MARK='Smart Farming: DNN'                         # log string the patch adds to the AMF
PREFIX=/opt/open5gs-sf
UNIT=open5gs-amfd
DROPIN=/etc/systemd/system/$UNIT.service.d/smartfarm-dnn-slice.conf
TARGET=src/amf/open5gs-amfd
PATCH="$(cd "$(dirname "$0")" && pwd)/0001-amf-select-slice-by-dnn.patch"
# Ubuntu names of the Open5GS Build-Depends (debian/control of v2.7.5); "a|b" accepts either package.
DEPS=(git pkg-config meson ninja-build build-essential cmake flex bison libgnutls28-dev libgcrypt20-dev libssl-dev
      "libidn-dev|libidn11-dev" libmongoc-dev libbson-dev libsctp-dev libyaml-dev libmicrohttpd-dev
      libcurl4-gnutls-dev libnghttp2-dev libtins-dev libtalloc-dev)

SRC=$HOME/src/open5gs-$VERSION-sf
DRY_RUN=0
RESTART=0
ROLLBACK=0
INSTALL_DEPS=0
FROM_UPSTREAM=0

usage() {
    sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"
    cat <<EOF
Options:
  --src DIR        Open5GS source tree (default: $SRC); when absent, cloned from $REPO_URL
                   branch $BRANCH (patch committed), or from $UPSTREAM_URL v$VERSION if that fails
  --from-upstream  clone the official $UPSTREAM_URL v$VERSION (+ git apply of the patch) directly
  --dry-run        print what would be done; only read-only checks run
  --no-restart     do not restart $UNIT (default); the restart command is printed
  --restart        restart $UNIT at the end (then restart the N3IWF on PC2 and the N3IWUE on PC3)
  --rollback       remove the drop-in and daemon-reload ($PREFIX and the source are kept)
  --install-deps   apt-get install the missing build dependencies (default: print the command and stop)
  -h, --help       this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --src) [ $# -ge 2 ] || { echo "--src needs a directory" >&2; exit 2; }; SRC=$2; shift ;;
        --src=*) SRC=${1#--src=} ;;
        --dry-run) DRY_RUN=1 ;;
        --no-restart) RESTART=0 ;;
        --restart) RESTART=1 ;;
        --rollback) ROLLBACK=1 ;;
        --install-deps) INSTALL_DEPS=1 ;;
        --from-upstream) FROM_UPSTREAM=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
case "$SRC" in /*) ;; *) SRC=$PWD/$SRC ;; esac
BUILD=$SRC/build
BUILT=$BUILD/$TARGET
BIN=$PREFIX/bin/open5gs-amfd

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
    if [ -n "${SUDO_USER:-}" ] && [ "$DRY_RUN" = 0 ] && [ "$ROLLBACK" = 0 ]; then
        echo "run it as your normal user (not under sudo): it builds in --src and calls sudo itself" >&2
        exit 2
    fi
elif [ -n "${SUDO_ASKPASS:-}" ]; then
    SUDO=(sudo -A)
else
    SUDO=(sudo)
fi

CHANGES=()
say() { echo "[amf-sf] $*"; }
ok() { echo "[amf-sf]   OK     $*"; }
warn() { echo "[amf-sf]   WARN   $*" >&2; }
die() { echo "[amf-sf] ERROR: $*" >&2; exit 1; }
todo() {  # todo <description>: records one change (done now, or that --dry-run would do)
    CHANGES+=("$*")
    if [ "$DRY_RUN" = 1 ]; then echo "[amf-sf]   WOULD  $*"; else echo "[amf-sf]   DO     $*"; fi
}
run() {  # run <command...>: runs it, or only prints it under --dry-run
    if [ "$DRY_RUN" = 1 ]; then
        printf '[amf-sf]          $'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}
as_root() { run "${SUDO[@]}" "$@"; }

dropin_content() {
    cat <<EOF
[Service]
# Smart Farming lab: AMF $VERSION + slice-by-DNN patch (smart-farming-oran/deployment/open5gs/0001-amf-select-slice-by-dnn.patch)
# Rollback: rm this file; systemctl daemon-reload; systemctl restart $UNIT
Environment=LD_LIBRARY_PATH=$PREFIX/lib
ExecStart=
ExecStart=$BIN -c /etc/open5gs/amf.yaml
EOF
}

pkg_installed() { dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null | grep -q '^ii'; }

# --- 1. installed Open5GS and build dependencies ----------------------------------------------------------------

check_open5gs_version() {
    say "1. installed Open5GS"
    local pkg_ver bin_ver
    pkg_ver=$(dpkg-query -W -f='${Version}' open5gs-amf 2>/dev/null || true)
    [ -n "$pkg_ver" ] || die "open5gs-amf is not installed: install Open5GS $VERSION from apt first"
    [[ "$pkg_ver" =~ ^${VERSION//./\\.}([~+-]|$) ]] ||
        die "open5gs-amf is $pkg_ver, the patch targets $VERSION (other NFs would run a different release)"
    bin_ver=$(/usr/bin/open5gs-amfd -v 2>&1 | head -n 3 || true)
    grep -q "v${VERSION//./\\.}\$" <<<"$bin_ver" || die "/usr/bin/open5gs-amfd -v says '$bin_ver', expected v$VERSION"
    ok "open5gs-amf $pkg_ver, /usr/bin/open5gs-amfd v$VERSION"
    if ! apt-mark showhold 2>/dev/null | grep -qx 'open5gs.*'; then
        local cand
        cand=$(apt-cache policy open5gs-amf 2>/dev/null | awk '/Candidate:|Candidato:/ {print $2; exit}')
        if [ -n "$cand" ] && [ "$cand" != "$pkg_ver" ]; then
            warn "apt would upgrade Open5GS to $cand; keep $VERSION with:"
            warn "  sudo apt-mark hold \$(dpkg-query -W -f='\${Package}\\n' 'open5gs*')"
        fi
    fi
}

# Open5GS installed from the oran-tvws-5g packages (github.com/diegopivoto/open5gs release): the packaged
# /usr/bin/open5gs-amfd is already the patched AMF and the /opt override is not needed.
packaged_patched_amf() {
    local pkg_ver
    pkg_ver=$(dpkg-query -W -f='${Version}' open5gs-amf 2>/dev/null || true)
    if [[ "$pkg_ver" == *"$PKG_SUFFIX"* ]]; then
        ok "open5gs-amf $pkg_ver is the patched oran-tvws-5g build"
    elif grep -aqF "$PATCH_MARK" /usr/bin/open5gs-amfd 2>/dev/null; then
        ok "/usr/bin/open5gs-amfd contains '$PATCH_MARK' (patched AMF, package $pkg_ver)"
    else
        return 1
    fi
}

report_packaged_amf() {
    local pid args
    if [ -f "$DROPIN" ]; then
        warn "$DROPIN still runs $BIN instead of the packaged patched AMF; it is redundant now:"
        warn "  $0 --rollback --restart   (removes the drop-in, then restart the N3IWF on PC2 and the N3IWUE on PC3)"
    else
        ok "no /opt drop-in: $UNIT runs /usr/bin/open5gs-amfd"
    fi
    pid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)
    if [ "${pid:-0}" != 0 ]; then
        args=$(ps -o args= -p "$pid" 2>/dev/null || true)
        say "   running AMF (pid $pid): ${args%% *}"
    fi
    say "patched AMF from the oran-tvws-5g packages: nothing to do"
}

DEPS_MISSING=()
check_build_deps() {
    say "   build dependencies"
    local dep alt found
    for dep in "${DEPS[@]}"; do
        found=0
        IFS='|' read -ra alts <<<"$dep"
        for alt in "${alts[@]}"; do pkg_installed "$alt" && found=1; done
        [ "$found" = 1 ] || DEPS_MISSING+=("${alts[0]}")
    done
    if [ "${#DEPS_MISSING[@]}" -eq 0 ]; then
        ok "all ${#DEPS[@]} build dependencies installed"
    elif [ "$INSTALL_DEPS" = 1 ]; then
        todo "apt-get install ${DEPS_MISSING[*]}"
        as_root apt-get install -y "${DEPS_MISSING[@]}"
        [ "$DRY_RUN" = 1 ] || DEPS_MISSING=()
    else
        warn "missing build dependencies: ${DEPS_MISSING[*]}"
        warn "install them with: sudo apt-get install -y ${DEPS_MISSING[*]}   (or rerun with --install-deps)"
    fi
}

need_deps() {  # called before anything that compiles
    [ "${#DEPS_MISSING[@]}" -eq 0 ] || [ "$DRY_RUN" = 1 ] ||
        die "build dependencies missing (${DEPS_MISSING[*]}): see the apt command above, or use --install-deps"
}

# --- 2. source + patch ------------------------------------------------------------------------------------------

clone_source() {  # clones into the absent $SRC: our branch first, the official tag as fallback
    if [ "$FROM_UPSTREAM" = 0 ]; then
        todo "clone $REPO_URL branch $BRANCH into $SRC (v$VERSION + patch)"
        if [ "$DRY_RUN" = 1 ]; then
            run git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC"
            say "   (if that clone fails: $UPSTREAM_URL tag v$VERSION + git apply of the patch)"
            return
        fi
        if git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC"; then
            return
        fi
        warn "could not clone $REPO_URL (private repository: needs GitHub access)"
        warn "falling back to $UPSTREAM_URL tag v$VERSION + $(basename "$PATCH")"
        rm -rf "$SRC"
        FROM_UPSTREAM=1
    fi
    todo "clone $UPSTREAM_URL tag v$VERSION into $SRC"
    run git -c advice.detachedHead=false clone --depth 1 --branch "v$VERSION" "$UPSTREAM_URL" "$SRC"
}

HAVE_SRC=1
get_source() {
    say "2. source $SRC"
    [ -f "$PATCH" ] || die "patch not found: $PATCH"
    if [ ! -e "$SRC" ]; then
        command -v git >/dev/null || die "git is required to clone the source"
        run mkdir -p "$(dirname "$SRC")"
        clone_source
        [ "$DRY_RUN" = 0 ] || HAVE_SRC=0
    fi
    if [ "$HAVE_SRC" = 1 ]; then
        [ -f "$SRC/meson.build" ] || die "$SRC exists but is not an Open5GS source tree"
        grep -Eq "^[[:space:]]*version[[:space:]]*:[[:space:]]*'${VERSION//./\\.}'" "$SRC/meson.build" ||
            die "$SRC is not Open5GS $VERSION (see the version in its meson.build)"
        ok "Open5GS $VERSION source tree"
    fi

    if [ "$HAVE_SRC" = 0 ]; then
        if [ "$FROM_UPSTREAM" = 1 ]; then
            todo "apply $(basename "$PATCH")"
            run git -C "$SRC" apply "$PATCH"
        else
            say "   (the patch is committed in $BRANCH; git apply runs only after the upstream fallback)"
        fi
    elif git -C "$SRC" apply --check --reverse "$PATCH" 2>/dev/null; then
        ok "patch already applied"
    elif git -C "$SRC" apply --check "$PATCH" 2>/dev/null; then
        todo "apply $(basename "$PATCH")"
        run git -C "$SRC" apply "$PATCH"
    else
        die "$(basename "$PATCH") neither applies to nor is applied in $SRC: the tree carries other changes (an
older version of the patch?). Inspect 'git -C $SRC diff'; to start over from the release files:
'git -C $SRC checkout -- src/amf' (git tree) and rerun this script."
    fi
}

# --- 3. build ---------------------------------------------------------------------------------------------------

meson_prefix() {
    python3 - "$BUILD/meson-info/intro-buildoptions.json" <<'EOF' 2>/dev/null
import json, sys
print(next(o["value"] for o in json.load(open(sys.argv[1])) if o["name"] == "prefix"))
EOF
}

build_amf() {
    say "3. build ($BUILD, AMF only)"
    local prefix out
    if [ "$HAVE_SRC" = 0 ] || [ ! -f "$BUILD/build.ninja" ]; then
        need_deps
        todo "meson setup $BUILD --prefix=$PREFIX (downloads the meson subprojects)"
        run meson setup --prefix="$PREFIX" "$BUILD" "$SRC"
    else
        prefix=$(meson_prefix || true)
        if [ "$prefix" = "$PREFIX" ]; then
            ok "build dir configured with prefix $PREFIX"
        else
            need_deps
            todo "meson configure prefix ${prefix:-?} -> $PREFIX"
            run meson configure -Dprefix="$PREFIX" "$BUILD"
        fi
    fi
    if [ "$DRY_RUN" = 1 ] && [ ! -f "$BUILD/build.ninja" ]; then
        todo "ninja -C $BUILD $TARGET"
        return
    fi
    out=$(ninja -C "$BUILD" -n "$TARGET" 2>&1 || true)
    if grep -q 'no work to do' <<<"$out"; then
        ok "AMF build up to date ($BUILT)"
    else
        need_deps
        todo "ninja -C $BUILD $TARGET"
        run ninja -C "$BUILD" "$TARGET"
    fi
}

# --- 4. install binary + libraries ------------------------------------------------------------------------------

build_libs() {  # prints "<soname> <path>" for each library the built AMF links from the build tree
    local build_real soname arrow path rest real
    build_real=$(readlink -f "$BUILD")
    env -u LD_LIBRARY_PATH ldd "$(readlink -f "$BUILT")" | while read -r soname arrow path rest; do
        [ "$arrow" = "=>" ] && [ -e "$path" ] || continue
        real=$(readlink -f "$path")
        case "$real" in "$build_real"/*) echo "$soname $path" ;; esac
    done
}

install_file() {  # install_file <src> <dest>: install + mv -f when missing or different
    local src=$1 dest=$2
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        return 0
    fi
    if [ -f "$dest" ]; then
        todo "update $dest (installed $(date -r "$dest" '+%F %T'), built $(date -r "$src" '+%F %T'))"
    else
        todo "install $dest"
    fi
    as_root install -m 0755 "$src" "$dest.new"
    as_root mv -f "$dest.new" "$dest"
}

install_amf() {
    say "4. install into $PREFIX"
    if [ ! -f "$BUILT" ]; then
        [ "$DRY_RUN" = 1 ] || die "build output missing: $BUILT"
        todo "install $BIN and the Open5GS libraries it links into $PREFIX/lib"
        return
    fi
    local n_before=${#CHANGES[@]} libs soname path wanted f
    if [ ! -d "$PREFIX/bin" ] || [ ! -d "$PREFIX/lib" ]; then
        todo "create $PREFIX/{bin,lib}"
        as_root install -d -m 0755 "$PREFIX/bin" "$PREFIX/lib"
    fi
    libs=$(build_libs)
    [ -n "$libs" ] || die "ldd lists no Open5GS library from the build tree for $BUILT"
    install_file "$BUILT" "$BIN"
    wanted=" "
    while read -r soname path; do
        install_file "$path" "$PREFIX/lib/$soname"
        wanted+="$soname "
    done <<<"$libs"
    if [ "${#CHANGES[@]}" -eq "$n_before" ]; then
        ok "$BIN and $(wc -l <<<"$libs") libraries identical to the build"
    fi
    for f in "$PREFIX"/lib/*; do
        [ -e "$f" ] || continue
        case "$wanted" in
            *" $(basename "$f") "*) ;;
            *) warn "$f is not linked by the AMF any more (left in place)" ;;
        esac
    done
}

# --- 5. systemd drop-in -----------------------------------------------------------------------------------------

write_dropin() {
    say "5. systemd drop-in $DROPIN"
    local tmp reload=0
    if [ -f "$DROPIN" ] && cmp -s "$DROPIN" <(dropin_content); then
        ok "drop-in up to date"
    else
        if [ -f "$DROPIN" ]; then
            todo "rewrite the drop-in"
            say "   diff installed -> expected:"
            diff -u "$DROPIN" <(dropin_content) | sed 's/^/[amf-sf]          /' || true
        else
            todo "write the drop-in"
        fi
        if [ "$DRY_RUN" = 1 ]; then
            as_root install -D -m 0644 /dev/stdin "$DROPIN"
        else
            tmp=$(mktemp)
            dropin_content >"$tmp"
            as_root install -D -m 0644 "$tmp" "$DROPIN"
            rm -f "$tmp"
        fi
        reload=1
    fi
    if [ "$reload" = 0 ]; then
        if [ "$(systemctl show -p NeedDaemonReload --value "$UNIT" 2>/dev/null)" = yes ] ||
            ! systemctl show -p DropInPaths --value "$UNIT" 2>/dev/null | tr ' ' '\n' | grep -qx "$DROPIN"; then
            reload=1
        fi
    fi
    if [ "$reload" = 1 ]; then
        todo "systemctl daemon-reload"
        as_root systemctl daemon-reload
    else
        ok "systemd has loaded the drop-in"
    fi
}

# --- 6. verification --------------------------------------------------------------------------------------------

RESTART_PENDING=0
check_running() {  # is the running AMF the binary/drop-in installed now?
    local pid args age started newest f
    pid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)
    if [ "${pid:-0}" = 0 ]; then
        say "   $UNIT is not running"
        return
    fi
    args=$(ps -o args= -p "$pid" 2>/dev/null || true)
    age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    started=$(( $(date +%s) - ${age:-0} ))
    newest=0
    for f in "$BIN" "$PREFIX"/lib/* "$DROPIN"; do
        [ -e "$f" ] && [ "$(stat -c %Y "$f")" -gt "$newest" ] && newest=$(stat -c %Y "$f")
    done
    if [ "$ROLLBACK" = 1 ]; then
        case "$args" in
            "$PREFIX"/*) RESTART_PENDING=1; say "   running AMF (pid $pid) is still the patched one: restart pending" ;;
            *) ok "running AMF (pid $pid) is the apt binary" ;;
        esac
    elif [[ "$args" == "$BIN "* ]] && [ "$started" -ge "$newest" ]; then
        ok "running AMF (pid $pid, up $(( ${age:-0} / 3600 )) h) is the installed patched binary"
    else
        RESTART_PENDING=1
        say "   running AMF (pid $pid: ${args%% *}) predates the installed files: restart pending"
    fi
}

verify() {
    say "6. verification"
    local bad=0 unit_text loaded lddout v
    unit_text=$(systemctl cat "$UNIT" 2>/dev/null || true)
    if grep -qx "Environment=LD_LIBRARY_PATH=$PREFIX/lib" <<<"$unit_text" &&
        grep -qx "ExecStart=$BIN -c /etc/open5gs/amf.yaml" <<<"$unit_text"; then
        ok "systemctl cat $UNIT shows the LD_LIBRARY_PATH + ExecStart override"
    else
        warn "systemctl cat $UNIT does not show the override"; bad=1
    fi
    loaded=$(systemctl show -p ExecStart --value "$UNIT" 2>/dev/null || true)
    if [[ "$loaded" == *"path=$BIN "* ]]; then
        ok "loaded unit ExecStart is $BIN"
    else
        warn "loaded unit ExecStart is not $BIN (daemon-reload pending?)"; bad=1
    fi
    if [ -x "$BIN" ]; then
        lddout=$(env LD_LIBRARY_PATH="$PREFIX/lib" ldd "$BIN" 2>&1 || true)
        if grep -q 'not found' <<<"$lddout"; then
            warn "ldd $BIN: $(grep 'not found' <<<"$lddout" | xargs)"; bad=1
        elif awk -v p="$PREFIX/lib/" '/^\t*(libogs|libprom)/ && index($3, p) != 1 {e = 1} END {exit e}' <<<"$lddout"; then
            ok "ldd resolves every library; Open5GS ones from $PREFIX/lib"
        else
            warn "ldd $BIN resolves Open5GS libraries outside $PREFIX/lib"; bad=1
        fi
        v=$(env LD_LIBRARY_PATH="$PREFIX/lib" "$BIN" -v 2>&1 | head -n 3 || true)
        if grep -q "v${VERSION//./\\.}\$" <<<"$v"; then
            ok "$BIN -v: $v"
        else
            warn "$BIN -v printed '$v'"; bad=1
        fi
    else
        warn "$BIN missing"; bad=1
    fi
    check_running
    return "$bad"
}

finish_restart() {
    local cmd="sudo systemctl restart $UNIT"
    local note="after an AMF restart the OAI gNB re-associates by itself; restart the N3IWF on PC2 (tmux n3iwf),"
    note+=" then the N3IWUE on PC3"
    if [ "$RESTART" = 1 ] && { [ "${#CHANGES[@]}" -gt 0 ] || [ "$RESTART_PENDING" = 1 ]; }; then
        todo "restart $UNIT"
        as_root systemctl restart "$UNIT"
        if [ "$DRY_RUN" = 0 ]; then
            sleep 2
            systemctl is-active --quiet "$UNIT" || die "$UNIT is not active after the restart: journalctl -u $UNIT -n 50"
            ok "$UNIT active: $(ps -o args= -p "$(systemctl show -p MainPID --value "$UNIT")")"
        fi
        say "$note"
    elif [ "$RESTART_PENDING" = 1 ] || { [ "$RESTART" = 0 ] && [ "${#CHANGES[@]}" -gt 0 ]; }; then
        say "the AMF is not restarted without --restart. To load the change: $cmd"
        say "$note"
    fi
}

summary() {
    local i
    if [ "${#CHANGES[@]}" -eq 0 ]; then
        if [ "$ROLLBACK" = 1 ]; then
            say "nothing to roll back: $UNIT already uses the apt AMF"
        else
            say "already installed, up to date: nothing to do"
        fi
        [ "$RESTART_PENDING" = 0 ] || say "(but the running AMF is not the installed one: restart pending, see above)"
        return
    fi
    if [ "$DRY_RUN" = 1 ]; then
        say "dry run: ${#CHANGES[@]} change(s) would be made:"
    else
        say "done: ${#CHANGES[@]} change(s) made:"
    fi
    for i in "${CHANGES[@]}"; do say "   - $i"; done
}

rollback() {
    say "rollback to the apt AMF (/usr/bin/open5gs-amfd)"
    if [ -f "$DROPIN" ]; then
        todo "remove $DROPIN"
        as_root rm -f "$DROPIN"
        as_root rmdir --ignore-fail-on-non-empty "$(dirname "$DROPIN")"
        todo "systemctl daemon-reload"
        as_root systemctl daemon-reload
    else
        ok "no drop-in at $DROPIN"
    fi
    if [ "$DRY_RUN" = 0 ] && systemctl cat "$UNIT" 2>/dev/null | grep -q "$PREFIX"; then
        die "systemctl cat $UNIT still references $PREFIX (another drop-in?)"
    fi
    check_running
    finish_restart
    summary
    [ ! -d "$PREFIX" ] || say "$PREFIX is kept (unused now); remove it with: sudo rm -rf $PREFIX"
}

main() {
    [ "$DRY_RUN" = 0 ] || say "dry run: nothing is changed, the commands that would run are printed"
    if [ "$ROLLBACK" = 1 ]; then
        rollback
        return
    fi
    for c in dpkg-query systemctl ldd cmp python3; do command -v "$c" >/dev/null || die "$c not found"; done
    check_open5gs_version
    if packaged_patched_amf; then
        report_packaged_amf
        return
    fi
    check_build_deps
    get_source
    build_amf
    install_amf
    write_dropin
    if verify; then
        ok "verification passed"
    elif [ "$DRY_RUN" = 1 ]; then
        say "   (verification reflects the current system; the pending changes above address it)"
    else
        die "verification failed (see the warnings above)"
    fi
    finish_restart
    summary
}

main
