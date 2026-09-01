#!/usr/bin/env bash
# Build the pinned runner image.
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

IMAGE="${RUNNER_IMAGE:-s3-backup-runner:1.0.0}"
RESTIC_VERSION="${RESTIC_VERSION:-0.17.3}"
RCLONE_VERSION="${RCLONE_VERSION:-1.68.2}"

echo "building $IMAGE (restic ${RESTIC_VERSION}, rclone ${RCLONE_VERSION})"
docker build \
  --build-arg "RESTIC_VERSION=${RESTIC_VERSION}" \
  --build-arg "RCLONE_VERSION=${RCLONE_VERSION}" \
  -t "$IMAGE" .

echo
docker run --rm "$IMAGE" restic version
docker run --rm "$IMAGE" rclone version | head -1
