# Variables

The knobs that matter, where they live, and what they cost.

For how variable precedence works at all, see
[Ansible concepts](../getting-started/ansible-basics.md#variables-and-where-they-live).

!!! warning "Precedence"
    `group_vars/<group>/main.yml` beats vars set in the inventory file itself.
    Put site-specific values in `hosts.local.yml` and keep them **out** of
    `group_vars`, or a placeholder will silently win.

## Collector

`roles/alloy_collector/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `alloy_package_state` | `present` | never `latest`; bump deliberately with `-e alloy_package_state=latest` |
| `alloy_scrape_interval` | `15s` | |
| `alloy_journal_max_age` | `12h` | lower to `1h` on hosts with huge journals |
| `alloy_enable_docker` | `auto` | `auto` stats `/var/run/docker.sock` |
| `alloy_enable_process` | `false` | per-process metrics; **opt-in on cardinality grounds** |
| `alloy_syslog_listener_port` | `""` | empty means no listener; `5514` in `group_vars/central` for the firewall's syslog (Suricata, filter log, resolver log, `pfstats`; any other program name is stored as `job="firewall-other"`) |
| `alloy_syslog_drop_filterlog_rules` | `[]` | filterlog rule labels dropped on arrival at the listener, on one interface only (the next row); empty keeps everything; `group_vars/central` lists OPNsense's default deny |
| `alloy_syslog_drop_filterlog_iface` | `vtnet0` | the interface the drop list applies to, as written in field 5 of a `filterlog` line: the firewall's WAN; must not be empty |
| `alloy_syslog_ratelimit_filterlog_rules` | `[]` | filterlog rule labels whose lines are kept at a few a second (rate 5, burst 20) and dropped beyond that; empty keeps everything; `group_vars/central` lists OPNsense's `<virusprot>` ban rule |
| `alloy_pve_firewall_log` | `false` | ships `/var/log/pve-firewall.log` to Loki as `job="pve-firewall"`; `host_vars/t7920.yml` turns it on |
| `alloy_fs_mount_points_exclude` | see file | |
| `alloy_fs_types_exclude` | see file | |
| `alloy_systemd_unit_exclude` | see file | |
| `journald_system_max_use` | `""` | empty leaves journald's own default |
| `collector_basic_auth_required` | `true` | `false` on the central node |

!!! warning "`alloy_enable_process` is off by default for a measured reason"
    It emits about **1000 series per host** (measured on `ubuntu-dev`: 47
    process groups, 1072 series) against a fleet head of ~52k. Enabling it
    everywhere is roughly a 20% jump in series, and at 7-day retention that is
    real disk on the central LXC, which has already hit 100% once.

    Turn it on where you would actually open a process view, and set it in
    `host_vars`, not with `-e`: a command-line override lasts one run, and the
    next `collectors.yml` pass would re-render the config without it and
    silently drop the metrics.

    ```yaml
    # inventory/host_vars/<host>.yml
    alloy_enable_process: true
    ```

!!! danger "Backslashes in these regexes"
    They land inside double quotes in the generated `.alloy` file, and Alloy
    uses **Go string escaping** there. A lone `\.` is an unknown escape
    sequence and `alloy fmt` rejects the whole config.

    Write `\\.` so the file contains `\\.`, which Alloy unescapes to the `\.`
    the regex engine wants. Keep these single-quoted in YAML.

### Firewall logs in Loki

Two log inputs, both off until a host asks for them. They add log streams, not
metrics. The firewall's rule counters are metrics and come from the hypervisor,
under [Proxmox guest NIC names](#proxmox-guest-nic-names).

**OPNsense, over syslog.** The firewall cannot run Alloy, so it sends syslog to
the listener `alloy_syslog_listener_port` opens (central node only). Each
stream's `job` is the sender's syslog program name when that is one of
`suricata` (as before), `filterlog`, `unbound` and `pfstats`; a sender with any
other program name gets `job="firewall-other"`. All carry `source="opnsense"`,
`host="opnsense"` and `role="firewall"`. A `filterlog` line also gets `iface`
and `action`, read from the fifth and seventh fields of its CSV, so one
segment's passes or blocks can be selected without scanning the rest. Each is
only taken when it is a value the firewall can write: `iface` one of `vtnet0` to
`vtnet99`, `lo0`, `enc0`, `pflog0`, `igb0` to `igb99` or `em0` to `em99`, and
`action` one of `pass`, `block`, `match`, `rdr`, `nat`, `binat`, `scrub`,
`rdr-anchor` or `nat-anchor`. A line whose field is anything else is still
stored, with no such label.

!!! warning "The listener cannot authenticate its senders"
    It is plain TCP syslog: anyone who can reach the port can connect and write
    any program name. Only the four names above become a `job`, so a stranger
    cannot mint streams by inventing names, but that bounds the `job` values;
    it does not prove who sent a line, and a sender that calls itself
    `filterlog` is stored as `filterlog`. The `iface` and `action` labels are
    read from the line itself, so they are no more trustworthy than the sender;
    only the fixed sets above can become a label, so they do not multiply
    streams. A forged line can still carry a valid interface and so escape the
    WAN-only drop, or be dropped by it. Keep the port reachable from the
    firewall only.

`alloy_syslog_drop_filterlog_rules` is a list of rule labels, the fourth field
of a `filterlog` line. A line carrying one is dropped before it is stored, but
only when it is on the interface `alloy_syslog_drop_filterlog_iface` names
(`vtnet0`, the WAN). A rule label does not name an interface, and the one
listed is OPNsense's default deny, which every interface can fire; dropping it
everywhere would hide what it catches on the lease and tenant segments.
`iface` is read from the line before this stage, so the value must be the
interface name exactly as `filterlog` writes it, and not empty. The labels are
joined with `|` into one regular expression, so write each as it is printed.
The list only has an effect where the listener is on, and an empty list keeps
every line.

```yaml
# inventory/group_vars/central/main.yml
alloy_syslog_drop_filterlog_rules:
  - 02f4bab031b57d1e30553ce08e0ec131   # the default deny, `block drop in log inet all`: dropped on the WAN side only
```

`02f4bab0...` is `block drop in log inet all`, an OPNsense automatic rule, so it
is the same on every install of the same release; check a label with `pfctl -sr
-v | grep <label>` on the firewall before reusing it. It shows no `on
<interface>` there, which is how to tell an interface-agnostic rule. On the WAN
it is home LAN broadcast and discovery, so it is dropped; on the lease and
tenant interfaces it shows out-of-state TCP and other default-denied traffic,
so it is kept. OPNsense's own filter log still keeps every line for 31 days.

!!! danger "Do not add `6125cb207f65033775d1069fdf6d0ccf` to the drop list"
    Its rule is labelled "let out anything from the firewall host itself" and
    is `pass out log route-to (vtnet0 10.10.0.1) inet from (vtnet0) to !
    (vtnet0:network)`. That reads like the firewall's own traffic, Unbound
    lookups and NTP, but pf translates NAT before it filters an outgoing
    packet, so the outbound leg of **every tenant and lease flow** (source the
    firewall's WAN address, with the translated source port) is logged under
    this label too. That line is what matches an abuse report (public IP,
    translated port, destination, time) to a VM, so it has to reach Loki. It
    adds about 1.5 lines a second. A test fails if it is listed.

`alloy_syslog_ratelimit_filterlog_rules` takes the same kind of list, but keeps
a rule's lines instead of dropping them: a line carrying one of these labels
passes only while the stream stays within a limit of 5 a second, after an
initial burst of 20, and the excess is dropped. Use it for a rule that fires on
every packet of a misbehaving source, where some lines are worth having and
hundreds a second are not. It applies after the drop list, uses the same
`|`-joined match on the fourth field, and, unlike the drop list, is not limited
to one interface. It likewise only has an effect where the listener is on. An
empty list keeps every line.

```yaml
# inventory/group_vars/central/main.yml
alloy_syslog_ratelimit_filterlog_rules:
  - add09fc915886757c300419fc2ecc1e6
```

That label is OPNsense's automatic `block <virusprot> hosts` rule. A source that
exceeds a per-source connection limit is banned for up to an hour, and this rule
logs every packet it sends meanwhile; the limit keeps a ban from flooding Loki.
As with the drop list, OPNsense's own filter log still has every line.

**Proxmox firewall, from a file.** With `alloy_pve_firewall_log: true`, Alloy
tails `/var/log/pve-firewall.log` and ships it as `job="pve-firewall"`. The
datacenter firewall's rules carry a log level, and what they drop lands in that
file, one line per event. `chain` (which rule fired) and `vmid` (which guest,
from the `PHYSIN=` or `PHYSOUT=` tap device) become labels; the rest of the
line is read at query time. The timestamp is taken from the line, so a backlog
after an Alloy restart lands at the right time. The Alloy user must be able to
read the file, which is `root:adm 640`; the role already adds it to `adm`.

```yaml
# inventory/host_vars/t7920.yml
alloy_pve_firewall_log: true
```

Only a Proxmox host has the file, so set it in that host's `host_vars`, not
in a group.

## SMART disk health

`roles/smart_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `smart_metrics_enabled` | `false` | **the only switch**; set `true` in `group_vars/metal` |
| `smart_metrics_oncalendar` | `*:0/15` | every 15 minutes, like `update_metrics` |
| `smart_metrics_randomized_delay` | `300` | staggers the fleet |
| `smart_metrics_nocheck` | `standby` | skips a spun-down drive rather than waking it |

!!! note "Inventory-gated, not autodetected, unlike `gpu_exporter`"
    Detecting SMART capability requires smartmontools to already be installed,
    so an autodetecting role would install it on every host to discover that
    almost none can use it, then report `changed` forever removing it again.
    It is also not guessable from the platform. One line in `group_vars/metal`
    is the honest way to express "this host has real disks", matching how
    `autoupdate` gates patching.

!!! danger "Never let `{` and `%` become adjacent in the exporter template"
    `templates/fleet-smart-metrics.j2` is rendered as Jinja before install, so
    a Prometheus format string like the obvious one for `name{labels} value`
    is read as a Jinja statement tag and the play dies with
    `Encountered unknown tag 's'`. The script builds that line by
    concatenation instead. This applies to comments too.

## Dell chassis fans

`roles/dell_fan_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `dell_fan_metrics_enabled` | `false` | **the only switch**; set `true` in `host_vars/t7920.yml` |
| `dell_fan_metrics_interval` | `30s` | each read is an SMI that briefly stalls every core; slower than the scrape on purpose |

Per host rather than per group: it is true of one Dell workstation, not of
bare metal in general. The role fails loudly if the host has no `/proc/i8k`.

## Dell fan boost

`roles/dell_fan_boost/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `dell_fan_boost_enabled` | `false` | **the only switch**; set `true` in `host_vars/t7920.yml` |
| `dell_fan_boost_channel` | `2` | the `dell_smm_hwmon` fan channel; 2 is CPU1 on a Precision 7920 |
| `dell_fan_boost_package` | `1` | the coretemp package that drives it |
| `dell_fan_boost_on_celsius` | `72` | boost at or above |
| `dell_fan_boost_off_celsius` | `60` | release at or below, once the hold is over |
| `dell_fan_boost_min_hold_seconds` | `120` | shortest boost |
| `dell_fan_boost_guard_celsius` | `80` | any other package this hot hands every fan back to the firmware |
| `dell_fan_boost_poll_seconds` | `5` | temperature reads are MSR-backed, no SMI |
| `dell_fan_boost_reassert_seconds` | `30` | fan writes are SMIs, so only this often while boosting |

The role refuses to install unless off < on < guard, and fails loudly if the
host has no `/proc/i8k` or no manual control on the chosen channel.

## Proxmox guest NIC names

`roles/pve_guest_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `pve_guest_metrics_enabled` | `false` | **the only switch**; set `true` in `group_vars/metal` |
| `pve_guest_metrics_oncalendar` | `*:0/5` | every 5 minutes, so a new guest is named quickly |
| `pve_guest_metrics_randomized_delay` | `60` | seconds of jitter |

node_exporter on a Proxmox VE node already reports every guest NIC, because
each one has a host-side device: `tap<vmid>i<n>` for a VM, `veth<vmid>i<n>` for
a container. This role writes `fleet_pve_guest_nic_info`, the join key that
names those counters, for the Guest Traffic dashboard. The same script also
reports on the tenant VMs. The metric reference is in the monitoring docs, under
[Proxmox guest NICs](https://harshitruwali.github.io/homelab-infra/monitoring/reference/metrics/#proxmox-guest-nics).

### What it writes

One file, `fleet-pve-guests.prom`, written atomically by
`/usr/local/bin/fleet-pve-guests` every 5 minutes and at boot.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `fleet_pve_guest_nic_info` | gauge, always `1` | `device`, `guest`, `vmid`, `type`, `nic`, `bridge` | one series per guest NIC; the join key for `node_network_*` |
| `fleet_pve_tenant_conntrack_entries` | gauge | `vmid`, `guest`, `ip`, `segment` | conntrack entries whose **original source** is the tenant VM's address |
| `fleet_pve_tenant_policed_packets_total` | counter | `vmid`, `guest`, `segment` | packets the packet-rate limit on the tenant VM's tap has dropped |
| `fleet_pve_tenant_packet_limit_active` | gauge | `vmid`, `guest`, `segment` | `1` when the packet-rate limit is on the tap of a running lease VM, `0` when it has been lost; lease VMs only |
| `fleet_pve_tenant_firewall_active` | gauge | `vmid`, `guest`, `segment` | `1` only when the hypervisor firewall is fully in force for a running tenant VM, `0` when any part is missing |
| `fleet_pve_firewall_dropped_packets_total` | counter | `chain`, `family`, `rule` | packets the `tenant` and `lease` firewall groups dropped, per address family (`ipv4` or `ipv6`) |
| `fleet_pve_guest_nics_total` | gauge | none | guest NICs named on this node; written even when it is `0` |
| `fleet_pve_guests_collection_timestamp_seconds` | gauge | none | when the last complete collection finished |

**Tenant VMs.** A VM whose Proxmox config has the tag `tenant` is a tenant VM,
whether it is the owner's own work VM or one let to someone else, and gets one
`fleet_pve_tenant_conntrack_entries` series. Containers are never tenants, so a
tag on one is ignored. `segment` is `lease` when the VM also has the tag
`lease`, otherwise `work`. The address is the first one in
`[IPSET ipfilter-net0]` in `/etc/pve/firewall/<vmid>.fw`; a VM with no firewall
file or no entry there gets no series. A VM with no tracked connections reads
`0`. A connection counts for the host that opened it: an SSH session from
OPNsense into a tenant counts for OPNsense's address, not the tenant's.

The table comes from `/usr/sbin/conntrack -L` (the `conntrack` package,
conntrack-tools), because the hypervisor's Proxmox kernel (7.0.14-pve) has no
`/proc/net/nf_conntrack`. The role does not install it; it fails the play
instead when `/usr/sbin/conntrack` is missing on a host where
`pve_guest_metrics_enabled` is true, because without it every run would fail
and every metric in the file would go stale. Install the `conntrack` package on
the hypervisor first. The output is streamed a line at a time and never held,
so a table of a million entries in a flood costs no more memory than a small
one, and it is run once for all tenants, however many there are, and only when
at least one has an address. A successful exit with no entries is an empty
table and every tenant reads `0`. A missing binary, a nonzero exit or a run
longer than 60 seconds is a failed read.

**Packet-rate limit.** The hookscript for leased VMs puts a `matchall` police
filter at preference 5 on the ingress of the VM's tap, `tap<vmid>i0`, which is
the guest-to-host direction. `fleet_pve_tenant_policed_packets_total` is the
number of packets that filter has dropped, so a value that keeps rising means a
tenant is hitting the ceiling. It needs no address, and every VM with the
`tenant` tag is a candidate, `segment` as above. A VM gets no series while it
is stopped, or if its tap has no preference-5 police filter (it was started
before the limit existed, say); neither is an error.

`fleet_pve_tenant_packet_limit_active` is the other side of that: it says
whether the filter is there at all. It is written for every **running** VM with
**both** tags `tenant` and `lease`, and for no other: a work VM is not capped,
so its absence of a limit is normal, and a stopped VM has no tap, so it gets no
series. It is `1` when the preference-5 police filter is on the tap and `0`
when it is not. The `0` matters because Proxmox's hot NIC update deletes the
whole ingress qdisc, so a running lease can lose its ceiling while everything
else looks normal, and a tap without the filter produces no
`fleet_pve_tenant_policed_packets_total` series to notice. A tap with no
ingress qdisc lists nothing to `tc`, which reads the same as a tap whose
qdisc has no preference-5 filter: `0`. It comes from the same `tc` listing as
the counter, so it adds no call. Proxmox's own bandwidth
limit is a different police filter at preference 50 on the same qdisc, and is
ignored. The filter lives on the tap, so the counter restarts from zero when
the VM does. The value is the `dropped` figure in the filter's action
statistics, read from the text of
`tc -s filter show dev tap<vmid>i0 parent ffff:` and not from `tc -j`, because
the JSON shape of a police action's statistics has not been checked against
this host's iproute2.

**Firewall in force.** `fleet_pve_tenant_firewall_active` is written for every
**running** VM with the tag `tenant`, in either segment, and is `1` only when
all of these hold, otherwise `0`:

1. the datacenter firewall is on: `enable` in `pvesh get
   /cluster/firewall/options`, read once per run, and only when a tenant VM is
   running (a cluster that never set it counts as off);
2. net0 of the VM has `firewall=1`;
3. its `/etc/pve/firewall/<vmid>.fw` has `enable: 1` under `[OPTIONS]` and an
   active `GROUP` rule for the segment's group: `tenant` for segment `work`,
   `lease` for segment `lease`. A rule switched off with a leading `|`, a
   different group, or a group name that only begins the same does not count;
4. the chain `tap<vmid>i0-OUT` exists in iptables, which is only asked about
   once the rest holds.

It matters because every way of turning the firewall off removes the `GROUP-*`
chains, the ipfilter and the macfilter together: `pve-firewall stop`, the
datacenter option, a `qm set --net0` without `firewall=1`, a VM file with
`enable: 0` or none at all. The drop counters below then read as quiet, since a
chain that is not there is silence, and nothing else says a tenant VM is
running unfiltered. A missing firewall file or chain is `0`, not an error. A
stopped VM, which has no tap, gets no series.

**Firewall drops.** The counters come from `iptables -w 5 -nvxL` and
`ip6tables -w 5 -nvxL` on `GROUP-tenant-IN`, `GROUP-tenant-OUT`,
`GROUP-lease-IN` and `GROUP-lease-OUT`, and the `family` label says which
(`ipv4` or `ipv6`). `-x` keeps the packet counts exact, since without it a
large one prints as `1234K`. A group's chains exist only while some VM uses the
group, so a chain that is not there, in either family, yields no series for
that family and is not an error. Two rules with one `rule` label in one chain
and family add up, and a drop that matches none of the labels below is not
reported.

| `rule` | What it counts |
|---|---|
| `private` | a DROP whose match is the datacenter `private` set, in an `-OUT` chain |
| `private_in` | the same match, in an `-IN` chain |
| `smtp` | a DROP with destination port 25, in either chain |

The private set has one name per family: `PVEFW-0-private-v4` in `iptables`
and `PVEFW-0-private-v6` in `ip6tables`. Each family counts only its own, so
the IPv6 drops that the `fe80::/10` and `fc00::/7` entries cause are included,
and an `ipv4` series never includes an IPv6 match.

**When a read fails.** If `pvesh`, a guest's config, a tenant VM's firewall
file (other than a missing one), `conntrack -L`, the datacenter firewall
option, a running tenant VM's `tc` listing or an `iptables` or `ip6tables`
listing cannot be read, the script prints why to stderr, exits 1 and leaves the previous file in place. `fleet_pve_guests_collection_timestamp_seconds` then
goes stale, which is the signal, rather than a `0` that looks like a quiet
tenant. Check with `journalctl -u fleet-pve-guests.service -n 50`.

!!! note "It checks the switch rather than trusting it"
    Enabled on a host without `/usr/bin/pvesh`, the role fails the play with a
    message saying so, instead of installing a timer that fails every five
    minutes on a host that cannot answer. It does the same for
    `/usr/sbin/conntrack`, the one prerequisite it will not install itself:
    install the `conntrack` package on the hypervisor, then run the play again.

!!! danger "Same Jinja trap as the SMART exporter"
    `templates/fleet-pve-guests.j2` is a Python script rendered as Jinja, so it
    too builds each exposition line by concatenation. Never let `{` and `%`
    become adjacent in it, comments included.

## GPU exporter

`roles/gpu_exporter/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `gpu_exporter_version` | `1.14.0` | pinned; no apt repo exists for this exporter, so this is what stands in for `alloy_package_state: present` |
| `gpu_exporter_listen_addr` | `127.0.0.1:9835` | loopback only; Alloy scrapes it locally |
| `gpu_exporter_collect_processes` | `true` | per-process VRAM, the nvtop process table |
| `gpu_exporter_checksums` | see file | sha256 per arch, from the release's signed `checksums.txt`; bump alongside the version |

!!! tip "`gpu_exporter_collect_processes` is what makes the GPU process table work"
    It passes `--collect.compute-apps`, which the exporter leaves off by
    default. Without it you can see that the GPU is full but not *what* is
    filling it, which is the one question the panel exists to answer.

    Cardinality is bounded by the number of processes holding a CUDA context,
    a handful even on a busy box, so this is nothing like the per-process CPU
    exporter. An empty table means nothing holds a context right now, not that
    collection is broken.

Installed only where `nvidia-smi` is found on the host (autodetected, same
`auto` idiom as `alloy_enable_docker`), and purged again if it disappears.
`gpu_exporter_available` is the fact the role sets, read by
`config.alloy.j2` to decide whether to emit the scrape block.

## Patching

`roles/unattended_upgrades/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `uu_apply_security_only` | `false` | `false` means `o=*`, every origin |
| `uu_automatic_reboot` | `false` | **hard requirement**, asserted every run |
| `uu_package_blacklist` | `[]` | only the `pi_debian` group overrides this |
| `uu_remove_unused_kernels` | `true` | `false` on `proxmox` and `central` |
| `uu_clean_interval_days` | `7` | `1` on small-rootfs hosts |
| `uu_upgrade_oncalendar` | `*-*-* 03:00` | |
| `uu_upgrade_randomized_delay` | `3600` | staggers the fleet |

## Desk appliance

`roles/desk_appliance/defaults/main.yml`

Every variable is prefixed `desk_appliance_`. The full table, with defaults
and what is deliberately left unset, is in `roles/desk_appliance/README.md`;
it is not repeated here.

## Tailscale node

`roles/tailscale_node/defaults/main.yml`

Every variable is prefixed `tailscale_node_`. The full table, with defaults and
what each is for, is in `roles/tailscale_node/README.md`; it is not repeated
here.

## Image registry

`roles/image_registry/defaults/main.yml`

Every variable is prefixed `image_registry_`. The full table, with defaults and
what each is for, is in `roles/image_registry/README.md`; it is not repeated
here.

## Container updates

`roles/docker_updates/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `docker_update_oncalendar` | `*-*-* 04:00` | must not overlap the apt window |
| `docker_update_randomized_delay` | `1800` | registry rate limits are per source IP |
| `docker_update_skip_projects` | `[]` | by Compose project name |
| `docker_update_prune` | `true` | dangling images only |
| `docker_update_health_wait_seconds` | `60` | settle time before the host-wide unhealthy/restarting check; skipped when no image changed |
| `docker_update_pull_retry_delay_seconds` | `120` | one retry of a failed pull or build |
| `docker_update_minor_step_images` | `[qdrant/qdrant:latest]` | may advance one minor release per run |
| `docker_update_run_now` | unset | `-e docker_update_run_now=true` for a supervised run |

## Update metrics

`roles/update_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `update_metrics_oncalendar` | `*:0/15` | every 15 minutes |
| `update_metrics_randomized_delay` | `300` | `600` on the Pis |
| `update_metrics_use_needrestart` | `true` | installed in non-interactive mode |

## Dashboards

`roles/grafana_dashboards/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `grafana_dashboard_root` | `/var/lib/grafana/dashboards` | parent of the two provider dirs |
| `grafana_dashboard_dirs` | `[fleet, servers]` | **must stay disjoint**; Grafana scans provider paths recursively |
| `grafana_dashboard_prune` | `true` | remove dashboards no longer committed |

!!! warning "Turning off `grafana_dashboard_prune` makes the deploy add-only"
    The provider runs with `disableDeletion: false`, so Grafana removes a
    dashboard from its database when the file disappears. Leave a stale file
    behind and it keeps resurrecting a dashboard you deleted from git.

## Alerting

`roles/grafana_alerting/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `grafana_admin_user` | `admin` | **override this**; `lxc-install.sh` writes a real username |
| `grafana_api_url` | `http://127.0.0.1:3000` | |
| `central_host_label` | from `host_vars` | must match `MONITOR_HOSTNAME` |
| `grafana_alerting_static_files` | every committed rules file, plus contact points, templates, policies and mute timings | `rules-availability.yaml` is templated, not listed |

## Site-wide

`group_vars/all/main.yml` and `hosts.local.yml`

| Variable | Notes |
|---|---|
| `monitoring_domain` | builds both ingest URLs |
| `monitoring_ingest_base_url` | builds both ingest URLs instead, e.g. `https://10.0.0.10`; for a LAN path to the central nginx |
| `monitoring_ingest_ca_file` | controller path to the CA that signed the private ingest certificate; collectors trust only it on ingest |
| `lan_jump_host` | ProxyJump target for `lan_guests` |
| `lan_use_jump_host` | set `false` when running from the LAN |
| `alloy_textfile_dir` | `/var/lib/node_exporter/textfile_collector` |
| `monitor_role` | per host: `server`, `pi`, `router`, `service`, `central` |
| `monitor_hostname` | per host; **never rename**, it forks Prometheus history |

## Vault

`group_vars/all/vault.yml`, ansible-vault encrypted and committed.

| Key | Used by |
|---|---|
| `vault_collector_basic_auth_password` | every collector, nginx htpasswd |
| `vault_grafana_admin_password` | `grafana_alerting` readback |
| `vault_matrix_homeserver_url` | relay |
| `vault_matrix_bot_mxid` | relay |
| `vault_matrix_bot_password` | relay |
| `vault_matrix_webhook_api_key` | Grafana contact point, `$__env{}` |
| `vault_matrix_alert_room_id` | internal room ID, **not** the alias |
| `vault_tailscale_authkey` | `tailscale_node` join: a tagged auth key, only read while a node is not yet on the tailnet |
| `vault_image_registry_push_password` | `image_registry`: the htpasswd password for the registry's one user |

```bash
cd ansible                                              # from the repository root
ansible-vault edit inventory/group_vars/all/vault.yml
```

## Per-group overrides in this fleet

=== "`pi`"

    ```yaml
    uu_package_blacklist:            # the only blacklist anywhere
      - "raspberrypi-kernel"
      - "raspberrypi-bootloader"
      - "linux-image-rpi-.*"
    update_metrics_randomized_delay: 600   # SD-card IO
    ```

=== "`metal`"

    ```yaml
    smart_metrics_enabled: true      # the only host with real disks
    pve_guest_metrics_enabled: true  # the only host with guests to name
    alloy_systemd_unit_exclude: '...(lxc|qemu|pve-container)@.+'   # cardinality
    alloy_fs_mount_points_exclude: '...|etc/pve|rpool...'          # always-full FUSE
    alloy_journal_max_age: 1h        # a hypervisor journal is enormous
    alloy_enable_docker: false       # PVE uses its own tooling, not Docker
    uu_remove_unused_kernels: false
    ```

    Platform-level: true because it is a real machine running PVE, not because
    of anything installed on it. Note this group is **not** in `autoupdate`.

=== "`tailscale-router` (host_vars)"

    ```yaml
    alloy_systemd_unit_exclude: '...(lxc|qemu|pve-container)@.+'   # cardinality
    alloy_fs_mount_points_exclude: '...|etc/pve|rpool...'          # always-full FUSE
    alloy_journal_max_age: 1h
    alloy_enable_docker: false
    uu_remove_unused_kernels: false
    uu_clean_interval_days: 1        # 2.0 GB root
    journald_system_max_use: 64M
    ```

    These are [host-specific overrides](../fleet/index.md#inventory-layout).
    The block looks identical to `metal` above and is deliberately **not** factored
    out: this host is an LXC that merely reports a `-pve` kernel and has no
    `/etc/pve`, so changing one must not silently change the other.

=== "`central`"

    ```yaml
    prometheus_remote_write_url: http://127.0.0.1:9090/api/v1/write
    loki_write_url: http://127.0.0.1:3100/loki/api/v1/push
    collector_basic_auth_required: false     # nginx owns auth
    uu_remove_unused_kernels: false
    ```

    Loopback, so the central node's own telemetry never depends on the WAN
    being up, which is exactly when you most need it.

## Wazuh enrollment and log inputs

| Variable | Default | Purpose |
|---|---|---|
| `wazuh_manager_address` | empty | Address agents use for the manager |
| `wazuh_manager_inventory_host` | first `wazuh_manager` member | Ansible delegate for group verification |
| `wazuh_manager_ca_src` | empty | Trusted CA PEM on the controller |
| `wazuh_manager_ca_path` | `/var/ossec/etc/manager-ca.pem` | Installed agent CA path |
| `vault_wazuh_enrollment_password` | required secret | Manager enrollment password, in Ansible Vault |
| `wazuh_enrollment_password_path` | `/var/ossec/etc/enrollment.pass` | Protected agent password file |
| `wazuh_agent_group` | `homelab` | Must exist on the manager before rollout |
| `wazuh_log_sources` | journald | List of `location` / `log_format` mappings |
| `wazuh_fim_directories` | `/etc`, `/bin`, `/sbin`, `/usr/bin`, `/usr/sbin`, `/root/.ssh` | Paths file integrity monitoring watches in real time |
| `wazuh_fim_ignore` | Wazuh's stock ignores plus the Proxmox volatile files | List of `type` (`path` or `sregex`) / `value` entries FIM skips. `sregex` is OSSEC's simple regex: only `^`, `$` and `\|`, and `.` is a literal dot |

See [Wazuh rollout prerequisites](playbooks.md#wazuh-agents).
