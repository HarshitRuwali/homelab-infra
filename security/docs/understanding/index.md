# How it works, end to end

[What runs where](../components/index.md) is the reference: paths, ports, config
files. This page is the **mental model**. It works from the foundations up, so
that when something breaks you can reason about it rather than search for the
error string.

Read it once in order. After that it is a lookup.

---

## Part 1: The foundations

### Virtualisation: two completely different things

Proxmox runs both, and the difference decides what each guest can do.

| | KVM (VM) | LXC (container) |
|---|---|---|
| Kernel | Its own | **Shares the host's** |
| Isolated by | Hardware (Intel VT-x / AMD-V) | Namespaces plus cgroups |
| Boots | A real kernel, ~20s | A process tree, ~1s |
| Overhead | A few percent CPU, real RAM for its kernel and page cache | Effectively none |
| Can load kernel modules | Yes | **No** |
| Can set sysctls | Yes | Only namespaced ones |

**A container is not a small VM.** It is a set of processes on the host kernel
that have been lied to about what they can see. The lies are:

- **Namespaces** (`mnt`, `pid`, `net`, `ipc`, `uts`, `user`, `cgroup`, `time`):
  change *what a process can see*. Its own PID 1, its own network interfaces,
  its own filesystem root.
- **cgroups v2**: change *what a process can consume*. CPU, memory, IO, PIDs.
- **Capabilities**: split root's power into ~40 pieces. `CAP_NET_RAW` for raw
  sockets, `CAP_NET_ADMIN` for interface config, `CAP_NET_BIND_SERVICE` for
  ports below 1024, `CAP_SYS_ADMIN` for most of the rest.
- **seccomp** and **AppArmor**: filter syscalls and file access.

### Unprivileged containers, and the UID shift

An *unprivileged* LXC adds a **user namespace**. Root inside the container
(UID 0) is mapped to an unprivileged UID on the host, conventionally 100000.

```
inside container:  uid 0    (root)
on the host:       uid 100000
```

Two consequences you will meet:

1. **Container root cannot harm the host.** Even if it escapes a process, it
   holds an unprivileged host UID.
2. **Bind mounts look wrong from one side.** A host directory owned by `root`
   (0) appears owned by `nobody` (65534) inside, because host UID 0 has no
   mapping in the container's range. This is the usual cause of "permission
   denied on a mount that looks fine".

!!! info "Why `sec-wazuh` and `sec-scan` are VMs"
    The Wazuh indexer is OpenSearch, which wants `vm.max_map_count=262144` and
    unlimited `memlock`. Those are host-kernel settings a container cannot own.
    Greenbone needs `CAP_NET_RAW` for scanning. Both are VMs for concrete
    technical reasons, not preference.

### Linux bridges: a software switch

A Linux bridge is a layer 2 switch in software. It learns MAC addresses and
forwards frames between the ports attached to it.

- A **VM** attaches via a `tap` device.
- A **container** attaches via a `veth` pair: one end inside, one on the bridge.
- A bridge with a **physical NIC** attached has an uplink to the real network.
- A bridge with `bridge-ports none` is **internal only**. Traffic on it reaches
  the outside world only if something routes it.

This estate:

```
vmbr0   bridge-ports nic1    10.10.0.0/24    has an uplink, gw 10.10.0.1
vmbr1   bridge-ports none    10.10.50.0/24   internal, routed by OPNsense
vmbr2   bridge-ports none    unused
```

OPNsense (VMID 102) has a leg on each: `net0` on vmbr0, `net1` on vmbr1,
`net2` on vmbr2. So **vmbr0 is OPNsense's WAN and vmbr1 is its LAN.**

### NAT, and the three things it breaks

Source NAT rewrites the source address of outbound packets so replies come
back. It is what lets many hosts share one address. It also:

1. **Breaks inbound connections.** There is no entry in the translation table
   until something goes out first. This is why WAN to LAN needs explicit port
   forwards.
2. **Destroys attribution.** Every host behind the NAT arrives as one address.
3. **Hides internal structure.** Useful for privacy, hostile to monitoring.

