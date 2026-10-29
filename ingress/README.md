# ingress

HTTPS names under `.lan` for the web UIs in a homelab, such as
`https://grafana.lan`, reachable from the home LAN and from the tailnet. One
small Debian LXC on the LAN bridge, joined to the tailnet, runs Caddy (a
reverse proxy with its own certificate authority) and two dnsmasq instances
(one answers LAN clients, one answers tailnet clients). Everything is rendered
from two tab-separated files. A site marked `tailnet` is reachable from the
tailnet only.

**Documentation:** <https://harshitruwali.github.io/homelab-infra/ingress/>

## Quick start

Run as root. Needs a Debian 13 host on the LAN with `/dev/net/tun`, joined to the tailnet,
with `caddy` (from the official Caddy apt repository), `tailscale`, `dnsmasq`,
`bind9-dnsutils`, `curl`, `openssl` and `iproute2` installed. [Setup](docs/setup.md) covers creating
the LXC and installing those. The installer installs no packages.

```bash
cd ingress
mkdir -p /etc/ingress/cas                   # CA files for https upstreams go here
cp sites.example.tsv /etc/ingress/sites.tsv # then edit it: real addresses, never committed
cp hosts.example.tsv /etc/ingress/hosts.tsv
./install.sh                                # dry run: renders and validates, changes nothing
./install.sh --apply                        # installs, reloads and verifies
```

`--apply` ends by printing the SHA-256 fingerprint of the root certificate.
Install `/etc/ingress/ca-root.crt` on each device once
([Certificates](docs/certificates.md)); the root can sign any name, so protect
the host. The rest of the network is pointed at the host by hand: an AdGuard
Home upstream rule, the router's second DNS server, and Tailscale Split DNS
for `lan`, in the order given in [Setup](docs/setup.md#wire-the-network-to-it).

To add a site later, edit `/etc/ingress/sites.tsv` and run `./install.sh
--apply` again ([Operations](docs/operations.md)).

## Layout

```text
render.sh            Pure renderer: data files in, Caddyfile and dnsmasq configs out
install.sh           Dry-run-first installer: render, validate, install, reload, verify
verify-target.sh     Picks the site and address install.sh checks over https
systemd/             ingress-dns@.service, the template for the two dnsmasq instances
sites.example.tsv    Example site data (documentation addresses only)
hosts.example.tsv    Example host records
.gitignore           Keeps a real sites.tsv or hosts.tsv out of git
tests/               Render, installer, Caddy access and docs-consistency tests
docs/                MkDocs source for the published site
mkdocs.yml           Site configuration
```

## Documentation

| Page | Covers |
|---|---|
| [Overview](docs/index.md) | What it is, why it sits where it does, how the parts connect |
| [Setup](docs/setup.md) | The LXC, packages, Tailscale, install, and wiring the network to it |
| [Sites](docs/sites.md) | The two file formats and every rule `render.sh` enforces |
| [Certificates](docs/certificates.md) | Trusting the root on devices, and what that costs |
| [DNS](docs/dns.md) | The two instances, the fallback behaviour, AdGuard and Split DNS |
| [Operations](docs/operations.md) | Add a site, check, logs, upgrade, roll back |

Preview the site locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve                      # live preview on :8000
```

## Tests

Run from this directory. They need docker; nothing here touches `/etc/ingress`
or a running service.

```bash
./tests/run.sh                              # everything below
./tests/render-test.sh                      # render.sh against the example files
./tests/verify-target-test.sh               # which site install.sh checks over https
./tests/tailnet-test.sh                     # Caddy: who reaches a tailnet site, how redirects change
./tests/install-test.sh                     # install.sh in a container: validate, order, rollback
./tests/docs-consistency-test.sh            # docs against the code
```

The render test checks the rendered Caddyfile with Caddy in a container, runs
both dnsmasq configs through `dnsmasq --test` in another, and confirms that
`render.sh` rejects each kind of bad input and names the line. The
docs-consistency test fails when a documented flag is not in a parser, a named
path is missing, a link or anchor does not resolve, a page is missing from the
nav, an error message is not documented, or an address outside the
documentation ranges appears.

The tailnet test runs the Caddyfile rendered from the example sites in a real
Caddy on two docker networks standing in for the LAN and the tailnet, and
checks that a LAN client is refused a tailnet site (also with forged
forwarding headers) and that only redirects to `.lan` names are sent to
`https`. The installer test runs `install.sh` as root in a container with the
real Caddy and stand-in services, and checks the order of the steps and the
rollback when a service refuses the new files.
