# Verification

Central-side checks that need no SSH, and the host-side ones for when they
disagree.

## The ingest path asymmetry

Get this wrong and you get confusing 404s. `/prometheus/` uses
`proxy_pass http://addr:9090/` **with** a trailing URI, so nginx **strips** the
prefix. `/loki/` uses `proxy_pass http://addr:3100` **without** one, so nginx
**preserves** the full path, and Loki's own API already lives under
`/loki/api/v1/`, so it maps straight through.

| External | Reaches | Note |
|---|---|---|
| `/prometheus/api/v1/query` | `:9090/api/v1/query` | prefix stripped |
| `/prometheus/-/ready` | `:9090/-/ready` | works |
| `/loki/api/v1/push` | `:3100/loki/api/v1/push` | prefix preserved |
| `/loki/api/v1/label/host/values` | same | **not** `/loki/loki/api/v1/...` |
| `/loki/ready` | `:3100/loki/ready` | **404**: Loki's is `/ready`, unreachable through this proxy |

## Is everyone reporting?

```bash
cd ansible   # required: ansible.cfg resolves the vault password file
PW=$(ansible-vault view inventory/group_vars/all/vault.yml \
     | awk '/collector_basic_auth_password/{print $2}' | tr -d '"')

curl -sG -u "collector:$PW" https://monitor.example.com/prometheus/api/v1/query \
  --data-urlencode 'query=count by (host, role) (node_uname_info)' \
  | jq -r '.data.result[].metric'
```

Expect one row per monitored host.

### Data age, the push-model health check

```bash
curl -sG -u "collector:$PW" https://monitor.example.com/prometheus/api/v1/query \
  --data-urlencode 'query=time() - max by (host) (max_over_time(timestamp(up{job=~"integrations/unix|host-unix"})[6h:1m]))' \
  | jq -r '.data.result[] | "\(.metric.host) \(.value[1])"'
```

**Every value must be under 30.** See [The push model](../architecture/push-model.md)
for why this, and not `up == 0`, is the right question.

### Logs arriving

```bash
curl -s -u "collector:$PW" \
  'https://monitor.example.com/loki/api/v1/label/host/values' | jq -r '.data[]'
```

## Are containers being collected?

```promql
count by (host, name) (container_last_seen{name!=""})
```

Expect one row per running container. Rows with an **empty** `name` mean
cAdvisor is only seeing the root cgroup, see
[Container metrics](../monitoring/container-metrics.md).

## Is patching working?

The role asserts the never-reboot config on every run. To check by hand:

```bash
ansible autoupdate -m shell \
  -a 'apt-config dump | grep "Unattended-Upgrade::Automatic-Reboot "' --become
# MUST print: Unattended-Upgrade::Automatic-Reboot "false";

ansible autoupdate -m shell \
  -a 'systemctl list-timers apt-daily-upgrade.timer fleet-update-metrics.timer --all --no-pager'
```

The best proof needs no SSH at all. Watch these in Grafana:

```promql
sum by (host) (apt_upgrades_pending)                    # should trend toward 0
apt_upgrades_security_pending                           # 0 on every autoupdate host
fleet_unattended_upgrades_last_run_timestamp_seconds    # advances daily
fleet_dpkg_needs_configure                              # must stay 0
```

Audit trail in Loki:

```logql
{unit="unattended-upgrades.service"} |= "Packages that will be upgraded"
```

## Is anything waiting on a reboot?

```promql
node_reboot_required == 1
(time() - fleet_reboot_required_since_timestamp_seconds) / 86400   # days waiting
```

The **Hosts Needing Reboot** panel on `VM Fleet Overview` shows the same thing
with names attached.

!!! tip "If a rebooted host still says it needs a reboot"
    The exporter runs every 15 minutes. Force a refresh rather than waiting:

    ```bash
    ansible <host> -m systemd \
      -a 'name=fleet-update-metrics.service state=started' --become
    ```

## Are the alert rules actually loaded?

```bash
curl -s -u "$GRAFANA_USER:$GRAFANA_PASS" \
  https://monitor.example.com/api/v1/provisioning/alert-rules | jq 'length'
```

!!! danger "An empty result is not a Grafana bug"
    It means the rules are gone from the database. Provisioning only runs at
    startup, so nothing restores them on its own. Fix with:

    ```bash
    ansible-playbook playbooks/central-alerting.yml
    ```

    The role asserts one known uid per rules file afterwards, because Grafana
    aborts alerting provisioning for **all** files when any one fails to
    parse, without failing startup.

## What is firing right now?

```bash
curl -s -u "$GRAFANA_USER:$GRAFANA_PASS" \
  'https://monitor.example.com/api/prometheus/grafana/api/v1/rules' \
  | jq -r '.data.groups[]?.rules[]? as $r
           | ($r.alerts[]? | select(.state != "Normal")
              | "\(.state)\t\($r.name)\thost=\(.labels.host // "-")")'
```

Blank output means everything is Normal.

Cross-check from the host, since Grafana only logs webhook sends at debug
level:

```bash
ssh <monitor-lxc> \
  'journalctl -u grafana-server --since "-1h" -o cat | grep "Sending alerts to local notifier" \
   | sed -E "s/.*rule_uid=([a-z0-9-]+).*/\1/" | sort | uniq -c | sort -rn'
```

## Idempotency

The real end-to-end check. A second run of anything should change nothing:

```bash
ansible-playbook playbooks/site.yml | sed -n '/PLAY RECAP/,$p'
# every host: changed=0
```