All three matter here. Traffic from 10.10.50.x to 10.10.0.x is NATed by
OPNsense, so:

- Wazuh **does not care**: agents are identified by ID and key, not IP.
- AdGuard **does care**: that whole segment logs as one client.
- Greenbone **would be blocked entirely** from the WAN side, because it needs to
  originate inbound. That is why `sec-scan` sits on vmbr1. Its scans of
  10.10.0.x now go *out* through the NAT, so those targets log OPNsense's
  address as the scanner.

### Routing asymmetry is the whole architecture

A stateful firewall with default rules gives you:

```
LAN  -> WAN   allowed  (and the reply is allowed by state)
WAN  -> LAN   denied   (no state exists yet)
```

Everything about where the managers sit follows from this:

- **Agent-initiated tools work.** Wazuh and CrowdSec agents dial *out* to their
  manager. A manager on the WAN side is reachable from the LAN side.
- **Scanner-initiated tools do not.** Greenbone originates connections *to* its
  targets. From the WAN side it cannot reach the LAN at all.

That is not a Greenbone limitation. It is a direct consequence of stateful
filtering, and it is the single most useful thing to understand about this
stack's layout.

The fix follows from the same table. Put the scanner on the LAN side and every
connection it makes is LAN to WAN or LAN to LAN, both allowed. So the managers
sit on vmbr0 and the scanner sits on vmbr1: each tool is placed so its
connections flow *with* the asymmetry, not against it.

### DNS: what actually happens

```
app → stub resolver (libc / systemd-resolved)
    → recursive resolver
         → root servers      "who handles .com?"
         → TLD servers       "who handles example.com?"
         → authoritative     "what is www.example.com?"
    → answer, cached for TTL
```

**AdGuard Home is a forwarder plus a filter, not a full recursive resolver** by
default. It answers from cache or filter rules, otherwise passes the query
upstream. You can point it at Unbound to make the chain genuinely recursive.

Transport matters for visibility:

| Transport | Port | Visible to you? |
|---|---|---|
| Plain DNS | 53 | Fully |
| DoT | 853/tcp | Encrypted, but the port gives it away |
| DoH | 443/tcp | **Hides in normal HTTPS** |
| DoQ | 853/udp | Encrypted |

**A device using DoH to a public resolver bypasses your DNS logging entirely**,
and it looks like ordinary web traffic. Countering it means blocking outbound
53 and known DoH endpoints at the firewall, which is a rule you maintain.

!!! tip "Why DNS is the cheapest detection data there is"
    Malware must resolve a name before it connects. So DNS shows intent
    **before any payload moves**. It catches C2 beaconing (regular intervals to
    one domain), DGA domains (algorithmically generated, high-entropy names),
    and exfiltration over TXT records. All of that is a text log, not packet
    capture.

### TLS: fingerprinting without decrypting

The handshake starts in the clear:

```
ClientHello   → TLS version, cipher suites, extensions, curves, SNI
ServerHello   → chosen cipher
Certificate   → (encrypted in TLS 1.3)
```

Two things survive encryption and are worth knowing:

- **SNI** (Server Name Indication): the hostname, sent in the clear so one IP
  can serve many sites. Suricata logs it. *Encrypted Client Hello will
  eventually remove this*, which is a real coming gap in network monitoring.
- **JA3**: a hash of the ClientHello's version, cipher list, extensions, curves
  and point formats. Different TLS stacks produce different hashes, so **you can
  identify what software is connecting without decrypting anything.** Malware
  families are often identifiable by JA3 alone. `JA3S` is the server-side
  equivalent; `JARM` actively probes to fingerprint a server.

---

## Part 2: Detection concepts

### Three ways to detect

| Approach | Finds | Misses | Example here |
|---|---|---|---|
| **Signature** | Known bad, precisely | Anything novel | Suricata rules |
| **Behavioural** | Patterns of action | Slow, careful attackers | CrowdSec scenarios |
| **Integrity / state** | Change from known good | Attacks that change nothing | Wazuh FIM |

