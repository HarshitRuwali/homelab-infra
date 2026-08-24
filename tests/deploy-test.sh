#!/usr/bin/env bash
# deploy-test.sh - install.sh staleness detection and fast reinstall.
# Runs in a throwaway container: it writes to /tmp/opt and /tmp/etc, never to
# the real /opt/s3-backup.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
exec docker run --rm \
  -v "$REPO:/src:ro" \
  ubuntu:24.04 bash /src/tests/deploy-test-inner.sh
