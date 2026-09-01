# Operations

Day-to-day running of the central stack.

<div class="grid cards" markdown>

- :material-play-circle: **[Lifecycle](lifecycle.md)**

    Starting, stopping, logs and health checks.

- :material-database-clock: **[Retention](retention.md)**

    How long metrics and logs are kept, and the disk maths behind it.

- :material-book-open-variant: **[Runbooks](runbooks.md)**

    Step-by-step recovery for the things that go wrong.

</div>

## Daily checks

None, by design. The alerting is supposed to tell you. If you want a
five-second glance, open **VM Fleet Overview** and confirm:

| Panel | Healthy |
|---|---|
| Collectors Reporting (10m) | equals your host count |
| Fleet Freshness | every row green, under 30 seconds |
| Reboot Required | `0`, or a host you already know about |
| Pending Package Updates | trending down, not monotonically up |

## Health endpoints

```bash
# on the central box
curl http://127.0.0.1:3000/api/health      # Grafana
curl http://127.0.0.1:9090/-/ready         # Prometheus
curl http://127.0.0.1:3100/ready           # Loki
curl http://127.0.0.1:12345/-/ready        # Alloy

# through the proxy
curl -u collector:<pw> https://monitor.example.com/prometheus/-/ready
curl -u collector:<pw> https://monitor.example.com/loki/api/v1/labels
```

!!! warning "`/loki/ready` through the proxy is a 404"
    The `/loki/` location preserves the path prefix, and Loki's readiness
    endpoint is `/ready`, not `/loki/ready`. Use `/loki/api/v1/labels` as the
    external liveness check instead. See
    [the path asymmetry](../fleet/verification.md#the-ingest-path-asymmetry).

## Service states, all at once

```bash
ssh <monitor-lxc> 'for s in prometheus loki grafana-server nginx alloy matrix-webhook; do
  printf "  %-16s %s\n" "$s" "$(systemctl is-active $s)"
done'
```

!!! bug "Do not `grep -q active`"
    It matches **`inactive`** too. Compare the exact string. This caused a
    rollout to be reported complete when nothing had been installed.
