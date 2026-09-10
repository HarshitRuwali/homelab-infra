#!/usr/bin/env bash
# Greenbone Community Edition (GVM) on sec-scan.
# Run inside the sec-scan VM.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y gvm

run gvm-setup          # first feed sync: allow SEVERAL HOURS, do not interrupt
run gvm-check-setup

ok "Greenbone installed"
cat <<'NOTE'

  The community feed sync is large and slow on first run. Let it finish.

  Configure a scan target per segment. Schedule weekly,
  off-peak. Take a baseline now and diff monthly. What you are looking for is DRIFT:
  a new unauthenticated service, another multi-homed host, an interface nobody declared.

  Give this guest a DHCP reservation and write the address down. An unexpected scanner
  is exactly what your new Wazuh rules will alert on, and you want to recognise your own.

  Caveat: the Greenbone Community Feed is free but delayed and reduced relative to the
  Enterprise Feed. Fine for drift detection; not parity with a commercial scanner.
NOTE
