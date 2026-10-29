# Sites and hosts

Two files hold all site data. `render.sh` reads them and fails, naming the file
and line, on anything it does not accept, before anything live is touched.
The real files live in `/etc/ingress` on the host and are never committed;
`sites.example.tsv` and `hosts.example.tsv` show the format, with addresses
from the documentation ranges only.

Both files share these rules:

- Fields are separated by spaces or tabs, in any amount. Align them however
  you like.
- A `#` starts a comment that runs to the end of the line, so a `#` can never
  be part of a value. Blank lines and comment-only lines are ignored.
- A carriage return at the end of a line (a file saved with Windows line
  endings) is dropped. Any other control character anywhere in a line, a
  comment included, is rejected. The tab is not one: it separates fields.
- A name is lowercase letters, digits and hyphens, starting and ending with a
  letter or digit. It is the label before `.lan`.
- A name may appear once across both files together. `ingress` is already
  taken: `ingress.lan` always points at the host itself.
- An address is checked for its shape (four dot-separated groups of one to
  three digits), not its range, so `192.0.2.999` passes. Review addresses by eye.

## sites.tsv

One line per web UI, four columns:

```text
# name          access   upstream                       tls
proxmox         lan      https://192.0.2.9:8006         ca=pve-root-ca.pem,sni=pve-host
grafana         lan      http://192.0.2.29:80           -
qbittorrent     tailnet  http://100.64.0.10:8081        -
```

| Column | Value |
|---|---|
| `name` | The site is served as `https://<name>.lan` |
| `access` | `lan` or `tailnet`, see below |
| `upstream` | `http://` or `https://`, then a host name, an IPv4 address or an IPv6 address in brackets, then `:` and a port. The port is required and at most 65535. Nothing follows the port |
| `tls` | For an `https` upstream: `ca=<file>` or `ca=<file>,sni=<name>`. For an `http` upstream: `-`, which may also be left out |

The host is lowercase letters, digits, dots and hyphens, starting and ending
with a letter or digit, or hexadecimal digits and colons inside `[` `]`. The
whole value must match `^https?://(host|[ipv6]):[0-9]{1,5}$`, so a path, a
query, a trailing slash, user information, quotes, braces and Caddy
placeholders such as `{env.HOST}` are all rejected.

### access

| Value | Who can reach it | What the name resolves to |
|---|---|---|
| `lan` | LAN and tailnet clients | The ingress LAN address for LAN clients, the tailnet address for tailnet clients |
| `tailnet` | Tailnet clients only | The ingress tailnet address, for everyone |

A `tailnet` site also imports a Caddy snippet that aborts any request whose
client address is outside `100.64.0.0/10` and `fd7a:115c:a1e0::/48`, so a LAN
device that learns the tailnet address still gets a dropped connection, not a
page. The tailnet check applies to the address Caddy sees; it does not
authenticate a person.

### tls for an https upstream

`ca=<file>` names a PEM file in `/etc/ingress/cas` (letters, digits, dot,
underscore and hyphen only, no directories). Caddy trusts only that CA for this
upstream, and the file must exist when `render.sh` runs.

`sni=<name>` sets the name Caddy asks for and checks the upstream certificate
against (letters, digits, dot and hyphen). Use it when the certificate does not
name the address you connect to:

- a Proxmox host whose certificate names only its hostname: `sni=pve-host`;
- a Wazuh dashboard whose certificate carries only the address `127.0.0.1`:
  `sni=127.0.0.1`.

### What an http upstream gets

Some apps build their own redirects with `http://` when they sit behind a
proxy (Nextcloud with `overwriteprotocol` set to `http`). For an `http`
upstream, Caddy rewrites a `Location` header to `https://` when it starts with
`http://` and a `.lan` name (lowercase letters, digits and hyphens, then
`.lan`) that is followed by `:`, `/` or the end of the value, so the browser
goes back through the proxy and not through a second redirect. The rule is:

```text
header_down Location "^http://([a-z0-9-]+\.lan)([:/]|$)" "https://$1$2"
```

A redirect to anything else keeps its scheme: an external site
(`http://example.com/x`), the upstream's own address and port, or a name that
only starts like a `.lan` name (`http://printer.lan.example.com/`). An `https`
upstream gets no such rewrite.

## hosts.tsv

Plain address records, with no proxy: `<name>.lan` for machines you reach by
ssh and the like. Three columns:

```text
# name          lan_ip          tailnet_ip
pve-host        192.0.2.9       100.64.0.9
nas             192.0.2.30      -
```

| Column | Value |
|---|---|
| `name` | The record is `<name>.lan` |
| `lan_ip` | An IPv4 address. LAN clients get this |
| `tailnet_ip` | An IPv4 address, or `-`, which may also be left out. Tailnet clients get it when there is one, otherwise they get `lan_ip` (reachable through a subnet router) |

## Which address each file answers

`render.sh` writes two host files, one per DNS instance ([DNS](dns.md)).

| Entry | LAN instance answers | Tailnet instance answers |
|---|---|---|
| `ingress.lan` | ingress LAN address | ingress tailnet address |
| site with `access` `lan` | ingress LAN address | ingress tailnet address |
| site with `access` `tailnet` | ingress tailnet address | ingress tailnet address |
| host | `lan_ip` | `tailnet_ip`, or `lan_ip` when there is none |

## What render.sh rejects

`render.sh` exits 1 with `render.sh: <file>:<line>: <message>`. The checks
run in the order below for each line; sites are read before hosts.

| Message contains | Cause |
|---|---|
| `too many fields` | A fifth column in `sites.tsv`, or a fourth in `hosts.tsv` |
| `bad name` | The name does not match the rule above |
| `duplicate name` | The name was already used, in either file, or is `ingress` |
| `access must be lan or tailnet` | The second column of `sites.tsv` is anything else |
| `bad upstream` | Not `http://` or `https://` plus a host and a port as described above: a missing port, a path, a query, user information, quotes, braces, an uppercase host |
| `upstream port above 65535` | The port is more than 65535 |
| `control character in line` | The line, a comment included, holds a control character other than a tab, after a trailing carriage return is dropped |
| `an https upstream needs tls ca=<file>[,sni=<name>]` | An `https` upstream with no `tls`, or one that does not have that form |
| `CA file` | The `ca=` file is not in the `--cas` directory (`CA file <path> not found`) |
| `tls must be - for an http upstream` | An `http` upstream with a `ca=` or any other `tls` value |
| `lan_ip is not an IPv4 address` | The second column of `hosts.tsv` is not an address |
| `tailnet_ip must be an IPv4 address or -` | The third column of `hosts.tsv` is neither |

Wrong command-line use exits 2 with the usage text: an unknown or missing
option, an option given without its value (`render.sh --sites`), or a
`--lan-ip` or `--tailnet-ip` that is not an address. Try an edit
without installing anything; [Operations](operations.md#preview-an-edit) shows
how.
