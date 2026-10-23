# Security

What this stack itself exposes, and how it is configured not to make things
worse. A security stack that is itself a soft target is a net negative.

## No secrets in the repository

Neither the provisioning script nor the installers contain a credential.
Enrollment secrets are read from protected files, never passed as command-line
values. LXC guests are created with
`--ssh-public-keys` and no root password. VMs get the same key through
cloud-init.

Wazuh's installer prints its admin password at the end, and also writes every
internal password and the TLS certificates to `wazuh-install-files.tar` in
`WAZUH_WORKDIR` (default `/root/wazuh-install`). Put the password in a password manager, and move the tar off the guest.

Two services start with a **known default login**, `admin` / `admin`: Greenbone
and ntopng. ntopng forces a change at first login; Greenbone does not, so change
it before anything else. AdGuard Home has no default, but whoever reaches its
setup wizard first chooses the admin login, so finish the wizard straight after
install.

Keep service credentials in a password manager.

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
`sec-scan` differs in *which* bridge, not in how many: it is the one guest on
the sandbox side, for reasons in
[Architecture](architecture/index.md#the-scanner-is-the-exception).

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

Keep management interfaces bound to loopback or the LAN and reach them over
SSH tunnels. If you need remote
access, use a VPN or an authenticated tunnel, never a bare port forward.

ntopng's UI deserves a specific note: it runs on the firewall and listens on
**every** firewall interface. The WAN is closed by default. Keep it closed.

## What the monitoring itself discloses

Telemetry is sensitive. A host metrics endpoint left unauthenticated hands over
kernel version, package inventory, core count, memory, disk layout and
hostname, which is a reconnaissance shortcut.

The rule that follows: **anything that reports on a host must require
authentication**, including the tools in this stack. Bind agents' local
interfaces to localhost where the agent only talks to itself, and put a real
auth layer in front of anything with a UI.

## Upstream installers

Installers download vendor scripts with HTTP error checking into a root-only
directory, then execute them only after a complete successful download. AdGuard
also verifies its service is active. Wazuh uses an explicit major/minor installer
URL (`WAZUH_VERSION`, default `4.14`). Vendor scripts and the Greenbone Compose
file still come from upstream and are a supply-chain trust dependency. Mutable
upstream content is not reproducible.

## Bootstrap inputs

Before installing Wazuh or CrowdSec, place these files on the intended guest
under `/root/security-bootstrap` (directory mode `0700`, private files `0600`):

| Guest | Required files | Environment overrides |
|---|---|---|
| Wazuh | `wazuh-enrollment.password`, `wazuh.crt`, `wazuh.key` | `WAZUH_ENROLLMENT_PASSWORD_FILE`, `WAZUH_TLS_CERT_FILE`, `WAZUH_TLS_KEY_FILE` |
| CrowdSec | `crowdsec.crt`, `crowdsec.key`, `ca.crt` | `CROWDSEC_TLS_CERT_FILE`, `CROWDSEC_TLS_KEY_FILE`, `CROWDSEC_TLS_CA_FILE` |

Issue server certificates from a CA you control. Include the service DNS name
in each certificate's SAN, distribute the CA through a trusted channel, and
keep the CA private key off these guests. Private keys must be unencrypted for
unattended service startup. Certificates are checked for expiry and key match.
Set `CROWDSEC_LAPI_URL=https://<certificate-dns-name>:8080`; that name must also
resolve on the LAPI guest because its local client uses the same verified URL.
Configure each remote CrowdSec agent and bouncer to trust the CA and use HTTPS;
never set `insecure_skip_verify` to work around a certificate problem.

The Wazuh enrollment password must be at least 20 characters without whitespace
(one trailing newline in the file is accepted). Store the same value as
`vault_wazuh_enrollment_password` in Ansible Vault. Set `wazuh_manager_ca_src` to
the CA PEM on the controller, and add the manager to `wazuh_manager` in your
private inventory. The agent role installs both files with mode `0640`, verifies
the requested group exists on the manager, and collects journald events.
`WAZUH_AGENT_GROUP` defaults to `homelab`; the configure command creates it.

**Keep network access to enrollment port 1515 blocked during initial vendor
installation.** Open it only after the configure step has succeeded and the
agents have their credentials. The vendor installer starts services before our
hardening step runs. Dashboard credentials are separate from enrollment
credentials; neither substitutes for the other.

Existing installations can apply configuration without rerunning vendor setup:

```bash
# Inside the corresponding guest; dry run without --apply.
bash install/configure-sec-wazuh.sh --apply
bash install/configure-sec-crowdsec.sh --apply
```

The configure commands require Python 3 and OpenSSL; CrowdSec also requires
`python3-yaml`. The installers provide these dependencies. Prepare and distribute
the agent credentials and CA before changing an existing manager. These commands
stop their service while changing configuration, then validate and restart it.
If validation fails after the stop, the service stays stopped for investigation.
Changing CrowdSec from HTTP to HTTPS requires updating all its clients in the
same maintenance window. Back up the existing configurations and credentials
before that change; reinstalling is not a migration procedure.

## Provisioning failure recovery

Once a create operation has succeeded, a later provisioning failure triggers
cleanup even when the failed command ran inside a helper function. Cleanup is
armed only after creation succeeds, so a VMID collision cannot delete someone
else's guest. If creation itself fails after allocating partial state, or cleanup
fails, inspect the VMID manually; an existing VMID is still skipped on rerun.

## Scope

This stack detects and hardens. It does not fix what it finds, and it is not a
substitute for doing so. An unauthenticated datastore is better closed than
monitored: alerting tells you someone reached it, a password means they could
not. Close the door first, then instrument it.
