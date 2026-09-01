# The backup runner. Nothing (restic, rclone, aws) is installed on the host:
# per the container-first policy this image supplies all backup tooling.
#
# Binaries are copied from the upstream official images so the versions are
# exactly what those projects publish, and pinned so a rebuild is reproducible.
ARG RESTIC_VERSION=0.17.3
ARG RCLONE_VERSION=1.68.2

FROM restic/restic:${RESTIC_VERSION} AS restic
FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM alpine:3.21

RUN apk add --no-cache ca-certificates tzdata fuse3 jq \
    && rm -rf /var/cache/apk/*

COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

# Fail the build loudly here rather than at 01:30 on a Tuesday.
RUN restic version && rclone version && jq --version

ENV RESTIC_CACHE_DIR=/root/.cache/restic \
    XDG_CONFIG_HOME=/tmp

WORKDIR /
