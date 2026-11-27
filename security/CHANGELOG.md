# Changelog

## [0.3.0](https://github.com/HarshitRuwali/homelab-infra/compare/security-v0.2.0...security-v0.3.0) (2026-11-27)


### ⚠ BREAKING CHANGES

* proxmox: `pve-guest.sh tenant` without `--uncapped` now needs an address in 10.10.90.0/24, the `lease` group and `lease_gw` alias in cluster.fw (`pve-guest.sh firewall` writes them on a new host and prints a diff for an existing one), a vmbr5 bridge and an OPNsense LEASE interface. Existing VMs are unchanged. Use `--uncapped` for the previous behaviour. The other modules in this change have no breaking change.

### Features

* **monitoring:** routers, internet uptime, inter-LAN traffic and permanent network history ([#55](https://github.com/HarshitRuwali/homelab-infra/issues/55)) ([f466677](https://github.com/HarshitRuwali/homelab-infra/commit/f466677c3181ae5e27d21bca4c11078cd38a32cf))
* put leased VMs on their own segment and log, limit and alert on tenant traffic ([#36](https://github.com/HarshitRuwali/homelab-infra/issues/36)) ([cf72b3b](https://github.com/HarshitRuwali/homelab-infra/commit/cf72b3b719a7c400a7e7f184b90759dfe69697d8))
* **security:** add the security stack with Wazuh, CrowdSec, AdGuard Home and Greenbone ([#3](https://github.com/HarshitRuwali/homelab-infra/issues/3)) ([058beb0](https://github.com/HarshitRuwali/homelab-infra/commit/058beb0371eb178e1899d552b001c48129f66031))


### Bug Fixes

* **security:** size sec-wazuh at 40 GB, prune old alert archives, no rootcheck on LXC agents ([#46](https://github.com/HarshitRuwali/homelab-infra/issues/46)) ([cb99095](https://github.com/HarshitRuwali/homelab-infra/commit/cb9909571f9f6860241df200288f276b7a2c831e))

## [0.2.0](https://github.com/HarshitRuwali/homelab-infra/compare/security-v0.1.0...security-v0.2.0) (2026-10-20)


### Features

* Add security stack provisioning and installation scripts ([e2c94be](https://github.com/HarshitRuwali/homelab-infra/commit/e2c94bec6b3963839f62b2e23c685da0d8a4865d))
* Add upgrade script for Wazuh all-in-one installation ([307148d](https://github.com/HarshitRuwali/homelab-infra/commit/307148d878cc8db3033d94aeedb0fc80c98616ae))


### Bug Fixes

* apply the verified review fixes for [#6](https://github.com/HarshitRuwali/homelab-infra/issues/6), [#7](https://github.com/HarshitRuwali/homelab-infra/issues/7) and [#8](https://github.com/HarshitRuwali/homelab-infra/issues/8) ([5dd353f](https://github.com/HarshitRuwali/homelab-infra/commit/5dd353f3e96e801c089c476bc55bc21a43a50b25))
