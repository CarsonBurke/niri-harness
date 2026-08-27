#!/bin/sh
# Self-test for niri-harness.
#
# Everything here runs without a human and without touching the caller's
# desktop: the OCR tests synthesise their own images, and the session test
# starts a nested compositor of its own.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export NIRI_HARNESS_STATE="${TMPDIR:-/tmp}/niri-harness-selftest-state"
rm -rf "$NIRI_HARNESS_STATE"

exec python3 "$root/tests/selftest.py" "$@"
