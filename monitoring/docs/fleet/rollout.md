# Rollout order

!!! tip "First time running any of this?"
    Do [Your first run](../getting-started/first-run.md) instead. It walks
    through a single host with dry runs, and explains each line of output.

Blast radius ascending. Do not skip the canary.

!!! tip "For an established fleet"
    `ansible-playbook playbooks/site.yml` does all of this in the right order
    and is idempotent. The staged sequence below is for a **first** rollout,
    or when you have changed something risky and want to watch it land one
    host at a time.

## Why alerting lands before patching

So the first thing you learn about a bad upgrade is a Matrix message, not a
host that stopped answering.

## The sequence

```bash
cd ansible
```

### 1. Central ingest capacity, before any collector exists

Onboarding several hosts at once backfills a lot of journal history through
one path. Raise the limits **first** or the first fleet start will `429`.

```bash
scripts/lxc-update.sh central --config-only && systemctl restart loki
ansible-playbook playbooks/rotate-collector-password.yml
```

### 2. Collectors, canary first

```bash
ansible-playbook playbooks/collectors.yml --limit ubuntu-dev        # verify fully
ansible-playbook playbooks/collectors.yml --limit 'ubuntu-ai,cloud-services,rpi5'
ansible-playbook playbooks/collectors.yml --limit rpi4b
ansible-playbook playbooks/collectors.yml --limit lan_guests
ansible-playbook playbooks/collectors.yml --limit tailscale-router  # risk: exit node
ansible-playbook playbooks/collectors.yml --limit monitor-lxc       # LAST
```

!!! warning "`monitor-lxc` goes last, deliberately"
    That run plants `/etc/alloy/.ansible-managed`, which transfers config
    ownership away from `lxc-install.sh`. Do it once the template has proven
    itself on five other hosts.

!!! danger "`tailscale-router` is the exit node"
    Restarting Alloy there is harmless. Restarting `tailscaled` is not, if
    your controller routes through it. Check first:
    `tailscale status | grep -i "exit node"`.

### 3. Update metrics everywhere

Low risk. Verify `apt_upgrades_pending` appears for every host afterwards.

```bash
ansible-playbook playbooks/update-metrics.yml
```

### 4. Alerting

```bash
ansible-playbook playbooks/central-alerting.yml
```

Do not proceed until the host-down test has actually fired **and** resolved.
See [Testing alerts](../monitoring/alerting.md#testing-the-chain).

### 5. Package patching, staged

```bash
ansible-playbook playbooks/unattended-upgrades.yml --limit ubuntu-dev   # 0 pending, proves config at zero risk
ansible-playbook playbooks/unattended-upgrades.yml --limit ubuntu-ai    # first real installs
ansible-playbook playbooks/unattended-upgrades.yml --limit rpi5         # SUPERVISED
ansible-playbook playbooks/unattended-upgrades.yml --limit rpi4b        # SUPERVISED
ansible-playbook playbooks/unattended-upgrades.yml --limit 'cloud-services,lan_guests'
ansible-playbook playbooks/unattended-upgrades.yml --limit tailscale-router
```

!!! danger "The Pis are the largest single change here"
    They carry the most pending packages, on different Debian majors. Image
    the SD cards first and watch the first run.

    Free some space before you start. `rpi4b` had 12 GB of stale apt cache;
    `apt-get clean` took it from 83% to 39% used and materially de-risked the
    upgrade.

### 6. Container updates

```bash
ansible-playbook playbooks/docker-updates.yml                      # installs timers, no pull
ansible-playbook playbooks/docker-updates.yml \
  --limit ubuntu-dev -e docker_update_run_now=true                 # canary, supervised
```

See [Container updates](container-updates.md) for what actually moves and what
does not.

### 7. First reboot window

The Pis will almost certainly need a reboot after step 5. One at a time,
confirming each returns before touching the next.

```bash
ansible rpi5 -m reboot --become
# wait, verify, then the next
```

## Adding a new host later

1. Add it to `hosts.local.yml` under the right group, and to `autoupdate` if
   it should self-patch.
2. Run `ansible-playbook playbooks/preflight.yml --limit <host>`.
3. Run `ansible-playbook playbooks/site.yml --limit <host>`.
4. Re-run `playbooks/central-alerting.yml` so the host-down or-chain is
   regenerated with the new host in it.

!!! warning "Step 4 is not optional"
    `rules-availability.yaml` is templated from the inventory. Skipping it
    leaves the new host with **no down-detection at all**, silently.
