# desk_appliance

Baseline for an always-on desk appliance: a mini PC with a real X11 desktop
session that an agent (or a person) drives. It logs itself in, never blanks,
locks or suspends, has audio and a stable serial symlink, gets a Bluetooth
adapter working that its kernel does not list yet, runs Docker, and patches
and reboots itself in the small hours.

Target: Ubuntu 26.04 and Kali Rolling, x86_64 (see
[Supported platforms](#supported-platforms)). The package names and the
generated configuration were exercised in an `ubuntu:26.04` and a
`kalilinux/kali-rolling` container with systemd (see
[Verification](#verification)). Of the layers, only `bluetooth` has been run
on the hardware itself.

## What it does

| Tag | Does |
|---|---|
| `desktop` | Installs Xorg, XFCE (variable) and LightDM; autologin as `desk_appliance_user` into a named X11 session; boots to `graphical.target`. |
| `power` | Ignores lid, suspend and hibernate in logind and masks the sleep targets; turns off the X server screen saver and DPMS; an autostart `xset s off -dpms`; XFCE power manager, screensaver and lock settings; hides the screen locker autostart entries. |
| `keyring` | Opt-in (`desk_appliance_plaintext_keyring`, default off). A second, blank-password gnome-keyring as the default, so logins survive an autologin boot. Secrets stored there are plaintext; see [Keyring](#keyring). |
| `audio` | PipeWire with ALSA and Pulse layers and WirePlumber; the user in `audio`; optional default sink and source; optional WebRTC echo cancellation. |
| `serial` | The user in `dialout`; a udev rule giving each listed USB serial device a stable `/dev/<name>` symlink. |
| `bluetooth` | Opt-in (`desk_appliance_btusb_quirks`, default empty). Rebuilds `btusb` through DKMS from the kernel's own source with each listed USB id added to its quirks table, for an adapter the kernel binds but never initialises. See [Bluetooth](#bluetooth). |
| `docker` | Docker engine and the Compose plugin from Ubuntu or from Docker's repository; the user in `docker`. |
| `updates` | `unattended-upgrades` for security updates only, with an automatic reboot at a set time when one is needed. Off by default on Kali (see [Kali Rolling](#kali-rolling)). |

The appliance user is created by the role (locked password, no sudo) under
the `always` tag, so every tag subset has an account to work with.

The role is applied by group membership (`desk_appliances`), with no host
conditional inside it. It refuses to run on a host that is also in
`autoupdate` or `no_autoupdate`; see [Patching](#patching-and-reboots).

## Supported platforms

| Platform | Status | Differences |
|---|---|---|
| Ubuntu 26.04 | the original target | none |
| Kali Rolling | exercised in a container | Compose package name, Docker repository, unattended updates off by default; see [Kali Rolling](#kali-rolling) |

The role stops at the start of the play on any other `ansible_distribution`
(`desk_appliance_supported_distributions`). On a distribution it has not been
tried on, the package names and the update origins would be wrong, and the
update layer would pass its own checks while patching nothing.

## Variables

Defaults are in `defaults/main.yml`, which has the longer reasoning. Variables
with no default are optional and leave their feature off while unset.
Everything host-specific (names, ids) is a variable; nothing is baked in.

| Variable | Default | Notes |
|---|---|---|
| `desk_appliance_supported_distributions` | `Ubuntu`, `Kali` | `ansible_distribution` values the role runs on. Any other fails the play at the start. |
| `desk_appliance_user` | `appliance` | Autologin account; created if missing. Not `root`. Set it per host in `host_vars` (see Example). |
| `desk_appliance_home` | `/home/<user>` | With `desk_appliance_create_user: false` and an existing account, the home recorded in `passwd` is used instead. |
| `desk_appliance_user_shell` | `/bin/bash` | Must be a real shell: LightDM's autologin PAM stack refuses `nologin`. |
| `desk_appliance_create_user` | `true` | `false` if the account already exists; the play then fails when it does not. |
| `desk_appliance_session` | `xfce` | X11 session file name in `/usr/share/xsessions`. The role fails if it is not there. XFCE settings are only written when this is `xfce`. |
| `desk_appliance_desktop_packages` | Xorg (with all video drivers and Mesa DRI), LightDM and XFCE package list | Installed without recommends, so drivers that Ubuntu only recommends are listed explicitly. |
| `desk_appliance_extra_packages` | `[]` | Anything else the desktop needs. |
| `desk_appliance_hidden_autostart` | `xfce4-screensaver`, `light-locker`, `xscreensaver`, `gnome-screensaver` | Per-user `Hidden=true` overrides. |
| `desk_appliance_plaintext_keyring` | `false` | `true` creates a blank-password default keyring for the appliance user. Plaintext secrets; see [Keyring](#keyring). |
| `desk_appliance_keyring_packages` | `gnome-keyring` | Installed when the keyring is enabled. |
| `desk_appliance_audio_packages` | PipeWire, WirePlumber and tools list | |
| `desk_appliance_audio_default_sink` | unset | PipeWire node name. ALSA nodes only. |
| `desk_appliance_audio_default_source` | unset | PipeWire node name. ALSA nodes only. |
| `desk_appliance_echo_cancel` | `false` | `true` writes the echo-cancel drop-in; `false` removes it. |
| `desk_appliance_echo_cancel_capture_name` | `echo-cancel-capture` | Node name, module's stream from the real microphone. |
| `desk_appliance_echo_cancel_playback_name` | `echo-cancel-playback` | Node name, module's stream to the real speaker. |
| `desk_appliance_echo_cancel_source_name` | `echo-cancel-source` | Virtual microphone for applications. |
| `desk_appliance_echo_cancel_sink_name` | `echo-cancel-sink` | Virtual speaker for applications. |
| `desk_appliance_echo_cancel_aec_args` | `{}` | Keys passed to the WebRTC backend, for example `webrtc.gain_control: true`. |
| `desk_appliance_serial_devices` | `[]` | List of `name`, `vendor_id`, `product_id` and optional `serial`. Ids are four hex digits, quoted. |
| `desk_appliance_btusb_quirks` | `[]` | List of `vendor_id`, `product_id` (four hex digits, quoted) and `driver_info` (btusb flags joined by ` \| `), each id once. Empty removes the DKMS module and its boot unit; the build packages stay. See [Bluetooth](#bluetooth). |
| `desk_appliance_btusb_build_packages` | `dkms`, the headers of the running kernel, the headers metapackage (`linux-headers-amd64`; on Ubuntu `linux-headers-generic`), `linux-source` | Installed when the quirk list is not empty. |
| `desk_appliance_docker_source` | `distro` | `distro` (`docker.io`, `docker-compose-v2`) or `docker_ce` (Docker's repository). |
| `desk_appliance_docker_packages` | per-source package lists | The Compose plugin is `docker-compose-v2` on Ubuntu and `docker-compose` on Kali. |
| `desk_appliance_docker_live_restore` | `true` | Writes `/etc/docker/daemon.json` with `{"live-restore": true}` only if that file is absent. See Patching. |
| `desk_appliance_docker_arch` | derived from the host | apt architecture name for the Docker repository. |
| `desk_appliance_docker_apt_url` | `https://download.docker.com/linux/ubuntu`; on Kali `.../linux/debian` | `docker_ce` only. Docker publishes no Kali repository. |
| `desk_appliance_docker_apt_key_url` | the URL above plus `/gpg` | `docker_ce` only. |
| `desk_appliance_docker_apt_suite` | the host's release codename; on Kali `trixie` | `docker_ce` only. Kali's own codename is not a directory in Docker's repository, so Kali uses Debian's. |
| `desk_appliance_docker_apt_keyring` | `/etc/apt/keyrings/docker.asc` | |
| `desk_appliance_updates_enabled` | `true` on Ubuntu, `false` on Kali | `false` skips the update layer and removes the role's own update files. See [Kali Rolling](#kali-rolling). |
| `desk_appliance_uu_origins` | Ubuntu security, ESM infra and apps security; on Kali `origin=Kali,codename=${distro_codename}` | Replaces the distro's origin list. On Kali that is every kali-rolling package. |
| `desk_appliance_reboot_time` | `"04:30"` | 24 hour `HH:MM`, quoted. |
| `desk_appliance_reboot_with_users` | `true` | Needed because the autologin user is always logged in. |
| `desk_appliance_upgrade_oncalendar` | `*-*-* 03:30` | `apt-daily-upgrade.timer`; must finish before the reboot time. |
| `desk_appliance_upgrade_randomized_delay` | `600` | Seconds. |

`vars/main.yml` holds derived paths. They are not settings.

## Example

Add the host to the `desk_appliances` group in the gitignored
`inventory/hosts.local.yml`, and keep it out of `autoupdate` and
`no_autoupdate`. Host-specific values go in `host_vars/<host>.yml`, also
gitignored.

```yaml
# inventory/hosts.local.yml (excerpt)
all:
  children:
    desk_appliances:
      hosts:
        desk-01:
          ansible_host: 192.0.2.10
```

```yaml
# inventory/host_vars/desk-01.yml
# The default account is `appliance`; name the real one per host.
desk_appliance_user: iris
desk_appliance_serial_devices:
  - name: ttydevice
    vendor_id: "1a86"
    product_id: "7523"
desk_appliance_echo_cancel: true
desk_appliance_audio_default_sink: alsa_output.example-sink-name
desk_appliance_btusb_quirks:
  - vendor_id: "0489"
    product_id: "e156"
    driver_info: "BTUSB_MEDIATEK | BTUSB_WIDEBAND_SPEECH"
```

```bash
cd ansible                                        # from the repository root
ansible-playbook playbooks/desk-appliance.yml --check --diff
ansible-playbook playbooks/desk-appliance.yml
ansible-playbook playbooks/desk-appliance.yml --tags power,audio
```

The play is `playbooks/desk-appliance.yml`:

```yaml
- name: Configure the desk appliance baseline
  hosts: desk_appliances
  become: true
  roles:
    - desk_appliance
```

Reboot once after the first run. LightDM is enabled, not started, and the
desktop, PipeWire and WirePlumber settings are read when the session next
starts. The role never restarts a running session.

## Run alongside

These are separate roles with their own playbooks and groups; this role does
not call them.

| Role | Playbook | Group |
|---|---|---|
| `alloy_collector` | `collectors.yml` | `monitored` |
| `wazuh_agent` | `wazuh-agents.yml` | `wazuh_agents` |

## Patching and reboots

This role is the deliberate exception to the fleet's never-reboot policy. The
fleet `unattended_upgrades` role asserts `Automatic-Reboot "false"` and
shares the `apt-daily-upgrade` timer drop-in point, so the two cannot share a
host, and this role fails early on one that is in `autoupdate` or
`no_autoupdate`.

- Updates come from the security origins only. The distro's own origin list
  is cleared first, because apt list options add to rather than replace an
  earlier file.
- The reboot is conditional on `/run/reboot-required` existing at the reboot
  time, not on the nightly run having caused it. A flag left earlier in the day
  by `playbooks/force-updates.yml` (which skips this group for that reason) or
  by a manual `apt upgrade` also reboots the host at the next window. Reboot
  yourself first, or expect the reboot at the window.
- `apt-daily-upgrade.timer` is moved to 03:30 so the run ends before the
  04:30 reboot time. If a run ends after the reboot time, the reboot is
  scheduled for the next day. Change the timer and the reboot time together.
- `desk_appliance_docker_source: docker_ce` installs from a repository that is
  not in the security-only origin list. Its updates need a manual
  `apt upgrade`, or an origin added to `desk_appliance_uu_origins`.
- A `docker.io` (or `docker-ce`) security update restarts `dockerd` when it is
  applied, which stops every container unless `live-restore` is set.
  `desk_appliance_docker_live_restore` (default `true`) writes
  `/etc/docker/daemon.json` with `{"live-restore": true}` only when that file
  does not exist. An existing `daemon.json`, hand-written or not, is never
  touched: add the key there yourself. `live-restore` does not work with Swarm
  mode, and turning the variable off later does not remove a file the role
  already wrote.
- A host that used to be in `autoupdate` or `no_autoupdate` still has the
  fleet role's files on disk. The role removes
  `/etc/apt/apt.conf.d/52unattended-upgrades-fleet`,
  `/etc/systemd/system/apt-daily-upgrade.timer.d/fleet.conf` and, when it
  carries the fleet role's "Managed by" header, `/etc/apt/apt.conf.d/20auto-upgrades`
  (Ubuntu's own file of that name is left alone). After configuring, it asks
  systemd (`systemctl show apt-daily-upgrade.timer`) and fails if the timer
  does not fire only at `desk_appliance_upgrade_oncalendar`.

## Kali Rolling

Run in a `kalilinux/kali-rolling` container, the role fails on two things and
silently does nothing on a third; all three are handled with distribution
defaults, so an Ubuntu host sees no change.

- **Compose package.** Kali has no `docker-compose-v2`; the Compose v2 plugin
  is `docker-compose`. Every other package in the default lists resolves on
  kali-rolling.
- **Docker repository** (`docker_ce` only). Docker publishes none for Kali, and
  `kali-rolling` is not a suite in its Ubuntu or Debian repositories. Kali
  defaults to Docker's Debian repository at suite `trixie`
  (`desk_appliance_docker_apt_suite`). `distro` (`docker.io` from Kali's own
  archive) needs none of this.
- **Unattended updates.** The Ubuntu origins match nothing on Kali, and
  `unattended-upgrade --dry-run` still exits 0, so the role's own check passes
  while the host never patches. There is no security-only pocket on a rolling
  distribution: the only origin is the whole archive (`origin=Kali`,
  codename `kali-rolling`). Applying that unattended, with a reboot, moves the
  kernel, Python, PipeWire and the desktop libraries while nobody is watching,
  so the update layer is **off by default on Kali**
  (`desk_appliance_updates_enabled`), and the host is updated by hand
  (`apt full-upgrade`). Setting it to `true` applies every kali-rolling update
  at the upgrade time and reboots at the reboot time if a kernel package asked
  for it. On Debian-family hosts only kernel packages create
  `/run/reboot-required` (through `/etc/kernel/postinst.d/unattended-upgrades`);
  a library update does not, so running services keep the old library until
  they restart. Turning the variable off later removes the role's apt policy
  file and timer drop-in; it leaves the `unattended-upgrades` package installed.

Things that behave the same but are worth knowing on an existing Kali install:

- **An existing account's XFCE settings are replaced**, for the two channels the
  role owns, `xfce4-power-manager` and `xfce4-screensaver`. A dated copy of an
  existing file is kept next to it (`backup: true`). Kali's own system-wide
  defaults for those channels (a power-button action and an on-screen-keyboard
  command) stay in effect, because per-user values override them one property
  at a time.
- **LightDM** reads every `lightdm.conf.d` drop-in. A hand-written autologin file
  with the same keys as `50-desk-appliance.conf` is harmless but redundant;
  `lightdm --show-config` shows which file each value came from.
- **Kali's desktop uses `xfce4-screensaver`**, not `light-locker`. Both are
  hidden from autostart and the xfconf properties cover the former.
- `kali-themes` also installs an `i3` session; the named `xfce` session is what
  LightDM logs into.

## Keyring

`desk_appliance_plaintext_keyring: true` (tag `keyring`, off by default) fixes
an autologin problem. gnome-keyring unlocks its `login` keyring with the
password typed at login; autologin types none, so that keyring stays locked and
apps that keep a login through the Secret Service (a music client's token, for
one) are logged out again after every boot. The role installs `gnome-keyring`
and writes, for the appliance user:

- `~/.local/share/keyrings/Default_keyring.keyring`, a plaintext keyring with a
  blank password, mode `0600`, `lock-on-idle=false` and `lock-after=false`. It
  is only written when absent, so a keyring that already holds secrets is
  never replaced.
- `~/.local/share/keyrings/default`, naming it as the default keyring (the old
  pointer is kept as a dated copy).

A blank-password keyring unlocks on first use without a prompt. The existing
`login.keyring` and `user.keystore` are not touched.

The cost is the reason it is off by default. Secrets that apps store from then
on are **plaintext on disk**, readable by anyone who can read the account's
home, and anything already in `login.keyring` stays locked until someone
unlocks it by hand (`seahorse`, or `gnome-keyring-daemon --unlock`). Enable it
only for an appliance whose account holds nothing you would not leave on disk.
The Iris desk box has an equivalent keyring, set up by hand.

## Bluetooth

`desk_appliance_btusb_quirks` (tag `bluetooth`, empty by default) is for a USB
Bluetooth adapter that the installed kernel does not list yet. `btusb` still
binds it, through the generic Bluetooth class match, but the vendor flag
(`BTUSB_MEDIATEK` and the like) comes only from btusb's own quirks table. With
no entry there, the vendor setup and its firmware download never run, so the
controller does not answer: every boot logs
`Bluetooth: hci0: Opcode 0x0c03 failed: -110` and `bluetoothctl` finds no
controller. Two things that look like they should help do not. `modinfo btusb`
does not list the quirks table, because it is not exported as module aliases.
`/sys/bus/usb/drivers/btusb/new_id` copies `driver_info` only from the generic
table, which has no vendor flags.

With a non-empty list the role writes one DKMS module, `desk-appliance-btusb`,
under `/usr/src/desk-appliance-btusb-1`, builds it for every kernel that has
headers installed (a kernel waiting for its reboot included), and reloads
`btusb` when the loaded one is not the one on disk. Before each build, its
`btusb-prepare.sh`:

- takes `drivers/bluetooth` from the `linux-source` package whose version is
  the version of the package that ships the kernel's in-tree `btusb.ko`, so the
  module is the distribution's own `btusb.c` for that kernel, Debian or Kali
  patches included;
- adds every listed id that is not in `quirks_table` already;
- copies the upstream Makefile's `btusb-*` lines, so the multi-file `btusb`
  of 7.3 and later builds too (checked against the mainline Makefile, not
  built against 7.3 sources);
- compiles it.

Whatever goes wrong in that script ends in exit 77, which DKMS records as
skipped, never as failed: no headers, no `linux-source` at the kernel's
version, a tarball that does not extract, a source that already lists every
id, a malformed list, a compile error, and any other failure (an `ERR` trap
turns it into the same exit). The kernel then keeps its in-tree `btusb`, and a
kernel package's postinst is not failed by this module's build. What DKMS does
outside the script (copying the sources in, installing the built module) is
not covered by that. The play prints the script's reason for each skipped
kernel; for a skip during an apt run, it is in
`/var/lib/dkms/desk-appliance-btusb/1/build/make.log`.

`desk-appliance-btusb.service`, a oneshot unit enabled with the module, runs at
boot: if the module is not installed for the kernel that booted, it builds it,
then reloads `btusb`. Once the kernels list the id themselves, empty the list.

Things to know:

- **Kernel upgrades.** DKMS autoinstall builds the module for a new kernel,
  which needs that kernel's headers and its `linux-source` at the same
  version; the two metapackages bring both. When apt configures the new kernel
  before `linux-source` has been unpacked, that build is skipped, and the boot
  unit builds it when the host boots that kernel.
- **Disk and time.** `linux-source` is a 160 MB tarball, decompressed in full
  (one xz thread, at low priority) on every build to reach one directory. The
  compile itself takes a few seconds. A skipped build is tried again on every
  run of this tag and on every boot, which repeats the decompression: empty
  the list once the kernels list the id.
- **Reload.** `modprobe -r btusb` also unloads the modules only it was using
  (the `bt*` helpers, and the `bluetooth` core when nothing else holds it), and
  every adapter bound to `btusb` resets: Bluetooth connections drop and come
  back. The USB controller is not reset, so other devices on it (a USB
  speakerphone, say) are not touched. A host with an empty list that never had
  the module is never reloaded.
- **Secure Boot.** With Secure Boot on, the module has to be signed with a key
  the firmware trusts. DKMS signs with its own MOK key, which has to be enrolled
  once (`mokutil --import`); the role does not do that. If the new `btusb` does
  not load, the role removes its DKMS builds, loads the in-tree `btusb` again
  and fails, so the host is not left without one; the boot unit does the same
  for the kernel that booted.
- **Removal.** An empty list removes the DKMS builds, the sources and the boot
  unit. `dkms`, the headers and `linux-source` stay installed.
- **rfkill.** A controller that never came up may have been soft-blocked
  while it was broken, and systemd-rfkill restores that state at boot. Run
  `rfkill unblock bluetooth` once; the role does not manage rfkill state.

## Things to know

- Membership of the `docker` group is root-equivalent on the host.
- The XFCE power and screensaver settings are written to the user's xfconf
  files and owned by this role: a value changed in the GUI is put back on the
  next run. They are per-user because `xfce4-power-manager` ships its own file
  under `/etc/xdg` as a package conffile.
- Under `--check` on a host that has not been configured yet, per-user tasks
  are skipped (the user does not exist) and the unit-enabling tasks tolerate
  their "unit not found" failures. The update checks (`apt-config dump`, the
  timer read-back, the `unattended-upgrade --dry-run`) are read-only and do run
  under `--check`, except in a dry run that is about to change the files they
  read, where they would judge the old configuration and are skipped. A second
  `--check` after a real run runs all of them and is exact.
- Ubuntu 26.04 also ships an XFCE Wayland session. The named session in the
  LightDM drop-in is what keeps the appliance on X11.
- The xfconf property names were checked against upstream
  (`common/xfpm-config.h` in xfce4-power-manager, `src/gs-prefs.h` in
  xfce4-screensaver) and against the strings in the Kali 4.20 binaries of both
  programs.
- LightDM autologin requires the user to have a real shell; the password stays
  locked.

## What it deliberately does not do

- Join Tailscale. The tag is applied by hand.
- Install the Alloy collector or the Wazuh agent (see Run alongside).
- Give the appliance user `sudo`.
- Unlock or migrate an existing `login` keyring.
- Restart LightDM, PipeWire or WirePlumber on a running session.
- Configure a firewall, remote desktop, SSH or backups.
- Pair Bluetooth devices, change BlueZ settings or rfkill state. The
  `bluetooth` layer only makes an adapter usable.
- Install GPU drivers.
- Manage Wayland, or the lock and power settings of a desktop other than XFCE.
- Install a browser or any desktop autostart application beyond the session
  itself and the one `xset` entry.
- Reboot a host that is not due one: reboots follow `/run/reboot-required` only, which also means a flag from any source (see Patching).

## Verification

Checked in an `ubuntu:26.04` container and in a `kalilinux/kali-rolling`
container, each running systemd: package names resolve,
`--check` on a fresh host completes, a real run completes, a second run
reports no changes and a second `--check` is clean, the rendered apt
configuration reads back as intended (`apt-config dump`), systemd reports the
upgrade timer firing only at the configured time, leftover fleet
`unattended_upgrades` files are removed, the udev rule passes
`udevadm verify`, and a PipeWire daemon started with the echo-cancel drop-in
creates the four named nodes.

To reproduce: build an `ubuntu:26.04` (or `kalilinux/kali-rolling`) image with
`systemd systemd-sysv dbus python3 python3-apt ansible-core sudo ca-certificates
udev iproute2 procps` installed (`CMD ["/usr/lib/systemd/systemd"]`), start it
with `--privileged --cgroupns=host`, `/sys/fs/cgroup` mounted read-write and tmpfs on `/run`,
mount `ansible/` read-only, and run `ansible-playbook` against a local
inventory with one host in `desk_appliances`.

On Kali the same sequence was run with the defaults, and then separately:
`docker_ce` from Docker's Debian repository (installs, second run reports no
changes), `desk_appliance_updates_enabled: true` (the unattended-upgrades dry
run lists the pending kali-rolling packages, which it did not with the Ubuntu
origins) and back to `false` (the role's files are removed, the timer is back
at the distro default, a second run reports no changes), an existing account
whose xfconf file is kept as a dated backup, and the distribution guard
refusing a host that is not in the list.

The `keyring` tag was run separately on `kalilinux/kali-rolling` (no systemd,
`--tags keyring,always`, `desk_appliance_user: sp00f`): the first run installs
`gnome-keyring` and writes both files, a second run reports no changes, and a
`gnome-keyring-daemon` started on a throwaway session bus reports the new
keyring as the default alias, unlocks it without a prompt and stores and reads
back a secret.

The `bluetooth` tag was run on the hardware it was written for, a Kali
Rolling mini PC with a MediaTek MT7902 (Bluetooth `0489:e156`, kernel
`7.1.5+kali-amd64`, which does not list that id):
`--tags bluetooth,always` with an existing account, the build packages
already installed. The run builds and installs the DKMS module from
`linux-source-7.1` for the one kernel with headers and reloads `btusb`;
`modinfo -n btusb` then names `updates/dkms/btusb.ko.xz`, the controller
initialises (`hci0: Device setup in ... usecs`) and `bluetoothctl` finds and
powers it. A second run reports no changes. A list holding only an id the
kernel already has makes the build decline, the play prints the script's
reason, and the in-tree `btusb` is loaded. Emptying the list removes the
builds, the sources and the boot unit and loads the in-tree `btusb`; a second
run with the empty list neither compares nor reloads `btusb` and reports no
changes. Restoring the list reinstalls everything, and the run after that
reports no changes. The boot unit, started by hand, is a no-op while the
module is installed, and after the build for the running kernel was removed it
built it again and reloaded `btusb`. The unit passes `systemd-analyze verify`.
The cold start (the firmware download, about 3.6 s, on a chip that had never
been initialised) was seen with a hand-built module of the same `srcversion`;
the host has not been rebooted onto the DKMS module.
`ansible/tests/desk-appliance-btusb-prepare-test.py` runs `btusb-prepare.sh`
against fixture source trees in both layouts (the single-file `btusb.c` and
the split `btusb_main.c` of 7.3), with a fake kbuild directory standing in for
the compile, drives its dpkg lookup through a fake `dpkg-query` on a
merged-`/usr` module tree, and checks that each way it can fail, an unexpected
one included, exits 77. It passes with gawk and with mawk.

Not checked: anything needing hardware or a display (autologin into a running
desktop, DPMS, USB serial matching, WirePlumber default-device selection,
suspend behaviour), the `arm64` Docker repository path, the `bluetooth` layer
on Ubuntu (only its generic `linux-source` tarball lookup covers Ubuntu's
layout), DKMS autoinstall during a real kernel upgrade (that a skipped build
does not fail it is read from the dkms 3.2.2 source, where autoinstall counts
exit 77 as skipped), a reboot onto the DKMS module, and Secure Boot (signing,
and the fallback when the new `btusb` does not load).
