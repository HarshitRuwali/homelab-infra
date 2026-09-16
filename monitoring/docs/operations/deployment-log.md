# Deployment log

A record of what was rolled out, in what order, and what it took. Newest
first. The point of this page is not ceremony: it is so the next person to hit
the same wall finds the command that cleared it, and so an unexplained change
in a graph can be lined up against a date.

---

## 2026-09-16: sec-dns, the first guest of the security stack

The security stack starts here. One guest, AdGuard Home on it, then the LAN
pointed at it. Staged: a dry run and an explicit go before each step, which is
also why the two stops below cost nothing.

```bash
# on the hypervisor, as root, after re-copying the script from the repo
./provision-security-stack.sh --only sec-dns            # dry run
./provision-security-stack.sh --apply --only sec-dns
pct set 211 --features nesting=1 && pct reboot 211      # see below
# inside the guest
./install/install-sec-dns.sh                            # dry run
./install/install-sec-dns.sh --apply
# from the controller, once it had a DHCP reservation
ansible-playbook playbooks/preflight.yml --limit sec-dns
ansible-playbook playbooks/onboard.yml --limit sec-dns
ansible-playbook playbooks/site.yml --limit sec-dns     # patching policy
ansible-playbook playbooks/central-alerting.yml --limit central
```

### A new container boots degraded without nesting

`pct create` does not set `features: nesting=1`; the Proxmox UI does, which is
why every other container here already had it. Without it the Debian 13
template's systemd cannot mount `/tmp`, `/run/lock` or the mqueue filesystem,
so the guest came up `degraded` with three failed units. Proxmox does warn at
create time ("Systemd 257 detected. You may need to enable nesting"), and it is
easy to read as advisory.

It is not advisory here: those three failed units would have tripped
`fleet-systemd-unit-failed` the moment the guest joined monitoring, and the
first thing a new host does is join monitoring. The provisioning script now
passes `--features nesting=1` for every LXC it creates.

### Dry runs of onboarding were failing for a different reason

`onboard.yml --check` failed at the ingest reachability test on any new host,
reporting "connection failed" no matter what the network was doing. The cause
was Ansible, not the network: the `uri` module skips itself under `--check`, so
the assert that reads its result found no status at all. The same shape broke
the clock check.

Fixed by marking both read-only probes `check_mode: false`, and by skipping the
post-install "did the data arrive" proofs under `--check`, since a dry run
installs nothing for them to find. A dry run of onboarding now gets as far as a
dry run can: it stops at adding Grafana's APT repository, because `gpg` is
installed by the step before it and check mode only reports that it would.

### Order of operations, and one metric that lags because of it

`site.yml` deliberately runs update-metrics before patching, so the exporter
wrote `fleet_unattended_upgrades_enabled 0` moments before unattended-upgrades
was configured. The hourly timer corrects it; starting
`fleet-update-metrics.service` corrects it immediately. Worth knowing before
reading it as a failed rollout.

### Verification

Metrics and logs both confirmed from the controller by the onboarding play
itself. Afterwards: `up=1` and `role="security"` in central Prometheus, the
host present in Loki's `host` label values, the committed `sec-dns` dashboard
live, 52 rules, and the inventory-derived Host Down rule now covering the
guest. AdGuard resolves and blocks from two different network segments, over
both UDP and TCP, with its upstream on Quad9 DoH.

### The receiver for Suricata, ahead of Suricata itself

Suricata runs on OPNsense, which is FreeBSD and deliberately outside Ansible's
reach here, so enabling it stays manual. The half that is not manual is the
landing point for its alerts, and that did not exist: the wiring page showed
the Alloy block to add but nothing rendered it.

`alloy_collector` now takes `alloy_syslog_listener_port`, empty by default so
no ordinary host opens a port, and set to 5514 in `group_vars/central`. Deployed
with `collectors.yml --limit central`. Port 5514 rather than the wiring page's
old 1514, which is Wazuh's agent event port: different hosts, no real conflict,
and still the wrong thing to meet halfway through an investigation.

