# Security Stack

Four small guests, plus Suricata and ntopng on the firewall, that give a homelab
intrusion detection, host integrity monitoring, DNS filtering, flow visibility
and vulnerability scanning. Every piece is chosen so a single-hypervisor estate
can run it without a dedicated budget or a dedicated box.

Open source throughout.

The design assumption is that **the network perimeter is not enough**. A
firewall only sees traffic that crosses it, so anything sharing a layer 2
segment, or any multi-homed host, is invisible to it. That is why this stack
pairs a network sensor with host agents rather than relying on either alone.

Four guests, **8 vCPU, 15.5 GB RAM, 96 GB disk** in total, sized for roughly
15 monitored hosts at 30 day retention, plus about 2 GB RAM and 2 vCPU added to
the firewall VM for ntopng. Full specs, and the working behind each number, in
[docs/reference](docs/reference/index.md).

## Quick start

Provisioning is **dry run by default**. Nothing is created until you pass
`--apply`.

```bash
# on the Proxmox host, as root
./provision-security-stack.sh                    # show what would happen
./provision-security-stack.sh --apply            # create all four
./provision-security-stack.sh --apply --only sec-dns   # or one at a time
```

Storage IDs, bridges and SSH key are environment variables with defaults.
`sec-scan` goes on `BRIDGE_SANDBOX`, every other guest on `BRIDGE`:

```bash
STORAGE_SSD=local-lvm STORAGE_HDD=local-lvm BRIDGE=vmbr0 BRIDGE_SANDBOX=vmbr1 \
  SSH_PUBKEY=~/.ssh/id_ed25519.pub ./provision-security-stack.sh
```

Both storage tiers default to the same pool because a stock Proxmox install has
exactly one storage that accepts guest disks. Split them only after confirming
the second pool advertises `images` and `rootdir`; a backup target does not, and
the script will tell you so before it creates anything.

The script refuses to run anywhere that is not a Proxmox host, refuses to run as
a non-root user, checks that each storage exists **and carries the content type
it is being asked for**, refuses to start if the requested RAM exceeds what the
host has available, and **skips any VMID that already exists** rather than
modifying it.

Then, inside each guest:

```bash
./install/install-sec-dns.sh            # dry run
./install/install-sec-dns.sh --apply    # execute
```

Recommended order, with the reasoning and the per-step cautions, is in
[Getting started](docs/getting-started/index.md#recommended-order). The short
version: `sec-dns` first, then CrowdSec, then Suricata on the firewall, then
Wazuh, then the rest.

## How it fits with the other modules

`security/` provisions the guests and installs the services. It does not own
the fleet or the dashboard:

- **Agents** are deployed by `ansible/roles/wazuh_agent`, enabled solely by
  membership of the `wazuh_agents` inventory group. Same contract as
  `autoupdate` gating patching.
- **Guest telemetry** uses the existing Alloy collector and update-metrics
  exporter. The `security_guests` inventory overlay identifies all four guests;
  their CPU, memory, disk, network, systemd metrics and logs feed the existing
  Grafana stack. Each has a committed Servers dashboard. See
  [Host monitoring](docs/getting-started/index.md#host-monitoring-in-grafana).
- **Alerts** are forwarded into Loki so `monitoring/`'s Grafana stays the single
  pane. The Wazuh dashboard is kept for deep investigation only.

## What's here

| Path | Contents |
|---|---|
| `provision-security-stack.sh` | Creates the four guests on Proxmox. Dry run by default. |
| `install/` | One installer per guest, same dry-run convention. |
| `docs/` | The MkDocs site published at the link below. |

## Documentation

Full docs at
[harshitruwali.github.io/homelab-infra/security/](https://harshitruwali.github.io/homelab-infra/security/),
or in [`docs/`](docs/index.md):

- [Getting started](docs/getting-started/index.md), provisioning and install
- [What runs where](docs/components/index.md), per-guest services, ports, config files and logins
- [How it works](docs/understanding/index.md), the mental model: virtualisation, routing, detection internals
- [Architecture](docs/architecture/index.md), placement and tool selection
- [Wiring](docs/wiring/index.md), connecting it to Grafana, Loki and the firewall
- [Operations](docs/operations/index.md), retention, sizing and maintenance
- [Reference](docs/reference/index.md), guest specs, ports, licences
- [Security](docs/security.md), this stack's own posture

## Licences

Suricata, CrowdSec, Wazuh, AdGuard Home, ntopng and Greenbone are all open
source. Per-tool licences are in
[docs/reference](docs/reference/index.md#licences).

## Authenticated bootstrap

Wazuh and CrowdSec require prepared credentials and server certificates
before `--apply`; see [Security bootstrap inputs](docs/security.md#bootstrap-inputs).
Existing guests use `install/configure-sec-wazuh.sh` or
`install/configure-sec-crowdsec.sh` to apply these settings without reinstalling.
Keep Wazuh enrollment blocked during the initial vendor installation, and
prepare all CrowdSec clients for HTTPS before changing an existing LAPI.
