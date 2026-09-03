# Onboarding a new host

Getting a machine from "it exists" to "its metrics and logs are in central
Grafana". One playbook does the work and proves it landed:

```bash
cd ansible                                                 # from the repository root
ansible-playbook playbooks/preflight.yml --limit newhost   # read-only
ansible-playbook playbooks/onboard.yml   --limit newhost
```

`onboard.yml` refuses to run without `--limit`. Onboarding is a per-host
operation, and the failure mode of forgetting the limit is installing a
collector across the whole fleet while you meant to touch one box.

## Step 1: SSH access

The controller needs key-based SSH and a way to reach root. Both shapes in
this fleet are fine:

- **a normal user with passwordless sudo** (the VMs and Pis), or
- **a root login** (the LXC guests, which ship no `sudo` at all).

The root case needs no sudoers work: ansible-core skips `become` entirely when
the login already is the become user.

```bash
ssh-copy-id -i ~/.ssh/your_key.pub user@newhost
```

## Step 2: put it in the inventory

Add it to `inventory/hosts.local.yml` under the leaf that matches its
platform, and to `autoupdate` if it should be auto-patched.

!!! danger "Read the platform off the host, do not infer it from the name"
    ```bash
    ansible newhost -m setup -a 'filter=ansible_virtualization*'
    ```
    `virtualization_type=lxc` is a container. `kvm` with `role=guest` is a VM.
    `role=host` is bare metal. This fleet already contains two hosts that are
    not what their names suggest, so the check is not theoretical. See
    [the group tables](index.md#what-this-manages).

```yaml
    monitored:
      children:
        vm:
          children:
            vm_debian:
              hosts:
                newhost:
                  ansible_host: 10.0.0.42
                  ansible_user: youruser
                  monitor_role: server

    autoupdate:
      hosts:
        newhost: {}
```

`monitor_role` is required. It becomes the `role` external label on every
metric and log line the host ships, and the dashboards group by it.

If the host has no direct route from the controller, also add it to the
`lan_guests` overlay so it is reached through `lan_jump_host`.

## Step 3: run it

```bash
ansible-playbook playbooks/onboard.yml --limit newhost
```

In order, it:

1. **Asserts the host is in `monitored`.** Otherwise you get "no hosts
   matched", which reads like a typo in the limit and is in fact the
   commonest onboarding mistake: added to the inventory, but not to a group
   any playbook targets.
2. **Checks the host can reach the ingest endpoint** before installing
   anything. Derived from that host's own `prometheus_remote_write_url`, so
   it follows whichever path the host is configured to push on.
3. **Warns if the clock is not disciplined.**
4. **Installs `alloy_collector` and `update_metrics`.**
5. **Waits for the data to appear in central Prometheus and Loki**, queried
   from the controller.

Idempotent. Re-running against an onboarded host reports `changed=0`.

## Why the central checks exist

A green `systemctl status alloy` is not evidence that anything arrived. Alloy
stays running and healthy while every `remote_write` attempt is rejected, so
the only question worth asking is whether the samples landed. That is what
step 5 asks, and it asks it from the controller, over the public endpoint,
exactly as Grafana would.

!!! warning "Verification keys on `monitor_hostname`, not the inventory name"
    They are usually the same, but not always: `monitor-lxc` pushes as
    `main-server`. Keying on the inventory name would look for a host that
    never reports. See `host_vars/monitor-lxc.yml` for why that pin exists.

## When it fails

| Failure | Meaning |
|---|---|
| Ingest check `401` | The collector password is wrong for this host. Check `vault_collector_basic_auth_password`. |
| Ingest check connection/DNS error | Egress or resolution is broken from the host. |
| Ingest check `502` | The central stack is down. Fix that first. |
| Metrics never arrive | Alloy is running but not shipping. `journalctl -u alloy -n 50` on the host shows `remote_write` errors directly. Check the clock warning. |
| Logs never arrive, metrics fine | Ingest and credentials are good; the journal source is not. On an LXC with a volatile-only journal there may be nothing to ship yet. |

More in [Troubleshooting](troubleshooting.md).

## Step 4: the rest of the policy

`onboard.yml` deliberately stops at telemetry. To apply patching and container
update policy for whichever groups the host is now in:

```bash
ansible-playbook playbooks/site.yml --limit newhost
```

Then confirm it shows up alongside everything else:

```promql
count by (host, role) (node_uname_info)
```
