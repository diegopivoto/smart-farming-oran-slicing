#!/usr/bin/env bash
# Builds the debug APKs (signed with the debug key in ~/.android/debug.keystore) and copies them to dist/:
#   app   -> dist/sf-farm-app-<version>.apk     (Smart Farm demo app, Kotlin + Compose)
#   probe -> dist/sf-slice-probe-<version>.apk  (phase-1 capability probe, Java + Views)
# Toolchain installed without sudo under ~/opt (see README.md); override with JAVA_HOME / ANDROID_HOME.
# Usage: ./build.sh [app|probe|all] [extra gradle args]   (default: all; unit tests run for the app)
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-$HOME/opt/jdk-17.0.21+9}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/opt/android-sdk}"
export PATH="$JAVA_HOME/bin:$PATH"
what="${1:-all}"; [ $# -gt 0 ] && shift
mkdir -p dist
if [ "$what" = "all" ] || [ "$what" = "app" ]; then
    ./gradlew --no-daemon :app:testDebugUnitTest :app:assembleRelease :app:assembleDebug "$@"
    version=$(sed -n "s/.*versionName '\(.*\)'.*/\1/p" app/build.gradle)
    # release = R8-optimized, non-debuggable, debug-signed (install this one); -debug = debuggable fallback
    cp app/build/outputs/apk/release/app-release.apk "dist/sf-farm-app-${version}.apk"
    cp app/build/outputs/apk/debug/app-debug.apk "dist/sf-farm-app-${version}-debug.apk"
    sha256sum "dist/sf-farm-app-${version}.apk" "dist/sf-farm-app-${version}-debug.apk"
fi
if [ "$what" = "all" ] || [ "$what" = "probe" ]; then
    ./gradlew --no-daemon :probe:assembleDebug "$@"
    version=$(sed -n "s/.*versionName '\(.*\)'.*/\1/p" probe/build.gradle)
    cp probe/build/outputs/apk/debug/probe-debug.apk "dist/sf-slice-probe-${version}.apk"
    sha256sum "dist/sf-slice-probe-${version}.apk"
fi
