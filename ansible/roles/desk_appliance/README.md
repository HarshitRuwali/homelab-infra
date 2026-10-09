# desk_appliance

Baseline for an always-on desk appliance: a mini PC with a real X11 desktop
session that an agent (or a person) drives. It logs itself in, never blanks,
locks or suspends, has audio and a stable serial symlink, runs Docker, and
patches and reboots itself in the small hours.

Target: Ubuntu 26.04, x86_64. The package names and the generated
configuration were exercised in an `ubuntu:26.04` container with systemd
(see [Verification](#verification)); nothing here has been run on the
hardware itself.

## What it does

| Tag | Does |
|---|---|
| `desktop` | Installs Xorg, XFCE (variable) and LightDM; autologin as `desk_appliance_user` into a named X11 session; boots to `graphical.target`. |
| `power` | Ignores lid, suspend and hibernate in logind and masks the sleep targets; turns off the X server screen saver and DPMS; an autostart `xset s off -dpms`; XFCE power manager, screensaver and lock settings; hides the screen locker autostart entries. |
| `audio` | PipeWire with ALSA and Pulse layers and WirePlumber; the user in `audio`; optional default sink and source; optional WebRTC echo cancellation. |
| `serial` | The user in `dialout`; a udev rule giving each listed USB serial device a stable `/dev/<name>` symlink. |
| `docker` | Docker engine and the Compose plugin from Ubuntu or from Docker's repository; the user in `docker`. |
| `updates` | `unattended-upgrades` for security updates only, with an automatic reboot at a set time when one is needed. |

The appliance user is created by the role (locked password, no sudo) under
the `always` tag, so every tag subset has an account to work with.

The role is applied by group membership (`desk_appliances`), with no host
conditional inside it. It refuses to run on a host that is also in
`autoupdate` or `no_autoupdate`; see [Patching](#patching-and-reboots).

## Variables

Defaults are in `defaults/main.yml`, which has the longer reasoning. Variables
with no default are optional and leave their feature off while unset.
Everything host-specific (names, ids) is a variable; nothing is baked in.

| Variable | Default | Notes |
|---|---|---|
| `desk_appliance_user` | `iris` | Autologin account; created if missing. Not `root`. |
| `desk_appliance_home` | `/home/<user>` | |
| `desk_appliance_user_shell` | `/bin/bash` | Must be a real shell: LightDM's autologin PAM stack refuses `nologin`. |
| `desk_appliance_create_user` | `true` | `false` if the account already exists. |
| `desk_appliance_session` | `xfce` | X11 session file name in `/usr/share/xsessions`. The role fails if it is not there. XFCE settings are only written when this is `xfce`. |
| `desk_appliance_desktop_packages` | Xorg, LightDM and XFCE package list | Installed without recommends. |
| `desk_appliance_extra_packages` | `[]` | Anything else the desktop needs. |
| `desk_appliance_hidden_autostart` | `xfce4-screensaver`, `light-locker`, `xscreensaver`, `gnome-screensaver` | Per-user `Hidden=true` overrides. |
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
| `desk_appliance_docker_source` | `distro` | `distro` (`docker.io`, `docker-compose-v2`) or `docker_ce` (Docker's repository). |
| `desk_appliance_docker_packages` | per-source package lists | |
| `desk_appliance_docker_arch` | derived from the host | apt architecture name for the Docker repository. |
| `desk_appliance_docker_apt_url` | `https://download.docker.com/linux/ubuntu` | |
| `desk_appliance_docker_apt_key_url` | `https://download.docker.com/linux/ubuntu/gpg` | |
| `desk_appliance_docker_apt_keyring` | `/etc/apt/keyrings/docker.asc` | |
| `desk_appliance_uu_origins` | Ubuntu security, ESM infra and apps security | Replaces the distro's origin list. |
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
desk_appliance_user: iris
desk_appliance_serial_devices:
  - name: ttydevice
    vendor_id: "1a86"
    product_id: "7523"
desk_appliance_echo_cancel: true
desk_appliance_audio_default_sink: alsa_output.example-sink-name
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
- A reboot happens only when an update leaves `/var/run/reboot-required`.
- `apt-daily-upgrade.timer` is moved to 03:30 so the run ends before the
  04:30 reboot time. If a run ends after the reboot time, the reboot is
  scheduled for the next day. Change the timer and the reboot time together.
- `desk_appliance_docker_source: docker_ce` installs from a repository that is
  not in the security-only origin list. Its updates need a manual
  `apt upgrade`, or an origin added to `desk_appliance_uu_origins`.

## Things to know

- Membership of the `docker` group is root-equivalent on the host.
- The XFCE power and screensaver settings are written to the user's xfconf
  files and owned by this role: a value changed in the GUI is put back on the
  next run. They are per-user because `xfce4-power-manager` ships its own file
  under `/etc/xdg` as a package conffile.
- Under `--check` on a host that has not been configured yet, per-user tasks
  are skipped (the user does not exist) and the unit-enabling tasks ignore
  their "unit not found" errors. A second `--check` after a real run is exact.
- Ubuntu 26.04 also ships an XFCE Wayland session. The named session in the
  LightDM drop-in is what keeps the appliance on X11.
- LightDM autologin requires the user to have a real shell; the password stays
  locked.

## What it deliberately does not do

- Join Tailscale. The tag is applied by hand.
- Install the Alloy collector or the Wazuh agent (see Run alongside).
- Give the appliance user `sudo`.
- Restart LightDM, PipeWire or WirePlumber on a running session.
- Configure a firewall, remote desktop, SSH, backups or Bluetooth.
- Install GPU drivers.
- Manage Wayland, or the lock and power settings of a desktop other than XFCE.
- Install a browser or any desktop autostart application beyond the session
  itself and the one `xset` entry.
- Reboot a host that is not due one: reboots follow `reboot-required` only.

## Verification

Checked in an `ubuntu:26.04` container running systemd: package names resolve,
`--check` on a fresh host completes, a real run completes, a second run
reports no changes, the rendered apt configuration reads back as intended
(`apt-config dump`), the udev rule passes `udevadm verify`, and a PipeWire
daemon started with the echo-cancel drop-in creates the four named nodes.
Not checked: anything needing hardware or a display (autologin into a running
desktop, DPMS, USB serial matching, WirePlumber default-device selection,
suspend behaviour) and the `arm64` Docker repository path.