None is sufficient. A signature engine misses a zero day by definition. A
behavioural engine misses an attacker who stays under the threshold. Integrity
monitoring misses an in-memory attack that touches no file. **Layering them is
not belt and braces, it is covering each one's structural blind spot.**

### The Pyramid of Pain

David Bianco's model, and the reason "block the IP" is weak:

```
        TTPs              ← tough for the attacker to change
      Tools
   Network/Host Artifacts
      Domain Names
        IP Addresses
      Hash Values        ← trivial to change
```

Blocking a hash costs the attacker a recompile. Blocking an IP costs them a new
VPS. Detecting a **technique**, such as "a process wrote to a directory it has
never written to, then opened an outbound connection", costs them a rethink.

This stack aims at the middle and upper bands: CrowdSec detects behaviour, Wazuh
detects state change and technique, Suricata covers the known-bad lower bands
cheaply.

### MITRE ATT&CK, briefly

A catalogue of what attackers actually do, structured as:

- **Tactic**: the goal. Initial Access, Persistence, Lateral Movement, Exfiltration.
- **Technique**: the method. T1053 Scheduled Task, T1021 Remote Services.
- **Procedure**: the specific implementation.

Useful because it turns "are we secure?" into "which techniques would we
actually see?" Wazuh maps many of its rules to ATT&CK IDs, which is how you
audit coverage rather than guess at it.

### The base rate problem, and why alert fatigue is the real risk

Suppose a detection is 99% accurate, and 1 in 10,000 events is genuinely
malicious. Out of 10,000 events:

- 1 true positive (probably)
- ~100 false positives

**So 99% of your alerts are wrong**, despite a "99% accurate" detector. This is
the base rate fallacy, and it is why tuning matters more than adding rules.

The practical consequence: **an untuned ruleset across a whole fleet produces a
volume nobody reads**, which is worse than no alerting, because it manufactures
confidence. Roll out to two hosts, tune, then expand.

### Defence in depth, stated usefully

Not "more tools". It means **no single control failing should be sufficient**.
Concretely here: the firewall fails open for intra-segment traffic, so the host
agents cover it. The host agent can be killed by an attacker with root, so the
network sensor and DNS log cover that. DNS can be bypassed with DoH, so Suricata
sees the TLS. Each layer covers the one before it.

---

## Part 3: Tool internals

### Wazuh: decoders and rules

The two-stage pipeline is the thing to understand.

**Decoders** turn raw text into fields. XML, and they nest:

```xml
<decoder name="sshd">
  <program_name>^sshd</program_name>
</decoder>

<decoder name="sshd-failed">
  <parent>sshd</parent>
  <prematch>^Failed \S+ for </prematch>
  <regex offset="after_prematch">^(\S+) from (\S+) port </regex>
  <order>user, srcip</order>
</decoder>
```

`prematch` is a cheap filter that runs first. `regex` extracts. `order` names
the captures. Without a matching decoder, no rule can ever fire, because the
rule has no fields to test.

**Rules** match decoded fields and assign a level:

```xml
<rule id="5710" level="5">
  <if_sid>5700</if_sid>
  <match>Failed password</match>
  <description>Failed SSH login</description>
</rule>

<rule id="5712" level="10" frequency="8" timeframe="120">
  <if_matched_sid>5710</if_matched_sid>
  <description>SSH brute force: 8 failures in 120s</description>
</rule>
```

Note the second rule: **correlation lives in the rule**, via
`frequency` plus `timeframe` plus `if_matched_sid`. That is how Wazuh turns
individual events into an incident.

**Levels 0 to 15.** 0 means suppress entirely. Alerts are written at level 3 and
above by default. Roughly: 5 user error, 10 repeated failures, 12 high
importance, 15 severe.

**FIM** (`syscheckd`) runs scheduled by default and compares hashes plus
metadata against a baseline database. `realtime="yes"` uses inotify. `whodata`
goes further and hooks the Linux audit subsystem so you learn **which process
and user** made the change, not just that it changed.

**Other modules** worth naming: SCA (evaluates CIS benchmark policies locally),
the vulnerability detector (pulls CVE feeds and compares against the agent's
installed package inventory, which is how it does authenticated-quality results
without a scanner), and active response (runs a script on a rule match).

