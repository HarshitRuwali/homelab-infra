# Controller setup

!!! tip "New to Ansible?"
    Read [Ansible concepts](../getting-started/ansible-basics.md) first, then
    come back here. This page is the practical setup; that one explains what
    an inventory, a playbook and a role actually are.

You can drive this from macOS or from a Linux box. `ubuntu-dev` is the
recommended Linux controller: it is a VM on the Proxmox host, so it sits on
the LAN with every guest.

## Install Ansible

=== "macOS"

    ```bash
    brew install ansible
    ```

=== "Debian / Ubuntu"

    Distro packages lag badly, so install into a venv rather than via apt:

    ```bash
    sudo apt update && sudo apt install -y python3-venv python3-pip git
    python3 -m venv ~/.venvs/ansible
    ~/.venvs/ansible/bin/pip install --upgrade pip ansible
    echo 'export PATH="$HOME/.venvs/ansible/bin:$PATH"' >> ~/.bashrc
    exec bash
    ansible --version    # expect core 2.21+
    ```

## Clone and create the inventory

```bash
git clone git@github.com:HarshitRuwali/monitorting-stack.git
cd monitorting-stack/ansible
cp inventory/hosts.example.yml inventory/hosts.local.yml
```

!!! danger "`hosts.local.yml` is what `ansible.cfg` loads, and it is gitignored"
    This repository is public. Real addresses, usernames and the monitoring
    domain must never be committed: an inventory is a complete map of the
    estate, the ingest endpoint, internal addressing, valid usernames, and
    which box to hit to blind the monitoring.

Fill it in from the example, including at minimum:

```yaml
all:
  vars:
    monitoring_domain: monitor.your-domain.tld   # builds the ingest URLs
    lan_jump_host: <host on both networks>       # for lan_guests
    grafana_admin_user: <matches GF_SECURITY_ADMIN_USER>
```

!!! warning "Do not assume the Grafana admin is `admin`"
    `lxc-install.sh` writes whatever `GRAFANA_ADMIN_USER` was set to in
    `.env`, which is often a real username. The `grafana_alerting` role
    authenticates with it, so a wrong value fails the readback assert.

Because it is not in git, `hosts.local.yml` must be copied to **each**
controller you run from, alongside the vault password and SSH key.

## The vault password

`ansible.cfg` points at `~/.config/ansible/monitorting-vault-pass`, mode
`0600`, on whichever machine you run from. It is deliberately outside the
repo, so copy it across by hand:

```bash
# from the Mac, to a Linux controller
ssh ubuntu-dev 'mkdir -p ~/.config/ansible && chmod 700 ~/.config/ansible'
scp ~/.config/ansible/monitorting-vault-pass ubuntu-dev:~/.config/ansible/
ssh ubuntu-dev 'chmod 600 ~/.config/ansible/monitorting-vault-pass'
```

!!! danger "Back this file up"
    Without it, `inventory/group_vars/all/vault.yml` is unrecoverable.

Edit secrets with:

```bash
ansible-vault edit inventory/group_vars/all/vault.yml
```

!!! bug "`ansible-vault` needs the right working directory"
    `vault_password_file` is resolved relative to `ansible.cfg`, and
    `ansible.cfg` is only found when your shell is **inside `ansible/`**.
    Running `ansible-vault view ansible/inventory/...` from the repo root
    silently produces nothing, and any command using that value then fails
    with a confusing `401`. Always `cd ansible` first.

## SSH

The key must be present on the controller. Either copy it over, or point
`ansible_ssh_private_key_file` at whatever key that box already uses.

`ansible.cfg` sets `IdentitiesOnly=yes`, and it is load-bearing:

```ini
ssh_args = -C -o ControlMaster=auto -o ControlPersist=120s -o IdentitiesOnly=yes
```

!!! bug "Why `IdentitiesOnly` matters"
    Without it, SSH offers every key in the agent and in `~/.ssh`. A host with
    a low `MaxAuthTries` disconnects before reaching the right one. That
    presents as `Permission denied (publickey)`, which is indistinguishable
    from genuinely lacking access, and led to a host being wrongly declared
    unreachable.

## Which controller you use changes one thing

`lan_guests` are reached by jumping through a host on the Proxmox LAN. If the
controller is **already** on that LAN, the jump is not merely unnecessary, it
fails, because it would proxy through the machine running the play.

| Controller | `lan_guests` reached via | Command |
|---|---|---|
| macOS, or any off-LAN host | ProxyJump through the jump host | default, nothing to add |
| ubuntu-dev, or any host on the guest LAN | direct | add `-e lan_use_jump_host=false` |

```bash
# running from ubuntu-dev
ansible-playbook playbooks/site.yml --limit lan_guests -e lan_use_jump_host=false
```

Everything else, including all tailnet hosts, behaves identically from either
controller.

## Preflight

Nothing here changes a target.

```bash
ansible-playbook playbooks/preflight.yml
```

Three things must be resolved before it passes:

1. **Addresses for the LAN guests.** Fill in every `ansible_host`. Get them
   from the Proxmox UI, or `pct config <vmid>` / `qm config <vmid>`.

2. **The central node's real `host` label.** It must match what is already in
   Prometheus, or the host-down alert watches a machine that never existed:

    ```bash
    ssh <monitor-lxc> 'grep MONITOR_HOSTNAME /etc/default/alloy'
    ```

    Put the answer in `inventory/host_vars/monitor-lxc.yml`.

3. **SSH and sudo on every host.** Containers often ship with
   `PermitRootLogin no`, and every role runs `become: true`, so a host without
   passwordless sudo is a silent no-op.

!!! warning "Check your own route first"
    If your controller reaches the fleet over a VPN whose exit node is itself
    in the inventory, confirm you are not routing through it. A play that
    restarts that daemon would sever its own connection:

    ```bash
    tailscale status | grep -i "exit node"
    ```

!!! note "Sudo passwords will not work fleet-wide"
    Hosts use different users (`harshit`, `sp00f`, `root`), so
    `--ask-become-pass` cannot supply one password for all. Preflight checks
    `sudo -n true` per host instead; fix any failures with NOPASSWD sudo or a
    per-host `ansible_become_password` in the vault.
