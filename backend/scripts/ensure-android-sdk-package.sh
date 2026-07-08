#!/usr/bin/env sh
# Add missing Android SDK packages to the shared SDK cache.
#
# This is intentionally narrower than raw sdkmanager:
# - local quickstart must opt in with ALLOW_AGENT_SDKMANAGER=true
# - installs only into the shared ANDROID_SDK_ROOT
# - skips packages that already exist
# - serializes installs with a simple directory lock
set -eu

if [ "${ALLOW_AGENT_SDKMANAGER:-false}" != "true" ]; then
    echo "ensure-android-sdk-package is disabled. Set ALLOW_AGENT_SDKMANAGER=true for local quickstart." >&2
    exit 1
fi

: "${ANDROID_SDK_ROOT:?ANDROID_SDK_ROOT must be set}"
WORKSPACE_BASE_PATH="${WORKSPACE_BASE_PATH:-/workspaces}"
EXPECTED_ROOT="${WORKSPACE_BASE_PATH}/caches/common/android"
if [ "${ANDROID_SDK_ROOT}" != "${EXPECTED_ROOT}" ]; then
    echo "Refusing to modify Android SDK outside shared cache: ${ANDROID_SDK_ROOT}" >&2
    echo "Expected: ${EXPECTED_ROOT}" >&2
    exit 1
fi

SDKMANAGER="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager"
if [ ! -x "${SDKMANAGER}" ]; then
    echo "sdkmanager not found at ${SDKMANAGER}. Run init-mobile-sdk.sh first." >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    echo "Usage: ensure-android-sdk-package <platform-tools|platforms;android-N|build-tools;VERSION|cmake;VERSION|ndk;VERSION> [...]" >&2
    exit 2
fi

package_dir() {
    case "$1" in
        platform-tools)
            printf '%s\n' "${ANDROID_SDK_ROOT}/platform-tools"
            ;;
        platforms\;android-[0-9]*)
            printf '%s\n' "${ANDROID_SDK_ROOT}/platforms/${1#platforms;}"
            ;;
        build-tools\;[0-9]*)
            printf '%s\n' "${ANDROID_SDK_ROOT}/build-tools/${1#build-tools;}"
            ;;
        cmake\;[0-9]*)
            printf '%s\n' "${ANDROID_SDK_ROOT}/cmake/${1#cmake;}"
            ;;
        ndk\;[0-9]*)
            printf '%s\n' "${ANDROID_SDK_ROOT}/ndk/${1#ndk;}"
            ;;
        *)
            echo "Package not allowed for agent-managed additive install: $1" >&2
            exit 2
            ;;
    esac
}

missing=""
for package in "$@"; do
    dir="$(package_dir "${package}")"
    if [ -d "${dir}" ]; then
        echo "Android SDK package already present: ${package}"
    else
        missing="${missing} ${package}"
    fi
done

if [ -z "${missing}" ]; then
    exit 0
fi

LOCK_DIR="${ANDROID_SDK_ROOT}/.sdkmanager.lock"
attempt=0
while ! mkdir "${LOCK_DIR}" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -gt 120 ]; then
        echo "Timed out waiting for Android SDK lock: ${LOCK_DIR}" >&2
        exit 1
    fi
    sleep 1
done
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT INT TERM

for package in ${missing}; do
    dir="$(package_dir "${package}")"
    if [ -d "${dir}" ]; then
        echo "Android SDK package already present after lock: ${package}"
        continue
    fi
    echo "Installing missing Android SDK package into shared cache: ${package}"
    yes | "${SDKMANAGER}" --sdk_root="${ANDROID_SDK_ROOT}" "${package}" >/dev/null
done

echo "Android SDK additive package check complete."
