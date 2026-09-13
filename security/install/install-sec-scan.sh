#!/usr/bin/env bash
# Greenbone Community Edition (GVM) on sec-scan, from the official containers.
# Run inside the sec-scan VM.
#
# NOT `apt-get install gvm`. Debian dropped the GVM/OpenVAS stack: `gvm` is in
# sid only, and is ABSENT from bookworm, trixie and forky. On a Debian stable
# guest `apt-get install gvm` fails with "Unable to locate package gvm", so the
# containers are the only supported path short of building from source.
# Greenbone's own documentation leads with them.
source "$(dirname "$0")/_common.sh"
require_root; banner

COMPOSE_URL="${COMPOSE_URL:-https://greenbone.github.io/docs/latest/_static/compose.yaml}"
GVM_DIR="${GVM_DIR:-/opt/greenbone}"

run apt-get update
# The VM is created with --agent enabled=1, but Debian's genericcloud image does
# not ship the agent. Without it PVE shows no IP for the guest, which is exactly
# what you need to set the DHCP reservation in the provisioning script's step 1.
run apt-get install -y qemu-guest-agent
run systemctl enable --now qemu-guest-agent

# docker-compose in trixie is 2.26.x, i.e. Compose v2 under the old name.
run apt-get install -y docker.io docker-compose curl ca-certificates
run systemctl enable --now docker

run mkdir -p "$GVM_DIR"
run sh -c "curl -fsSL '$COMPOSE_URL' -o '$GVM_DIR/compose.yaml.part' && mv -f '$GVM_DIR/compose.yaml.part' '$GVM_DIR/compose.yaml'"

# 21 services, all bound to 127.0.0.1 by the shipped compose file. Pull first
# so a slow registry does not look like a broken start.
run sh -c "cd '$GVM_DIR' && docker compose pull"
run sh -c "cd '$GVM_DIR' && docker compose up -d"

ok "Greenbone containers started from $GVM_DIR/compose.yaml"
cat <<'NOTE'

  THE FEED SYNC IS THE LONG PART. Allow several hours and do not interrupt it.
  Nothing works until it finishes, and a half-synced feed reports zero findings
  rather than an error, which reads exactly like a clean estate:

      cd /opt/greenbone
      docker compose run --rm greenbone-feed-sync greenbone-feed-sync --type all
      docker compose logs -f gvmd          # watch it settle

  SET THE ADMIN PASSWORD. There is no default login:

      docker compose exec -u gvmd gvmd gvmd --user=admin --new-password='<pick one>'

  THE UI BINDS TO LOCALHOST ONLY: 127.0.0.1:9392 (http) and 127.0.0.1:443
  (https, self-signed). That is deliberate, so reach it over an SSH tunnel
  rather than republishing it:

      ssh -N -L 9392:127.0.0.1:9392 admin@<sec-scan>

  To serve it on the LAN instead, set NGINX_HOST on the gvm-config service in
  compose.yaml and put Authelia in front. Do not expose it directly.

  SCANNING SCOPE IS A ROUTING QUESTION, not a Greenbone one. A scanner
  ORIGINATES connections to its targets, unlike the Wazuh and CrowdSec agents
  which dial out to their managers. It can only scan a segment it has a route
  to. Confirm before you trust an empty report:

      ip route
      nmap -sn <target-segment>          # hosts it can actually see

  Take a baseline now and diff monthly. What you are looking for is DRIFT: a
  new unauthenticated service, another multi-homed host, an interface nobody
  declared.

  Caveat: the Community Feed is free but delayed and reduced relative to the
  Enterprise Feed. Fine for drift detection; not parity with a commercial
  scanner.
NOTE