### CrowdSec: the parse chain and the bucket

Acquisition to enforcement, in ordered stages:

```
acquis.yaml   what files/journald units to read
   ↓
s00-raw       split into lines
s01-parse     grok patterns, extract fields
s02-enrich    GeoIP, whois, reverse DNS
   ↓
scenarios     leaky buckets
   ↓
LAPI          stores the decision
   ↓
bouncers      enforce it
```

**Bucket types** matter for writing your own:

- **leaky**: fills with events, drains at a rate. Overflow means "too fast".
- **trigger**: fires on a single event. For things that are bad once.
- **counter**: counts over a fixed window, no leak.

A leaky bucket with capacity 5 and a 10s leak rate tolerates a user mistyping a
password twice an hour forever, and catches 6 attempts in 20 seconds. That
distinction is the whole point, and it is why thresholds are expressed as rates
rather than counts.

**The Hub** packages parsers plus scenarios as *collections*:
`cscli collections install crowdsecurity/sshd`.

**The community blocklist** is opt-in: you share anonymised signals, you receive
a curated list of IPs other people are being attacked by. You can run entirely
local and skip it.

### Suricata: inside the detection engine

```
capture → decode → flow tracking → stream reassembly
        → app-layer parsing → detection → output
```

- **Flow tracking** groups packets into bidirectional conversations.
- **Stream reassembly** rebuilds TCP byte streams, so an attack split across
  packet boundaries still matches. This is what separates an IDS from `grep`.
- **App-layer parsing** identifies HTTP, TLS, DNS, SMB regardless of port, so a
  web server on 8443 is still parsed as HTTP.
- **MPM** (multi-pattern matcher) is a fast prefilter. With thousands of rules
  you cannot evaluate each one per packet, so Suricata extracts the longest
  `content` string from each rule and runs them all simultaneously, using
  **Hyperscan** where available and Aho-Corasick otherwise. Only rules whose
  pattern hit are then fully evaluated.

A rule, annotated:

```
alert http $HOME_NET any -> $EXTERNAL_NET any ( \
  msg:"Suspicious user agent";                  \
  flow:established,to_server;                   \
  http.user_agent; content:"evilbot";           \
  sid:1000001; rev:1;)
```

`flow:` limits direction and state. `http.user_agent` is a *sticky buffer*: it
scopes the following `content` to that field only, which is far cheaper and more
precise than scanning the whole payload.

**EVE JSON** emits `event_type` of `alert`, `dns`, `http`, `tls`, `flow`,
`fileinfo` and more. Only `alert` is an alert. The rest is the metadata that
makes retrospective hunting possible, and is often more valuable.

### Flows and ntopng packet capture

A **flow** is a unidirectional conversation summary: the 5-tuple (src IP, dst
IP, src port, dst port, protocol) plus start and end times, packet count and
byte count. It is metadata, not content.

ntopng captures packets on the firewall through the `os-ntopng` plugin. It
builds flows from those packets and identifies applications through its nDPI
library.

**Which interface matters.** On OPNsense's WAN, every vmbr1 host has already
been rewritten to OPNsense's own address by NAT. Capture on LAN to see who is
really talking.

### Greenbone: why authenticated scanning changes everything

- **Unauthenticated**: connect, port scan, read banners, infer from version
  strings. On Debian this is **actively misleading**, because security fixes are
  *backported*: `nginx 1.22.1-9+deb12u1` may contain a patch for a CVE while the
  version string still looks like vulnerable 1.22.1.
- **Authenticated**: log in over SSH, read the actual installed package list,
  compare against the feed. This is what you want.

**NASL** is the scripting language the vulnerability tests are written in. The
feeds are NVTs (the tests), SCAP (CVE and CPE data), CERT advisories, and
gvmd-data (scan configs and report formats).

**CVE** identifies a vulnerability. **CPE** identifies a product version.
**CVSS** scores severity 0.0 to 10.0. A CVSS score is *not* a risk score: it
ignores whether the service is exposed, whether you run the affected
configuration, and whether a patch exists. Treat it as an input.

