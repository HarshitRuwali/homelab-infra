# The push model

This is the single most important thing to understand about this stack, and
the source of the subtlest bug it has had.

## `up` is not what you think

`prometheus/prometheus.yml` has **no scrape configs for hosts**. Everything
arrives through `--web.enable-remote-write-receiver`. So `up{job="host-unix"}`
is not Prometheus's own judgement about whether a target answered. It is a
series the collector **pushes about itself**.

The consequence:

<div class="grid" markdown>

!!! failure "Pull model (what you expect)"
    Host dies → Prometheus scrape fails → `up` becomes `0` → alert on
    `up == 0` fires, carrying the target's labels.

!!! danger "Push model (what happens here)"
    Host dies → nothing is pushed → the series **stops existing** after the
    5m lookback. It never becomes `0`.

</div>

An `up == 0` alert therefore **never fires**. It goes `NoData`, and a `NoData`
instance carries no `host` label, so you cannot even tell which machine died.

## The or-chain

The fix is a per-host constant floor:

```promql
max by (host) (max_over_time(up{job=~"integrations/unix|host-unix"}[10m]))
  or label_replace(vector(0), "host", "rpi5", "", "")
  or label_replace(vector(0), "host", "rpi4b", "", "")
  -- one per monitored host --
```

Why this works:

1. `or` returns all left-hand samples, plus right-hand samples whose **full
   label set** is unmatched on the left.
2. After `max by (host)` the left label set is exactly `{host="x"}`, and each
   `vector(0)` is also exactly `{host="x"}`.
3. So the floor is **suppressed** while the host is alive, and **surfaces with
   its `host` label intact** once the host stops reporting.
4. `vector(0)` always returns a sample, so the expression can never be
   `NoData`.

Threshold is `IS BELOW 1`. Detection latency is about 10 to 11 minutes.

!!! success "Why it is generated, not written"
    `rules-availability.yaml` is the one alerting file that is **templated
    from the inventory** rather than committed. Adding a host to
    `hosts.local.yml` therefore cannot leave a silent gap in down-detection.
    Every other rules file is a static commit.

## The `job` label trap

Alloy's unix exporter sets its **own** `job` label, overriding the `job_name`
you configure. So the series arrives as `job="integrations/unix"`, not
`job="host-unix"`.

Anything matching only `job="host-unix"` silently returns nothing. The
original "Collectors Up" dashboard panel had **always** shown No data for
exactly this reason.

Every query in this repo matches both:

```promql
up{job=~"integrations/unix|host-unix"}
```

## Freshness, not liveness

Because a dead host's series vanishes, "how old is the newest sample" is the
honest health question. `timestamp()` inside a subquery recovers it and keeps
the series alive for up to 6h after a host stops reporting:

```promql
time() - max by (host) (
  max_over_time(timestamp(up{job=~"integrations/unix|host-unix"})[6h:1m])
)
```

This backs both the `Fleet Freshness` table and the
`fleet-collector-lagging` alert. Expect every value under 30 seconds.

!!! note "Why the freshness table has no `$host` filter"
    It is the authoritative "is everyone here" view. A saved single-host
    selection must not be able to hide an outage.

## The same shape, elsewhere

Two other rules use this pattern because they have the same problem:

| Rule | Vanishing series | Window that keeps it alive |
|---|---|---|
| `fleet-host-down` | `up` | `vector(0)` floor per host |
| `fleet-container-disappeared` | `container_last_seen` | `max_over_time(...[1h])` |
| `fleet-collector-lagging` | `up` | `[6h:1m]` subquery |

For containers the 1h window is deliberate: the alert names the container that
vanished, then self-resolves after an hour rather than nagging about something
you deliberately removed.

## Known blind spot

**Nothing here can tell you the central LXC is down**, because Grafana dies
with it. That needs an external dead-man's-switch: a healthchecks.io ping from
an `OnCalendar` timer on the central box, or an Uptime Kuma elsewhere.

Likewise `fleet-remote-write-failing` reads
`prometheus_remote_storage_samples_failed_total`, which is itself pushed. If
remote write is *totally* down, the metric proving it is also not arriving.
`fleet-host-down` is the backstop for that case.
