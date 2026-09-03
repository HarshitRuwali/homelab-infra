# Reference

Look-up material. Nothing here is a walkthrough; each page is the complete list
of one kind of thing.

<div class="grid cards" markdown>

- :material-chart-box-outline: **[Metrics catalogue](metrics.md)**

    Every metric this repository adds on top of the stock exporters.

- :material-book-open-page-variant: **[Building the docs](tooling.md)**

    Serving this site locally, the pinned toolchain, and how it is published.

</div>

## Where the authoritative value lives

When a page here and the code disagree, the code wins. These are the files to
check first, all relative to `monitoring/`:

| Question | File |
|---|---|
| What does this alert actually query? | `grafana/provisioning/alerting/rules-*.yaml` |
| What is on this dashboard? | `grafana/dashboards/fleet/`, `grafana/dashboards/servers/` |
| How long is data kept? | `prometheus/prometheus.yml`, `loki/loki-config.yml` |
| What does the collector scrape? | `alloy/config.alloy` for the Docker collector |

!!! info "Playbooks and inventory variables moved"
    The Ansible control plane has its own site. Look up a playbook in
    [Playbooks](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/)
    and an inventory variable in
    [Variables](https://harshitruwali.github.io/homelab-infra/ansible/reference/variables/).
    Note that `alloy/config.alloy` above covers the **Docker** collector only;
    every native install is templated from
    `ansible/roles/alloy_collector/templates/config.alloy.j2`.
