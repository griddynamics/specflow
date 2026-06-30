#!/usr/bin/env sh
# Provision the PER-WORKSPACE Flutter SDK on first use by copying the shared template.
#
# Flutter self-mutates $FLUTTER_ROOT/bin/cache at runtime (engine artefacts, the
# flutter_tools snapshot, a lockfile touched on every invocation) and offers no env var to
# relocate that cache. A single shared SDK would therefore race across the concurrent
# workspaces a P10Y run spins up. Instead init-mobile-sdk.sh builds ONE pristine template at
# $FLUTTER_TEMPLATE_ROOT and this helper copies it into each workspace's own FLUTTER_ROOT.
#
# Lazy: only workspaces that actually invoke flutter/dart pay the ~1.5 GB copy; the copy lives
# under the workspace cache root and is reclaimed by clear_workspace_caches() on completion.
set -eu

: "${FLUTTER_ROOT:?FLUTTER_ROOT must be set (per-workspace path from setup_workspace_cache_directories)}"
FLUTTER_TEMPLATE_ROOT="${FLUTTER_TEMPLATE_ROOT:-/workspaces/caches/common/flutter}"

if [ -x "${FLUTTER_ROOT}/bin/flutter" ]; then
    exit 0
fi

if [ ! -x "${FLUTTER_TEMPLATE_ROOT}/bin/flutter" ]; then
    echo "Flutter template not found at ${FLUTTER_TEMPLATE_ROOT}." >&2
    echo "Run init-mobile-sdk.sh on the persistent volume first." >&2
    exit 1
fi

# Atomic copy: cp to a PID-unique tmp, then mv -nT into place. `-T` (--no-target-directory) is
# required: without it `mv -n tmp FLUTTER_ROOT` moves tmp *inside* an existing FLUTTER_ROOT
# instead of no-op'ing. Both paths share the same NFS filesystem, so the rename is atomic.
mkdir -p "$(dirname "${FLUTTER_ROOT}")"
tmp="${FLUTTER_ROOT}.tmp.$$"
rm -rf "${tmp}"
cp -a "${FLUTTER_TEMPLATE_ROOT}" "${tmp}"
mv -nT "${tmp}" "${FLUTTER_ROOT}" || true
rm -rf "${tmp}"