---

## Part 4: Deployment layout

### Suricata on the firewall

OPNsense bundles Suricata, so it runs without an extra guest. Its native EVE
JSON output feeds the Loki collector configuration in
[Wiring](../wiring/index.md).

### Host agents rather than network sensors alone

A network sensor cannot see what does not cross the wire. On this estate,
`ubuntu-dev` to `ubuntu-ai` is bridge-local and never reaches OPNsense. Without
agents, a compromise that pivoted between them would be **completely invisible**.

### Managers on the trusted segment

Agent-initiated tools dial out. Put the manager on the segment that cannot be
reached inward and every agent still reaches it, while the segment running risky
workloads does not host the thing watching it.

### The scanner on the sandbox side

A scanner originates connections, so it goes where its connections flow *with*
the asymmetry: vmbr1. The cost is that it lives beside the workloads trusted
least, which cuts against "the sandbox should not host the thing that watches
it". A scanner does not watch continuously, so the cost is smaller than for a
manager, and it is contained by never giving it privileged scan credentials.
The alternative, a pinhole letting a vmbr0 scanner route into vmbr1, weakens
the one direction the asymmetry protects.

### One NIC per guest

A multi-homed host bridges two segments whether or not you intended it, and the
traffic is **invisible in the firewall's own logs** because it never reaches the
firewall. If a security guest seems to need a second NIC, the routing needs
fixing instead. The scanner is the proof: the answer to "it needs both
segments" was a better placement, not a second NIC.

### ntopng on the firewall

The firewall sees every packet crossing between its interfaces, and ntopng
captures there directly. ntopng and Redis share its RAM and CPU with Suricata,
and ntopng adds another parser of hostile traffic to the firewall. Budget for
that resource use and keep its management UI closed to WAN.

### Provisioned from files, not the UI

A rule edited in a UI has no history, no review and no recovery. A rule in git
has all three. The corollary is that UI edits are *lost*, which is intentional:
git is the source, the UI is the view.

### Dry run by default

Every script here prints what it would do and exits without doing it. `--apply`
is the only thing that mutates. The reason is that the expensive failure is not
"the command errored", it is "the command half-worked against production".

---

## Part 5: Things that are true here and will surprise you

1. **The hypervisor has no route to 10.10.50.0/24.** Its only routes are
   `default via 10.10.0.1` and `10.10.0.0/24` on vmbr0.
2. **Wazuh agents on 10.10.50.x show OPNsense's address** in `agent_control -l`,
   because of NAT. Not a bug; agents key on ID, not IP.
3. **An empty ntopng is not evidence of no lateral traffic.** It cannot see
   intra-segment conversations at all.
4. **An empty Greenbone report usually means no route or an unsynced feed**, not
   a clean estate.
5. **A CrowdSec LAPI with zero parsed lines reports perfectly healthy.** Check
   `cscli metrics`, never `systemctl status`.
6. **Wazuh stops ingesting silently when the indexer disk fills.** Set the ISM
   retention policy before you have data.
7. **Greenbone is not in Debian.** `gvm` is in sid only, absent from bookworm,
   trixie and forky. It runs from Greenbone's containers.
8. **Greenbone and ntopng both start as `admin` / `admin`.** ntopng forces a
   change; Greenbone does not.

---

## Part 6: A debugging method that works

When something in this stack is "not working", the question is almost never
"is the service running". Work the pipeline from the source outward:

1. **Is data arriving?** `tcpdump`, `cscli metrics`, `agent_control -l`. Most
   failures are here, and most of them report healthy.
2. **Is it being parsed?** `wazuh-logtest`, `cscli explain`. A missing decoder
   and a quiet network look identical from the dashboard.
3. **Is the rule matching?** Check the level threshold before assuming the rule
   is wrong.
4. **Is the output reaching the store?** Disk full, index policy, Loki labels.
5. **Only then**, suspect the tool.

Verify that each component produces the expected output. A successful exit or
a running service does not prove that data is being collected and processed.
