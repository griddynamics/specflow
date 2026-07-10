#!/usr/bin/env sh
# Provision ALL heavy mobile SDKs into the shared NFS cache (caches/common/...).
#
# Run this manually once per persistent volume. Small runtimes (Node/npm/yarn/pnpm,
# JDK, Gradle, Kotlin) are baked into the Docker image; only the large SDKs that are
# impractical to bake — the Android SDK and the Flutter SDK — live here.
#
# Idempotent per component via a COMPLETION MARKER: each component writes its marker file
# only AFTER every one of its steps has succeeded. Because `set -e` aborts the script the
# moment any step fails, a component that fails partway (e.g. a transient download during the
# multi-GB Android package install) never gets its marker and is therefore RETRIED in full on
# the next run. Keying the skip on the marker — not on the first artifact a block happens to
# create (the sdkmanager binary / the flutter checkout, both of which exist before licenses and
# package install run) — is what makes a re-run actually finish a half-done install rather than
# skip it. The heavy downloads inside each block stay guarded so a retry reuses what's present.
set -eu

# ---------------------------------------------------------------------------
# Android SDK (cmdline-tools, platform-tools, build-tools, platforms, cmake, ndk)
# ---------------------------------------------------------------------------
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/workspaces/caches/common/android}"
ANDROID_SDK_CMDLINE_TOOLS_VERSION="${ANDROID_SDK_CMDLINE_TOOLS_VERSION:-11076708}"
SDKMANAGER="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager"
ANDROID_MARKER="${ANDROID_SDK_ROOT}/.specflow-provisioned"

if [ -f "${ANDROID_MARKER}" ]; then
    echo "Android SDK already provisioned. Skipping."
else
    # cmdline-tools bootstrap is itself idempotent: download+extract only when sdkmanager is
    # absent, so a retry after a failed package install reuses the already-extracted tools.
    if [ ! -x "${SDKMANAGER}" ]; then
        echo "Downloading Android cmdline-tools..."
        mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools"
        wget -q "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_SDK_CMDLINE_TOOLS_VERSION}_latest.zip" -O /tmp/android-cmdline-tools.zip
        rm -rf /tmp/android-cmdline-tools
        unzip -q /tmp/android-cmdline-tools.zip -d /tmp/android-cmdline-tools
        mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools/latest"
        mv /tmp/android-cmdline-tools/cmdline-tools/* "${ANDROID_SDK_ROOT}/cmdline-tools/latest/"
        rm -rf /tmp/android-cmdline-tools /tmp/android-cmdline-tools.zip
    fi

    echo "Accepting licenses and installing packages..."
    yes | "${SDKMANAGER}" --sdk_root="${ANDROID_SDK_ROOT}" --licenses > /dev/null
    # cmdline-tools are already extracted into cmdline-tools/latest above, so we do NOT
    # pass "cmdline-tools;latest" here — sdkmanager would refuse to overwrite the existing
    # dir and install a redundant duplicate into cmdline-tools/latest-2.
    #
    # Several recent platform / build-tools versions are pre-installed so projects targeting
    # different API levels find a match without each agent running sdkmanager against this
    # SHARED, read-only SDK (which would race across concurrent workspaces). sdkmanager is
    # idempotent — already-installed packages are a fast no-op — so re-running this after a
    # partial failure only fills the gaps.
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

    touch "${ANDROID_MARKER}"
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
FLUTTER_MARKER="${FLUTTER_TEMPLATE_ROOT}/.specflow-provisioned"

if [ -f "${FLUTTER_MARKER}" ]; then
    echo "Flutter template already provisioned. Skipping."
else
    # Clone only when the checkout is absent/incomplete. `rm -rf` first so a retry after an
    # interrupted clone doesn't fail on `git clone` into a non-empty dir; a good checkout from a
    # prior run (failed later, at config/precache) is kept and reused.
    if [ ! -x "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" ]; then
        echo "Downloading Flutter template..."
        rm -rf "${FLUTTER_TEMPLATE_ROOT}"
        mkdir -p "$(dirname "${FLUTTER_TEMPLATE_ROOT}")"
        git clone --depth 1 --branch "${FLUTTER_VERSION}" https://github.com/flutter/flutter.git "${FLUTTER_TEMPLATE_ROOT}"
    fi

    # Bootstrap the bundled Dart SDK and pre-warm Android build artefacts so per-workspace
    # copies do minimal first-run downloading; disable telemetry for unattended runs. Both are
    # idempotent, so they re-run cleanly on a retry that reused an existing checkout.
    "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" config --no-analytics > /dev/null
    "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" precache --android > /dev/null

    touch "${FLUTTER_MARKER}"
    echo "Flutter template initialization complete."
fi

echo "Mobile SDK initialization complete."
