# Deployment log

A record of what was rolled out, in what order, and what it took. Newest
first. The point of this page is not ceremony: it is so the next person to hit
the same wall finds the command that cleared it, and so an unexplained change
in a graph can be lined up against a date.

---

## 2026-09-23: Hardware sensors on t7920, and a targeted deploy

CPU package temperatures and chassis fan speeds for the hypervisor, as a
hardware row on the `t7920` dashboard and six rules in `rules-hardware.yaml`.

### Second pass: every fan, by name, on Dell's own map

hwmon only exposes four unnamed fans. The driver's older `/proc/i8k` ioctl
takes any fan index, and SMBIOS names twelve, so `roles/dell_fan_metrics`
reads all of them every 30 s (12 reads in about 10 ms). Mapping was checked
against the live hwmon readings and Dell's Owner's Manual fan figures before
any name went on a dashboard. Result: 9 fans spinning, CPU0 an empty header,
PSU with no tachometer, and FB4 listed by the BIOS but reading 0. That turned
out to be a position, not a fan: the rear FlexBays are optional and this
machine has none (both drives sit in the front bays), so FB4 is drawn as not
fitted and left out of the stall alert.

```bash
ansible-playbook playbooks/update-metrics.yml --limit t7920   # collector
ansible-playbook playbooks/dashboards.yml -e grafana_dashboard_prune=false
# then the same targeted copy of rules-hardware.yaml and restart as below
```

The t7920 dashboard gained a Chassis Fan Map canvas laid out as Dell's two
figures, and System Overview gained a basic hardware row.

### Nothing new to collect

Probed before designing anything. A Precision tower has no IPMI or iDRAC
(`/dev/ipmi*` does not exist), but `coretemp` and `dell_smm_hwmon` were
already loaded, and node_exporter's hwmon collector had been pushing both
since the host was onboarded. The work was dashboards and rules only.

Two findings shaped the rules:

- **The LXC guests push copies of the host's sensors.** Same kernel, same
  `/sys/class/hwmon`, so six hosts reported t7920's fans. Every rule is scoped
  to `role="hypervisor"`.
- **Thresholds come from a week of data.** CPU 1 runs 10 to 15 C hotter than
  CPU 0 (median 72 C against 58 C) and peaked at 89 C, but spent one minute
  above its 83 C rated max all week. Populated fans never dropped below
  759 RPM or rose above 1731. `fan1` read 0 throughout: the CPU0 header,
  which Dell leaves empty on this model (see below).

### Deployed around the playbooks, on purpose

The `--check --diff` runs showed the live central stack running the unmerged
`feat/security-stack` branch. `central-alerting.yml` would have overwritten
the contact points, templates and policies with master's copies, dropping the
`matrix-security` receiver and route that `rules-security.yaml` depends on,
and `dashboards.yml` would have pruned `sec-scan.json` and
`sec-crowdsec.json`. So only the two new files went out:

```bash
ansible-playbook playbooks/dashboards.yml -e grafana_dashboard_prune=false
ansible monitor-lxc -b -m copy -a "src=../monitoring/grafana/provisioning/alerting/rules-hardware.yaml \
  dest=/etc/grafana/provisioning/alerting/rules-hardware.yaml owner=root group=grafana mode=0640"
ansible monitor-lxc -b -m systemd -a "name=grafana-server state=restarted"
```

