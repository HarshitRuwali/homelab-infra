#!/usr/bin/env bash
# ntopng Community on sec-ntopng, as a NetFlow COLLECTOR (not a sniffer).
# Collecting rather than sniffing is why this can stay an unprivileged LXC.
# Run inside the sec-ntopng LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y wget ca-certificates lsb-release gnupg

run sh -c 'wget -qO /tmp/apt-ntop.deb https://packages.ntop.org/apt-stable/$(lsb_release -rs)/all/apt-ntop-stable.deb'
run dpkg -i /tmp/apt-ntop.deb
run apt-get update
run apt-get install -y ntopng nprobe

# nprobe receives NetFlow on 2055/udp and feeds ntopng over ZMQ.
run tee /etc/nprobe/nprobe.conf >/dev/null <<'CFG'
--collector-port=2055
--zmq=tcp://127.0.0.1:5556
--interface=none
CFG

run tee /etc/ntopng/ntopng.conf >/dev/null <<'CFG'
-i=tcp://127.0.0.1:5556
-w=3000
--community
CFG

run systemctl enable --now nprobe ntopng

ok "ntopng on :3000, collecting NetFlow on 2055/udp"
cat <<'NOTE'

  On OPNsense: install the softflowd plugin and export flows to this guest on 2055/udp.
  On pfSense or a Linux router, use softflowd or pmacct to the same port.

  Verify flows are arriving:
    tcpdump -ni any port 2055 -c 5

  Scope caveat: this sees only traffic that CROSSES
  the firewall. It cannot see traffic between two hosts that share a layer 2 segment,
  nor any host that is multi-homed across segments. Wazuh agents cover those. An empty
  ntopng is not evidence of no lateral traffic.
NOTE
