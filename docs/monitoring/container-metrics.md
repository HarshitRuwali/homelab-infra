# Container metrics

Container **logs** work as soon as the `alloy` user is in the `docker` group.
Container **metrics** do not, and the way they fail is the problem.

## The silent failure

cAdvisor additionally opens `/run/containerd/containerd.sock` to resolve the
overlayfs storage driver. That socket is `root:root 0660` with **no joinable
group**.

The resulting error is per-container and non-fatal, so the exporter stays up
and keeps publishing exactly one series, the root cgroup `id="/"`:

```text
level=error msg="Failed to create existing container:
/system.slice/docker-<id>.scope: unable to create containerd client for
overlayfs storage driver: containerd: cannot unix dial containerd api service:
dial unix /run/containerd/containerd.sock: connect: permission denied"
```

!!! danger "Everything looks configured"
    `integrations/cadvisor` is a live job. The panels exist. Every one of them
    says **No data**. Nothing is marked unhealthy, and the container logs
    arriving in Loki make it look like Docker integration is working.

    This state persisted unnoticed from the original rollout.

## The fix

`roles/alloy_collector` installs a systemd drop-in that runs Alloy as root,
**only** on hosts with a Docker socket, and removes it again if Docker goes
away:

```ini
# /etc/systemd/system/alloy.service.d/10-cadvisor-root.conf
[Service]
User=root
Group=root
```

### Why root and not something narrower

| Option | Why not |
|---|---|
| Add `alloy` to a group | The socket is `root:root`. There is no group to join. |
| Chown the containerd socket | Requires editing `/etc/containerd/config.toml` and restarting containerd, which **restarts every container on the host**. |
| `CAP_DAC_OVERRIDE` | Bypasses all file permission checks. Barely narrower than root, and less well understood. |
| Run cAdvisor as root | What upstream documents as the requirement. |

The drop-in is scoped and reversible, which is what makes it acceptable.

!!! note "Storage path ownership"
    Once the unit runs as root, root-owned WAL segments appear in
    `/var/lib/alloy`. Root can still write them, so nothing breaks now. The
    hazard is on the way **back**: removing the drop-in leaves the
    unprivileged user with files it cannot open.

    The role re-chowns the tree, but only when the drop-in has just changed.
    An unconditional `recurse: true` would report `changed` on every run
    forever.

## Verifying

```promql
count by (host, name) (container_last_seen{name!=""})
```

Expect one row per running container.

| Result | Meaning |
|---|---|
| One row per container | working |
| Nothing | cAdvisor is not running, or no containers |
| Rows with empty `name` | **the drop-in is missing** |

The `name` label is what separates a container from the machine it runs on:
the root cgroup and the per-container cgroup slices both carry an `id`, but
only real Docker containers get a `name`. Every container query and rule in
this repo filters on `name!=""`.

## `container_health_state` does not mean what it looks like

!!! bug "Verified on this fleet"
    cAdvisor reports `0` for **both** "healthcheck failing" and "image
    declares no HEALTHCHECK".

    | Container | `container_health_state` | `docker inspect` |
    |---|---|---|
    | `matrix-cloudflared` | `0` | **NO-HEALTHCHECK** |
    | `qdrant` | `0` | **unhealthy** |
    | `matrix-synapse` | `1` | healthy |

Alerting on `== 0` therefore pages you forever about every container that
simply never opted in, which is most images.

`fleet-container-unhealthy` requires evidence the container has **ever** been
healthy:

```promql
min by (host, name) (container_health_state{name!=""} == 0)
  and on (host, name)
max by (host, name) (max_over_time(container_health_state{name!=""}[6h]) == 1)
```

A no-healthcheck container is pinned at `0` forever and can never satisfy the
second clause. What remains is exactly the case worth waking up for: something
that **was** passing and now is not.

!!! info "Accepted tradeoff"
    A container unhealthy for more than 6h drops out of the window and stops
    alerting. Deliberate: by then it is a known-broken thing to go and fix,
    not news. The restart-loop and disappeared rules still cover it if it
    actually falls over.

## Useful queries

```promql
# containers per host
count by (host) (count by (host, name) (container_last_seen{name!=""}))

# CPU by container
sum by (host, name) (rate(container_cpu_usage_seconds_total{name!=""}[5m]))

# memory, working set
sum by (host, name) (container_memory_working_set_bytes{name!=""})

# restarts in the last 30 minutes
max by (host, name) (changes(container_start_time_seconds{name!=""}[30m]))

# uptime
time() - max by (host, name) (container_start_time_seconds{name!=""})

# percent of its own memory limit, guarded against unlimited containers
100 * container_memory_working_set_bytes{name!=""}
       / on (host, name) (container_spec_memory_limit_bytes{name!=""} > 0)
```

!!! warning "The `> 0` guard is load-bearing"
    A container with no memory limit reports
    `container_spec_memory_limit_bytes` as `0` (or the host total, depending
    on cgroup version). Dividing by it gives `+Inf`, which is above any
    threshold, so the alert would fire for every unlimited container in the
    fleet, which is most of them.
