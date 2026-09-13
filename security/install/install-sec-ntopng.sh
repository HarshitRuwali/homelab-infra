#!/usr/bin/env bash
# ntopng Community on sec-ntopng, as a NetFlow COLLECTOR (not a sniffer).
# Collecting rather than sniffing is why this can stay an unprivileged LXC.
# Run inside the sec-ntopng LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y wget ca-certificates lsb-release gnupg

# lsb_release -cs, NOT -rs. ntop indexes its Debian repo by CODENAME:
# packages.ntop.org/apt-stable/trixie/ is 200, .../13/ is 404. With -rs this
# fetched a 404 and dpkg then failed on the error page. (ntop indexes its
# UBUNTU repo by version number, which is where the confusion comes from.)
run sh -c 'wget -qO /tmp/apt-ntop.deb https://packages.ntop.org/apt-stable/$(lsb_release -cs)/all/apt-ntop-stable.deb'
run dpkg -i /tmp/apt-ntop.deb
run apt-get update
run apt-get install -y ntopng nprobe

# nprobe receives NetFlow on 2055/udp and feeds ntopng over ZMQ.
write_file /etc/nprobe/nprobe.conf <<'CFG'
--collector-port=2055
--zmq=tcp://127.0.0.1:5556
--interface=none
CFG

write_file /etc/ntopng/ntopng.conf <<'CFG'
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
