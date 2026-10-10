# image_registry

A private container image registry for the Iris project: CI pushes the images
it builds, and the desk box pulls them. It runs the CNCF Distribution registry
(`registry:2`) in a Docker Compose project on `127.0.0.1`, with one htpasswd
credential from the vault and a weekly garbage collection. The tailnet side
(the HTTPS listener and its certificate) is the `tailscale_node` role's job.

Target: Debian and Ubuntu. The Docker install, the Compose project, the
credential, the garbage collection and a push and pull through the running
registry were exercised in an `ubuntu:26.04` container with systemd (see
[Verification](#verification)); nothing here has been applied to a live host.

## What it does

| Tag | Does |
|---|---|
| `docker` | Installs Docker CE and the Compose plugin from Docker's apt repository (key pinned by SHA-256), only when the host has no `docker` at all; enables the service; checks `docker compose` works. |
| `config` | Creates `/opt/image-registry` and the data directory; writes `config.yml` and `compose.yml`; writes the bcrypt htpasswd file (private, from the vault password) only when the stored entry does not already match; recreates the container if the configuration changed. |
| `gc` | Installs `fleet-registry-gc.sh`, its oneshot service and a weekly timer, and a metric file for the textfile collector. |
| `service` | Runs `docker compose up -d`, then checks the registry refuses an anonymous call (401) and accepts the credential (200). |

The role is applied by group membership (`image_registries`), with no host
conditional inside it. Run `config` and `service` together.

## Prerequisites: Harshit's steps

Only you can do these.

1. **Create the guest.** An LXC on the hypervisor with enough disk for the
   images (the `pve-guest.sh` default of 8 GB will not hold many builds; size
   the data directory's filesystem for your image history). Docker inside an
   unprivileged LXC needs `nesting=1`, which `pve-guest.sh` sets. The guest also
   needs the TUN passthrough described in `roles/tailscale_node/README.md`,
   Prerequisites, step 4.
2. **Onboard it** with `playbooks/onboard.yml` like any other guest, so it is
   in the inventory and reachable.
3. **Inventory.** Add the host to `image_registries` and to `tailscale_nodes`
   in the gitignored `inventory/hosts.local.yml`, and set its tags and serve
   entry in `inventory/host_vars/<host>.yml` (see Example).
4. **The credential.** Choose a password of at least 12 characters and put it in
   the vault as `vault_image_registry_push_password`:

   ```bash
   cd ansible                                              # from the repository root
   ansible-vault edit inventory/group_vars/all/vault.yml
   ```

5. **Tailnet side.** Everything in `roles/tailscale_node/README.md`,
   Prerequisites: the tag (proposed here as `tag:registry`), an auth key, HTTPS
   certificates enabled for the tailnet, and the grants that let `tag:iris`
   and the CI runner reach the node on `tcp:443`.
6. **Log in from each client.** The box and the CI runner each run
   `docker login <node>.<tailnet>.ts.net` once with the credential. Put the
   credential in the client's own secret store, not in a repository.
7. **Exclude it from container auto-updates** if the host is in `autoupdate`:
   `docker_update_skip_projects: [image-registry]` in its `host_vars` (see
   Patching).

## Variables

Defaults are in `defaults/main.yml`, which has the longer reasoning.

| Variable | Default | Notes |
|---|---|---|
| `image_registry_docker_install` | `true` | Install Docker CE when `docker` is absent. `false` requires an existing engine with Compose v2. |
| `image_registry_docker_apt_url` | `https://download.docker.com/linux/<distribution>` | Built from the distribution name. |
| `image_registry_docker_apt_key_url` | `<apt url>/gpg` | |
| `image_registry_docker_apt_keyring` | `/etc/apt/keyrings/docker.asc` | |
| `image_registry_docker_apt_key_sha256` | `1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570` | Checksum the downloaded key must match; a mismatch fails the run. Checked 2026-10-10 against the Ubuntu and Debian key URLs. |
| `image_registry_docker_packages` | `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin` | |
| `image_registry_project_name` | `image-registry` | The Compose project name; what `docker_update_skip_projects` matches. |
| `image_registry_project_dir` | `/opt/image-registry` | Holds `compose.yml`, `config.yml` and `auth/`. |
| `image_registry_data_dir` | `/var/lib/image-registry` | The registry's blobs. Put it on a filesystem sized for your images. |
| `image_registry_image` | `registry:2.8.3` | A patch tag. Pin by digest (`registry:2.8.3@sha256:...`) once you have chosen a release. |
| `image_registry_port` | `5000` | Host port on `127.0.0.1`. The `tailscale_node_serve` target must match it. |
| `image_registry_delete_enabled` | `true` | Lets clients delete manifests, which garbage collection needs. |
| `image_registry_user` | `registry` | The htpasswd user. |
| `image_registry_password` | `vault_image_registry_push_password`, else `REPLACE_ME` | At least 12 characters; the placeholder is refused. Never shown in output. |
| `image_registry_gc_oncalendar` | `Sun *-*-* 05:00` | systemd calendar form; checked with `systemd-analyze calendar`. |
| `image_registry_gc_randomized_delay` | `900` | Seconds. |
| `image_registry_gc_delete_untagged` | `false` | Adds `--delete-untagged`. See Things to know. |
| `image_registry_metrics_dir` | `alloy_textfile_dir`, else `/var/lib/node_exporter/textfile_collector` | |
| `image_registry_metrics_file` | `fleet-registry-gc.prom` | |
| `image_registry_ready_retries` | `30` | How many times `service` asks the registry for a 401 before failing. |
| `image_registry_ready_delay` | `2` | Seconds between those asks. |

`vars/main.yml` holds derived paths and values. They are not settings.

## Example

Add the host to both groups in the gitignored `inventory/hosts.local.yml`.

```yaml
# inventory/hosts.local.yml (excerpt)
all:
  children:
    image_registries:
      hosts:
        registry:
    tailscale_nodes:
      hosts:
        registry:
```

```yaml
# inventory/host_vars/registry.yml
tailscale_node_tags: ["tag:registry"]
tailscale_node_serve:
  - https_port: 443
    target: http://127.0.0.1:5000        # image_registry_port

# Keep the container-update timer away from the registry (see Patching).
docker_update_skip_projects: [image-registry]
```

```bash
cd ansible                                        # from the repository root
ansible-playbook playbooks/image-registry.yml --check --diff
ansible-playbook playbooks/image-registry.yml --limit registry
ansible-playbook playbooks/tailscale-node.yml --limit registry
ansible-playbook playbooks/image-registry.yml --tags gc
```

The play is `playbooks/image-registry.yml`:

```yaml
- name: Run the image registry
  hosts: image_registries
  become: true
  roles:
    - image_registry
```

Once it is published, a client uses it like this (the host name is the node's
MagicDNS name):

```bash
docker login <node>.<tailnet>.ts.net
docker tag myimage:1 <node>.<tailnet>.ts.net/iris/myimage:1
docker push <node>.<tailnet>.ts.net/iris/myimage:1
```

## Run alongside

These are separate roles with their own playbooks and groups; this role does
not call them.

| Role | Playbook | Group |
|---|---|---|
| `tailscale_node` | `tailscale-node.yml` | `tailscale_nodes` |
| `alloy_collector` | `collectors.yml` | `monitored` |

## Patching

- Docker's repository is a third-party origin. On a host in `autoupdate` the
  nightly run applies every origin, so a `docker-ce` or `containerd.io` upgrade
  restarts `dockerd` and the registry container with it. The container comes
  back (`restart: unless-stopped`) but a push in flight at that moment fails and
  is retried by the client.
- The container image is pinned to a patch tag. Move it deliberately by editing
  `image_registry_image` and re-running the role.
- `docker_updates` pulls and re-applies every Compose project it finds running.
  Add `image-registry` (the `image_registry_project_name` value) to
  `docker_update_skip_projects` in the host's `host_vars`, so a re-pull can
  never recreate the registry at 04:00. The skip list matches the project name.
- Garbage collection stops the registry for its run. The default time, Sunday
  05:00 plus up to 15 minutes, is after the 03:00 patching window and the 04:00
  container-update window.

## Things to know

- **TLS comes from `tailscale serve`**, in the `tailscale_node` role: a
  Tailscale-issued `*.ts.net` certificate, tailnet only. The Iris plan said "a
  certificate from the fleet CA". The fleet CA key lives on one machine and no
  role can issue from it, so this is a deliberate change that Harshit should
  confirm. The registry itself listens on plain HTTP on `127.0.0.1` only.
- **Pulling needs the credential too.** The registry's htpasswd authentication
  has no read-only role and no anonymous mode: an anonymous pull in the test was
  refused ("no basic auth credentials"). A Docker client also only sends
  credentials once the registry challenges it (the likely reason a proxy that lets
  reads through breaks pushes; not tried here). Every client logs in with the one
  credential, which also lets it push and delete. The tailnet policy is what
  limits who can reach it. Per-repository access control and anonymous read are
  features of the alternative, Zot, which is the likely next step if that matters.
- **One credential, rotated by rerunning the role.** Changing
  `vault_image_registry_push_password` and re-running rewrites the htpasswd
  file; the registry reads it again when it changes, with no restart.
- **Garbage collection is not online.** It stops the registry, collects, and
  starts it again, because collecting while pushes arrive can delete a blob an
  upload is about to reference. `--delete-untagged` is off by default because the
  per-platform manifests of a multi-arch image are untagged by design, and the
  flag has been reported to remove them; test it before turning it on.
  The run writes `fleet_registry_gc_last_success`, `..._last_run_timestamp_seconds`
  and `..._duration_seconds` to the textfile collector. No alert rule uses
  them yet.
- **Docker is installed only if absent.** A host with `docker.io` keeps it, and
  the role only checks that `docker compose` works.
- **Disk.** Nothing prunes images by age. Delete old tags through the API
  (deletion is enabled) and let the weekly collection reclaim the space.
- **`--check` on a bare host.** The Docker repository task needs `gnupg` on the
  host, and the package step tolerates the repository not being there yet. The
  startup probe does not wait under `--check`. A second `--check` after a real
  run is exact.

## What it deliberately does not do

- Join the tailnet or publish the registry (the `tailscale_node` role).
- Issue a certificate from the fleet CA, or terminate TLS itself.
- Provide a read-only credential or anonymous pulls.
- Run package caches (Kellnr, devpi); the Iris plan adds those when a second
  consumer appears.
- Create the guest, or pass `/dev/net/tun` through to it.
- Mirror or pull-through-cache Docker Hub.
- Alert on garbage collection or disk use.
- Back up the data directory.

## Verification

Checked in an `ubuntu:26.04` container running systemd: `--check` on a fresh
container, a real run (Docker CE from Docker's repository, the Compose project,
the container up on `127.0.0.1`), a second run that reported no changes and a
second `--check` that reported no changes. Against that running registry:
a push and a pull without credentials were refused, a login with a wrong
password was refused, and with the right password a push, a pull and the
catalog listing worked. Changing the password and re-running the role changed
exactly the htpasswd entry; the old password then got 401 and the new one 200
without a restart. The garbage collection service ran from systemd, the registry
was back afterwards, the metric file was written, and after a manifest was
deleted through the API a collection reclaimed the blob space.

To reproduce: build an `ubuntu:26.04` image with `systemd systemd-sysv dbus
python3 python3-apt ansible-core sudo ca-certificates udev iproute2 procps curl
iptables kmod gnupg` installed (`CMD ["/usr/lib/systemd/systemd"]`), start it
with `--privileged --cgroupns=host`, `/sys/fs/cgroup` mounted read-write, tmpfs
on `/run`, and volumes on `/var/lib/docker` and `/var/lib/containerd` (overlay
on overlay does not work), mount `ansible/` read-only, and run
`ansible-playbook` against a local inventory with one host in
`image_registries`, `image_registry_password` set to a throwaway value.

Not checked: the path through `tailscale serve` and a Tailscale certificate
(a client over HTTPS to the node's `ts.net` name, and the registry's relative
`Location` headers behind that proxy), a real LXC guest, Debian 13, the `arm64`
Docker repository, `image_registry_gc_delete_untagged: true`, a pin by digest,
and `docker_updates` skipping the project.
