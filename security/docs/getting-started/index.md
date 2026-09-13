# Getting started

Two phases: create the guests on the hypervisor, then install a service inside
each one. Both phases are dry run by default.

## Prerequisites

- Proxmox VE, with root shell access on the node.
- Two storage IDs, one on SSD and one on bulk disk. `pvesm status` lists them.
  They are storage **IDs**, not mount paths.
- Two bridges: the trusted one most guests attach to (`BRIDGE`, default
  `vmbr0`), and the firewall's LAN side, where only `sec-scan` goes
  (`BRIDGE_SANDBOX`, default `vmbr1`).
  [Architecture](../architecture/index.md#the-scanner-is-the-exception) explains
  why the scanner is different.
- An SSH public key. No guest here is given a password of any kind; the LXCs
  get your key for `root`, and the VMs get it through cloud-init for `admin`.
  [Logging in](../components/index.md#logging-in) has the details.

## Phase 1: provision the guests

```bash
# on the Proxmox host, as root
./provision-security-stack.sh
```

That prints exactly what it would do and creates nothing. Read it, then:

```bash
./provision-security-stack.sh --apply
```

To do one guest at a time, which is the gentler path:

```bash
./provision-security-stack.sh --apply --only sec-dns
./provision-security-stack.sh --apply --only sec-crowdsec,sec-wazuh
```

Override defaults with environment variables:

```bash
STORAGE_SSD=local-lvm \
STORAGE_HDD=local-lvm \
STORAGE_TMPL=local \
BRIDGE=vmbr0 \
BRIDGE_SANDBOX=vmbr1 \
SSH_PUBKEY=~/.ssh/id_ed25519.pub \
LXC_TEMPLATE_FAMILY=debian-13-standard \
  ./provision-security-stack.sh --apply
```

`STORAGE_SSD` and `STORAGE_HDD` are storage **IDs** as `pvesm status` prints
them, not mount paths. They default to the same pool, because a stock Proxmox
install has exactly one storage that accepts guest disks. Check yours before
splitting the tiers:

```bash
pvesm status                      # the ID is the first column
pvesh get /storage/<id>           # "content" must include images and rootdir
```

A storage declared `content backup` will pass a name check and then fail at
`qm create`, after earlier guests already exist. The script checks content type
up front so that cannot happen.

`LXC_TEMPLATE_FAMILY` names a template family rather than an exact build. The
script prefers a matching template already in the local cache and otherwise
resolves the newest one in the appliance index. Set `LXC_TEMPLATE` to a full
filename from `pveam available` if you need a specific build.

### What the script refuses to do

It fails before touching anything if:

- you are not root, or `pvesm`, `pct` or `qm` are missing, which means you are
  not on a Proxmox host
- a configured storage ID does not exist (it prints the available storage rather
  than making you go look)
- a storage exists but lacks the content type it is being asked for, for example
  a backup target being used for guest disks
- the requested RAM exceeds what the host has available right now
- a storage tier does not have enough free space for the disks assigned to it
- a bridge any selected guest needs does not exist, or the SSH key is not
  readable
- no template matches `LXC_TEMPLATE_FAMILY`, in cache or in the index

It warns, but continues, when the stack would consume more than 70% of
available RAM, or when total vCPU exceeds the host core count. CPU overcommit is
normal on a hypervisor; memory exhaustion is not, which is why one is a warning
and the other stops the run.

It **skips any VMID that already exists** and never modifies it, so re-running
after a partial failure is safe.

## Phase 2: give every guest a reservation

Do this before installing anything. DHCP leases move, and when they do, agent
configs and firewall rules that reference an address break silently. Add a
reservation per guest, or set static addresses outside the DHCP pool.

The trusted-side guests get their leases from your router. `sec-scan` is on the
sandbox bridge, so its lease comes from whatever serves DHCP there, usually the
firewall.

## Phase 3: install the services

Copy `install/` into each guest and run the matching script. Same convention:
dry run unless you pass `--apply`.

```bash
./install/install-sec-dns.sh              # show what it would do
./install/install-sec-dns.sh --apply      # execute
```

`sec-scan` sits behind the firewall and the hypervisor has no route to it, so
reach it through your jump host (`scp -J` and `ssh -J`).

Each script ends with the post-install steps that cannot be automated, such as
setting retention in a web UI or changing a default password.

## Recommended order

Each step is useful on its own, so a partial rollout still improves things.

1. **`sec-dns`**, then repoint DHCP at it. Lowest risk, and DNS logs start
   accruing immediately. Verify resolution from a test client *before*
   changing DHCP, and keep a public resolver as secondary for the first week so
   a failure degrades instead of taking the LAN offline.
2. **`sec-crowdsec`**, then agents outward, starting with whichever host faces
   the internet.
3. **Suricata on the firewall.** No guest required; OPNsense and pfSense both
   bundle it. Run in alert-only mode for a week before enabling blocking, or
   you will block something you need and misattribute the cause.
4. **`sec-wazuh`**, then agents outward from the hypervisor. This is the
   largest single step; do it when you have an unhurried evening. Agents are
   **not** installed by hand: add hosts to the `wazuh_agents` inventory group
   and run `ansible-playbook playbooks/wazuh-agents.yml --limit <host>`. Tune
   the alert volume after the first two hosts, before going wider.
5. **ntopng on the firewall.** No guest required: install `os-redis` and then
   `os-ntopng`, and capture on LAN. Give the firewall VM the extra RAM and CPU
   first. See [ntopng](../components/index.md#ntopng).
6. **`sec-scan`**, weekly schedule, baseline scan first. Change the default
   `admin` password before the feed sync, not after.

## Authenticated bootstrap

Wazuh and CrowdSec require prepared credentials and server certificates
before `--apply`; see [Security bootstrap inputs](../security.md#bootstrap-inputs).
Existing guests use `install/configure-sec-wazuh.sh` or
`install/configure-sec-crowdsec.sh` to apply these settings without reinstalling.
Keep Wazuh enrollment blocked during the initial vendor installation, and
prepare all CrowdSec clients for HTTPS before changing an existing LAPI.

## Host monitoring in Grafana

All four security guests use the same native Alloy collector as the rest of the
fleet. It pushes CPU, memory, filesystem, network, uptime and systemd metrics to
central Prometheus, and journal logs to Loki. Grafana reads those existing
datasources. The onboarding play also installs the pending-package and
reboot-status exporter.

### Inventory

Merge the security guest entries from `ansible/inventory/hosts.example.yml`
into your private `ansible/inventory/hosts.local.yml`, replacing the example
addresses with each guest's DHCP reservation:

| Guest | Platform group under `monitored` | SSH user | VMID |
|---|---|---|---|
| `sec-wazuh` | `vm_debian` | `admin` | 200 |
| `sec-scan` | `vm_debian` | `admin` | 201 |
| `sec-crowdsec` | `lxc_debian` | `root` | 210 |
| `sec-dns` | `lxc_debian` | `root` | 211 |

Also add all four to the `security_guests` overlay under `monitored`. Its
committed group variables set `monitor_role: security` and retain Docker
autodetection. Keep the inventory names above: `monitor_hostname` defaults to
that name, and the dedicated dashboards query those exact `host` labels.

`sec-scan` belongs in `lan_guests` because it needs the jump host to reach the
sandbox. Add any other guest there only if the controller needs a jump host to
reach it. Keep `sec-wazuh` in `wazuh_manager` for agent enrollment checks.
Monitoring membership does not enable automatic patching; that remains a
separate `autoupdate` inventory choice.

### Onboard and verify

Use the same `monitoring_domain` and
`vault_collector_basic_auth_password` as the existing fleet. Each guest needs
DNS resolution and outbound HTTPS to the central ingest endpoint. There is no
inbound exporter port to open: Alloy scrapes locally and pushes through the
authenticated Prometheus and Loki endpoints.

Run from the controller, once the guest's service installer has completed:

```bash
cd ansible                                     # from the repository root
ansible-playbook playbooks/preflight.yml --limit sec-dns
ansible-playbook playbooks/onboard.yml --limit sec-dns
```

Repeat for `sec-crowdsec`, `sec-wazuh` and `sec-scan`. Onboarding checks ingest
reachability, installs the collectors, and queries central Prometheus and Loki
to confirm that data arrived. On `sec-scan`, Docker autodetection adds Greenbone
container CPU, memory and logs. If the collector was installed before Docker,
rerun `playbooks/collectors.yml --limit sec-scan` after installing Greenbone.

Load the committed dashboards and refresh inventory-derived host-down alerts
after onboarding the guests:

```bash
ansible-playbook playbooks/dashboards.yml --limit central
ansible-playbook playbooks/central-alerting.yml --limit central
```

The alerting play reloads Grafana provisioning and can restart Grafana. Its
host-down rule includes every member of `monitored`, so add only guests ready
to report telemetry to the private inventory.

### Grafana views

The **Servers** folder contains `sec-wazuh`, `sec-scan`, `sec-crowdsec` and
`sec-dns`. Each has the standard resource, systemd, pending-update and log
panels; `sec-scan` also has container panels. The existing fleet dashboards
discover their host labels automatically.

In Grafana Explore, confirm the four hosts in Prometheus:

```promql
count by (host, role) (
  node_uname_info{host=~"sec-wazuh|sec-scan|sec-crowdsec|sec-dns"}
)
```

And select each host's journal in Loki:

```logql
{host="sec-dns", job="systemd-journal"}
```

Detailed security alert forwarding is covered separately in
[Wiring](../wiring/index.md).
