# Homelab Security Stack

Four small guests, plus Suricata and ntopng on the firewall, that give a
self-hosted estate intrusion detection, host integrity monitoring, DNS
filtering, flow visibility and vulnerability scanning. Open source throughout.

## The premise

Most homelab security advice stops at "put a firewall in front of it". A
firewall is necessary and not sufficient, because **it only sees traffic that
crosses it**. Two hosts on the same layer 2 segment talk without it. A
multi-homed host routes around it. A container bridged to two networks
sidesteps it entirely.

So this stack deliberately pairs two kinds of sensor:

<div class="grid cards" markdown>

- **Network, at the chokepoint**

    Suricata and ntopng, both on the firewall. Cheap, broad, and structurally
    blind to anything that does not cross the boundary.

- **Host, on every machine**

    Wazuh and CrowdSec agents. They see what the chokepoint cannot, including
    lateral movement inside a segment.

- **Name resolution**

    AdGuard Home. DNS logs are the cheapest detection data you will ever
    collect, and a resolver you control is a prerequisite for having them.

- **Vulnerability scanning**

    Greenbone on `sec-scan`. Scheduled scans check reachable hosts for
    vulnerable software and exposed services.

</div>

## Start here

!!! tip "Standing this up for the first time?"
    **[Getting started](getting-started/index.md)** provisions the four guests
    with one dry-run-by-default script, then installs each service.

| If you want to… | Go to |
|---|---|
| Create the guests | [Getting started](getting-started/index.md) |
| See what each guest runs, its configs, and how to log in | [What runs where](components/index.md) |
| Understand the mechanisms, from bridges to decoders | [How it works](understanding/index.md) |
| Understand why things sit where they do | [Architecture](architecture/index.md) |
| Connect it to Grafana, Loki and the firewall | [Wiring](wiring/index.md) |
| Size disks, set retention, keep it running | [Operations](operations/index.md) |
| Look up a spec, a port or a licence | [Reference](reference/index.md) |
| Know what this stack itself exposes | [Security](security.md) |

## What it is not

This is detection and hardening infrastructure. It is not a substitute for
fixing what it finds. An unauthenticated datastore is better closed than
monitored: alerting tells you someone reached it, a password means they could
not. Close the door first, then instrument it.
