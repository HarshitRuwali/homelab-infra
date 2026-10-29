# Changelog

## [0.3.0](https://github.com/HarshitRuwali/homelab-infra/compare/monitoring-v0.2.0...monitoring-v0.3.0) (2026-10-29)


### Features

* **ansible:** desk_appliance role, X11 autologin appliance baseline for the Iris box ([#9](https://github.com/HarshitRuwali/homelab-infra/issues/9)) ([d29b619](https://github.com/HarshitRuwali/homelab-infra/commit/d29b619270a6c0c82aff82573f43de1d728914bd))
* **media:** add Plex and qBittorrent behind ProtonVPN as a module ([#20](https://github.com/HarshitRuwali/homelab-infra/issues/20)) ([bfdc530](https://github.com/HarshitRuwali/homelab-infra/commit/bfdc530dd547600d9c2706561be9705f0640e35c))
* **monitoring:** add the skullsaints dashboard and fix the host's spelling ([#24](https://github.com/HarshitRuwali/homelab-infra/issues/24)) ([425e324](https://github.com/HarshitRuwali/homelab-infra/commit/425e3242a3b1f4df88225b07f5a0af7b310eac2f))
* **monitoring:** onboard ingress and fix the private-ingest check in onboard.yml ([#26](https://github.com/HarshitRuwali/homelab-infra/issues/26)) ([aa5e508](https://github.com/HarshitRuwali/homelab-infra/commit/aa5e5083e3755664d468d57d9d93b9cec3e12cc1))
* **security:** add the security stack with Wazuh, CrowdSec, AdGuard Home and Greenbone ([#3](https://github.com/HarshitRuwali/homelab-infra/issues/3)) ([058beb0](https://github.com/HarshitRuwali/homelab-infra/commit/058beb0371eb178e1899d552b001c48129f66031))


### Bug Fixes

* **monitoring:** bring committed alert rules in line with the deployed ones ([#23](https://github.com/HarshitRuwali/homelab-infra/issues/23)) ([96b638e](https://github.com/HarshitRuwali/homelab-infra/commit/96b638e743ab7d9448fe83cb08cd7a47a22b0cbd))

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
