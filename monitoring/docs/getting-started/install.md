# Central stack install

Two supported shapes. Pick one; they are alternatives, not layers.

Everything below runs from `monitoring/`.

=== "Direct LXC"

    Native systemd units in a Debian or Ubuntu LXC, which is what this fleet
    runs. Grafana, Loki and Alloy come from the Grafana APT repository and
    Prometheus from the distro's.

    ```bash
    export PUBLIC_DOMAIN=monitor.example.com
    export GRAFANA_ADMIN_PASSWORD=<strong-password>
    export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
    scripts/lxc-install.sh central
    ```

    Run as root, inside the LXC. It installs the four services, writes an nginx
    reverse proxy with an htpasswd file on the ingest paths, and starts
    everything.

    | Flag | Effect |
    |---|---|
    | `--config-only` | rewrite configuration, install nothing |
    | `--no-start` | install and configure, but leave units stopped |

    State lives in `/var/lib/{grafana,prometheus,loki,alloy}`.

=== "Docker Compose"

    ```bash
    cp .env.example .env    # then set the passwords and the domain
    scripts/monitoring.sh central up
    ```

    `up` creates the external volumes, validates the Compose config, then
    starts the services. The other actions are `init`, `validate`, `down`,
    `restart`, `status` and `logs`.

    State lives in four **external** Docker volumes
    (`monitoring-grafana-data`, `-prometheus-data`, `-loki-data`,
    `-alloy-data`), which is why `docker compose down -v` does not destroy it.

    Ports bind to `127.0.0.1`, so put your own TLS reverse proxy in front. The
    Compose path does not write an nginx config for you.

## Configuration

Both paths read `monitoring/.env`. `.env.example` documents every key; these
are the ones you must not leave at their defaults:

| Key | Why it matters |
|---|---|
| `GRAFANA_ADMIN_PASSWORD` | required in central mode; the installer refuses without it |
| `COLLECTOR_BASIC_AUTH_PASSWORD` | shared by **every** collector pushing to this host |
| `GRAFANA_ROOT_URL` | wrong value breaks alert links and OAuth redirects |
| `PUBLIC_DOMAIN` | the hostname nginx serves and the collectors push to |
| `PROMETHEUS_RETENTION`, `LOKI_RETENTION_PERIOD` | sized to your disk, not to taste |

!!! warning "Retention is a disk decision, not a preference"
    Roughly 28 MB per host per day for Prometheus. Raise the defaults only
    after confirming free space: running the monitoring box out of disk is a
    self-inflicted outage that also takes out the alerting that would have
    warned you. See [Retention](../operations/retention.md).

!!! note "`MATRIX_WEBHOOK_API_KEY` and `MATRIX_ALERT_ROOM_ID` are usually blank here"
    Both are managed by the `matrix_webhook` role and written to
    `/etc/default/grafana-monitoring`. Set them in `.env` only when
    bootstrapping the LXC without Ansible.

## Public exposure

```text
https://monitor.example.com/                         -> Grafana UI
https://monitor.example.com/prometheus/api/v1/write  -> Basic Auth metrics ingest
https://monitor.example.com/loki/api/v1/push         -> Basic Auth log ingest
```

Never expose raw Grafana, Prometheus, Loki or Alloy ports. See
[Security](../security.md).

## Hand configuration over to Ansible

The installer is a **bootstrap path**, not the source of truth. Once the box is
up, configuration belongs to the fleet control plane, which re-renders the
Alloy config, the alert rules, the dashboards and the Matrix relay:

```bash
cd ansible                 # from the repository root
ansible-playbook playbooks/site.yml --limit <central-host>
```

!!! info "The installer cannot revert what Ansible manages"
    `lxc-install.sh` is guarded by a marker file for exactly this reason, so
    re-running it after Ansible has taken over does not undo the fleet's
    configuration. See
    [Controller setup](https://harshitruwali.github.io/homelab-infra/ansible/fleet/setup/).

## Updating

```bash
scripts/lxc-update.sh central               # direct LXC: packages and config
scripts/monitoring.sh central restart       # Compose: after editing .env
```

`lxc-update.sh central --config-only` re-copies configuration without touching
packages. It copies from the checkout on the box, which may be behind this
repository, so prefer the Ansible path for anything Ansible owns. See
[Lifecycle](../operations/lifecycle.md).
