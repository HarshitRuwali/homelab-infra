#!/usr/bin/env bash
# Run the smoke test as root inside a throwaway container, so nothing on the
# host is touched and no packages are installed.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
exec docker run --rm \
  -v "$REPO:/opt/s3-backup:ro" \
  -e REPO=/opt/s3-backup \
  ubuntu:24.04 \
  bash /opt/s3-backup/tests/smoke-test.sh
