# DNS

`.lan` names are answered by dnsmasq on the ingress host. There are two
instances, one per kind of client, and both are also ordinary forwarding
resolvers, which is what lets the ingress LAN address be a fallback DNS server.

## Why two instances

A LAN client and a tailnet client need different answers for the same name: a
LAN client should get the ingress LAN address, a tailnet client the tailnet
address. One dnsmasq instance cannot do this reliably, because Tailscale's
interface is a `/32`, so it has no network that dnsmasq can match a client
against. Each instance therefore listens on one address only and has its own
host file:

| Unit | Listens on | Host file | Answers with |
|---|---|---|---|
| `ingress-dns@lan` | the ingress LAN address | `hosts-lan` | LAN addresses for `lan` sites |
| `ingress-dns@tailnet` | the ingress tailnet address | `hosts-tailnet` | tailnet addresses for every site |

Both come from one template unit, `systemd/ingress-dns@.service`. The instance
name picks the config: `ingress-dns@lan` runs dnsmasq with
`/etc/ingress/rendered/dnsmasq-lan.conf`. The packaged `dnsmasq.service` is
disabled by the installer, because it listens on every address and would hold
port 53. For which addresses go in each host file, see
[Sites](sites.md#which-address-each-file-answers).

## What each instance does

The rendered configuration is the same for both apart from the listen address
and the host file:

| Setting | Effect |
|---|---|
| `listen-address`, `bind-dynamic` | Listen on that one address, and keep working if the interface comes up later |
| `no-resolv`, `no-hosts` | Ignore the host's own resolver settings and `/etc/hosts` |
| `local=/lan/` | Authoritative for `.lan`: a name that is not listed is NXDOMAIN and is never forwarded |
| `addn-hosts` | The rendered host file |
| `domain-needed`, `bogus-priv` | Do not forward bare names or reverse lookups for private ranges |
| `server=1.1.1.1`, `server=1.0.0.1` | Everything else goes to these public resolvers |
| `cache-size=1000` | A small cache |

## Fallback behaviour

Because both instances forward everything outside `.lan` to public resolvers
and do not depend on AdGuard Home, the ingress LAN address can be the router's
secondary DHCP DNS server:

- AdGuard Home is up: devices ask it first. Ads and trackers are filtered, and
  `.lan` questions go to the ingress LAN instance through the rule below.
- AdGuard Home is down: a device moves to its second DNS server, the ingress
  LAN instance. Internet names still resolve (unfiltered), and `.lan` still
  works, because that instance answers it itself.
- A device that asks the ingress instance first gets the same answers.

The ingress host itself is then a dependency for `.lan` and for the fallback.
If the host is down, `.lan` stops resolving; the rest of the network does not
depend on it as long as AdGuard Home is up.

## AdGuard Home

In AdGuard Home, Settings, DNS settings, Upstream DNS servers, add this line
above the default upstream, using the ingress LAN address:

```text
[/lan/]192.0.2.50
```

`[/lan/]` sends only names under `.lan` to that server; everything else uses
the default upstream.

## Router DHCP

Set the DHCP DNS servers to AdGuard Home first and the ingress LAN address
second. Give the ingress container a DHCP reservation, since both the rule above
and the router point at its address, and the rendered files contain it.

## Tailscale Split DNS

In the Tailscale admin console, DNS, Nameservers, add a custom nameserver: the
ingress tailnet address, with Restrict to domain turned on and the domain set to
`lan`. Tailnet devices then ask the tailnet instance for `.lan` only, and keep
resolving everything else the way they did.

The ingress host runs `tailscale up` with `--accept-dns=false`, so this setting
does not change its own resolver.

## OPNsense Unbound (optional)

Guests behind OPNsense that resolve through Unbound can reach `.lan` with a
domain override: Services, Unbound DNS, Overrides, Domain Overrides, domain
`lan`, server the ingress LAN address.

## Names that do not exist

`local=/lan/` makes an unknown `.lan` name an immediate NXDOMAIN. That is
deliberate: a typo does not leak to a public resolver, and a removed site stops
resolving as soon as `install.sh --apply` runs. Check from the ingress host
with `dig` ([Operations](operations.md#check-the-dns-instances)).
