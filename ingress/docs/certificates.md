# Certificates

No public certificate authority issues for `.lan`, so Caddy signs the site
certificates itself with a local CA (`local_certs` in the Caddyfile). It does
not install that CA anywhere (`skip_install_trust`). Each device trusts the
root once; after that every `https://<name>.lan` is valid, including sites you
add later, because they are signed by the same root.

## Get the root

`install.sh --apply` copies the root to `/etc/ingress/ca-root.crt` on the host
and prints its SHA-256 fingerprint. Copy the file to the device with `scp` or
any other channel. The fingerprint is how you check, on the device, that the
file you installed is the one the host made:

```bash
openssl x509 -in /etc/ingress/ca-root.crt -noout -fingerprint -sha256
```

Compare it with the line the installer printed. The certificate is public;
it is the private key next to it that must stay on the host.

## Trust it on a device

### macOS

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca-root.crt
```

Or open the file in Keychain Access, add it to the System keychain, open it,
and set Trust to Always Trust.

### iOS and iPadOS

1. Open `ca-root.crt` on the device (AirDrop, a file share or a download link).
2. Settings, Profile Downloaded, Install.
3. Settings, General, About, Certificate Trust Settings: turn on full trust for
   **Caddy Local Authority**.

Step 3 is the one people miss: an installed profile is not trusted for HTTPS
until it is enabled there.

### Other systems

Add the file to the system trust store (Debian and Ubuntu: copy it to
`/usr/local/share/ca-certificates/` as a `.crt` and run
`update-ca-certificates`), or to the browser's own store (Firefox keeps one).
Tools that do not read the system store, such as `curl` with a bundled CA file,
take it with `--cacert`.

## What this costs

The root can sign a certificate for any name, not only `.lan`. Whoever obtains
`/var/lib/caddy` on the ingress host, where Caddy keeps the root's private key,
can impersonate any website to every device that trusts the root. Treat the
host accordingly:

- keep it small, patched, and running nothing else;
- give no one a login on it who does not need one;
- exclude `/var/lib/caddy` from backups that leave the host, or encrypt them;
- if the key may have leaked, stop Caddy, delete `/var/lib/caddy/.local/share/caddy/pki`,
  start it, run `./install.sh --apply` so that `/etc/ingress/ca-root.crt` and
  its fingerprint are refreshed, and trust the new root on every device. The
  old one must be removed from each device, or it stays trusted.

## Where the key is

The only copy of the CA's private key is
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.key`. The key file is owned by `caddy` with mode 0600. `install.sh` copies only
the public `root.crt` out of it. A dry run does not create a second CA: it
runs `caddy validate` with `HOME` and the XDG directories pointed into a
scratch directory that it deletes, so no key is written under `/root` or
anywhere else.

Rotation (the last bullet above) creates a new root and key, so it must be
followed by `./install.sh --apply`: until then `/etc/ingress/ca-root.crt` is
still the old root, and the installer's `https` check would fail against it.

## Upstream certificates

These are separate. Caddy checks an `https` upstream against the CA file named
in `sites.tsv` ([Sites](sites.md#tls-for-an-https-upstream)), not against the
local root, and a browser never sees the upstream certificate.

Caddy runs as the `caddy` user and reads the CA file when it starts, so the
file must be readable by that user (`chmod 0644`; it is a public certificate).
`install.sh` stops before rendering if one is not, because a check made as
root would pass and Caddy would then fail at its next start.

Caddy renews its site certificates on its own. The root is valid far longer
than the site certificates it signs, so a device needs a new root only after
the rotation above.
