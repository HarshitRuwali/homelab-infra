# Obsidian desktop app on a virtual display, so its command line interface
# (obsidian-cli, Obsidian 1.12+) works on a server with no screen. The app is
# the official Linux build, pinned by version and checksum. See ../docs/obsidian.md.
FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0

ARG OBSIDIAN_VERSION=1.15.1
ARG OBSIDIAN_SHA256=09074f0635ecf366307d808d0fec06c01a87a0bcc66da6d34126ceece0eb8519
# The app runs as the user that owns the vault on the host, so files it writes
# keep that owner. setup.sh passes the host user's ids.
ARG UID=1000
ARG GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xvfb \
      libgtk-3-0t64 libnss3 libasound2t64 libgbm1 libxss1 libxtst6 libsecret-1-0 \
      libdrm2 libnotify4 libatspi2.0-0t64 fonts-dejavu-core \
 && curl -fsSL -o /tmp/obsidian.tar.gz \
      "https://github.com/obsidianmd/obsidian-releases/releases/download/v${OBSIDIAN_VERSION}/obsidian-${OBSIDIAN_VERSION}.tar.gz" \
 && echo "${OBSIDIAN_SHA256}  /tmp/obsidian.tar.gz" | sha256sum -c - \
 && mkdir /opt/obsidian \
 && tar -xzf /tmp/obsidian.tar.gz -C /opt/obsidian --strip-components=1 \
 && rm /tmp/obsidian.tar.gz \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /home/obsidian /vault/.obsidian \
 && chown "${UID}:${GID}" /home/obsidian /vault/.obsidian

COPY --chmod=0755 entrypoint.sh /usr/local/bin/obsidian-entrypoint

ENV HOME=/home/obsidian \
    XDG_RUNTIME_DIR=/tmp/run \
    DISPLAY=:99 \
    OBSIDIAN_VAULT=/vault
USER ${UID}:${GID}
WORKDIR /vault
# Healthy once the app answers on its CLI socket with the CLI switched on.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD /opt/obsidian/obsidian-cli version | grep -q installer
ENTRYPOINT ["/usr/local/bin/obsidian-entrypoint"]
