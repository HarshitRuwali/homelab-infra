# Reference

Look-up material. Nothing here is a walkthrough; each page is the complete list
of one kind of thing.

<div class="grid cards" markdown>

- :material-play-box-multiple: **[Playbooks](playbooks.md)**

    Every playbook and role, what it changes, and the common invocations.

- :material-tune-variant: **[Variables](variables.md)**

    Inventory variables, per-group overrides, and what lives in the vault.

- :material-book-open-page-variant: **[Building the docs](tooling.md)**

    Serving this site locally, the pinned toolchain, and how it is published.

</div>

## Where the authoritative value lives

When a page here and the code disagree, the code wins. These are the files to
check first, all relative to `ansible/`:

| Question | File |
|---|---|
| What does this variable default to? | `roles/<role>/defaults/main.yml` |
| Which hosts are in this group? | `inventory/hosts.local.yml`, gitignored |
| What does the group scheme mean? | `inventory/hosts.example.yml`, documented inline |
| When does this timer fire? | the role's `*.timer.j2` template |
| Where is the inventory and vault password? | `ansible.cfg` |

For what the deployed alert rules actually query, see the monitoring site's
[metrics catalogue](https://harshitruwali.github.io/homelab-infra/monitoring/reference/metrics/).
