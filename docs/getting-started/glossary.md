# Glossary

Every term used in these docs, including the ones that are not Ansible.

## Ansible

**Ad-hoc command**
: Running a single module without a playbook: `ansible rpi5 -m ping`.

**Agentless**
: Nothing is installed on managed machines. Ansible uses SSH and the Python
  already present.

**`become`**
: Escalate privileges, normally to root via `sudo`. Nearly every play here
  sets `become: true`.

**Control node**
: The machine running `ansible-playbook`. Your Mac, or `ubuntu-dev`.

**Fact**
: Information Ansible collects about a host before running tasks: OS,
  addresses, memory, mounts. The `Gathering Facts` task.

**Group**
: A named set of hosts in the inventory. Groups can contain groups. A host can
  be in many.

**`group_vars`**
: Variables applied to every host in a group. Lives in
  `inventory/group_vars/<group>/main.yml`.

**Handler**
: A task that runs only if another task notified it, and only once, at the end
  of the play. Used for service restarts.

**`host_vars`**
: Variables for one specific host.

**Idempotent**
: Running it twice has the same effect as running it once. The core promise;
  see [`changed` vs `ok`](ansible-basics.md#idempotency-ok-versus-changed).

**Inventory**
: The file listing which machines exist, how to reach them, and what groups
  they belong to. Here: `inventory/hosts.local.yml`, gitignored.

**Jinja2**
: The templating language. `{{ variable }}`, `{% if %}`.

**Managed node**
: A machine Ansible configures.

**Module**
: A unit of functionality Ansible ships: `apt`, `copy`, `template`, `systemd`.
  Tasks call modules.

**`notify`**
: How a task asks a handler to run.

**Play**
: A mapping of one host group to a list of roles or tasks.

**Playbook**
: A file containing one or more plays.

**Registered variable**
: A task's result saved with `register:` for a later task to inspect.

**Role**
: A reusable bundle of tasks, templates, handlers and defaults in a standard
  directory layout.

**Role defaults**
: `roles/<role>/defaults/main.yml`. The lowest-priority variables; a role's
  knobs. **Not visible to other roles.**

**Task**
: One unit of work. Calls exactly one module.

**Vault**
: `ansible-vault`. Encrypts secret values so they can be committed to git
  safely.

**`when`**
: A condition on a task. If false the task reports `skipping`.

## This repo

**Blast radius**
: How much breaks if a change goes wrong. Rollouts here are ordered from
  smallest to largest.

**Canary**
: The first host a change is applied to, chosen because failure there is
  cheap. `ubuntu-dev` usually.

**Capability gate vs policy gate**
: A capability gate asks "can this host do this at all?" (does it have
  Docker). A policy gate asks "should it?" Policy is expressed only through
  inventory group membership, never inside a role.

**Central node / central LXC**
: The machine running Grafana, Prometheus, Loki and nginx. Group `central`,
  host `monitor-lxc`.

**Collector**
: Grafana Alloy running on a host, shipping its metrics and logs to the
  central node.

**Fleet**
: All managed machines together.

**Jump host**
: A machine SSH connections hop through to reach hosts on a private network.
  `lan_guests` are reached this way.

**or-chain**
: The PromQL pattern that makes host-down detection work in a push model. See
  [The push model](../architecture/push-model.md).

**Textfile collector**
: A directory where scripts drop `.prom` files that node_exporter then
  publishes. How apt and reboot state become metrics.

## Monitoring

**Alloy**
: Grafana's collector agent. Replaces separate node_exporter, promtail and
  cAdvisor deployments.

**Alertmanager**
: The component that groups, deduplicates and routes firing alerts to a
  contact point. Built into Grafana here.

**cAdvisor**
: Container Advisor. Produces per-container CPU, memory and filesystem
  metrics. Needs root here, see
  [Container metrics](../monitoring/container-metrics.md).

**Contact point**
: Where Grafana sends a notification. Here, a webhook to the Matrix relay.

**Grafana**
: Dashboards and alerting.

**LogQL**
: Loki's query language. Looks like PromQL with a log-stream selector:
  `{unit="alloy.service"} |= "error"`.

**Loki**
: Log storage, queried by label rather than full-text index.

**node_exporter**
: The standard Linux metrics exporter. Embedded in Alloy as
  `prometheus.exporter.unix`.

**Notification policy**
: The routing tree deciding which contact point an alert reaches, and how
  often.

**Prometheus**
: Time-series metrics database.

**PromQL**
: Prometheus's query language.

**Provisioning**
: Configuring Grafana from files on disk rather than clicking in the UI. Read
  **only at startup**, which is why changes need a restart.

**Pull vs push**
: Prometheus normally *pulls* by scraping targets. This stack *pushes* via
  remote-write, which changes what `up` means. See
  [The push model](../architecture/push-model.md).

**`remote_write`**
: Prometheus's protocol for accepting metrics pushed to it.

**Silence**
: Temporarily suppressing an alert in Grafana. Expires; an exclusion in the
  rule file does not.

**Textfile exporter**
: See *Textfile collector*.

**`up`**
: A metric that is `1` when a target is reachable. **In this stack it is
  pushed**, so it vanishes rather than becoming `0` when a host dies.

## Linux and systemd

**`apt-daily-upgrade.timer`**
: The systemd timer that triggers unattended-upgrades. 03:00 here.

**cgroup**
: Kernel mechanism limiting and accounting resources. What container metrics
  are derived from.

**containerd**
: The container runtime under Docker. Its socket is `root:root`, which is why
  cAdvisor needs root.

**dpkg**
: The low-level Debian package tool under apt. Can become "wedged" and need
  `dpkg --configure -a`.

**Drop-in**
: A file in `<unit>.service.d/` that overrides part of a systemd unit without
  editing the packaged one.

**journald**
: systemd's log daemon. Alloy reads it and ships to Loki.

**LXC**
: Linux container. Lighter than a VM; several Proxmox guests here are LXCs.

**`needrestart`**
: Reports services running outdated binaries after an upgrade. Configured in
  non-interactive mode here, since its default prompt would hang an
  unattended run.

**oneshot**
: A systemd service that runs once and exits, rather than staying resident.

**`Persistent=true`**
: A timer setting: if the machine was off when it should have fired, run on
  next boot.

**Proxmox / PVE**
: The hypervisor hosting most of this fleet.

**`RandomizedDelaySec`**
: Jitter on a systemd timer, so many machines do not act simultaneously.

**Tailscale**
: The mesh VPN connecting most hosts.

**unattended-upgrades**
: The Debian package that applies updates automatically. Configured here to
  apply everything and **never reboot**.
