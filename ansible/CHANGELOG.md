# Changelog

## [0.3.0](https://github.com/HarshitRuwali/homelab-infra/compare/ansible-v0.2.0...ansible-v0.3.0) (2026-10-23)


### Features

* **security:** add the security stack with Wazuh, CrowdSec, AdGuard Home and Greenbone ([#3](https://github.com/HarshitRuwali/homelab-infra/issues/3)) ([058beb0](https://github.com/HarshitRuwali/homelab-infra/commit/058beb0371eb178e1899d552b001c48129f66031))

## [0.2.0](https://github.com/HarshitRuwali/homelab-infra/compare/ansible-v0.1.0...ansible-v0.2.0) (2026-10-20)


### Features

* Add alerting rules for GPU, network, resources, services, and updates ([88cbb53](https://github.com/HarshitRuwali/homelab-infra/commit/88cbb53aa5285ed88dca40710a734283441a05be))
* Add ansible palybook for stack and enhance Grafana alerting configuration ([58af6aa](https://github.com/HarshitRuwali/homelab-infra/commit/58af6aa3cc3672583ab4e970af06672e09d10009))
* Add Grafana alerting rules for hardware monitoring on Dell Precision 7920 Tower ([f7d0a65](https://github.com/HarshitRuwali/homelab-infra/commit/f7d0a65b2deb0278310f028f206d411f1677f9b4))
* Add security stack provisioning and installation scripts ([e2c94be](https://github.com/HarshitRuwali/homelab-infra/commit/e2c94bec6b3963839f62b2e23c685da0d8a4865d))
* Enhance unattended upgrades and monitoring configurations ([ecdf477](https://github.com/HarshitRuwali/homelab-infra/commit/ecdf4777ca00fb042d234c90495790403e93e554))
* **grafana:** add new dashboard for ubuntu-dev monitoring ([68bad55](https://github.com/HarshitRuwali/homelab-infra/commit/68bad55eb2ee92de0460cc70a6f2035e25b398eb))
* **grafana:** add storage health monitoring rules ([68bad55](https://github.com/HarshitRuwali/homelab-infra/commit/68bad55eb2ee92de0460cc70a6f2035e25b398eb))
* Update alerting documentation and rules to exclude workstation noise ([ebabf87](https://github.com/HarshitRuwali/homelab-infra/commit/ebabf876a9e390a09e1a52ed102ca06c8e4d1e57))
* Update docker dependencies; fix monitoring spam and alerting rules ([0dca0ba](https://github.com/HarshitRuwali/homelab-infra/commit/0dca0ba8e9e659230bcc4c5a33f6322007297f20))
* Update the docs and add docs build pipeline ([bd5d2db](https://github.com/HarshitRuwali/homelab-infra/commit/bd5d2dbf89d144b681d9846ef7703b4c89f1c18c))


### Bug Fixes

* apply the verified review fixes for [#6](https://github.com/HarshitRuwali/homelab-infra/issues/6), [#7](https://github.com/HarshitRuwali/homelab-infra/issues/7) and [#8](https://github.com/HarshitRuwali/homelab-infra/issues/8) ([5dd353f](https://github.com/HarshitRuwali/homelab-infra/commit/5dd353f3e96e801c089c476bc55bc21a43a50b25))
* **grafana:** update alerting rules to exclude specific hosts ([68bad55](https://github.com/HarshitRuwali/homelab-infra/commit/68bad55eb2ee92de0460cc70a6f2035e25b398eb))
* unblock all update origins, correct SSH/alerting bugs ([c15178d](https://github.com/HarshitRuwali/homelab-infra/commit/c15178d9a3632b357c1633feeb69d7a9ff64deba))
* Update alerting documentation and rules to exclude workstation … ([98e4ce9](https://github.com/HarshitRuwali/homelab-infra/commit/98e4ce948808f587c5592f14c0523422bb73f85d))


### Refactoring

* inventory structure and onboarding process ([9410830](https://github.com/HarshitRuwali/homelab-infra/commit/94108301f5d359af196e210ede5cd577fbc5a848))
* promote ansible from monitoring to the repository root ([0c9a5f1](https://github.com/HarshitRuwali/homelab-infra/commit/0c9a5f1caf857f3389210f95b824ec4ac872af10))
* update host processes dashboard for improved metrics and descriptions ([319cf39](https://github.com/HarshitRuwali/homelab-infra/commit/319cf3969c56e90a806a2429374b1cad55475e8f))


### Documentation

* add deployment log to documentation ([68bad55](https://github.com/HarshitRuwali/homelab-infra/commit/68bad55eb2ee92de0460cc70a6f2035e25b398eb))
* give every module a dedicated site and bring the four in sync ([c3f7e6d](https://github.com/HarshitRuwali/homelab-infra/commit/c3f7e6d83ef7cbd3b67b341b9704fd46e639293f))