The candidate config was rendered to a scratch path and run through
`alloy validate` before deploying, because this restart is the monitoring stack
restarting itself. Worth knowing for that check: `vars_files` outranks play
`vars`, so loading the role defaults to render the template silently reset the
port to empty and validated a config without the block. `-e` is the way.

A test message sent with `logger --tcp --rfc5424` from a host on the firewall's
LAN proved the path before the firewall was pointed at it, and caught a
labelling bug on the way: the line arrived as `host="main-server"`,
`role="central"`. The central collector's `loki.write` stamps everything it
ships with its own identity, so every firewall alert would have looked like
the monitoring box raising it. The listener now sets `host="opnsense"` and
`role="firewall"` itself; labels on the stream win over `external_labels`, and
a second test confirmed it. The first test line keeps the wrong labels until
retention removes it.

Verified end to end once the firewall was pointed at it: a response containing
`uid=0(root)` produced sid 2100498 in Loki as `host="opnsense"` within seconds,
both from an internet server and from a guest on the other side of the
firewall on the same hypervisor. The usual trigger site, `testmynids.org`, is
NXDOMAIN now; the wiring page has the `httpbin.org` replacement.

### Still outstanding

- **30 security updates pending on the new guest.** Its first automatic run is
  the night after onboarding, and `fleet-security-updates-stuck` has a 24 hour
  fuse that started when patching was enabled, so the two nearly coincide. Run
  `force-updates.yml --limit sec-dns` to settle it deliberately.
- **`1.1.1.1` is still the secondary resolver.** That is the documented first
  week, and it means blocking and query logging are partial until it is
  removed. Before removing it: this resolver is a guest, so every hypervisor
  reboot takes the LAN's DNS with it. The kernel update two nights earlier
  would have meant about four minutes without it.
- **Suricata's first-minutes noise.** sid 2019102 (SSDP amplification) fires
  on ordinary UPnP discovery from guests to the firewall, and the `.tk` and
  `.to` DNS rules fire on the torrent client's trackers. Suppress or accept
  them before building anything that pages on Suricata.
- **The rest of the stack is untouched.** sec-crowdsec and sec-wazuh need
  certificates from a CA and a Wazuh enrollment password in the vault before
  they can be installed at all.

## 2026-09-14: guest traffic, the hardened updater, and drift the dry runs caught

Three changes: names for every guest NIC on the hypervisor plus the Guest
Traffic dashboard, the first deployment of the hardened Docker updater (without
`--wait`), and an alert fix so a failed update pages once. Staged, dry run
first, one stage at a time.

```bash
ansible-playbook playbooks/update-metrics.yml --limit t7920   # guest NIC names
ansible-playbook playbooks/dashboards.yml                     # Guest Traffic, 4 security-guest dashboards
ansible-playbook playbooks/central-alerting.yml               # updater excluded from Systemd Unit Failed
# expired 2 dead silences; fixed qdrant's healthcheck on memory (one-off plays)
ansible-playbook playbooks/docker-updates.yml --limit ubuntu-dev -e docker_update_run_now=true
ansible-playbook playbooks/docker-updates.yml
# first real runs on the other five the same day, instead of waiting for 04:00
ansible-playbook playbooks/docker-updates.yml --limit 'autoupdate:!ubuntu-dev' -e docker_update_run_now=true
ansible-playbook playbooks/central-alerting.yml               # OOM runbook, see below
```

!!! note "One expected dry-run failure"
    `update-metrics.yml --check` stopped at enabling `fleet-pve-guests.timer`:
    check mode never writes the unit file, so systemd cannot find the unit it
    is asked to enable. The real run writes it first.

### What the dry runs caught before anything shipped

**The alerting deploy would have rolled back two live fixes.** The silence-link
fix and the ubuntu-dev exclusion were deployed from
`fix/alerting-silences-and-workstation-noise`, which had not been merged into
the branch being deployed. The `--diff` gave it away: it *removed* lines nobody
had written. When a diff deletes lines you did not touch, the box is ahead of
your branch. The fix was merged first, and the redone dry run changed one file.

