# Security and automation regression checks

These tests use temporary files, fake Proxmox/Docker commands, and Ansible
controller-only assertions. They do not connect to the inventory or deploy
services. Install Jinja2 and PyYAML in your test Python environment, then run:

```bash
python3 -m unittest discover -s tests -v
```

With `ansible-playbook` on PATH, the suite also checks opt-out host selection and
temporary package-hold resolution using synthetic inventory. Those checks skip
when Ansible is unavailable; use the controller environment to run the full suite.

The suite covers installer download failures, provisioning cleanup and collision
handling, TLS/enrollment configuration, Wazuh log sources, Docker file/pull/health
failures, and preservation of existing package holds.

Additional local checks:

```bash
shellcheck -x -P security/install security/provision-security-stack.sh security/install/*.sh
```

Use an isolated Ansible config and synthetic inventory for `--syntax-check`;
never use a live playbook run to validate these changes.
