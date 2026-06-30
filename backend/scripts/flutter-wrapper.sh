#!/usr/bin/env sh
set -eu
ensure-flutter-sdk
exec "${FLUTTER_ROOT}/bin/flutter" "$@"