**plex had moved.** Its container had been moved to the OPNsense LAN on DHCP,
while the inventory still held its old static address. Monitoring never
noticed, because Alloy pushes; only SSH broke. And since `docker-updates.yml`
runs `serial: 1`, that one unreachable host stopped the play for every host
after it.

**qdrant had been "unhealthy" since Sept 4** (28,435 failed probes) while
serving normally. Its healthcheck calls `curl`, which the image does not ship.
The old updater counted unhealthy containers but failed only on restarting
ones; the new one fails the run, so memory would have paged every night. Fixed
on the host by editing the compose file it actually runs, an older copy than
the repo's with different volumes, and in both repo compose files, where the
same check also stopped Compose from ever starting the API behind it.

### What the test run caught

The run-now test on ubuntu-dev failed, correctly. `fastapi-lxc` was started
from a compose file that no longer exists: the project became `api-service` in
`0e43419` and its folder was deleted. On Sept 4, Docker recreated only the
missing logs mount, as root, at the old path. The updater refused to touch a
project it cannot read in full. The rest of that run worked, including the
one-shot `superset-init`, the exact case `--wait` used to fail on.

It also exposed two bugs in the role's run-now path, both fixed:

- Starting a oneshot blocks until it finishes, and the new script exits
  non-zero on failure, so the start step failed first with systemd's generic
  "control process exited" error. The play now reads back what the run wrote
  and fails with the run's own `ERROR` lines.
- "Report what changed" always printed `[]`. Inside a `>-` block, `'\n'`
  reaches Jinja as a backslash and an `n`, so `.split('\n')` never split.
  `.splitlines()` does.

A check of every running Compose project on all six Docker hosts found no
other missing compose file.

With `fastapi-lxc` skipped, the next ubuntu-dev run failed on `superset`
instead, and this one did harm. superset's websocket takes its host port from
`WEBSOCKET_PORT` in whatever shell ran `up`; the project's `.envrc.example`
picks the first free port from 8080. The systemd updater has no such variable,
so Compose resolves `${WEBSOCKET_PORT:-8080}` to 8080, decides the running
websocket container has changed, and recreates it. The new one cannot bind
8080, which `fastapi` holds, so a websocket that was up before the run was
down after it. The rest of superset was untouched, and the websocket was
brought back on 8082, the next free port.

Both projects are now in ubuntu-dev's `docker_update_skip_projects`, with the
reasons beside them in `host_vars/ubuntu-dev.yml`. The general lesson: a
project whose Compose variables come from the interactive shell (direnv, an
`export` before `up`) is not safe to update unattended until they are pinned
in its `.env`, because the updater applies the whole configuration every run.

### An old runbook was flooding Grafana's log

`fleet-container-oom`'s runbook contained
`docker inspect --format '{{.HostConfig.Memory}}'`. Grafana expands
annotations as Go templates, so that failed on every evaluation: 32,945 errors
in the 24 hours before this rollout. The runbook now reads
`docker stats --no-stream <name>`, and the journal has shown none since.

### Verification

- The read-only validation play: **33 of 33 pass**, including a clean first run
  of the new updater on all six Docker hosts (`failed=0`, none unhealthy). No
  image had changed since the previous night, so nothing was restarted.
- All 16 Guest Traffic queries return data against live Prometheus: 15 guest
  NICs named, 13 carrying traffic (the other two belong to stopped guests).
- 52 alert rules live, 0 errors of any kind in Grafana's journal since the last
  restart, and nothing newly firing.

### Still outstanding

- ubuntu-dev skips `fastapi-lxc` (retire it, or redeploy from `api-service`)
  and `superset` (pin `WEBSOCKET_PORT=8082` in its `.env`). Remove each skip
  once fixed.
- A static DHCP mapping for plex in OPNsense, or its address drifts again.
- `rules-backup.yaml` on monitor-lxc holds 5 live backup rules that are not in
  this repo.
- ubuntu-dev's root filesystem is at 96%, mostly unused images and build cache.

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
