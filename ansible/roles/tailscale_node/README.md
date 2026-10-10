# tailscale_node

Puts a host on the tailnet under a tag and publishes local services to the
tailnet over HTTPS with `tailscale serve`. It is written for two guests: the
Matrix VM, so the homeserver is reachable on a tailnet-only address, and the
image registry guest.

Target: Debian and Ubuntu (the Tailscale apt repository, per release). The
package install, the service and the join and serve logic were exercised in an
`ubuntu:26.04` container with systemd (see [Verification](#verification));
nothing here has been applied to a live host.

## What it does

| Tag | Does |
|---|---|
| `install` | Adds Tailscale's apt repository (key pinned by SHA-256), installs `tailscale`, enables and starts `tailscaled`. On an LXC guest it first checks `/dev/net/tun` exists. |
| `join` | Runs `tailscale up` with a tagged auth key, only while the node is not `Running`. The key goes to the command as a `0600` file in `/run`, not on the command line, and the file is removed afterwards. Then reads the state back and checks the tags took. |
| `serve` | Applies each `tailscale_node_serve` entry with `tailscale serve --bg --https=<port> <target>`, only for entries that `tailscale serve status --json` does not already show. Fails if any Funnel is on. |

The role is applied by group membership (`tailscale_nodes`), with no host
conditional inside it. It never restarts `tailscaled`.

## Prerequisites: Harshit's steps

Only you can do these. The role does not, and a first run fails or does
nothing useful until they are done.

1. **Admin console, DNS page:** turn on MagicDNS and HTTPS certificates for
   the tailnet. `tailscale serve --https` cannot get a certificate without
   them, and the command waits on a prompt (the role wraps it in `timeout 60`).
2. **Tags.** Add the tags to `tagOwners` in the tailnet policy. This role's
   docs propose `tag:matrix` for the Matrix VM and `tag:registry` for the
   registry guest; the names are your decision, and the role takes whatever you
   put in `tailscale_node_tags`.
3. **Auth keys.** Settings, Keys, generate an auth key that carries the tag.
   Single-use, short expiry, not ephemeral (an ephemeral node disappears from
   the tailnet when it goes offline). If device approval is on, pre-approve the
   key's devices. Put it in the vault, never in a file:

   ```bash
   cd ansible                                              # from the repository root
   ansible-vault edit inventory/group_vars/all/vault.yml   # add vault_tailscale_authkey
   ```

   One key per tag means updating `vault_tailscale_authkey` between the two
   hosts, or one reusable key that allows both tags, revoked once both have
   joined. The role advertises only the tags in `tailscale_node_tags`.
4. **LXC guests only: TUN passthrough.** An unprivileged LXC has no
   `/dev/net/tun`, and `tailscaled` cannot start without it. On the Proxmox
   host, with the guest stopped, add these two lines to
   `/etc/pve/lxc/<id>.conf` (they are raw `lxc.*` keys, so they go in the file,
   not through `pct set`), then start the guest:

   ```text
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   ```

   Check inside the guest: `ls -l /dev/net/tun` shows a character device. The
   role's first task fails with this pointer if it is missing on an LXC.
   (`scripts/pve-guest.sh` from PR 7 creates LXCs with DHCP and `nesting=1` and
   no TUN passthrough, so this is a manual step after it.)
5. **Inventory.** Add the host to `tailscale_nodes` in the gitignored
   `inventory/hosts.local.yml`, and put its tags and serve entries in
   `inventory/host_vars/<host>.yml` (see Example).
6. **Policy grants, once the nodes have tailnet addresses.** Read each address
   with `tailscale ip -4` on the node, then add the grants from the Iris
   `docs/tailscale-policy.md` draft: `tag:iris` to the Matrix node on `tcp:443`,
   and `tag:iris` and the CI runner to the registry node on `tcp:443`.
7. **Elements and bots.** After the Matrix node serves, log Element out and in
   again on each device with the custom homeserver URL
   (`https://<node>.<tailnet>.ts.net`), and point the Iris and delivery-bot
   `.env` files at it. Whether Element on iOS keeps push working on a custom
   homeserver URL is unconfirmed; test it with a real message.

## Variables

Defaults are in `defaults/main.yml`, which has the longer reasoning. Variables
that need a value per host (`tailscale_node_tags`, `tailscale_node_serve`) default to an empty list.

| Variable | Default | Notes |
|---|---|---|
| `tailscale_node_apt_base` | `https://pkgs.tailscale.com/stable` | The repository and key URLs are built from this, the distribution name and the release codename. |
| `tailscale_node_apt_keyring` | `/usr/share/keyrings/tailscale-archive-keyring.gpg` | Where the unarmoured key is stored; the repository line points `signed-by` at it. |
| `tailscale_node_apt_key_sha256` | `3e03dacf222698c60b8e2f990b809ca1b3e104de127767864284e6c228f1fb39` | Checksum the downloaded key must match. A mismatch fails the run. Checked 2026-10-10 against the Ubuntu and Debian key URLs. |
| `tailscale_node_manage_upgrades` | `false` | `false` installs with `state: present`. `true` uses `latest`, so a run of the role upgrades the package, which restarts `tailscaled`. See Patching. |
| `tailscale_node_join` | `true` | `false` leaves tailnet state alone: package and service only. |
| `tailscale_node_authkey` | `vault_tailscale_authkey`, else `REPLACE_ME` | Refused if it is the placeholder or empty, but only when a join is needed. Never shown in output. |
| `tailscale_node_tags` | `[]` | Required for a join: a non-empty list of `tag:<name>`. An untagged server would belong to whoever made the key. |
| `tailscale_node_hostname` | `inventory_hostname` | Machine name on the tailnet; the first label of its `ts.net` name and certificate. |
| `tailscale_node_accept_dns` | `false` | Servers keep their own resolver. |
| `tailscale_node_serve` | `[]` | List of `https_port` and `target`. The target must be `http://127.0.0.1:<port>` (or `https://`). |

`vars/main.yml` holds derived values (the repository URL, the key URL, the key
file path). They are not settings.

## Example

Add the host to `tailscale_nodes` in the gitignored
`inventory/hosts.local.yml`, and keep the tailnet router out of it.

```yaml
# inventory/hosts.local.yml (excerpt)
all:
  children:
    tailscale_nodes:
      hosts:
        matrix:
```

```yaml
# inventory/host_vars/matrix.yml
tailscale_node_tags: ["tag:matrix"]
tailscale_node_serve:
  - https_port: 443
    # Synapse's client listener. Check the real port on the VM before applying:
    #   docker ps --filter name=matrix-synapse --format '{{.Ports}}'
    # The target must be published on the VM's loopback; a port that only exists
    # on the Docker network cannot be reached from the host.
    target: http://127.0.0.1:8008
```

```bash
cd ansible                                        # from the repository root
ansible-playbook playbooks/tailscale-node.yml --check --diff
ansible-playbook playbooks/tailscale-node.yml --limit matrix
ansible-playbook playbooks/tailscale-node.yml --tags serve
```

The play is `playbooks/tailscale-node.yml`:

```yaml
- name: Join the tailnet and publish services
  hosts: tailscale_nodes
  become: true
  roles:
    - tailscale_node
```

The Grafana-to-Matrix relay on the central node reads
`vault_matrix_homeserver_url`. To move it onto the tailnet address, change that
vault value; no role changes. It only works if the central node can reach the
Matrix node on the tailnet and the policy grants it, which is a separate grant
from the `tag:iris` one.

## Run alongside

These are separate roles with their own playbooks and groups; this role does
not call them.

| Role | Playbook | Group |
|---|---|---|
| `image_registry` | `image-registry.yml` | `image_registries` |
| `alloy_collector` | `collectors.yml` | `monitored` |

The registry guest is in both `image_registries` and `tailscale_nodes`: this
role publishes the registry's loopback port on 443, and `image_registry` runs
the registry.

## Patching

- Tailscale's repository is a third-party origin, and a host in `autoupdate`
  applies every origin nightly (`o=*`). A `tailscale` package upgrade restarts
  `tailscaled`, which drops the node off the tailnet for a few seconds. To keep
  it out of the nightly run, put `tailscale` in `uu_package_blacklist` for that
  host (host_vars), as `docs/fleet/patching.md` describes for held packages,
  and upgrade deliberately.
- `tailscale_node_manage_upgrades` only decides what this role does when you run
  it: `false` never upgrades, `true` upgrades. It does not change the
  nightly policy.
- The role never restarts `tailscaled`, and the join step does not run on a node
  that is already `Running`, so a repeat run does not touch the connection.
- The `tailscale-router` host (the exit node and subnet router) must never be in
  `tailscale_nodes`: restarting `tailscaled` there takes the whole estate's
  routed traffic with it.

## Things to know

- **TLS comes from `tailscale serve`.** Tailscale issues a valid `*.ts.net`
  certificate and keeps the listener tailnet-only. This differs from the Iris
  plan's "certificate from the fleet CA" for the registry, because the fleet CA
  key lives on one machine and no role can issue from it. It is a decision for
  Harshit to confirm.
- **Funnel is never enabled**, and the `serve` step fails if any Funnel is on
  for the node, before and after it applies anything.
- **The join is not repeated.** An auth key is used up by use, so `tailscale up`
  runs only while `tailscale status --json` reports a state other than
  `Running`. `NeedsMachineAuth` means device approval is on: approve the node in
  the admin console; the post-join check says so.
- **The router backstop.** The join step refuses a node whose status already
  shows it as an exit node or as serving subnet routes. Group membership is the
  control; this is best effort, because those fields only appear once routes are
  approved.
- **`--check` on a bare host.** A dry run on a host that has never been
  configured skips the join and the serve steps (nothing to read yet), and the
  package step tolerates the repository not being there yet. The repository
  task itself needs `gnupg` on the host. A second `--check` after a real run is
  exact.
- **Synapse behind the proxy.** Synapse sees every request as coming from
  `127.0.0.1` unless its listener sets `x_forwarded: true` in
  `homeserver.yaml`, which this repository does not manage.
- **`tailscale ssh` is off** (`--ssh=false`). The role does not manage SSH.

## What it deliberately does not do

- Create tags, auth keys, grants or the tailnet policy. Those are the admin
  console's.
- Deploy Synapse. The Matrix compose project (`matrix-synapse`,
  `matrix-postgres`, `matrix-cloudflared`) is not in this repository.
- Pass `/dev/net/tun` through to an LXC guest, or create the guest.
- Enable Funnel, advertise routes, or make the node an exit node.
- Replace the Cloudflare tunnel for the Matrix VM: it stays for everyone who is
  not on the tailnet.
- Restart `tailscaled`, or log a node out, or remove a serve entry (remove one
  by hand with `tailscale serve --https=<port> off`).
- Manage the tailnet router.

## Verification

Checked in an `ubuntu:26.04` container running systemd, with `--check` on a
fresh container, a real run, a second run that reported no changes, and a
second `--check` that reported no changes. That covered the signing key
download against the pinned checksum, the repository, the package, the
`tailscaled` service (which came up in `NeedsLogin`), and the state parsing
against the real daemon's JSON.

The join and serve logic was exercised against a stub `tailscale` command that
returns the documented JSON shapes, not against a tailnet: a first run joined
and published, a second run changed nothing, and a second `--check` changed
nothing. Against the stub it also refused a placeholder key, an empty tag list,
a non-loopback serve target, a node that looks like an exit node, a node with
Funnel on, and a serve entry on a node that is not joined. The auth key file
mode (`0600`) and its removal were observed, and the key does not appear in the
stub's argument log.

To reproduce: build an `ubuntu:26.04` image with `systemd systemd-sysv dbus
python3 python3-apt ansible-core sudo ca-certificates udev iproute2 procps curl
iptables kmod gnupg` installed (`CMD ["/usr/lib/systemd/systemd"]`), start it
with `--privileged --cgroupns=host`, `/sys/fs/cgroup` mounted read-write and
tmpfs on `/run`, mount `ansible/` read-only, and run `ansible-playbook` against
a local inventory with one host in `tailscale_nodes`, `tailscale_node_join:
false` and the authkey left at its placeholder.

Not checked: a real `tailscale up` with an auth key, the real format of
`tailscale serve status --json` (the parsing follows Tailscale's published
`ServeConfig` structure; a mismatch makes the role re-apply the entries on
every run rather than fail), certificate issuance, a real LXC without TUN
passthrough, Debian 13, the `arm64` repository, and Element push on a custom
homeserver URL.
