# Ansible concepts

Every term you need, explained once, with a real example from this repo.

## A note on YAML first

Ansible files are YAML. Three rules cover almost everything:

**Indentation is structure, and it must be spaces.** A tab character is a
syntax error. Two spaces per level is the convention here.

**`key: value` makes a dictionary; `- item` makes a list.**

```yaml
# a dictionary with two keys
name: Install Alloy
become: true

# a list of two strings
roles:
  - alloy_collector
  - update_metrics

# a list of dictionaries: the shape almost every playbook uses
tasks:
  - name: First task
    ansible.builtin.apt:
      name: alloy
  - name: Second task
    ansible.builtin.service:
      name: alloy
```

**Quote anything ambiguous.** `yes`, `no`, `on`, `off` become booleans.
Version numbers like `1.10` become floats and lose the trailing zero. When in
doubt, quote it.

!!! warning "The most common beginner error"
    Inconsistent indentation. If Ansible says
    `mapping values are not allowed in this context`, count your spaces on the
    line above the one it names.

## Control node and managed nodes

| Term | Meaning | Here |
|---|---|---|
| **Control node** | the machine running `ansible-playbook` | your Mac, or `ubuntu-dev` |
| **Managed node** | a machine being configured | rpi5, matrix, plex, … |

Ansible only needs to be installed on the control node. Managed nodes need
SSH and Python, which they already have.

## Inventory

The inventory answers "which machines, and what do I know about each one?"

```yaml title="ansible/inventory/hosts.local.yml (simplified)"
all:
  vars:
    ansible_user: sp00f              # (1)!
  children:
    monitored:                       # (2)!
      children:
        pi:                          # (3)!
          hosts:
            rpi5:
              ansible_host: 100.64.0.11    # (4)!
              monitor_role: pi             # (5)!
            rpi4b:
              ansible_host: 100.64.0.12
              monitor_role: pi
    autoupdate:                      # (6)!
      hosts:
        rpi5: {}
        rpi4b: {}
```

1. Applies to every host unless overridden. This is the SSH username.
2. A **group**. Groups can contain other groups.
3. A nested group. `rpi5` is therefore in `pi`, `monitored` and `all`.
4. Where to actually connect. The inventory name (`rpi5`) is just a label.
5. A **host variable**: arbitrary data attached to this host.
6. A host can be in **many** groups. This is how patching policy is expressed.

### Special variables

Names beginning `ansible_` are understood by Ansible itself:

| Variable | Meaning |
|---|---|
| `ansible_host` | the address to connect to |
| `ansible_user` | the SSH username |
| `ansible_ssh_private_key_file` | which key to use |
| `ansible_python_interpreter` | which Python to use on the target |

Everything else, like `monitor_role`, is yours to invent and use in templates.

### Group membership is how policy is expressed

This is worth pausing on, because it is the central design decision here.

Whether a machine auto-patches itself is decided **only** by whether it is
listed under `autoupdate`. There is no "should I patch?" logic inside the
code. To stop a host patching, you move it between groups in the inventory.

!!! info "Why it is done that way"
    A conditional buried in a role is one typo away from auto-upgrading a
    hypervisor. A group membership is visible in one file, in one place, and
    `ansible-inventory --graph` prints it.

### Seeing what the inventory contains

```bash
cd ansible
ansible-inventory --graph          # the tree of groups and hosts
ansible-inventory --host rpi5      # every variable that applies to rpi5
```

## Variables and where they live

The same variable can be set in several places. Later entries win:

```text
role defaults          roles/alloy_collector/defaults/main.yml
  ↓
inventory group vars   inventory/group_vars/pi/main.yml
  ↓
inventory host vars    inventory/host_vars/rpi5.yml
  ↓
command line           -e alloy_journal_max_age=1h
```

!!! danger "The precedence trap that cost an afternoon here"
    **`group_vars/<group>/main.yml` beats variables written in the inventory
    file itself.** A placeholder left in `group_vars/lan_guests` silently
    overrode the real value from `hosts.local.yml`, and everything looked
    correct while connecting to the wrong host.

    Rule of thumb: site-specific values go in `hosts.local.yml`, never in
    `group_vars`.

### Role defaults

`roles/<role>/defaults/main.yml` holds the lowest-priority values. This is
where a role declares its knobs:

```yaml title="roles/unattended_upgrades/defaults/main.yml"
uu_automatic_reboot: false
uu_clean_interval_days: 7
```

Any group or host can override them. `group_vars/proxmox/main.yml` sets
`uu_clean_interval_days: 1` because that host has a 2 GB disk.

## Tasks and modules

A **task** is one unit of work. It calls a **module**, which is a piece of
code Ansible ships that knows how to do one thing.

```yaml
- name: Install Alloy               # (1)!
  ansible.builtin.apt:              # (2)!
    name: alloy                     # (3)!
    state: present                  # (4)!
```

