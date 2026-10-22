# Setup

The steps run in this order. The first half builds the `ingress` host; the
second half ([Wire the network to it](#wire-the-network-to-it)) points the
rest of the network at it. Nothing is wired until the host answers.

## Create the LXC

On the Proxmox host, create a small unprivileged Debian 13 container on the
home LAN bridge with `proxmox/pve-guest.sh` (see [its README](https://github.com/HarshitRuwali/homelab-infra/blob/master/proxmox/README.md)).
It is a dry run until `--apply` is added:

```bash
./pve-guest.sh lxc --name ingress --cores 1 --memory 256 --disk 4 --bridge vmbr0
./pve-guest.sh lxc --name ingress --cores 1 --memory 256 --disk 4 --bridge vmbr0 --apply
```

One core, 256 MB of memory and 4 GB of disk are enough: Caddy and two dnsmasq
instances are small. The script creates an unprivileged container from the
`debian-13-standard` template by default, takes its address from DHCP, and
downloads nothing, so fetch that template with `pveam` first.

### Pass /dev/net/tun into the container

Tailscale needs a TUN device, and an unprivileged container has none. Stop the
container (`pct stop <vmid>`), add two lines to its configuration on the
Proxmox host (`/etc/pve/lxc/<vmid>.conf`), then start it (`pct start <vmid>`):

```text
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Inside the container, `ls -l /dev/net/tun` must show a character device.

## Install the packages

Run as root inside the container. Debian's own `caddy` package is too old for
this module, so Caddy comes from its official apt repository, and Tailscale
from `pkgs.tailscale.com`. `dnsmasq`, `bind9-dnsutils` (for `dig`), `curl`,
`openssl` and `iproute2` (for `ip`) come from Debian.

```bash
apt-get update
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg

curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list

curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
  > /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list \
  > /etc/apt/sources.list.d/tailscale.list

apt-get update
apt-get install -y caddy tailscale dnsmasq bind9-dnsutils curl openssl iproute2
```

The repository lines follow the vendors' own install pages for Debian; check
them there if a download fails. Installing `dnsmasq` starts the packaged
`dnsmasq.service`, which listens on every address. The installer disables it
in [the install step](#install), because the two `ingress-dns` instances need
port 53.

## Join the tailnet

```bash
tailscale up --hostname=ingress --advertise-tags=tag:t7920 --accept-dns=false --accept-routes=false
```

- `--advertise-tags` makes the host a tagged node. The tag must be listed
  under `tagOwners` in your tailnet policy, and the policy decides what the tag
  may reach. The tag name here is an example; use your own.
- `--accept-dns=false` keeps Tailscale from rewriting the host's resolver. The
  host resolves through the LAN, not through itself.
- `--accept-routes=false` keeps routes advertised by other nodes out of this
  host. It reaches upstreams behind OPNsense by their tailnet addresses, not
  through a subnet route.

Complete the login URL it prints, then check that the address exists, because
the installer refuses to run without one:

```bash
tailscale ip -4
```

## Create the data files

Copy the example files to `/etc/ingress` and replace every address with a
real one. They are the only site data; the real copies stay on the host and
are never committed ([Sites](sites.md) has the formats).

```bash
mkdir -p /etc/ingress/cas
cp sites.example.tsv /etc/ingress/sites.tsv
cp hosts.example.tsv /etc/ingress/hosts.tsv
```

### CA files for https upstreams

Each `https` line in `sites.tsv` names a `ca=<file>`. That file is the PEM of
the certificate authority that signed the upstream's certificate, and it goes
in `/etc/ingress/cas`. Proxmox keeps its root at `/etc/pve/pve-root-ca.pem`;
copy it to the container, and do the same for any other upstream with a
private CA. These are public certificates, not keys.

Caddy runs as the `caddy` user and reads these files itself, so each one must
be readable by that user. The installer checks this and stops if one is not:

```bash
chmod 0644 /etc/ingress/cas/*
ls -l /etc/ingress/cas
```

## Install

From a checkout of this repository, run the installer on the `ingress` host
as root. The first run is a dry run: it checks the host, renders and validates
everything into a scratch directory, and prints each action without doing it.

```bash
cd ingress
./install.sh
./install.sh --apply
```

The installer:

1. checks that `caddy`, `dnsmasq`, `tailscale`, `dig`, `curl`, `openssl`, `ip`
   and `runuser` are installed, that both data files exist, that the host has
   a LAN address and a tailnet address (the LAN address is the source address
   of the route to `1.1.1.1`), and that every file in `/etc/ingress/cas` is
   readable by the `caddy` user;
2. renders the Caddyfile, the host files and both dnsmasq configs with
   `render.sh`, then runs `caddy validate` as the `caddy` user, with `HOME` and
   the XDG directories inside the scratch directory so nothing is written
   outside it, and `dnsmasq --test` on the result, so a bad edit never reaches
   the running services;
3. copies the five live files it is about to replace (the Caddyfile and the
   four files under `/etc/ingress/rendered`) into the scratch directory, then
   installs the new ones, and the `ingress-dns@.service` template unit;
4. disables the packaged `dnsmasq.service` if that unit exists, enables and
   reloads Caddy, then enables and restarts `ingress-dns@lan` and
   `ingress-dns@tailnet`. If Caddy or an instance refuses the new files, the
   five files are restored, the services are reloaded or restarted with them,
   and the installer exits non-zero;
5. with `--apply` only, waits for Caddy's root certificate, copies it to
   `/etc/ingress/ca-root.crt` and prints its SHA-256 fingerprint, then checks
   the first site's name against both DNS instances, a name outside `.lan`
   through the LAN instance, and `https` to the first `lan` site with the
   root (from the host itself). When every site is `tailnet`, it checks the
   first one from the host's tailnet address instead, because Caddy refuses
   the host's loopback address for a `tailnet` site.

It exits non-zero at the first failure and names it. Keep the fingerprint: it
is how you confirm, on each device, that the root you installed is this one
([Certificates](certificates.md)).

## Wire the network to it

These steps change other systems, so they are not part of the installer. Do
them in this order, and test after each.

1. **Reserve the LAN address.** Give the container a DHCP reservation for its
   MAC address in the router. Every device is told this address, and the
   rendered files contain it.
2. **AdGuard Home.** Add an upstream rule above the default upstream, so that
   `.lan` goes to the ingress LAN address and everything else is unchanged:

    ```text
    [/lan/]192.0.2.50
    ```

3. **Router DHCP.** Hand out two DNS servers: AdGuard Home first, the ingress
   LAN address second. Devices that cannot reach AdGuard fall back to the
   second ([DNS](dns.md#fallback-behaviour)).
4. **Tailscale Split DNS.** In the admin console, DNS, add a nameserver: the
   ingress tailnet address (`tailscale ip -4` on the host), restricted to the
   domain `lan`.
5. **OPNsense (optional).** Add an Unbound domain override for `lan` that
   forwards to the ingress LAN address, for guests behind OPNsense that use
   Unbound.
6. **Trust the root on each device**, then open a site
   ([Certificates](certificates.md)).
7. **Apps that keep their own host allowlist** need the new name. Nextcloud, for
   example, needs it in `trusted_domains` and the ingress tailnet address in
   `trusted_proxies`:

    ```bash
    occ config:system:set trusted_domains 3 --value=nextcloud.lan
    occ config:system:set trusted_proxies 0 --value=100.64.0.50
    ```

    Use a free index for each. qBittorrent and Grafana worked unchanged.
    If an app answers 400 or "untrusted domain", this is the cause.

## Check it

From the ingress host, with a name from your `sites.tsv`:

```bash
dig +short @192.0.2.50 grafana.lan
dig +short @100.64.0.50 grafana.lan
```

The first answers with the LAN address and the second with the tailnet
address, for a site marked `lan`. [Operations](operations.md#check-the-dns-instances)
has the full set of checks.
