# Runbooks

Ordered recovery steps. Each one starts from the alert that brought you here.

## Host Down

**Alert:** `fleet-host-down`: no metrics for 10 minutes.

```bash
tailscale ping <host>                       # is the machine reachable at all?
ssh <host> 'systemctl status alloy'         # is the collector the problem?
ssh <host> 'journalctl -u alloy -n 50'
```

| Finding | Action |
|---|---|
| Machine unreachable | Check Proxmox UI. It is a host problem, not a monitoring one. |
| Machine up, Alloy dead | `systemctl start alloy`, then read the journal for why. |
| Alloy running, still no data | Go to **Alloy Remote Write Failing** below. |

!!! tip "If it is a host you deliberately removed"
    Take it out of `monitored` in `hosts.local.yml` and re-run
    `playbooks/central-alerting.yml`, or the or-chain keeps a floor of `0`
    for it forever.

## Alloy Remote Write Failing

**Alert:** `fleet-remote-write-failing`.

```bash
ssh <host> 'journalctl -u alloy -n 100 | grep -iE "401|429|error"'
```

| Status | Cause | Fix |
|---|---|---|
| `401` | collector password mismatch | `ansible-playbook playbooks/rotate-collector-password.yml` |
| `429` | Loki ingest limits | raise `ingestion_rate_mb`, see [Retention](retention.md#ingest-limits) |
| `530` | Cloudflare cannot reach the origin | check whether the central LXC rebooted |
| connection refused | nginx down on the central box | `systemctl status nginx` |

## Disk Usage High / Critical

**Alert:** `fleet-disk-high` (95%), `fleet-disk-critical` (98%).

```bash
ssh <host> 'df -h /'
ssh <host> 'du -xh --max-depth=2 / 2>/dev/null | sort -h | tail -20'
```

Reclaim in this order, cheapest and safest first:

```bash
apt-get clean                       # often the single biggest win
journalctl --vacuum-size=64M
docker image prune -f               # dangling only, never -a
```

Then make it durable so it does not refill:

```yaml
# group_vars/<group>/main.yml
uu_clean_interval_days: 1
journald_system_max_use: 64M
```

!!! example "A worked case"
    `tailscale-router`: 2.0 GB root, 124 MB free, 94% used, `predict_linear`
    projecting **−0.13 GB in 24h**. 199 MB was apt cache and 192 MB was
    journal. `apt-get clean` plus a 64 MB journal cap took it to 70%, and
    `CleanInterval: 1` keeps it there.

## Reboot Required Too Long

**Alert:** `fleet-reboot-required-too-long`: 7 days outstanding.

This fleet never reboots itself, so this is always a human action.

```bash
ansible <host> -m reboot --become
```

One at a time, confirming each returns before the next. Afterwards:

```bash
ansible <host> -m systemd -a 'name=fleet-update-metrics.service state=started' --become
```

!!! tip "If the metric does not clear"
    The exporter runs every 15 minutes; the command above forces it. If
    `/run/reboot-required` is gone but the metric still says `1`, you are
    looking at a stale `.prom` file, not a failed reboot.

## dpkg Wedged

**Alert:** `fleet-dpkg-wedged`: critical.

```bash
ssh <host> 'sudo dpkg --configure -a'
ssh <host> 'sudo apt-get -f install'
```

Then confirm unattended-upgrades can run again:

```bash
ssh <host> 'sudo unattended-upgrade --dry-run --debug 2>&1 | tail -20'
```

!!! warning "Check the pending count, not the exit code"
    `unattended-upgrade` returns `0` when another run holds its lock.

## Container Restart Loop

**Alert:** `fleet-container-restart-loop`: more than 2 restarts in 30 min.

```bash
ssh <host> 'docker ps -a --filter name=<name>'
ssh <host> 'docker logs --tail 200 <name>'
ssh <host> 'docker inspect <name> --format "{{json .State}}" | jq'
```

If it started after a nightly update, the new image is the suspect:

```bash
ssh <host> 'journalctl -u fleet-docker-update.service -n 100'
```

Roll back by pinning the previous tag in the compose file and redeploying:

```bash
cd <compose dir> && docker compose up -d
```

Then add the project to `docker_update_skip_projects` until it is understood.

## Container Update Failed

**Alert:** `fleet-docker-update-failed`: critical. Does not fire for
`role="workstation"` hosts; check those by hand with the same commands.

```bash
ssh <host> 'journalctl -u fleet-docker-update.service -n 200'
ssh <host> 'docker ps --filter status=restarting'
```

The script logs one prefixed line per project, so the failing project is
named. Common causes: registry rate limit (`429`), a compose file that moved,
or a new image that needs a config change.

## Central Stack Unit Down

**Alert:** `fleet-central-stack-down`.

```bash
ssh <monitor-lxc> 'systemctl status prometheus loki grafana-server nginx alloy matrix-webhook'
ssh <monitor-lxc> 'journalctl -u <unit> -n 100'
```

If Grafana failed to upgrade:

```bash
mv /var/lib/grafana/plugins-bundled /var/lib/grafana/plugins-bundled.bak
dpkg --configure -a
systemctl restart grafana-server
```

Afterwards verify the alerting survived:

```bash
curl -s -u "$U:$P" http://127.0.0.1:3000/api/v1/provisioning/alert-rules | jq length
```

## No Alerts Arriving

Not an alert, you noticed silence.

```bash
# 1. do the rules exist?
curl -s -u "$U:$P" https://monitor.example.com/api/v1/provisioning/alert-rules | jq length

# 2. is the scheduler evaluating?
ssh <monitor-lxc> 'journalctl -u grafana-server --since "-1h" -o cat \
  | grep -c "Sending alerts to local notifier"'

# 3. is the relay up?
ssh <monitor-lxc> 'systemctl is-active matrix-webhook; \
  systemctl show matrix-webhook -p NRestarts --value'
```

| Result | Meaning |
|---|---|
| rules `0` | provisioning did not load, or they were deleted in the UI |
| rules fine, scheduler silent | nothing is actually wrong |
| both fine, relay restarting | credentials or room membership |

Restore rules with `ansible-playbook playbooks/central-alerting.yml`.

!!! warning "The relay logs nothing on success"
    Zero journal lines from `matrix-webhook` is **not** evidence that nothing
    was delivered. Test it directly, see
    [Testing the chain](../monitoring/alerting.md#testing-the-chain).
