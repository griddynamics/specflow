#!/usr/bin/env sh
# Provision ALL heavy mobile SDKs into the shared NFS cache (caches/common/...).
#
# Run this manually once per persistent volume. Small runtimes (Node/npm/yarn/pnpm,
# JDK, Gradle, Kotlin) are baked into the Docker image; only the large SDKs that are
# impractical to bake — the Android SDK and the Flutter SDK — live here.
#
# Idempotent per component: an already-provisioned SDK is detected and skipped, so this
# script is safe to re-run (e.g. after bumping a version below).
set -eu

# ---------------------------------------------------------------------------
# Android SDK (cmdline-tools, platform-tools, build-tools, platforms, cmake, ndk)
# ---------------------------------------------------------------------------
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/workspaces/caches/common/android}"
ANDROID_SDK_CMDLINE_TOOLS_VERSION="${ANDROID_SDK_CMDLINE_TOOLS_VERSION:-11076708}"
SDKMANAGER="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager"

if [ -x "${SDKMANAGER}" ]; then
    echo "Android SDK already exists in volume. Skipping download."
else
    echo "Android SDK not found in volume. Downloading..."

    mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools"
    wget -q "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_SDK_CMDLINE_TOOLS_VERSION}_latest.zip" -O /tmp/android-cmdline-tools.zip
    unzip -q /tmp/android-cmdline-tools.zip -d /tmp/android-cmdline-tools
    mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools/latest"
    mv /tmp/android-cmdline-tools/cmdline-tools/* "${ANDROID_SDK_ROOT}/cmdline-tools/latest/"
    rm -rf /tmp/android-cmdline-tools /tmp/android-cmdline-tools.zip

    echo "Accepting licenses and installing packages..."
    yes | "${SDKMANAGER}" --sdk_root="${ANDROID_SDK_ROOT}" --licenses > /dev/null
    # cmdline-tools are already extracted into cmdline-tools/latest above, so we do NOT
    # pass "cmdline-tools;latest" here — sdkmanager would refuse to overwrite the existing
    # dir and install a redundant duplicate into cmdline-tools/latest-2.
    #
    # Several recent platform / build-tools versions are pre-installed so projects targeting
    # different API levels find a match without each agent running sdkmanager against this
    # SHARED, read-only SDK (which would race across concurrent workspaces).
    "${SDKMANAGER}" --sdk_root="${ANDROID_SDK_ROOT}" \
        "platform-tools" \
        "build-tools;34.0.0" \
        "build-tools;35.0.0" \
        "build-tools;36.0.0" \
        "platforms;android-34" \
        "platforms;android-35" \
        "platforms;android-36" \
        "cmake;3.22.1" \
        "ndk;27.1.12297006" > /dev/null

    echo "Android SDK initialization complete."
fi

# ---------------------------------------------------------------------------
# Flutter SDK — pristine TEMPLATE (bundles the Dart SDK; covers Flutter + Dart projects)
#
# This installs ONE read-only template. Unlike the Android SDK, Flutter is NOT shared at
# runtime: it self-mutates bin/cache and can't relocate that cache, so each workspace gets its
# own copy of this template, made on first use by the flutter/dart wrappers (see Dockerfile and
# scripts/ensure-flutter-sdk.sh). Pre-warming the template here means those per-workspace copies
# start fully cached and do no first-run downloading.
#
# ORDER MATTERS: this block runs AFTER the Android block above on purpose —
# `flutter precache --android` warms Android-targeted engine artefacts and expects the
# Android SDK env (ANDROID_SDK_ROOT) to already be provisioned. Keep Android first.
# ---------------------------------------------------------------------------
FLUTTER_TEMPLATE_ROOT="${FLUTTER_TEMPLATE_ROOT:-/workspaces/caches/common/flutter}"
FLUTTER_VERSION="${FLUTTER_VERSION:-3.27.4}"

if [ -x "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" ]; then
    echo "Flutter template already exists in volume. Skipping download."
else
    echo "Flutter template not found in volume. Downloading..."

    mkdir -p "$(dirname "${FLUTTER_TEMPLATE_ROOT}")"
    git clone --depth 1 --branch "${FLUTTER_VERSION}" https://github.com/flutter/flutter.git "${FLUTTER_TEMPLATE_ROOT}"

    # Bootstrap the bundled Dart SDK and pre-warm Android build artefacts so per-workspace
    # copies do minimal first-run downloading; disable telemetry for unattended runs.
    "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" config --no-analytics > /dev/null
    "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" precache --android > /dev/null

    echo "Flutter template initialization complete."
fi

echo "Mobile SDK initialization complete."