1. Human-readable label. Printed as the task runs. Always write one.
2. The module. `ansible.builtin.apt` manages Debian packages.
3. Module argument.
4. The **desired state**, not a command. "present" means "be installed".

Modules you will see in this repo:

| Module | Does |
|---|---|
| `apt` | install/remove Debian packages |
| `copy` | put a literal file on the target |
| `template` | render a Jinja2 template, then put it on the target |
| `file` | create directories, set ownership, delete things |
| `systemd` | start/stop/enable services and timers |
| `stat` | look at a file and report back |
| `command` / `shell` | run something arbitrary, the escape hatch |
| `assert` | fail the run unless a condition holds |
| `debug` | print something |

!!! tip "Prefer a real module over `shell`"
    `apt` knows whether the package is already installed, so it can report
    `ok` instead of `changed`. `shell: apt install alloy` cannot, so it
    reports `changed` every single time and you lose idempotency.

## Idempotency, `ok` versus `changed`

Every task reports one of these:

| Result | Meaning |
|---|---|
| `ok` | already correct, nothing done |
| `changed` | it was wrong, Ansible fixed it |
| `skipping` | a `when:` condition was false |
| `failed` | it could not be done |

This is the whole value proposition. The second run of any playbook here
should be all `ok`.

## Conditionals

```yaml
- name: Remove the cAdvisor root drop-in on non-Docker hosts
  ansible.builtin.file:
    path: /etc/systemd/system/alloy.service.d/10-cadvisor-root.conf
    state: absent
  when: not (alloy_docker_effective | bool)     # (1)!
```

1. Only runs when the expression is true. Otherwise the task reports
   `skipping`.

## Registering results

A task can save its result for a later task to inspect:

```yaml
- name: Check for a Docker socket
  ansible.builtin.stat:
    path: /var/run/docker.sock
  register: docker_update_sock        # (1)!

- name: Skip hosts without Docker
  ansible.builtin.debug:
    msg: "no Docker here"
  when: not docker_update_sock.stat.exists    # (2)!
```

1. Store the outcome in a variable.
2. Use it. `stat` returns a dictionary with an `exists` key.

## Loops

```yaml
- name: Ensure directories exist
  ansible.builtin.file:
    path: "{{ item }}"        # (1)!
    state: directory
  loop:
    - /etc/alloy
    - /var/lib/node_exporter/textfile_collector
```

1. `item` is the current element. The task runs once per list entry.

## Handlers and `notify`

A **handler** is a task that only runs if something asked for it, and only
once at the end, no matter how many tasks asked.

```yaml
# in tasks/main.yml
- name: Write the Alloy configuration
  ansible.builtin.template:
    src: config.alloy.j2
    dest: /etc/alloy/config.alloy
  notify: restart alloy          # (1)!

- name: Write the Alloy environment file
  ansible.builtin.template:
    src: alloy.env.j2
    dest: /etc/default/alloy
  notify: restart alloy          # (2)!
```

1. "If you changed the file, ask for a restart."
2. Same request. Alloy still restarts only **once**.

```yaml
# in handlers/main.yml
- name: restart alloy
  ansible.builtin.systemd:
    name: alloy
    state: restarted
    daemon_reload: true
```

!!! info "Why this matters"
    Handlers only fire on `changed`. So a run where nothing changed does not
    restart anything, which is exactly what you want from something you run
    regularly against production.

### `meta: flush_handlers`

Handlers normally run at the very end. Sometimes you need one to run *now*,
for instance so a health check tests the new config rather than the old one:

```yaml
- name: Flush handlers so the readiness check tests the new config
  ansible.builtin.meta: flush_handlers

- name: Wait for Alloy to become ready
  ansible.builtin.uri:
    url: "http://127.0.0.1:12345/-/ready"
```

## Templates

A **template** is a file with placeholders, rendered per host. Ansible uses
Jinja2.

```jinja title="roles/alloy_collector/templates/config.alloy.j2 (excerpt)"
// Rendered for {{ inventory_hostname }} (role={{ monitor_role }}).

prometheus.exporter.unix "host" {
  filesystem {
    mount_points_exclude = "{{ alloy_fs_mount_points_exclude }}"
  }
}
{% if alloy_docker_effective %}
prometheus.exporter.cadvisor "containers" {
  docker_host = "unix:///var/run/docker.sock"
}
{% endif %}
```

| Syntax | Does |
|---|---|
| `{{ var }}` | insert the value |
| `{% if %}` … `{% endif %}` | include this block conditionally |
| `{% for x in list %}` … `{% endfor %}` | repeat |

So one template produces a slightly different file on each host, and the
differences are visible in the template rather than hidden in ten copies.

!!! bug "Jinja will eat other things that use braces"
    A Go template like `docker ps --format '{{.Names}}'` breaks, because
    Ansible tries to render `.Names` as a variable. Put such commands in a
    script file and use the `script` module instead.

