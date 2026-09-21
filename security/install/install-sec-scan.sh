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
download_file "$COMPOSE_URL" "$GVM_DIR/compose.yaml"

# 21 services, all bound to 127.0.0.1 by the shipped compose file. Pull first
# so a slow registry does not look like a broken start.
run docker compose --project-directory "$GVM_DIR" -f "$GVM_DIR/compose.yaml" pull
run docker compose --project-directory "$GVM_DIR" -f "$GVM_DIR/compose.yaml" up -d

ok "Greenbone containers started from $GVM_DIR/compose.yaml"
cat <<'NOTE'

  THE FEED SYNC IS THE LONG PART. Allow several hours and do not interrupt it.
  Nothing works until it finishes, and a half-synced feed reports zero findings
  rather than an error, which reads exactly like a clean estate:

      cd /opt/greenbone
      docker compose pull                 # data images carry the Community Feed
      docker compose up -d                # copies feed data into shared volumes
      docker compose ps                   # data services must become healthy
      docker compose logs -f gvmd          # watch it settle

  CHANGE THE ADMIN PASSWORD FIRST. The containers create admin / admin:

      docker compose exec -u gvmd gvmd gvmd --user=admin --new-password='<pick one>'

  THE UI BINDS TO LOCALHOST ONLY: 127.0.0.1:9392 (http) and 127.0.0.1:443
  (https, self-signed). That is deliberate, so reach it over an SSH tunnel
  rather than republishing it. This guest sits behind the firewall, so the
  tunnel goes through your jump host:

      ssh -J <jump-host> -N -L 9392:127.0.0.1:9392 admin@<sec-scan>

  Keep the UI bound to loopback and use the SSH tunnel for access.

  SCANNING SCOPE IS A ROUTING QUESTION, not a Greenbone one. A scanner
  ORIGINATES connections to its targets, unlike the Wazuh and CrowdSec agents
  which dial out to their managers. It can only scan a segment it has a route
  to, which is why this guest sits on the sandbox bridge: from there it
  reaches the sandbox directly and the trusted segment through the firewall's
  outbound NAT. Confirm before you trust an empty report:

      ip route
      nmap -sn <target-segment>          # hosts it can actually see

  Three consequences of sitting on the sandbox side:
    - Trusted-side targets log the FIREWALL's address as the scanner, not
      this guest's. That is NAT, not a bug.
    - Suricata sees every scan of the trusted side and will alert on it.
      Accept the alerts or add a pass rule for this guest's address, and
      know that the pass rule also blinds Suricata to this guest.
    - If you add a sandbox-to-trusted block rule, this guest needs its own
      pass rule above it, or it can only scan the sandbox.

  For authenticated scans, use an unprivileged account on each target. Reading
  the package list needs no root, and this guest lives beside the workloads
  you trust least.

  Take a baseline now and diff monthly. What you are looking for is DRIFT: a
  new unauthenticated service, another multi-homed host, an interface nobody
  declared.

  Caveat: the Community Feed is free but delayed and reduced relative to the
  Enterprise Feed. Fine for drift detection; not parity with a commercial
  scanner.
NOTE
