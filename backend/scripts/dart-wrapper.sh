#!/usr/bin/env sh
# Flutter's internal dart calls resolve via $FLUTTER_ROOT directly, not this wrapper.
set -eu
ensure-flutter-sdk
exec "${FLUTTER_ROOT}/bin/dart" "$@"
