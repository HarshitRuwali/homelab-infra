# Alerting

Provisioned rules in the `Fleet` and `Security` folders, routed to a
self-hosted Matrix room through a local relay. Most are committed under
`grafana/provisioning/alerting/`; the availability rules are generated from the
inventory, so adding a host cannot leave a silent gap in down-detection.

## Rule catalogue

=== "Availability (6)"

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-host-down` | critical | 0m | no metrics for 10 min ([or-chain](../architecture/push-model.md)) |
    | `fleet-collector-lagging` | warning | 5m | data arriving but >15 min stale |
    | `fleet-remote-write-failing` | warning | 10m | Alloy failing to push samples |
    | `fleet-prometheus-out-of-order` | warning | 30m | Prometheus still accepting out-of-order samples after 30m: two collectors probably send identical series |
    | `fleet-clock-unsynced` | warning | 30m | NTP not synchronised |
    | `fleet-central-stack-down` | critical | 2m | a core service on the central LXC is not active |

=== "Resources (9)"

    | uid | Severity | For | Threshold |
    |---|---|---|---|
    | `fleet-disk-high` | warning | 15m | filesystem over **95%** |
    | `fleet-disk-critical` | critical | 5m | filesystem over **98%** |
    | `fleet-disk-will-fill` | warning | 1h | trending to full in 24h **and** under 10% free |
    | `fleet-memory-high` | warning | 15m | available memory under 10% |
    | `fleet-oom-kills` | critical | 0m | kernel OOM killer active in last 15 min |
    | `fleet-load-high` | warning | 20m | load15 over 2× core count; `tailscale-router` and the LXCs on t7920 excluded (lxcfs reports the hypervisor's load inside them) |
    | `fleet-filesystem-readonly` | critical | 5m | kernel remounted a filesystem read-only after an I/O error |
    | `fleet-inodes-high` | warning | 15m | over **90%** of the inode limit, which fails writes while space looks free |
    | `wazuh-indexer-disk` | warning | 15m | `sec-wazuh` root over **80%**; OpenSearch stops writing at 90 to 95%, before `fleet-disk-high` would fire |

=== "Updates (6)"

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-security-updates-stuck` | warning | 24h | security updates pending a full day |
    | `fleet-apt-cache-stale` | warning | 1h | `apt-get update` has not succeeded in 3 days, so pending counts are untrustworthy |
    | `fleet-autoupdates-disabled` | warning | 1h | config drifted or timer masked |
    | `fleet-reboot-required-too-long` | warning | 1h | reboot outstanding 7 days |
    | `fleet-dpkg-wedged` | critical | 30m | dpkg needs `--configure -a` |
    | `fleet-update-metrics-stale` | warning | 30m | exporter stopped writing |

=== "Containers (7)"

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-container-restart-loop` | warning | 5m | >2 restarts in 30 min |
    | `fleet-container-oom` | critical | 0m | container OOM-killed in last 15 min |
    | `fleet-container-memory-near-limit` | warning | 15m | over 90% of its own limit |
    | `fleet-container-unhealthy` | warning | 10m | was healthy, now failing ([why that matters](container-metrics.md#container_health_state-does-not-mean-what-it-looks-like)) |
    | `fleet-container-disappeared` | warning | 10m | container gone while host metrics are still arriving, non-workstation hosts; excludes the temporary S3 backup runner |
    | `fleet-docker-update-failed` | critical | 0m | nightly update failed or left restarts, non-workstation hosts |
    | `fleet-docker-update-stale` | warning | 1h | no successful update in 50h |

=== "Services (3)"

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-systemd-unit-failed` | warning | 10m | a unit is failed, excluding known-chronic ones |
    | `fleet-unattended-upgrades-errors` | warning | 0m | errors in the journal (Loki query) |
    | `fleet-systemd-unit-restart-loop` | warning | 10m | >5 restarts in 30 min; a flapping unit reads `active` between crashes, so the failed-unit rule never sees it |

