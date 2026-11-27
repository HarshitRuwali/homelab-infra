# Changelog

## [0.3.0](https://github.com/HarshitRuwali/homelab-infra/compare/monitoring-v0.2.0...monitoring-v0.3.0) (2026-11-27)


### ⚠ BREAKING CHANGES

* proxmox: `pve-guest.sh tenant` without `--uncapped` now needs an address in 10.10.90.0/24, the `lease` group and `lease_gw` alias in cluster.fw (`pve-guest.sh firewall` writes them on a new host and prints a diff for an existing one), a vmbr5 bridge and an OPNsense LEASE interface. Existing VMs are unchanged. Use `--uncapped` for the previous behaviour. The other modules in this change have no breaking change.

### Features

* **ansible:** boost the Dell CPU fan while its package is hot ([#31](https://github.com/HarshitRuwali/homelab-infra/issues/31)) ([ffd415a](https://github.com/HarshitRuwali/homelab-infra/commit/ffd415afe7acab4ee85e9320dfd380ffc27dc973))
* **ansible:** desk_appliance role, X11 autologin appliance baseline for the Iris box ([#9](https://github.com/HarshitRuwali/homelab-infra/issues/9)) ([d29b619](https://github.com/HarshitRuwali/homelab-infra/commit/d29b619270a6c0c82aff82573f43de1d728914bd))
* **media:** add Plex and qBittorrent behind ProtonVPN as a module ([#20](https://github.com/HarshitRuwali/homelab-infra/issues/20)) ([bfdc530](https://github.com/HarshitRuwali/homelab-infra/commit/bfdc530dd547600d9c2706561be9705f0640e35c))
* **monitoring:** add the skullsaints dashboard and fix the host's spelling ([#24](https://github.com/HarshitRuwali/homelab-infra/issues/24)) ([425e324](https://github.com/HarshitRuwali/homelab-infra/commit/425e3242a3b1f4df88225b07f5a0af7b310eac2f))
* **monitoring:** onboard ingress and fix the private-ingest check in onboard.yml ([#26](https://github.com/HarshitRuwali/homelab-infra/issues/26)) ([aa5e508](https://github.com/HarshitRuwali/homelab-infra/commit/aa5e5083e3755664d468d57d9d93b9cec3e12cc1))
* **monitoring:** routers, internet uptime, inter-LAN traffic and permanent network history ([#55](https://github.com/HarshitRuwali/homelab-infra/issues/55)) ([f466677](https://github.com/HarshitRuwali/homelab-infra/commit/f466677c3181ae5e27d21bca4c11078cd38a32cf))
* put leased VMs on their own segment and log, limit and alert on tenant traffic ([#36](https://github.com/HarshitRuwali/homelab-infra/issues/36)) ([cf72b3b](https://github.com/HarshitRuwali/homelab-infra/commit/cf72b3b719a7c400a7e7f184b90759dfe69697d8))
* **security:** add the security stack with Wazuh, CrowdSec, AdGuard Home and Greenbone ([#3](https://github.com/HarshitRuwali/homelab-infra/issues/3)) ([058beb0](https://github.com/HarshitRuwali/homelab-infra/commit/058beb0371eb178e1899d552b001c48129f66031))


### Bug Fixes

* **monitoring:** bring committed alert rules in line with the deployed ones ([#23](https://github.com/HarshitRuwali/homelab-infra/issues/23)) ([96b638e](https://github.com/HarshitRuwali/homelab-infra/commit/96b638e743ab7d9448fe83cb08cd7a47a22b0cbd))
* **monitoring:** bump grafana/alloy from v1.21.3 to v1.22.0 in /monitoring ([#53](https://github.com/HarshitRuwali/homelab-infra/issues/53)) ([f5168a2](https://github.com/HarshitRuwali/homelab-infra/commit/f5168a2e728537485d33fcbe8508dc80bc3d8994))
* **monitoring:** bump grafana/loki from 3.7.9 to 3.7.10 in /monitoring ([#54](https://github.com/HarshitRuwali/homelab-infra/issues/54)) ([52e510b](https://github.com/HarshitRuwali/homelab-infra/commit/52e510b56a6bc33cbe81af215fdd48842beb42ad))
* **monitoring:** bump prom/prometheus from v2.53.3 to v2.55.1 in /monitoring ([#52](https://github.com/HarshitRuwali/homelab-infra/issues/52)) ([b8f9a41](https://github.com/HarshitRuwali/homelab-infra/commit/b8f9a41a53948fabb45745dba01cf51fb99d9e3a))

## [0.2.0](https://github.com/HarshitRuwali/homelab-infra/compare/monitoring-v0.1.0...monitoring-v0.2.0) (2026-10-20)


### Features

* Add Grafana alerting rules for hardware monitoring on Dell Precision 7920 Tower ([f7d0a65](https://github.com/HarshitRuwali/homelab-infra/commit/f7d0a65b2deb0278310f028f206d411f1677f9b4))
* consolidate documentation deployment workflows and update links for homelab-infra ([a278009](https://github.com/HarshitRuwali/homelab-infra/commit/a27800989a98e558703827322d62aa1ab093b495))
* Update alerting documentation and rules to exclude workstation noise ([ebabf87](https://github.com/HarshitRuwali/homelab-infra/commit/ebabf876a9e390a09e1a52ed102ca06c8e4d1e57))
* Update docker dependencies; fix monitoring spam and alerting rules ([0dca0ba](https://github.com/HarshitRuwali/homelab-infra/commit/0dca0ba8e9e659230bcc4c5a33f6322007297f20))


### Bug Fixes

* **alerting:** scope hardware liveness to hypervisor, require fans and clean reads ([2872e25](https://github.com/HarshitRuwali/homelab-infra/commit/2872e25b6739ef01f05a8c8504c7bace11381b8a))
* Update alerting documentation and rules to exclude workstation … ([98e4ce9](https://github.com/HarshitRuwali/homelab-infra/commit/98e4ce948808f587c5592f14c0523422bb73f85d))


### Refactoring

* promote ansible from monitoring to the repository root ([0c9a5f1](https://github.com/HarshitRuwali/homelab-infra/commit/0c9a5f1caf857f3389210f95b824ec4ac872af10))


### Documentation

* give every module a dedicated site and bring the four in sync ([c3f7e6d](https://github.com/HarshitRuwali/homelab-infra/commit/c3f7e6d83ef7cbd3b67b341b9704fd46e639293f))
