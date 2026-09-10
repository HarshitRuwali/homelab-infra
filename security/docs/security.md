# Security

What this stack itself exposes, and how it is configured not to make things
worse. A security stack that is itself a soft target is a net negative.

## No secrets in the repository

Neither the provisioning script nor the installers contain a credential, and
none accepts one as an argument. LXC guests are created with
`--ssh-public-keys` and no root password. VMs get the same key through
cloud-init.

Wazuh's installer prints its admin password exactly once. Put it straight into
OpenBao rather than leaving it in scrollback.

OpenBao's unseal keys and root token must live **off the guest that runs
OpenBao**. Print them, or store them in a password manager. Auto-unseal that
keeps the keys beside the vault defeats the vault.

## Destructive by explicit action only

`provision-security-stack.sh` is **dry run by default**. It prints what it
would do and creates nothing until `--apply` is passed. The same convention
applies to every installer.

It also refuses to proceed when it is not root, when it is not on a Proxmox
host, when a configured storage ID or bridge does not exist, or when the SSH
key is unreadable. Preflight runs before any guest is touched.

**It never modifies an existing guest.** A VMID that already exists is skipped
with a warning, so re-running after a partial failure cannot clobber work.

## One interface per guest

Every guest is created with a single NIC on one bridge. This is deliberate.

A multi-homed host is a hole in segmentation whether or not it was intended,
and it is invisible in the firewall's own logs, because the traffic never
reaches the firewall. The sandbox segment should reach these guests **through**
the firewall, which is the point of having one.

If a security guest seems to need a second NIC, the routing needs fixing rather
than the guest.

## Do not publish the management planes

None of these UIs belongs on the public internet. A SIEM dashboard reachable
from outside is a worse outcome than the problem it was deployed to solve: it
aggregates, in one place, exactly the information an attacker would otherwise
have to gather host by host.

Put them behind Authelia. If you need remote access, reach them over a VPN or
an authenticated tunnel, never a bare port forward.

## What the monitoring itself discloses

Telemetry is sensitive. A host metrics endpoint left unauthenticated hands over
kernel version, package inventory, core count, memory, disk layout and
hostname, which is a reconnaissance shortcut.

The rule that follows: **anything that reports on a host must require
authentication**, including the tools in this stack. Bind agents' local
interfaces to localhost where the agent only talks to itself, and put a real
auth layer in front of anything with a UI.

## Upstream installers

Several installers here pipe a vendor script to a shell, which is the
installation method those projects document. That is a supply chain trust
decision, and it is worth making consciously rather than by default. If you
would rather not, each project also publishes signed APT repositories; pin
those instead and the rest of the tooling is unaffected.

## Scope

This stack detects and hardens. It does not fix what it finds, and it is not a
substitute for doing so. An unauthenticated datastore is better closed than
monitored: alerting tells you someone reached it, a password means they could
not. Close the door first, then instrument it.
