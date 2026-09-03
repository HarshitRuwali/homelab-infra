# Your first run

Four steps, each one safer than the next. Nothing before step 3 changes
anything on any machine.

## Before you start

You need, on the machine you will run from:

- [x] Ansible installed, see [Controller setup](../fleet/setup.md#install-ansible)
- [x] `ansible/inventory/hosts.local.yml` filled in (it is gitignored, so it
      does not arrive with the clone)
- [x] The vault password at `~/.config/ansible/monitorting-vault-pass`
- [x] An SSH key that reaches the fleet

```bash
cd ~/Developer/homelab-infra/ansible
```

!!! danger "Always `cd ansible` first"
    `ansible.cfg` lives there, and it is what tells Ansible where the
    inventory and vault password are. Every command on this page assumes you
    are inside that directory. Run them from the repo root and you will get
    confusing errors, including a `401` from a completely unrelated command.

## Step 0: what does Ansible think it manages?

Talks to nothing. Reads files only.

```bash
ansible-inventory --graph
```

```text
@all:
  |--@monitored:
  |  |--@lxc:
  |  |  |--@lxc_debian:
  |  |  |  |--tailscale-router
  |  |  |  |--plex
  |  |  |  |--memory
  |  |  |  |--monitor-lxc
  |  |--@vm:
  |  |  |--@vm_debian:
  |  |  |  |--ubuntu-dev
  |  |  |  |--ubuntu-ai
  |  |  |  |--cloud-services
  |  |  |  |--matrix
  |  |--@pi:
  |  |  |--@pi_debian:
  |  |  |  |--rpi5
  |  |  |  |--rpi4b
  |  |--@lan_guests:
  |  |  |--plex
  |  |  |--memory
  |  |  |--matrix
  |  |--@central:
  |  |  |--monitor-lxc
  |--@autoupdate:
  |  |--ubuntu-dev
  ...
```

Read this as: `rpi5` is in `pi_debian`, which is in `pi`, which is in
`monitored`, which is in `all`. It also appears under `autoupdate`, which is
what makes it self-patch.

The nesting is **platform, then distro family**, and every monitored host is
in exactly one leaf. `lan_guests` and `central` are **overlays** that cut
across that tree, which is why `plex` and `monitor-lxc` each appear twice: a
host can be in as many groups as apply to it, and "is a container" and "needs
a jump host to reach" are independent facts. See
[Fleet management](../fleet/index.md#what-this-manages).

To see everything Ansible knows about one host, including inherited variables:

```bash
ansible-inventory --host rpi5
```

## Step 1: can it reach everything?

The first command that opens an SSH connection. It runs a trivial module and
changes nothing.

```bash
ansible monitored -m ping
```

```text
rpi5 | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
ubuntu-dev | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

!!! info "This is not ICMP ping"
    It SSHes in and runs a Python module. `pong` proves SSH works **and**
    Python is present, which is everything Ansible needs.

### If a host fails here

| Error | Usually means |
|---|---|
| `Permission denied (publickey)` | wrong key, wrong user, or the `IdentitiesOnly` issue in [Troubleshooting](../fleet/troubleshooting.md) |
| `Connection timed out` | wrong `ansible_host`, or you need the jump host |
| `Failed to connect to the host via ssh` with `Host key verification failed` | new host; connect once by hand to accept the key |
| `/usr/bin/python3: not found` | rare; set `ansible_python_interpreter` |

## Step 2: preflight

Read-only, but far more thorough than `ping`. It checks sudo, Python, disk
and the things the later playbooks assume.

```bash
ansible-playbook playbooks/preflight.yml
```

Fix anything it reports before going further. In particular a host without
passwordless sudo will silently do nothing later, because every play here uses
`become: true`.

## Step 3: a dry run

Now the real playbook, but with `--check` so Ansible only *reports* what it
would do, and `--diff` so it shows file contents it would change.

```bash
ansible-playbook playbooks/site.yml --limit rpi5 --check --diff
```

```text
TASK [alloy_collector : Write the Alloy configuration] *************************
--- before: /etc/alloy/config.alloy
+++ after: /etc/alloy/config.alloy
@@ -44,6 +44,10 @@
   enable_collectors = ["systemd", "processes"]

+  textfile {
+    directory = "/var/lib/node_exporter/textfile_collector"
+  }
+
   filesystem {
changed: [rpi5]
```

`changed: [rpi5]` in `--check` mode means "**would** change", not "did".

!!! warning "`--check` is not perfect"
    A task that depends on an earlier task's effect can report oddly, because
    that earlier effect did not actually happen. Treat `--check` as a strong
    hint, not a contract. It is still the right first move on anything risky.

## Step 4: do it, on one host

Drop `--check`. Keep `--limit`.

```bash
ansible-playbook playbooks/site.yml --limit rpi5
```

Abridged output:

```text
PLAY [Deploy the Alloy collector] **********************************************

TASK [Gathering Facts] *********************************************************
ok: [rpi5]

TASK [alloy_collector : Install Alloy] *****************************************
ok: [rpi5]

TASK [alloy_collector : Write the Alloy configuration] *************************
changed: [rpi5]

TASK [alloy_collector : Remove the cAdvisor root drop-in on non-Docker hosts] ***
skipping: [rpi5]

RUNNING HANDLER [alloy_collector : restart alloy] ******************************
changed: [rpi5]

TASK [alloy_collector : Wait for Alloy to become ready] ************************
ok: [rpi5]

PLAY RECAP *********************************************************************
rpi5   : ok=20   changed=3   unreachable=0   failed=0   skipped=1
```

Line by line:

| Line | Meaning |
|---|---|
| `Gathering Facts` | connected, collected OS/network/memory info |
| `Install Alloy` → `ok` | already installed, nothing done |
| `Write the Alloy configuration` → `changed` | the file differed, it was replaced |
| `Remove the cAdvisor root drop-in` → `skipping` | a `when:` was false; rpi5 does run Docker |
| `RUNNING HANDLER … restart alloy` | the config change requested this |
| `Wait for Alloy to become ready` | proof of life, not just "started" |

## Step 5: prove it is idempotent

Run **exactly the same command again**.

```bash
ansible-playbook playbooks/site.yml --limit rpi5
```

```text
PLAY RECAP *********************************************************************
rpi5   : ok=18   changed=0   unreachable=0   failed=0   skipped=2
```

`changed=0`. Nothing needed doing, so nothing was done, and no handler fired,
so Alloy was not restarted.

!!! success "This is the check that matters"
    If a second identical run still reports `changed`, something is wrong,
    even if the playbook "works". Find the task and fix it. See
    [Troubleshooting](../fleet/troubleshooting.md#ansible).

## Step 6: the whole fleet

```bash
ansible-playbook playbooks/site.yml
```

Ansible configures up to 7 hosts in parallel (`forks = 7` in `ansible.cfg`),
so output from different hosts interleaves. The `PLAY RECAP` at the end is
the authoritative summary.

## What you have and have not done

After a successful `site.yml`, every host has:

- the Alloy collector, configured and shipping
- the apt/reboot metrics exporter and its timer
- unattended-upgrades configured, never rebooting
- the container update timer installed

!!! warning "Configuring is not upgrading"
    `site.yml` does **not** install package updates or pull container images.
    It installs and configures the machinery that does that on a schedule:
    03:00 for packages, 04:00 for containers.

    To force either now, see
    [Forcing an update now](../fleet/schedules.md#forcing-an-update-now). Both
    restart services, so be deliberate.

## Safe things to run any time

```bash
ansible-inventory --graph                          # reads files only
ansible monitored -m ping                          # connects, changes nothing
ansible-playbook playbooks/preflight.yml           # read-only checks
ansible-playbook playbooks/site.yml --check --diff # dry run
ansible-playbook playbooks/site.yml                # idempotent
```

## Things to be careful with

| Command | Why |
|---|---|
| `ansible-playbook playbooks/central-alerting.yml` | **restarts Grafana** |
| `ansible autoupdate -m shell -a 'unattended-upgrade -v' --become` | installs updates now, restarts services |
| `ansible-playbook playbooks/docker-updates.yml -e docker_update_run_now=true` | pulls images, recreates containers |
| `ansible-playbook playbooks/rotate-collector-password.yml` | brief 401 window on ingest |

None of them reboot a machine. That is a hard commitment across the whole
fleet.

## Next

[Reading the output](reading-output.md) covers the statuses, the recap, and
how to interpret a failure.
