# Package patching

Every host in `autoupdate` applies **every** available update nightly and
**never reboots**.

## The policy

Rendered to `/etc/apt/apt.conf.d/52unattended-upgrades-fleet`, numbered above
50 so it overrides the distro's stock file rather than fighting it.

```apt
Unattended-Upgrade::Origins-Pattern {
        "o=*";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::SyslogEnable "true";
```

### `o=*` and why listing origins is not equivalent

!!! bug "The gap this closed"
    Removing the package blacklists was **not** sufficient. `Origins-Pattern`
    only listed Debian and Ubuntu origins, so packages from any other origin
    were silently never considered. They sat in `apt_upgrades_pending` forever
    while every run reported success. That is how ~50 Raspberry Pi Foundation
    packages went unpatched.

`o=*` covers every origin, including third-party repos: Raspberry Pi
Foundation, Docker, Grafana, NodeSource. Set `uu_apply_security_only: true` to
go back to the security pocket only.

### Never auto-reboot

```apt
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
// Automatic-Reboot-Time deliberately NOT set.
```

!!! danger "Do not set `Automatic-Reboot-Time`"
    On some versions, setting it re-enables rebooting **even with**
    `Automatic-Reboot "false"`.

The role asserts this at the end of every run. Verify independently:

```bash
ansible autoupdate -m shell \
  -a 'apt-config dump | grep "Unattended-Upgrade::Automatic-Reboot "' --become
# MUST print: Unattended-Upgrade::Automatic-Reboot "false";
```

!!! warning "This policy has a cost"
    Kernel updates accumulate until a human acts. `fleet-reboot-required-too-long`
    nags at seven days, and that alert is what makes "never auto-reboot" a
    safe policy rather than a way to silently run unpatched kernels.

### `MinimalSteps` and `AutoFixInterruptedDpkg`

`MinimalSteps` upgrades in smaller transactions so an interruption resumes
cleanly. It matters most on the SD-card Pis.

`AutoFixInterruptedDpkg` recovers from a dpkg run interrupted by a power cut.
Without it, a single bad night leaves apt wedged until someone notices;
`fleet_dpkg_needs_configure` alerts if it happens anyway.

### `SyslogEnable` is load-bearing

It puts every action into the journal, which Alloy already ships to Loki. That
is a central audit trail of every package applied on every host, with no extra
plumbing and no per-host log files to collect:

```logql
{unit="unattended-upgrades.service"} |= "Packages that will be upgraded"
{unit="unattended-upgrades.service"} |~ "(?i)(traceback|error)"
```

## The only blacklist in the fleet

```yaml
# group_vars/pi/main.yml
uu_package_blacklist:
  - "raspberrypi-kernel"
  - "raspberrypi-bootloader"
  - "linux-image-rpi-.*"
```

!!! danger "Why these specifically"
    They are **unversioned**. Upgrading them overwrites `/boot/firmware` and
    `/lib/modules/$(uname -r)` **in place** rather than installing alongside.
    Under a never-auto-reboot policy that leaves the running kernel with no
    modules on disk, so anything not already loaded, USB storage, a
    filesystem, a netfilter module, fails until reboot.

    Remove them only if you are prepared to reboot the Pi immediately.

Everywhere else the list is empty. Be aware what that means: these upgrades
restart **services** (not machines) via maintainer scripts.

| Package | Restarts |
|---|---|
| `docker-ce`, `containerd.io` | every container on the host |
| `tailscale` | `tailscaled`; drops exit node and subnet routes briefly |
| `pve-*` | `pveproxy`, `pvedaemon` |
| `grafana`, `loki`, `prometheus` | the monitoring itself |

## Cache growth

!!! bug "`AutocleanInterval` is not enough"
    `autoclean` only deletes `.debs` that can no longer be downloaded from any
    configured repo. Everything still current stays forever. Under `o=*` that
    is a nightly download of everything, so the cache grows without bound:
    `tailscale-router` reached 199 MB of archives on a 2.0 GB root and was
    projected to fill within 24 hours.

`APT::Periodic::CleanInterval` runs a real `apt-get clean`. Default is 7 days;
small-rootfs hosts set `uu_clean_interval_days: 1`.

The only cost is re-downloading a package if you reinstall it, which is not a
cost worth 200 MB.

## Small-disk hosts

`group_vars/proxmox/main.yml` tightens two things for the 2.0 GB
`tailscale-router` root:

```yaml
uu_clean_interval_days: 1
journald_system_max_use: 64M
```

The journal cap is safe **because** Alloy ships everything to Loki within
seconds. The on-disk journal is only a local buffer plus enough history to
make `journalctl` useful over SSH; central retention is what you actually
query.

## Validation gate

The role ends with `unattended-upgrade --dry-run --debug`, asserting no
`Traceback` and no blacklisted package appears. That fails the play rather
than discovering breakage at 03:00.
