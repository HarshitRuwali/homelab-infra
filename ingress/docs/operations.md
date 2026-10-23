# Operations

Run everything here as root on the ingress host, from the `ingress/` directory
of a checkout. The installer is idempotent and is the only supported way to
change the running configuration.

## Add, change or remove a site

1. Edit `/etc/ingress/sites.tsv` (or `hosts.tsv`) following [Sites](sites.md).
   For an `https` upstream, put its CA file in `/etc/ingress/cas` first.
2. Dry run. It renders and validates the result and prints what it would do,
   and changes nothing:

    ```bash
    ./install.sh
    ```

3. Apply:

    ```bash
    ./install.sh --apply
    ```

A bad edit stops in the render and validate stage, naming the file and line,
before any live file is replaced, so the running services keep the previous
configuration. Removing a line removes the site and its name at the next
`--apply`. Devices need nothing new: a new name is covered by the root they
already trust ([Certificates](certificates.md)). An app with its own host
allowlist needs the new name added there ([Setup](setup.md#wire-the-network-to-it)).

## Preview an edit

`render.sh` writes its output into a directory you choose and touches nothing
else, so you can read exactly what an edit produces:

```bash
mkdir -p /tmp/ingress-preview
./render.sh --sites /etc/ingress/sites.tsv --hosts /etc/ingress/hosts.tsv \
  --lan-ip 192.0.2.50 --tailnet-ip 100.64.0.50 \
  --cas /etc/ingress/cas --out /tmp/ingress-preview
```

It writes `Caddyfile`, `hosts-lan`, `hosts-tailnet`, `dnsmasq-lan.conf` and
`dnsmasq-tailnet.conf`. Use the host's real addresses in place of the examples.

## Check the DNS instances

Ask each instance directly, with a name from your `sites.tsv`. A `lan` site
answers with the LAN address from the first and the tailnet address from the
second:

```bash
dig +short @192.0.2.50 grafana.lan A
dig +short @100.64.0.50 grafana.lan A
```

An unknown `.lan` name must be refused, and a name outside `.lan` must still
resolve (this is the fallback behaviour in [DNS](dns.md#fallback-behaviour)):

```bash
dig @192.0.2.50 nosuch.lan A | grep status
dig +short @192.0.2.50 example.com A
```

The first prints `status: NXDOMAIN`, the second an address.

## Check a site

From the host, with the root the installer saved. `--resolve` skips DNS, so
this tests Caddy and the upstream alone:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' --cacert /etc/ingress/ca-root.crt \
  --resolve grafana.lan:443:127.0.0.1 https://grafana.lan/
```

To test a `tailnet` site, run the same request from a tailnet device that
trusts the root, then from a LAN-only device: the first answers, the second is
dropped.

## Logs and status

```bash
systemctl status caddy ingress-dns@lan ingress-dns@tailnet
journalctl -u caddy
journalctl -u ingress-dns@lan
journalctl -u ingress-dns@tailnet
```

`journalctl -u caddy -f` follows requests and certificate events live.

## When something fails

| Symptom | Likely cause |
|---|---|
| `install.sh` stops with `not on the tailnet yet` | `tailscale ip -4` is empty: run `tailscale up` ([Setup](setup.md#join-the-tailnet)) |
| `install.sh` stops with `not readable by the caddy user` | A file in `/etc/ingress/cas` (listed above the message) can only be read by root: `chmod 0644` it. These are public certificates |
| `install.sh` stops with `Caddy refused the new Caddyfile` | The validated Caddyfile was still refused at reload. The previous files were restored and Caddy reloaded with them; read `journalctl -u caddy` |
| `install.sh` stops with `failed with the new files` | A DNS instance would not start with the new files. The previous files were restored and both instances restarted; read `journalctl -u ingress-dns@lan` or `@tailnet` |
| `install.sh` stops with `answered '502'` or another status in its last step | The site's upstream is down or wrong, not the ingress host: test the upstream with `curl` from the host |
| `install.sh` stops with `the rendered Caddyfile does not validate` | Read the Caddy error it prints above; usually a CA file that is not a PEM |
| `install.sh` stops with `dnsmasq-lan.conf does not validate` | The rendered dnsmasq config has a syntax problem: run the preview and read the config |
| A `render.sh` error naming `sites.tsv` and a line | See the table in [Sites](sites.md#what-rendersh-rejects) |
| Browser warns about the certificate | The device does not trust the root, or iOS full trust is off ([Certificates](certificates.md)) |
| `NXDOMAIN` for a listed name | The query reached an instance that does not have the name: check the AdGuard rule and Split DNS ([DNS](dns.md)) |
| 502 from a site | Caddy cannot reach the upstream: test it with `curl` from the host, and check `tls` for an `https` upstream |
| An app says "untrusted domain" or answers 400 | The app's own host allowlist lacks the new name ([Setup](setup.md#wire-the-network-to-it)) |
| `ingress-dns@lan` fails to start | Another process holds port 53 on that address: check that `dnsmasq.service` is disabled |
| Names stop answering after the host's LAN address changed | The addresses are baked into the rendered files: re-run `./install.sh --apply`, and fix the DHCP reservation |

## Upgrade

Update the checkout, then re-apply, which re-renders and reinstalls the unit:

```bash
git pull
./install.sh
./install.sh --apply
```

Caddy, Tailscale and dnsmasq update through `apt`. Back up `/etc/ingress`
(the data files and CA certificates) with your other host configuration.
`/var/lib/caddy` holds the root's private key: see
[what this costs](certificates.md#what-this-costs) before putting it in a backup.

## Roll back

- **A service refusing an apply:** nothing to do. `install.sh` keeps a copy of
  the five live files it replaces, puts them back when Caddy or a DNS instance
  refuses the new ones, and exits non-zero. A copy is held only for the run.
- **A bad site edit:** restore the previous `sites.tsv` or `hosts.tsv` from
  your copy of `/etc/ingress`, and run `./install.sh --apply`. Keep a copy before
  large edits; the installer does not keep one.
- **A bad module version:** check out the previous tag, `ingress-vX.Y.Z` as in
  [Contributing](https://github.com/HarshitRuwali/homelab-infra/blob/master/CONTRIBUTING.md#deploying-a-pinned-version),
  and run `./install.sh --apply` from it.
- **Back out the whole thing:** undo the wiring in [Setup](setup.md#wire-the-network-to-it)
  in reverse (remove the Split DNS nameserver, the router's second DNS server
  and the AdGuard rule), then on the host:

    ```bash
    systemctl disable --now ingress-dns@lan ingress-dns@tailnet caddy
    ```

  Remove the root from each device that trusted it. Nothing else on the host
  depends on this module.

## What lives where

| Path | Holds |
|---|---|
| `/etc/ingress/sites.tsv`, `hosts.tsv` | Your data. Never committed |
| `/etc/ingress/cas/` | Upstream CA files |
| `/etc/ingress/ca-root.crt` | The root to install on devices |
| `/etc/ingress/rendered/` | Host files and dnsmasq configs, written by `install.sh` |
| `/etc/caddy/Caddyfile` | The rendered Caddyfile. Do not edit it by hand |
| `/etc/systemd/system/ingress-dns@.service` | The template unit |
| `/var/lib/caddy` | Caddy's state, including the CA's private key |