=== "Storage (8)"

    SMART disk health. Only hosts in the `metal` group can produce these
    series, so every rule uses `noDataState: OK`.

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-smart-health-failed` | critical | 5m | the drive's own self-assessment failed |
    | `fleet-smart-pending-sectors` | critical | 15m | unreadable sectors not yet remapped |
    | `fleet-smart-reallocated-sectors` | warning | 30m | any remapped bad sector |
    | `fleet-smart-crc-errors-rising` | warning | 0m | **new** SATA link errors in 24h, not the lifetime count |
    | `fleet-smart-nvme-wearout` | warning | 1h | over 85% of rated write endurance |
    | `fleet-smart-nvme-spare-low` | critical | 15m | under 10% spare blocks left |
    | `fleet-smart-temperature-high` | warning | 30m | over 60 C |
    | `fleet-smart-collector-stale` | warning | 30m | no refresh in an hour on a host that has disks |

=== "Backup (5)"

    Off-site backup health, from the s3-backup module's textfile metrics
    (`s3_backup.prom`). Only the host that runs the backup produces these
    series, so every rule uses `noDataState: OK`.

    | uid | Severity | For | Rule |
    |---|---|---|---|
    | `fleet-backup-not-succeeded` | critical | 1h | Backup Has Not Succeeded |
    | `fleet-backup-failed` | warning | 5m | Backup Failed |
    | `fleet-backup-phase-failed` | warning | 5m | Backup Phase Failed |
    | `fleet-backup-duration-anomalous` | info | 5m | Backup Duration Anomalous |
    | `fleet-backup-metrics-stale` | warning | 30m | Backup Metrics Stale |

=== "Network (5)"

    Every rule excludes virtual interfaces (`veth`, `tap`, `fwbr`, `vmbr`,
    `docker`, `cni`), which churn constantly on Docker and PVE hosts.

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-net-interface-errors` | warning | 15m | any sustained rx/tx error rate |
    | `fleet-net-interface-drops` | warning | 15m | over 1 dropped packet/s |
    | `fleet-net-link-lost` | critical | 5m | an interface that was up in the last 6h is now down |
    | `fleet-net-conntrack-near-limit` | warning | 10m | conntrack table over 85% |
    | `fleet-net-tcp-retransmits` | warning | 20m | over 5% of segments retransmitted, above a traffic floor |

=== "GPU (5)"

    From `roles/gpu_exporter`. Liveness keys on `up{job="nvidia-gpu"}`, not on
    absent `nvidia_smi_*` series.

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-gpu-temperature-critical` | critical | 10m | at or above 87 C, where NVIDIA parts throttle |
    | `fleet-gpu-temperature-high` | warning | 30m | above 80 C |
    | `fleet-gpu-vram-exhausted` | warning | 15m | under 5% VRAM free |
    | `fleet-gpu-fan-stopped` | critical | 15m | fan reads zero while the card is over 70 C |
    | `fleet-gpu-exporter-down` | warning | 15m | the exporter stopped responding |

=== "Hardware (6)"

    Chassis sensors on the hypervisor (a Dell Precision 7920 Tower).
    Temperatures come from node_exporter's stock hwmon collector (`coretemp`
    for the CPU packages, `dell_smm_hwmon` for the board sensors); fans come
    from `roles/dell_fan_metrics`, which reads all twelve the firmware knows,
    by name. No IPMI or iDRAC exists on a Precision tower. Every query is
    scoped to `role="hypervisor"`, because the LXC guests share the host's
    kernel and push identical copies of the hwmon sensors. Liveness keys on
    the host still pushing `node_uname_info` while the sensors are gone or the
    fan data has gone stale.

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-hw-cpu-temperature-critical` | critical | 5m | a CPU package at or above 90 C (TjMax is 93 C) |
    | `fleet-hw-cpu-temperature-high` | warning | 15m | a CPU package above its 83 C rated max |
    | `fleet-hw-fan-stalled` | critical | 5m | a fitted fan under 500 RPM; the BIOS never parks them. Excludes CPU0 (no fan), PSU (no tachometer) and FB2-FB4 (listed by the BIOS since 2026-10-01, but no fan is fitted in those bays) |
    | `fleet-hw-fans-high` | warning | 30m | a 120 mm fan above 3000 RPM (75% of max): the BIOS is compensating |
    | `fleet-hw-board-temperature-high` | warning | 30m | a Dell SMM board sensor above 65 C |
    | `fleet-hw-sensors-missing` | warning | 15m | the host reports but its CPU sensors do not, or fan data is over 5 min old |

