# Security Stack

Six small guests that give a homelab intrusion detection, host integrity
monitoring, DNS filtering, SSO, secrets, flow visibility and vulnerability
scanning. Everything here is free and open source, and every piece is chosen so
a single-hypervisor estate can run it without a dedicated budget or a dedicated
box.

The design assumption is that **the network perimeter is not enough**. A
firewall only sees traffic that crosses it, so anything sharing a layer 2
segment, or any multi-homed host, is invisible to it. That is why this stack
pairs a network sensor with host agents rather than relying on either alone.

Six guests, **11 vCPU, 18.5 GB RAM, 120 GB disk** in total, sized for roughly
15 monitored hosts at 30 day retention. Full specs, and the working behind each
number, in [docs/reference](docs/reference/index.md).

## Quick start

Provisioning is **dry run by default**. Nothing is created until you pass
`--apply`.

```bash
# on the Proxmox host, as root
./provision-security-stack.sh                    # show what would happen
./provision-security-stack.sh --apply            # create all six
./provision-security-stack.sh --apply --only sec-dns   # or one at a time
```

Storage IDs, bridge and SSH key are environment variables with defaults:

```bash
STORAGE_SSD=local-lvm STORAGE_HDD=local-lvm BRIDGE=vmbr0 \
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
- **Alerts** are forwarded into Loki so `monitoring/`'s Grafana stays the single
  pane. The Wazuh dashboard is kept for deep investigation only.

See the NOC/SOC roadmap in `monitoring/docs/roadmap.md` for the phasing this
module implements.

## What's here

| Path | Contents |
|---|---|
| `provision-security-stack.sh` | Creates the six guests on Proxmox. Dry run by default. |
| `install/` | One installer per guest, same dry-run convention. |
| `docs/` | The MkDocs site published at the link below. |

## Documentation

Full docs at
[harshitruwali.github.io/homelab-infra/security/](https://harshitruwali.github.io/homelab-infra/security/),
or in [`docs/`](docs/index.md):

- [Getting started](docs/getting-started/index.md), provisioning and install
- [Architecture](docs/architecture/index.md), placement and tool selection
- [Wiring](docs/wiring/index.md), connecting it to Grafana, Loki and the firewall
- [Operations](docs/operations/index.md), retention, sizing and maintenance
- [Reference](docs/reference/index.md), guest specs, ports, licences
- [Security](docs/security.md), this stack's own posture

## Licences

Every component is free and open source: Suricata, CrowdSec, Wazuh, AdGuard
Home, Authelia, OpenBao, ntopng and Greenbone. Per-tool licences, verified
against primary sources, are in
[docs/reference](docs/reference/index.md#licences).

**OpenBao, not HashiCorp Vault.** Vault moved to BUSL 1.1 in August 2023 and is
no longer an OSI-approved open source licence.
