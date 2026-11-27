# Changelog

## [0.3.0](https://github.com/HarshitRuwali/homelab-infra/compare/proxmox-v0.2.0...proxmox-v0.3.0) (2026-11-27)


### ⚠ BREAKING CHANGES

* proxmox: `pve-guest.sh tenant` without `--uncapped` now needs an address in 10.10.90.0/24, the `lease` group and `lease_gw` alias in cluster.fw (`pve-guest.sh firewall` writes them on a new host and prints a diff for an existing one), a vmbr5 bridge and an OPNsense LEASE interface. Existing VMs are unchanged. Use `--uncapped` for the previous behaviour. The other modules in this change have no breaking change.

### Features

* put leased VMs on their own segment and log, limit and alert on tenant traffic ([#36](https://github.com/HarshitRuwali/homelab-infra/issues/36)) ([cf72b3b](https://github.com/HarshitRuwali/homelab-infra/commit/cf72b3b719a7c400a7e7f184b90759dfe69697d8))


### Bug Fixes

* **proxmox:** default to the Ubuntu template 9001 and count committed vCPU and RAM ([#47](https://github.com/HarshitRuwali/homelab-infra/issues/47)) ([beb6a27](https://github.com/HarshitRuwali/homelab-infra/commit/beb6a27dccdf7ee48330206c51a9f410499e9985))

## [0.2.0](https://github.com/HarshitRuwali/homelab-infra/compare/proxmox-v0.1.0...proxmox-v0.2.0) (2026-10-20)


### Features

* **proxmox:** add pve-guest.sh for templates, guests, tenants and the host firewall ([852f7b3](https://github.com/HarshitRuwali/homelab-infra/commit/852f7b34ae539db074b4137a5da786d26145247a))


### Bug Fixes

* apply the verified review fixes for [#6](https://github.com/HarshitRuwali/homelab-infra/issues/6), [#7](https://github.com/HarshitRuwali/homelab-infra/issues/7) and [#8](https://github.com/HarshitRuwali/homelab-infra/issues/8) ([5dd353f](https://github.com/HarshitRuwali/homelab-infra/commit/5dd353f3e96e801c089c476bc55bc21a43a50b25))
* **proxmox:** give every VM a named CPU model, x86-64-v3 by default ([997f536](https://github.com/HarshitRuwali/homelab-infra/commit/997f536ee278bdcc784231e8196de4ce3cf9028e))