`grafana_dashboard_prune=false` crashed the role before this (the prune loop
is templated before the block's `when`); fixed with
`subelements('files', skip_missing=True)`.

Verified end to end with a throwaway API rule reusing the CPU query at a
50 C threshold: two alerts (one per socket, no LXC duplicates), routed to
`matrix-homelab`, delivered 37 s after firing, then deleted.

---

## 2026-08-16: SMART disk health, and onboarding the hypervisor

Adding disk health turned out to require onboarding a host that had been
deliberately excluded, because **nothing already monitored can report SMART**.

### Why the fleet could not answer

Probed before designing anything, which is what changed the plan:

| Platform | Hosts | Result |
|---|---|---|
| LXC | tailscale-router, plex, memory, monitor-lxc | disks visible in `/sys` (so `lsblk` lists them) but **no `/dev` nodes** |
| KVM | ubuntu-dev, ubuntu-ai, cloud-services, matrix | `device lacks SMART capability` on the QEMU virtual disk |
| Pi | rpi5, rpi4b | `type=SD`; SD does not implement SMART, and eMMC fields are absent |

The LXC case is the trap: `lsblk` inside those containers lists the real
Seagate and Samsung drives, so it looks like SMART should work. It cannot;
`smartctl` reports `No such device` because the device nodes do not exist in
the container.

The only real disks belong to **t7920**, the Proxmox host, which was out of
inventory by an earlier explicit decision. Onboarded as a fourth platform
group, `metal`, and **deliberately kept out of `autoupdate`**: an unattended
upgrade there restarts pveproxy/pvedaemon underneath every running guest.

```bash
ansible-inventory --host t7920      # monitor_role=hypervisor, smart_metrics_enabled=True
ansible autoupdate --list-hosts     # t7920 absent, as intended

ansible-playbook playbooks/collectors.yml     --limit t7920
ansible-playbook playbooks/update-metrics.yml --limit t7920
```

!!! note "`--check` cannot work against a brand-new host"
    The first dry run failed with `No package matching 'alloy' is available`.
    That is a check-mode artifact, not a fault: check mode does not really add
    the Grafana APT repository, so the package genuinely is not visible yet.
    Verified by confirming no repo file existed, then ran for real.

### What it found immediately

```text
/dev/sda     ST2000DM005-2U9102   18077 power-on hours, 33 C
             reallocated=0  pending=0  offline_uncorrectable=0  CRC=23
/dev/nvme0   PM9A1 NVMe 1TB       20180 power-on hours, 35 C
             life used=23%  spare=100%  media errors=0  unsafe shutdowns=266
```

Both healthy. The 23 CRC errors are a past SATA link event, not a failing
platter, and 266 unsafe shutdowns is worth knowing on a hypervisor.

### A false "we are patched" signal, exposed by the new host

`fleet_unattended_upgrades_enabled` reported **1 for t7920**, a box where
`unattended-upgrade` is not even installed. The exporter tested only
`apt-daily-upgrade.timer`, which ships enabled on stock Debian.

This was invisible while every monitored host was in `autoupdate`, since both
halves were true there. It applies equally to the `no_autoupdate` group, whose
`20auto-upgrades` sets `Unattended-Upgrade "0"` while leaving the timer on.
That group is empty today, which is why it had never surfaced.

The metric now requires the timer **and** an effective
`APT::Periodic::Unattended-Upgrade "1"` **and** the binary to exist, and
`fleet-security-updates-stuck` is gated on it, so it no longer fires on a
hand-patched host with a runbook naming a binary that is not installed.

```bash
ansible-playbook playbooks/update-metrics.yml     # corrected exporter, fleet-wide
ansible-playbook playbooks/central-alerting.yml   # rules-storage + gated rules-updates
# t7920 now correctly reports enabled=0; the other ten still report 1
```

### Verification

- **29** unique PromQL expressions across the t7920 dashboard and the 8 new
  storage rules: all parse and all return data against live Prometheus.
- **8 storage alerts live, all `inactive`.** Notably
  `fleet-smart-crc-errors-rising` is inactive despite the existing 23 errors,
  which is the whole point of alerting on `increase()` rather than `> 0`.
- Fleet-wide: 32 inactive, 1 firing (`rpi5` systemd unit, pre-existing),
  1 pending (`main-server` security updates, genuine and pre-existing).

### Still outstanding

`rpi5` has a failed systemd unit that predates this work and is still firing.

---

## 2026-08-15: per-host dashboards and GPU telemetry

Three changes in one rollout:

1. **Per-host dashboards.** One dedicated dashboard per host in a new `Servers`
   Grafana folder, alongside the existing fleet-wide `Monitoring` folder.
2. **GPU telemetry on `ubuntu-ai`.** The `nvtop` figures (utilization, VRAM,
   temperature, power, fan) via a new `gpu_exporter` role.
3. **Silenced `fleet-load-high` for `tailscale-router`**, which was firing
   permanently.

### Phase 0: preflight

Every host was unreachable on the first attempt except `tailscale-router`:

```bash
cd ansible                 # from the repository root
ansible monitored -m ping
# 9 of 10 UNREACHABLE, "Operation timed out"
```

This was **not** an outage. Tailscale reported every peer `active`, and
`nc -z <ip> 22` succeeded on all of them. The paths were cold DERP relays and
Ansible's default 10-second timeout expired during path setup. Raising the
timeout fixed it permanently:

```bash
ANSIBLE_TIMEOUT=30 ansible monitored -m ping     # 10/10 SUCCESS
```

!!! tip "`ANSIBLE_TIMEOUT=30` for any tailnet-wide play"
    Every command in this rollout used it. Without it the first play of the
    day fails on hosts that are perfectly healthy, which is an expensive way
    to learn that a relay took eleven seconds to come up.

Two facts were confirmed against the hosts before anything was written,
because both were assumptions the work depended on:

```bash
ansible monitor-lxc -m shell -a 'grep MONITOR_HOSTNAME /etc/default/alloy'
#   MONITOR_HOSTNAME="main-server"   <- NOT "monitor-lxc"

ansible ubuntu-ai -m shell -a 'nvidia-smi --query-gpu=name,driver_version --format=csv,noheader'
#   NVIDIA RTX A5000, 595.84
```

The first is why `servers/monitor-lxc.json` queries `host="main-server"`
throughout. Had it been taken on trust, all 16 of its panels would have been
permanently blank. See [Dashboards](../monitoring/dashboards.md).

### Phase 1: the central LXC was 100% full

The fleet-wide dry run failed on `monitor-lxc` only:

```bash
ansible-playbook playbooks/collectors.yml --check --diff
#   monitor-lxc : failed=1
#   E:Write error - write (28: No space left on device)
```

`/` was **7.4G of 7.8G used, 0 bytes available**, with `/var/lib/prometheus`
at 3.1G. All five services still reported `active`, which is exactly why this
had gone unnoticed: nothing crashes, writes just start failing.

Cleared with the two cheapest steps from
[the disk runbook](runbooks.md#disk-usage-high-critical), neither of which
discards monitoring data:

```bash
ansible monitor-lxc -m shell -a 'apt-get clean; journalctl --vacuum-size=64M'
#   100% used, 0 avail  ->  97% used, 234M avail
```

!!! note "Freeing 234M eventually returned 2.1G"
    By the end of the rollout `/` was at **72% used, 2.1G available**. Nothing
    else was deleted. Prometheus had been unable to complete a compaction with
    zero bytes free; given headroom it finished and dropped the old blocks.
    A full disk on the central node is therefore self-reinforcing, and worth
    breaking early.

### Phase 2: collectors, in blast-radius order

The dry run also confirmed GPU autodetection fleet-wide before any host was
touched: `gpu=True` on `ubuntu-ai` alone, `gpu=False` on the other nine.

```bash
ansible-playbook playbooks/collectors.yml --limit ubuntu-dev          # canary
ansible-playbook playbooks/collectors.yml --limit ubuntu-ai           # the GPU host
ansible-playbook playbooks/collectors.yml --limit 'cloud-services,rpi5,rpi4b'
ansible-playbook playbooks/collectors.yml --limit lan_guests          # plex, memory, matrix
ansible-playbook playbooks/collectors.yml --limit tailscale-router    # exit node
ansible-playbook playbooks/collectors.yml --limit monitor-lxc         # LAST
```

| Host | changed | Note |
|---|---|---|
| ubuntu-dev | 0 | canary; config renders byte-identical on a non-GPU host |
| **ubuntu-ai** | **9** | exporter installed, Alloy gained the GPU scrape block |
| cloud-services, rpi5, rpi4b | 1 each | |
| plex, matrix | 0 | |
| memory | 1 | |
| tailscale-router | 0 | |
| monitor-lxc | 1 | |

`changed=0` on the canary is the important number: it proves the template
change is inert on hosts without a GPU, so nine machines got no Alloy restart.

### Phase 3: Grafana

Dashboards are not handled by any role, so they go by the copy path documented
in [Dashboards](../monitoring/dashboards.md). The two provider directories
must be disjoint, because Grafana scans each provider path **recursively**:

```bash
ansible monitor-lxc -m file -a 'path=/var/lib/grafana/dashboards/fleet   state=directory owner=grafana group=grafana mode=0755'
ansible monitor-lxc -m file -a 'path=/var/lib/grafana/dashboards/servers state=directory owner=grafana group=grafana mode=0755'

ansible monitor-lxc -m copy -a 'src=../grafana/dashboards/fleet/   dest=/var/lib/grafana/dashboards/fleet/   owner=grafana group=grafana mode=0644'
ansible monitor-lxc -m copy -a 'src=../grafana/dashboards/servers/ dest=/var/lib/grafana/dashboards/servers/ owner=grafana group=grafana mode=0644'
ansible monitor-lxc -m copy -a 'src=../grafana/provisioning/dashboards/dashboards.yml dest=/etc/grafana/provisioning/dashboards/dashboards.yml owner=root group=grafana mode=0644'
```

The three fleet dashboards moved from the root of the tree into `fleet/`, so
the old copies at the root were removed. No provider reads that path any more,
but leaving them invites a future reader to edit a file nothing loads:

```bash
for f in services-monitoring.json system-overview.json vm-fleet-overview.json; do
  ansible monitor-lxc -m file -a "path=/var/lib/grafana/dashboards/$f state=absent"
done
```

The alert rule change is a different mechanism: `rules-resources.yaml` is one
of `grafana_alerting_static_files`, so it ships with the role, which also
restarts Grafana. Running it last meant a single restart picked up both the
new provider config and the new rule:

```bash
ansible-playbook playbooks/central-alerting.yml    # changed=2
```

### Phase 4: verification

Against the live stack, not the repo:

```bash
# 13 dashboards, correctly split, no duplicate-provisioning errors
curl -s -u "$AUTH" 'http://127.0.0.1:3000/api/search?type=dash-db&limit=100'
#   Monitoring [3], Servers [10]

# GPU metrics arriving in CENTRAL Prometheus, labelled host=ubuntu-ai
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=nvidia_smi_temperature_gpu'

# the load alert now evaluates 9 hosts, not 10
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode \
  'query=max by (host) (node_load15{host!="tailscale-router"}) / on (host) count by (host) (count by (host, cpu) (node_cpu_seconds_total{mode="idle",host!="tailscale-router"}))'
```

Results:

- **13 dashboards**: 3 in `Monitoring`, 10 in `Servers`, zero provisioning errors.
- **GPU**: all five `nvidia_smi_*` families present with `host="ubuntu-ai"`.
  Observed mid-rollout at 22.2 GB VRAM / 55 °C under load, and 454 MB / 33 °C
  after it finished, so the panels were confirmed against both states.
- **Per-host dashboards**: the primary CPU query returned data on all ten,
  `main-server` included.
- **Load alert**: 9 hosts evaluated, `tailscale-router` absent. Its real value
  is **2.5 load/core** against a threshold of 2, so it had been firing
  continuously; the next-highest host in the fleet is 1.25.
- **Fleet**: `alloy` active on all 10; `grafana-server`, `prometheus`, `loki`,
  `alloy`, `nginx` all active centrally.

### Still outstanding

**`/var/lib/prometheus` is 3.1G on a 7.8G disk.** The 72% figure above is
healthy today, but nothing about the rollout changed retention, and the
incident in Phase 1 will recur as the TSDB grows. The durable fix is a
retention decision, not a cleanup: see [Retention](retention.md).
