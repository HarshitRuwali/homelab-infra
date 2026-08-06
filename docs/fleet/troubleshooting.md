# Troubleshooting

Failure modes this fleet has actually hit, and what each one looks like.

## Alerting

??? failure "Rules exist in the UI but the API returns `[]`"
    They were deleted from the database. Grafana reads provisioning **only at
    startup**, so nothing puts them back on its own.

    The tell is a scheduler still working through its last in-memory copy
    while the API is empty:

    ```bash
    journalctl -u grafana-server | grep "Sending alerts to local notifier"
    ```

    Fix: `ansible-playbook playbooks/central-alerting.yml`.

??? failure "No alerts at all, and no error anywhere"
    Grafana aborts alerting provisioning for **every** file when any single
    one fails to parse, and it does **not** fail startup. One typo silently
    unalerts the whole fleet.

    A mute timing crossing midnight caused this. Grafana rejects
    `start_time: '23:00', end_time: '07:00'`; the window must be split into
    two same-day intervals.

    ```bash
    journalctl -u grafana-server -n 200 | grep -i provision
    ```

    The `grafana_alerting` role now asserts one known uid per rules file to
    turn this into a red play.

??? failure "Provisioned rule cannot be edited in the UI"
    Working as intended. Use a **Silence**, not an edit. Better still, change
    the YAML and re-run the playbook, a silence expires and has to be renewed
    by hand, an exclusion in git does not.

??? failure "`Host Down` fires forever for a host that is fine"
    It is in `monitored` in the inventory but has no collector, so it sits in
    the host-down or-chain with nothing ever pushing for it. Either onboard it
    or remove it from `monitored`.

??? failure "An alert instance shows a host label of `-`"
    A `NoData` instance carries no labels. If a rule can go NoData and you
    need to know *which* host, it needs the or-chain treatment. See
    [The push model](../architecture/push-model.md).

## Collectors

??? failure "Container panels empty, but Docker logs work"
    cAdvisor cannot reach `/run/containerd/containerd.sock`. Full explanation
    in [Container metrics](../monitoring/container-metrics.md).

    ```bash
    journalctl -u alloy | grep -i "containerd.*permission denied"
    ```

??? failure "`alloy fmt` rejects the config after a regex change"
    Alloy uses Go string escaping inside double quotes. A lone `\.` is an
    unknown escape sequence and the whole config is rejected.

    Write `\\.` in the Ansible variable so the rendered file contains `\\.`,
    which Alloy unescapes to the `\.` the regex engine wants. Keep these
    single-quoted in YAML.

    This is the `validate:` guard working as designed: a bad config **never
    lands**, which matters most on the exit node and the central LXC.

??? failure "A dashboard panel says No data but the metric exists"
    Check the `job` label. Alloy's unix exporter sets its own, overriding
    `job_name`, so series arrive as `job="integrations/unix"`. Match both:

    ```promql
    up{job=~"integrations/unix|host-unix"}
    ```

??? failure "Loki returns 429 during onboarding"
    Default limits are 4 MB/s overall and 3 MB/s per stream. Starting
    `loki.source.journal` with `max_age = "12h"` on several hosts at once
    backfills half a day of journals simultaneously.

    Raise `ingestion_rate_mb` in `loki/loki-config.yml` **before** onboarding,
    lower `alloy_journal_max_age` on hosts with huge journals, and keep
    `retry_on_http_429 = true`.

??? failure "Cardinality blowup on a PVE host"
    Proxmox enumerates hundreds of transient per-guest systemd units
    (`lxc@103`, `qemu@110`, scopes, slices). Left alone the systemd collector
    alone emits ~2000 series per host. `alloy_systemd_unit_exclude` in
    `group_vars/proxmox` drops them.

## Patching

??? failure "`unattended-upgrade` exits 0 but nothing was installed"
    It has its own lock, separate from `/var/lib/dpkg/lock-frontend`. When
    another run holds it you get `Lock file is already taken, exiting` and
    **exit status 0**.

    Never trust the return code. Check the pending count afterwards.

??? failure "Packages stay pending forever while runs report success"
    `Origins-Pattern` is not matching them. Listing Debian and Ubuntu origins
    explicitly means packages from any **other** origin are silently never
    considered. Use `o=*`.

??? failure "A host fills its disk despite `AutocleanInterval`"
    `autoclean` only removes `.debs` that can no longer be downloaded.
    Everything current stays forever. Set `APT::Periodic::CleanInterval`,
    which is what actually runs `apt-get clean`.

??? failure "dpkg is wedged after a power cut"
    `fleet_dpkg_needs_configure` alerts on it. Recover with:

    ```bash
    ssh <host> 'sudo dpkg --configure -a'
    ```

    `AutoFixInterruptedDpkg "true"` should prevent recurrence.

## Ansible

??? failure "`Permission denied (publickey)` on a host you can SSH to manually"
    Without `IdentitiesOnly=yes`, SSH offers every key in the agent and in
    `~/.ssh`. A host with a low `MaxAuthTries` disconnects before reaching the
    right one, and the error is indistinguishable from genuinely lacking
    access. `ansible.cfg` sets it; check it is still there.

??? failure "A variable has a placeholder value you thought you overrode"
    `group_vars/<group>/main.yml` beats vars set in the inventory file. Put
    site-specific values in `hosts.local.yml` and keep them out of
    `group_vars`.

??? failure "`ansible-vault` produces nothing and later commands 401"
    `vault_password_file` resolves relative to `ansible.cfg`, which is only
    found when your shell is inside `ansible/`. `cd ansible` first.

??? failure "A oneshot service task returns before the work is done"
    `systemd: state=started` returns once the job is **enqueued**. Poll for
    completion instead:

    ```yaml
    - command: systemctl show <unit> -p SubState --value
      register: st
      until: st.stdout | trim == "dead"
      retries: 120
      delay: 15
    ```

??? failure "A task reports `changed` on every run"
    Usually a `file:` task with `recurse: true` re-fixing ownership of files
    another process keeps writing. Condition it on the thing that actually
    changed. An Ansible run that is never green is one nobody reads.

## Central stack

??? failure "Grafana package upgrade fails on the LXC"
    `mv: cannot overwrite '/var/lib/grafana/plugins-bundled': Directory not
    empty`. Move the directory aside and finish the configure:

    ```bash
    mv /var/lib/grafana/plugins-bundled /var/lib/grafana/plugins-bundled.bak
    dpkg --configure -a
    ```

    Then verify rules, dashboards and contact points survived.

??? failure "`lxc-update.sh central --config-only` reverted my config"
    It copies from the checkout at `/root/monitorting-stack` on the box, which
    may be **behind** the repo and may lack `grafana/provisioning/alerting/`
    entirely. Update that checkout, or use `playbooks/central-alerting.yml`,
    which is the supported path.

    The Alloy config is protected from this by
    `/etc/alloy/.ansible-managed`; the alerting config is not.

??? failure "Both subdomains return HTTP 530"
    Cloudflare cannot reach the origin. Usually the central LXC rebooted;
    check `journalctl --list-boots`. Not a credentials problem, even though it
    looks like one from outside.
