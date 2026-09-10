#!/usr/bin/env bash
# Authelia (SSO) plus OpenBao (secrets) on sec-auth.
# OpenBao, not HashiCorp Vault: Vault is BUSL 1.1 since Aug 2023 and is not OSI open
# source. OpenBao is the MPL-2.0 Linux Foundation fork. See docs/architecture/index.md.
# Run inside the sec-auth LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y curl ca-certificates gnupg

# OpenBao
run sh -c 'curl -fsSL https://openbao.org/install.sh | bash'

# Authelia
run sh -c 'curl -fsSL https://apt.authelia.com/setup.sh | bash'
run apt-get install -y authelia

ok "Authelia and OpenBao installed, both need configuration"
cat <<'NOTE'

  OpenBao: keep the unseal keys and root token OFF this machine. Print them, or store
  them in a password manager. Auto-unseal that keeps the keys beside the vault defeats
  the vault.

      bao operator init          # record the keys somewhere physical
      bao operator unseal        # x3

  Then move your service credentials into it rather than leaving them in .env files:
  datastore passwords, API keys, and service tokens.

  Authelia: configure /etc/authelia/configuration.yml with your users and put it in
  front of the internal UIs (Grafana, Superset, the portal, Netdata, qBittorrent).

  Do NOT publish any management UI through the Cloudflare tunnel. A SIEM dashboard on
  the public internet is worse than the problem it was deployed to solve.
NOTE