=== "Security (1)"

    Routed to the **security room**, never the fleet room. See
    [Security alerts](#security-alerts).

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `security-suricata-alert` | ET severity 1 is critical, 2 is warning | 0m | Suricata on the firewall raised a severity 1 or 2 alert in the last 5 min, known-noise SIDs excluded |

=== "Tenant and lease"

    Work VMs (interface `vtnet2`) and leased VMs (`vtnet5`), from
    `rules-tenant.yaml` in the `Security` folder. They go to the **fleet
    room** by severity, not to the security room; see
    [Tenant and lease alerts](#tenant-and-lease-alerts). Each rule opens a panel
    of the [Tenant Activity dashboard](dashboards.md#tenant-activity).

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `tenant-pf-state-table-high` | warning | 10m | the firewall's pf state table is over **50%** full |
    | `tenant-pf-state-table-critical` | critical | 5m | the state table is over **80%** full; at 100% new connections fail on every segment |
    | `tenant-pf-src-limit-hit` | warning | 0m | a source hit its per-source state or connection limit in the last 10 min |
    | `tenant-pf-state-limit-hit` | warning | 0m | pf refused to create a state because a state limit was reached in the last 10 min |
    | `tenant-pf-source-banned` | critical | 0m | a source is in the `virusprot` table: banned for flooding, cut off for up to an hour |
    | `tenant-pf-stats-stopped` | critical | 5m | fewer than 5 `pfstats` lines in 10 min, **and no data also fires**: the dead-man switch for every `pfstats` rule above |
    | `tenant-lease-packet-rate-high` | warning | 5m | lease interface receiving over **15000** packets/s, summed over every lease: 75% of one lease's 20000 packets/s ceiling |
    | `tenant-work-packet-rate-high` | warning | 5m | work interface receiving over **50000** packets/s |
    | `tenant-lease-packet-limit-reached` | warning | 5m | one leased VM has averaged more than 100 packets/s dropped by its own packet-rate ceiling over 15 min, the exact per-guest counter |
    | `tenant-lease-packet-limit-missing` | critical | 10m | a running lease has lost its packet-rate ceiling (the gauge reads 0), so nothing limits it |
    | `tenant-firewall-not-filtering` | critical | 10m | the Proxmox firewall is not filtering a tenant or lease VM (the gauge reads 0), so ipfilter, macfilter and the group rules do not apply to it |
    | `tenant-lease-suricata-alert` | critical | 0m | a **severity 1** Suricata alert on `vtnet5` in the last 10 min |
    | `tenant-lease-suricata-alerts` | warning | 0m | 3 or more Suricata alerts of any severity on `vtnet5` in the last 10 min; the git-over-SSH false positive lands here |
    | `tenant-work-suricata-alerts` | warning | 0m | 5 or more Suricata alerts on `vtnet2` in the last 10 min |
    | `tenant-smtp-attempt` | critical | 0m | a tenant or lease VM tried port 25 and the Proxmox firewall dropped it (log, last 10 min) |
    | `tenant-private-address-probing` | warning | 0m | over 20 **outbound** packets from tenant or lease VMs to private addresses dropped in 10 min (the OUT chains only), Tailscale's UDP 41641 excluded |
    | `tenant-dns-nxdomain-storm` | warning | 0m | over 300 NXDOMAIN replies to work or lease clients in 5 min |
    | `tenant-new-flow-rate-high` | warning | 5m | over **3000** new flows a minute on the work and lease interfaces |
    | `tenant-conntrack-high` | warning | 10m | one tenant or lease VM holds over **20000** conntrack entries on the hypervisor |
    | `tenant-guest-egress-high` | warning | 0m | a guest on the tenant or lease bridges (`vmbr2`, `vmbr5`) averaged over **10 MB/s** sent for 15 min; the lease cap is 12.5 MB/s |
    | `tenant-smtp-dropped-counter` | critical | 0m | the Proxmox firewall's `smtp` rule dropped packets in the last 10 min (the exact counter behind `tenant-smtp-attempt`) |
    | `tenant-guest-exporter-stale` | warning | 5m | the Proxmox guest exporter has not completed a run for over 20 min; a failed read keeps the old file, so its age is how a failure shows |

## Rule structure

Every rule uses the same A→B→C shape:

```yaml
data:
  - refId: A          # the PromQL or LogQL query
  - refId: B          # reduce: last
  - refId: C          # threshold
condition: C
noDataState: OK
execErrState: Error
```

`noDataState: OK` is the right default here **because** the rules that must
never go NoData are built not to. See
[The push model](../architecture/push-model.md).

## Noise control

!!! tip "Exclude in git, do not silence in the UI"
    A Grafana silence expires and has to be renewed by hand. An exclusion in
    the YAML is version-controlled and self-documenting.

`fleet-systemd-unit-failed` excludes units that fail permanently and
harmlessly. A unit that is failed and will stay failed produces one instance
per evaluation forever, pure noise that trains you to ignore the rule that
would have caught a real failure.

| Excluded | Why |
|---|---|
| `NetworkManager-wait-online` | times out at boot, network fine afterwards |
| `systemd-*`, `user@*`, `*-cleanup` | routinely transient |
| `apt-daily*`, `man-db`, `logrotate`, `e2scrub_reap` | maintenance jobs that retry |
| `fleet-docker-update` | not harmless, already covered: **Container Update Failed** pages for the same failure, so this would page twice, and would page for workstations, which that rule skips on purpose |

Thresholds were raised deliberately: a homelab routinely runs boxes in the
high 80s, and a warning that is always on is a warning nobody reads.

## Silences that do not silence

A Grafana silence matches on the **alert instance's** labels, and an instance
carries far fewer labels than the metrics behind it. Every rule here ends in an
aggregation, and `max by (host) (...)` throws away every label except `host`. A
matcher on anything the aggregation dropped matches nothing, and Grafana does
not warn you: the silence is created, sits there looking active, and suppresses
nothing.

The labels you can actually match on are:

| Label | Comes from | Example |
|---|---|---|
| `alertname` | the rule's **title**, not its uid | `Container Update Failed` |
| `grafana_folder` | the provisioning folder | `Fleet` |
| `__alert_rule_uid__` | Grafana, reserved | `fleet-docker-update-failed` |
| `__alert_rule_namespace_uid__` | Grafana, reserved | the folder's uid |
| `severity` | the rule's own `labels:` block | `critical` |
| whatever survives the final `by (...)` | the query | `host`, and sometimes `name`, `device`, `mountpoint`, `uuid`, `cpu` |

!!! bug "Markdown used to eat the underscores in the silence link"
    The rule uid label is `__alert_rule_uid__`, with two underscores each side.
    `matrix-webhook` renders the notification body with Python-Markdown, where
    `__x__` means bold, so the silence URL arrived with
    `<strong>alert_rule_uid</strong>` in it and all four underscores gone.
    Following that link pre-filled a silence matching a label that does not
    exist. The silence saved happily, showed **Active**, and suppressed nothing.

    Fixed by emitting the URL as a markdown autolink, `<{{ .SilenceURL }}>`,
    whose contents markdown leaves alone. See `templates.yaml`.

    Any silence created from an old notification is still broken. Delete and
    recreate it. You can spot one from two columns of the Silences list:

    | Column | Healthy | Broken |
    |---|---|---|
    | **Alert rule targeted** | the rule's name | `None` |
    | **Alerts silenced** | 1 or more while firing | `0` |

    `None` means Grafana could not resolve a `__alert_rule_uid__` matcher, so
    there isn't one.

!!! bug "`role` is never silenceable"
    It is an Alloy external label, so it exists on the raw series and works
    inside a rule expression, which is how `role!="workstation"` filters work.
    But `max by (host)` drops it before the alert instance is created, so a
    silence matching `role=workstation` never fires.

!!! tip "Two reliable ways to get the matchers right"
    **Alert rule → ⋮ → Silence notifications** in the Grafana UI, which never
    goes through the Matrix relay and so is never mangled. Or silence on
    `alertname` plus `host`, which contain no underscores and cannot be
    corrupted by any renderer.

### Proving whether a silence matched

Ask the embedded Alertmanager directly, on the central box. An instance that a
silence caught has `state: suppressed` and a populated `silencedBy`:
`curl -u admin` prompts for the Grafana admin password, which keeps it out of
shell history and out of the process list:

```bash
curl -s -u admin \
  http://127.0.0.1:3000/api/alertmanager/grafana/api/v2/alerts \
  | jq '.[] | {alertname: .labels.alertname, labels, state: .status.state,
               silencedBy: .status.silencedBy}'
```

If `state` is `active` rather than `suppressed`, compare the `labels` printed
there against your silence's matchers; they disagree somewhere.

```bash
curl -s -u admin \
  http://127.0.0.1:3000/api/alertmanager/grafana/api/v2/silences \
  | jq '.[] | {id, state: .status.state, startsAt, endsAt,
               matchers: [.matchers[] | "\(.name)\(if .isEqual then "=" else "!=" end)\(.value)"]}'
```

!!! warning "Two more ways a silence quietly does nothing"
    **It expired, or never started.** Silences are absolute timestamps. If the
    central box's clock has drifted, a silence created "now" in the browser can
    start in the future. `fleet-clock-unsynced` exists for this; check
    `timedatectl` on the box before blaming Grafana.

    **It was lost in a restart.** Silence state lives in Grafana's database and
    is flushed periodically, not on every write. `central-alerting.yml`
    restarts Grafana every run, so a silence created minutes before a playbook
    run can disappear with it.

This is the practical argument behind the tip above. A silence is the right
tool for something genuinely temporary, like a maintenance window. For a host
that will *always* be noisy, exclude it in the rule expression, where it is
reviewable and cannot expire. `fleet-container-disappeared` and
`fleet-docker-update-failed` both do this with `role!="workstation"`.

## Routing

```yaml
group_by: [alertname, host]
group_wait: 30s
group_interval: 5m
repeat_interval: 12h

routes:
  - severity = critical:  group_wait 10s, group_interval 2m, repeat 2h
  - severity = info:      group_wait 5m,  repeat 24h, muted overnight
```

`group_by` includes `host` so a fleet-wide event arrives as one grouped
message per alertname rather than one per machine. Warning and critical are
**never** muted.

!!! bug "Mute timings cannot cross midnight"
    Grafana rejects `start_time: '23:00', end_time: '07:00'`. Split it:

    ```yaml
    - times: [{start_time: '23:00', end_time: '24:00'}]
    - times: [{start_time: '00:00', end_time: '07:00'}]
    ```

    And that single error aborts provisioning for **every** alerting file.

## Security alerts

Suricata alerts go to their own Matrix room, so a real detection is not
buried under disk and update warnings, and fleet noise never buries a
detection. The chain: Suricata EVE on the firewall, syslog on 5514/tcp into
Loki as `host="opnsense"`, `job="suricata"` ([wiring](https://harshitruwali.github.io/homelab-infra/security/wiring/)),
the `security-suricata-alert` rule, then the first route in `policies.yaml`:

```yaml
- receiver: matrix-security       # same relay, room_id=$__env{MATRIX_SECURITY_ROOM_ID}
  matchers: [route = security]
  group_by: [alertname, alert_signature_id]
  group_wait: 30s
  group_interval: 15m
  repeat_interval: 24h
  continue: false                 # never also posted to the fleet room
```

One message per **signature**, listing up to 10 flows with counts, a
dashboard link pre-filtered to that SID, and a silence link. Resolve messages
are off: an IDS alert does not "recover", and a second message saying so is
noise.

**What does not page.** ET severity 3, which is mostly protocol trivia; it
stays on the [Suricata Alerts dashboard](dashboards.md#suricata-alerts). And
SIDs excluded in the rule's query as known noise in this network (UPnP SSDP
discovery, the torrent client resolving `.tk` and `.to` trackers). They are
listed with reasons in `rules-security.yaml`. Noise with no investigative
value is better disabled in OPNsense by SID; exclude in the rule only what
should stay visible on the dashboard.

**Timing.** A new signature arrives about 30 seconds after the alert.
Further flows of the **same** signature wait for the 15 minute group
interval, so a burst becomes one follow-up, not a stream.

### Setting up the room

1. Create a private room. Leave encryption off: the relay posts through the
   plain API, and an encrypted room can show its messages as undecryptable.
2. Invite `@grafana-alerts:<homeserver>`. The relay joins on its first post.
3. Copy the internal ID (Room settings, Advanced; it starts with `!`) into
   the vault as `vault_matrix_security_room_id`, quoted, since a bare `!` is
   a YAML tag.
4. `ansible-playbook playbooks/central-alerting.yml`. It refuses to run
   without the ID rather than deploy a contact point that posts nowhere.

### Testing it

```bash
# From any host whose traffic crosses the firewall. Trips sid 2100498.
curl "http://httpbin.org/base64/$(printf 'uid=0(root) gid=0(root) groups=0(root)\n' | base64)"
```

A "GPL ATTACK_RESPONSE id check returned root" message should reach the room
within about two minutes. The relay alone can be tested as in
[Testing the chain](#testing-the-chain), with the security room's ID.

!!! bug "Use `.Labels`, not `$labels`, in a provisioned rule's labels"
    Grafana expands environment variables in provisioned **label** values
    (not in annotations or templates), so `$labels.alert_severity` became
    `.alert_severity`. The template then failed, and the raw
    `{{ if ... }}` text shipped as the label, into the message and the silence
    link. `.Labels.alert_severity` is the same data with no `$` to expand.

## Tenant and lease alerts

The firewall and the hypervisor log what the work VMs and the leased VMs do,
and these rules read that data for abuse, floods and a firewall under strain.
It is not a complete record: the wiring page lists
[what is not covered](https://harshitruwali.github.io/homelab-infra/security/wiring/#what-is-not-covered),
and every count here is a floor. The streams (their wiring is in the security
module's [wiring page](https://harshitruwali.github.io/homelab-infra/security/wiring/)):

| Stream | What it holds | Used by |
|---|---|---|
| `{job="pfstats"}` | one logfmt line a minute from the firewall: state table, limit hits, `banned`, per-interface counters; the producer is in [`security/opnsense/pfstats/`](https://harshitruwali.github.io/homelab-infra/security/wiring/#pfstats) | the state table, limit, ban, dead-man and packet rate rules |
| `{job="filterlog"}` | pf's filter log: a line per new flow a logging pass rule lets through on `vtnet2` and `vtnet5`, and per block; lines from a banned source are rate-limited at ingest | the new flow rate rule |
| `{job="unbound"}` | the resolver's query and reply lines, plain DNS on port 53 only | the NXDOMAIN rule |
| `{job="suricata"}` | EVE alerts; `in_iface` says which interface saw it | the two Suricata rules |
| `{job="pve-firewall"}` | what the Proxmox firewall dropped, with `chain` and `vmid` labels, at most one line a second per rule | the SMTP and private-address rules |
| `fleet_pve_*` | Prometheus series from `roles/pve_guest_metrics`, listed in the [metrics catalogue](../reference/metrics.md#proxmox-guest-nics) | the conntrack, egress, SMTP counter, packet limit and exporter staleness rules |

!!! info "Routed by severity, not to the security room"
    These rules carry no `route: security`. The `matrix-security` contact point
    and its template are written for Suricata signatures, which these rules do
    not have, so a message through it would have an empty title. Critical rules
    take the fast route to the fleet room (repeat every 2 hours), warnings the
    default route. Aggregated Loki results carry no labels, so each rule sets a
    static `host` (`opnsense` or `t7920`) and the message reads "... on
    opnsense".

!!! warning "`tenant-pf-stats-stopped` is the only rule that alerts on no data"
    Every other rule here reads **nothing** in the healthy case: no SMTP
    attempt, no probing and no alert all return an empty result, which is
    `noDataState: OK`. The dead-man rule inverts that, because an empty
    `pfstats` window is how a broken sender, syslog path or Loki looks, and
    in that state the state table, limit and ban rules would all report
    healthy.

**Counters reset.** `src_limit_hits`, `state_limit_hits` and the interface
counters are cumulative since the firewall booted. Every rule that reads one
uses `rate_counter`, which treats a drop as a reset, so a firewall reboot is not
read as a hit and shows at most as one low sample.

**Tailscale is excluded on purpose.** The work VM's Tailscale client probes
LAN peers on UDP 41641 about every 5 seconds, which the Proxmox firewall
drops and logs. `tenant-private-address-probing` and the Blocked Attempts
panels filter out lines containing `DPT=41641 `, trailing space included, so
the match ends at the end of the field.

**Proxmox rate-limits its own log.** Each log rule writes at most one line per
second, so `tenant-smtp-attempt` and `tenant-private-address-probing` count
log lines, not packets; zero versus non-zero is the signal. The exact count is
`fleet_pve_firewall_dropped_packets_total`, which `tenant-smtp-dropped-counter`
reads. The counter has a `family` label (`ipv4` or `ipv6`) that the rule sums
over, so an IPv6 SMTP attempt counts too.

**Only outbound drops name a guest.** `tenant-private-address-probing` counts
the `GROUP-tenant-OUT` and `GROUP-lease-OUT` chains only. A drop in an IN chain
is a packet *to* a VM, labelled with the receiving VM, and another guest can
cause it (multicast, for one), so the VM named would not be the one probing.
The Blocked Attempts panels still show the IN chains.

**The lease packet ceiling has three rules.** Each lease is policed to 20000
packets/s on its own tap device, so one lease can never sustain more than that
and the summed segment rate can only show it near 15000 or more.
`tenant-lease-packet-rate-high` therefore fires at 75% of the ceiling, not at
the ceiling. `tenant-lease-packet-limit-reached` reads the exact counter of
packets the ceiling dropped, per guest, as a 15 minute average: the exporter
writes a new value only every 5 minutes, with up to a minute of jitter, so a
shorter window can hold no update and read 0. `tenant-lease-packet-limit-missing` is
the other failure: Proxmox's hot NIC update deletes the police filter, and a
lease without it has no ceiling. Its gauge is `0` for such a lease, and no data
is normal where no lease runs, so it does not alert on no data.

**A firewall that is not filtering looks like a quiet VM.** Every host-side
protection of a tenant or lease VM (ipfilter, macfilter, the SMTP and private
range drops) comes from the Proxmox firewall, and the drop rules in this file
go quiet for a VM it is not filtering. `tenant-firewall-not-filtering` reads
`fleet_pve_tenant_firewall_active`, which is `1` only when the datacenter
firewall is on, the VM has `firewall=1` on `net0`, its `.fw` file has
`enable: 1` and the right group, and its tap chain exists. No data is normal
where no tenant VM runs, so it does not alert on no data.

**A silent exporter looks like healthy series.** The hypervisor series come
from one script that keeps the previous file when a read fails, so a broken run
leaves old values in place rather than gaps. `tenant-guest-exporter-stale`
watches the one value that stops moving, the collection timestamp, the same
way `fleet-smart-collector-stale` does for SMART.

**Suricata on the lease segment is split by severity.** A severity 1 alert is
critical. Any other alert counts towards a warning at 3 or more in 10 minutes,
because the known git-over-SSH false positive (ET SCAN Potential SSH Scan
OUTBOUND) is not severity 1 and would otherwise page.

**`tenant-guest-egress-high` covers only the tenant and lease bridges.** It
reads each guest's tap device with the same name join as
[Guest Traffic](dashboards.md#guest-traffic), and selects `fleet_pve_guest_nic_info`
on its `bridge` label with `vmbr(2|5)`, so the firewall VM's own taps, which
carry any large download on the LAN, are not counted. If a bridge is renamed or
a segment moves, change the matcher in the rule, in the Egress per Guest panel
and in the Guest picker.

**What these rules do not see.** Only plain DNS on port 53 reaches Unbound: DNS
over TLS (TCP 853) is blocked on the lease segment only, DNS over QUIC (UDP 853)
is being blocked there, and DNS over HTTPS, or DNS inside a tenant's own
Tailscale tunnel, is not visible. Traffic inside a tenant's Tailscale tunnel, or
through their exit node, is one flow to a Tailscale relay or peer, not the inner
destinations. A quiet rule is not proof of a quiet VM; see
[what is not covered](https://harshitruwali.github.io/homelab-infra/security/wiring/#what-is-not-covered).

**Loki expressions use a single-capture regexp, not `| logfmt`.** On a `pfstats`
line `logfmt` turns every field into a label, so an aggregation sees dozens of
series. The rules extract one number as `v`. Edit one of these expressions only
after running it in Explore.

**Retention.** These rules look back at most 10 minutes, but the dashboard and
any investigation of a leased VM look back weeks. See
[Retention](../operations/retention.md#loki-retention-for-the-tenant-logs).

### Testing it

The harmless trigger is an SMTP attempt, because the Proxmox firewall drops it.
From a work or lease VM, open a connection to port 25 of any public address:

```bash
timeout 3 bash -c '</dev/tcp/192.0.2.1/25' || true   # 192.0.2.1 is a documentation address
```

`tenant-smtp-attempt` should reach the fleet room within a few minutes of the
attempt, and is critical, so tell whoever reads that room first. The Blocked
Attempts by Chain and VM panel on Tenant Activity should show the `vmid` and a
`GROUP-tenant-OUT` or `GROUP-lease-OUT` chain.

## Provisioned rules are not UI rules

!!! danger "Two behaviours that surprise people"
    **Provisioned rules are read-only in the UI.** To quiet one temporarily
    use a Silence, not an edit.

    **Deleting them in the UI does stick, and leaves you unalerted.** Grafana
    reads provisioning only at startup, so nothing puts them back. The symptom
    is an empty `/api/v1/provisioning/alert-rules` while
    `journalctl -u grafana-server | grep "Sending alerts"` shows the scheduler
    still working through its last in-memory copy.

    Restore with `ansible-playbook playbooks/central-alerting.yml`.

## The Matrix relay

`matrix-webhook` runs on the central LXC as a native systemd service in a venv,
listening on `127.0.0.1:4785`. Grafana's webhook contact point posts to it and
it forwards to the room.

!!! warning "Its env var names are not what the README says"
    The actual names, confirmed with `--help`, are `API_KEY`, `HOST` and
    `PORT`: not `MATRIX_API_KEY` / `API_HOST` / `API_PORT`. Getting this
    wrong crash-loops the unit.

Secrets are injected into Grafana's `EnvironmentFile` so `$__env{}` in
`contactpoints.yaml` resolves. They must be in the unit's environment, not
just your shell.

The relay logs nothing on a successful delivery, so zero journal lines is
**not** evidence that nothing was sent.

## Testing the chain

Escalating, so each step isolates one link.

```bash
# 1. Relay only. Proves bot login and room membership.
ssh <monitor-lxc> 'curl -s -X POST \
  "http://127.0.0.1:4785/?formatter=grafana&key=$KEY&room_id=$ROOM" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"smoke\",\"message\":\"relay alive\"}"'
# expect: {"status": 200, "ret": "OK"}

# 2. Grafana -> Alerting -> Contact points -> matrix-homelab -> Test.
#    Proves $__env{} resolution and loopback reachability.

# 3. Rule -> policy -> relay, end to end and reversible.
ssh rpi4b 'sudo systemd-run --unit=alert-smoke-test /bin/false'
#    ~10 min later fleet-systemd-unit-failed fires with host=rpi4b
ssh rpi4b 'sudo systemctl reset-failed alert-smoke-test'

# 4. The push-model down path. Worth proving properly.
ssh rpi4b 'sudo systemctl stop alloy'
#    ~10-11 min: fleet-host-down fires WITH host="rpi4b" in the message,
#    not a bare NoData. That is the whole point of the or-chain.
ssh rpi4b 'sudo systemctl start alloy'
```

!!! danger "Use rpi4b for steps 3 and 4"
    No Docker, nothing depends on it. **Never** run step 4 against the exit
    node: stopping its collector is harmless, but it is the wrong place to
    practise.

## Known blind spot

Nothing here can tell you the central LXC is down, because Grafana dies with
it. That needs an external dead-man's-switch: a healthchecks.io ping from an
`OnCalendar` timer on the central box, or an Uptime Kuma elsewhere.