### Validation

The `template` module can check a file **before** installing it:

```yaml
- name: Write the Alloy configuration
  ansible.builtin.template:
    src: config.alloy.j2
    dest: /etc/alloy/config.alloy
    validate: /usr/bin/alloy fmt %s      # (1)!
```

1. Ansible renders to a temp file, runs this command on it, and only moves it
   into place if the command succeeds. A broken config **never lands**.

That guard is why a syntax error fails the run instead of leaving a
crash-looping service on the exit node.

## Roles

A **role** is a reusable bundle of tasks, templates, defaults and handlers,
in a fixed directory layout:

```text
roles/alloy_collector/
  defaults/main.yml     the knobs, lowest priority
  tasks/main.yml        what to do
  handlers/main.yml     restart alloy, restart journald
  templates/            config.alloy.j2, alloy.env.j2
```

!!! warning "Roles do not see each other's defaults"
    A variable defined in `roles/update_metrics/defaults/main.yml` is not
    available to `roles/docker_updates`. This caused a real failure here:
    `update_metrics_dir is undefined`. Both roles now define it, pointing at
    the same source of truth in `group_vars/all`.

## Plays and playbooks

A **play** maps a group of hosts to a list of roles or tasks. A **playbook**
is a file of one or more plays.

```yaml title="ansible/playbooks/collectors.yml"
- name: Deploy the Alloy collector    # (1)!
  hosts: monitored                    # (2)!
  become: true                        # (3)!
  roles:
    - alloy_collector                 # (4)!
```

1. Label for the play.
2. Which inventory group to run against.
3. Escalate to root with `sudo` for every task. Almost everything here needs it.
4. Apply this role.

`site.yml` is a playbook of playbooks, using `import_playbook` to run them in
a deliberate order.

## Facts

Before running tasks, Ansible connects and gathers **facts**: OS, IP
addresses, memory, mounts. They are available as variables like
`ansible_facts['distribution']`. That first `Gathering Facts` task in the
output is this.

## Ansible Vault

Secrets are encrypted at rest but still committed to git:

```bash
cd ansible                                            # (1)!
ansible-vault view inventory/group_vars/all/vault.yml
ansible-vault edit inventory/group_vars/all/vault.yml
```

1. **Required.** The password file location comes from `ansible.cfg`, which
   is only found when your shell is inside `ansible/`. Run it from the repo
   root and it silently produces nothing.

The password itself lives at `~/.config/ansible/monitorting-vault-pass`,
outside the repo. Encrypted values are used like any other variable:

```yaml
password: "{{ vault_matrix_bot_password }}"
```

!!! danger "Back up the vault password file"
    Without it, the encrypted values are unrecoverable.

## `ansible.cfg`

Settings that would otherwise be command-line flags every time:

```ini title="ansible/ansible.cfg"
[defaults]
inventory           = inventory/hosts.local.yml   # (1)!
roles_path          = roles
forks               = 7                           # (2)!
vault_password_file = ~/.config/ansible/monitorting-vault-pass

[ssh_connection]
pipelining = True                                 # (3)!
ssh_args   = -C -o ControlMaster=auto -o ControlPersist=120s -o IdentitiesOnly=yes
```

1. Which inventory to use, so you never pass `-i`.
2. How many hosts to configure in parallel.
3. Fewer SSH round trips per task. Noticeably faster.

!!! bug "`IdentitiesOnly=yes` is load-bearing"
    Without it SSH offers every key in your agent. A host with a low
    `MaxAuthTries` disconnects before reaching the right one, and the error is
    `Permission denied (publickey)`, indistinguishable from genuinely lacking
    access. This sent us chasing an authorisation problem that did not exist.

## Useful flags

| Flag | Does |
|---|---|
| `--limit rpi5` | only this host, or group, or comma list |
| `--check` | dry run, change nothing |
| `--diff` | show file differences |
| `--tags x` | only tasks tagged `x` |
| `-v`, `-vvv` | more output; `-vvv` includes the SSH commands |
| `-e key=value` | set a variable at the highest priority |
| `--syntax-check` | parse the files, connect to nothing |
| `--list-hosts` | show what would be targeted |

```bash
# the safest thing you can run
ansible-playbook playbooks/site.yml --check --diff --limit rpi5
```

## Ad-hoc commands

For one-off things, without writing a playbook:

```bash
ansible monitored -m ping                                  # (1)!
ansible rpi5 -m shell -a 'uptime' --become                 # (2)!
ansible autoupdate -m systemd -a 'name=alloy state=restarted' --become
```

1. `-m` is the module. `ping` checks SSH and Python, not ICMP.
2. `-a` is the module's arguments.

## Where to go next

[Your first run](first-run.md) walks through actually doing this, with the
real output.
