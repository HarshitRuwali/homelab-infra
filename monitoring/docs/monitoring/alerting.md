# Alerting

53 provisioned rules in the `Fleet` folder, routed to a self-hosted Matrix
room through a local relay. 48 are committed under
`grafana/provisioning/alerting/`; the five availability rules are generated
from the inventory, so adding a host cannot leave a silent gap in
down-detection.

## Rule catalogue

=== "Availability (5)"

    | uid | Severity | For | Fires when |
    |---|---|---|---|
    | `fleet-host-down` | critical | 0m | no metrics for 10 min ([or-chain](../architecture/push-model.md)) |
    | `fleet-collector-lagging` | warning | 5m | data arriving but >15 min stale |
    | `fleet-remote-write-failing` | warning | 10m | Alloy failing to push samples |
    | `fleet-clock-unsynced` | warning | 30m | NTP not synchronised |
    | `fleet-central-stack-down` | critical | 2m | a core service on the central LXC is not active |

=== "Resources (8)"

    | uid | Severity | For | Threshold |
    |---|---|---|---|
    | `fleet-disk-high` | warning | 15m | filesystem over **95%** |
    | `fleet-disk-critical` | critical | 5m | filesystem over **98%** |
    | `fleet-disk-will-fill` | warning | 1h | trending to full in 24h **and** under 10% free |
    | `fleet-memory-high` | warning | 15m | available memory under 10% |
    | `fleet-oom-kills` | critical | 0m | kernel OOM killer active in last 15 min |
    | `fleet-load-high` | warning | 20m | load15 over 2× core count, `tailscale-router` excluded |
    | `fleet-filesystem-readonly` | critical | 5m | kernel remounted a filesystem read-only after an I/O error |
    | `fleet-inodes-high` | warning | 15m | over **90%** of the inode limit, which fails writes while space looks free |

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
    | `fleet-container-disappeared` | warning | 10m | container gone, non-workstation hosts |
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
    | `fleet-hw-fan-stalled` | critical | 5m | a fitted fan under 500 RPM; the BIOS never parks them. Excludes CPU0 (no fan), PSU (no tachometer) and FB4 (no rear bays, so no fan) |
    | `fleet-hw-fans-high` | warning | 30m | a 120 mm fan above 3000 RPM (75% of max): the BIOS is compensating |
    | `fleet-hw-board-temperature-high` | warning | 30m | a Dell SMM board sensor above 65 C |
    | `fleet-hw-sensors-missing` | warning | 15m | the host reports but its CPU sensors do not, or fan data is over 5 min old |

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
| `openipmi` | no IPMI hardware inside a container |
| `NetworkManager-wait-online` | times out at boot, network fine afterwards |
| `systemd-*`, `user@*`, `*-cleanup` | routinely transient |
| `apt-daily*`, `man-db`, `logrotate`, `e2scrub_reap` | maintenance jobs that retry |

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

```bash
curl -s -u admin:PASS \
  http://127.0.0.1:3000/api/alertmanager/grafana/api/v2/alerts \
  | jq '.[] | {alertname: .labels.alertname, labels, state: .status.state,
               silencedBy: .status.silencedBy}'
```

If `state` is `active` rather than `suppressed`, compare the `labels` printed
there against your silence's matchers; they disagree somewhere.

```bash
curl -s -u admin:PASS \
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
