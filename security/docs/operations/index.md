# Operations

## Retention, and the sizing that follows from it

Retention here is **30 days**, chosen deliberately: this is a homelab, not a
compliance environment. That single decision drives most of the disk budget.

The working, for roughly 15 monitored hosts:

- Most hosts are idle VMs. A quiet Linux server produces on the order of 1,000
  to 3,000 alerts per day under default Wazuh rules. Call it 30,000 alerts per
  day across the estate, which is generous.
- An indexed alert with OpenSearch overhead runs about 1.5 KB, so roughly
  **45 MB per day**.
- Allow an order of magnitude for a genuinely noisy surface, such as an
  internet-facing web tier, plus rule tuning noise: **300 MB per day** is a
  defensible planning figure.
- 30 days at 300 MB per day is about **9 GB**.
- Add roughly 20 GB for the OS plus the manager, indexer and dashboard.
- Keep about 25 percent of the disk free for OpenSearch segment merges.

`(9 + 20) / 0.75` is about 39 GB, which is the generous ceiling. The
allocation is **40 GB**. The original 25 GB assumed the stack settles at about
10 GB, but the vulnerability feed alone is about 12 GB, and Wazuh needs the feed
and a feed update's download side by side. Single node, so no replica shard
doubles it.

Disk budget on `sec-wazuh` (measured 2026-11-13, 19 GB used of 40 GB):

| What | Size | Grows? |
|---|---|---|
| Vulnerability feed, `/var/ossec/queue/vd/feed` | about 12 GB | Flat, but a feed update needs room for a second copy while it downloads |
| Feed updater scratch, `queue/vd_updater/tmp` | under 100 MB | Kept small by the nightly cleanup, was 9 GB on 2026-09-23 |
| Indexer queue, `/var/ossec/queue/indexer` | about 1.6 GB | Slowly |
| Indexer data, `/var/lib/wazuh-indexer` | under 1 GB | Bounded by the 30 day index retention |
| Alert archives, `/var/ossec/logs/alerts` | about 3.4 MB per day | Bounded by the archive retention (90 days by default, about 0.3 GB) |
| OS, packages, Wazuh binaries | the rest | No |

!!! warning "Watch the disk, not just the alerts"
    OpenSearch stops allocating shards at 90 percent disk and turns every index
    read-only at 95 percent, which is a silent stop in ingestion. The fleet's
    Disk Usage High alert fires at 95 percent, **after** the first of those, so
    it is not enough here on its own. Give `sec-wazuh` its own disk alert at 80
    percent when it is onboarded, and when it fires, grow the disk as below
    rather than shortening retention.

!!! danger "Set the policy before you have data"
    Create the ISM policy in the Wazuh indexer on day one: hot for 7 days then
    rollover, delete at 30 days, applied to `wazuh-alerts-*`. Without it,
    indices grow until the disk fills, and the failure mode is a **silent stop
    in ingestion**, not an alert.

### Keep cheap archives longer than the searchable index

The real cost of 30 day retention is investigative: if you detect something on
day 45, the index no longer holds the evidence.

Wazuh already rotates and gzips daily alert files into
`/var/ossec/logs/alerts/YYYY/MMM/`. Those are alert records only, not the full
event stream, and they compress to a few MB per day. A year is a couple of GB.
The nightly cleanup prunes them after 90 days by default; set
`ALERT_RETENTION_DAYS` (see Recurring work) to 365 or more if you keep a long
archive.
Attach a second disk from the bulk tier and bind-mount it:

```
/mnt/archive/alerts  /var/ossec/logs/alerts  none  bind  0 0
```

Searching them means `grep` and `jq` rather than the dashboard, which is fine
for the rare retrospective question. You get 30 days of fast search plus a year
of "can I still answer this", for almost nothing.

!!! warning "Do not reach for `logall`"
    Enabling `<logall>` or `<logall_json>` archives the **full event stream**
    rather than alerts, and is genuinely large. It is the opposite of the cheap
    archive described above.

Set AdGuard's query log retention to 30 days too, in Settings, so the two
halves of your detection data expire together.

## Growing, and why to start small

Proxmox makes growing a disk trivial and shrinking it a manual, risky
operation:

```bash
qm resize 110 scsi0 +20G     # then grow the filesystem inside the guest
```

That asymmetry is the argument for sizing from measured data rather than a
guess, as the 25 GB first allocation was. If real ingest runs
higher, you add disk in a minute. Over-provision and reclaiming it means a
backup, a rebuild and a restore.

## Thin provisioning

If your storage is LVM-thin, which is the Proxmox default for `local-lvm`, an
allocated disk only consumes what is actually written. Over-allocation costs
little **until you over-commit and the pool fills**, at which point every guest
on it suffers at once.

```bash
pvesm status                 # Type column: lvmthin vs lvm vs dir
lvs -o lv_name,lv_size,data_percent,metadata_percent
```

If it is thin, watch `data_percent`. If it is thick, the allocation is consumed
immediately and the SSD versus bulk split matters more.

## Measure, then re-size

After two weeks of real operation, replace the estimate above with your own
number:

```bash
# on sec-wazuh
curl -sk -u admin:<pw> \
  "https://localhost:9200/_cat/indices/wazuh-alerts-*?v&h=index,docs.count,store.size&s=index"
du -sh /var/ossec/logs/
```

Project from that, not from the planning figure.

## Backups

Add all four guests to Proxmox Backup Server. They hold your detection history
and the credentials that protect it, which cost more to lose than a rebuildable
service.

```bash
vzdump 200 --storage <pbs-storage> --mode snapshot --compress zstd
```

Suricata and ntopng live on the firewall, so their settings are in the
firewall's own configuration. Back that up too: OPNsense exports it from
**System > Configuration > Backups**.

Snapshot before every Wazuh major upgrade. Its index migrations are not
reliably reversible.

## Recurring work

Roughly monthly:

- **Confirm every agent is still reporting.** A silent agent is the failure
  mode nobody notices.
- **Tune whatever fires constantly.** An alert stream nobody reads is the same
  as no alerting.
- **Re-run the Greenbone baseline and diff it** against last month. What you
  are looking for is drift: a new unauthenticated service, another multi-homed
  host, an interface nobody declared.

Automatic, nightly at 02:00 IST on `sec-wazuh`:

- **`wazuh-vd-tmp-cleanup.timer`** does two things. It deletes vulnerability
  feed updater scratch files in `/var/ossec/queue/vd_updater/tmp` that have not
  been written for six hours. Wazuh leaves its feed downloads there; on
  2026-09-23 they reached 9 GB, filled the then 25 GB root, stopped the manager
  and silenced the guest's monitoring. The disk was grown to 40 GB the same day,
  because a full feed update needs the feed and its download side by side. It
  also deletes rotated alert archives (`ossec-alerts-*.gz` under
  `/var/ossec/logs/alerts/`) older than `ALERT_RETENTION_DAYS`, 90 by default,
  because Wazuh never prunes them. Installed by
  `install/configure-sec-wazuh-cleanup.sh`, which `install-sec-wazuh.sh` runs.
  Check it with `journalctl -u wazuh-vd-tmp-cleanup`.

  Preview what it would delete with `sudo wazuh-vd-tmp-cleanup --dry-run`. Change
  the retention with a drop-in
  (`systemctl edit wazuh-vd-tmp-cleanup.service`, then
  `Environment=ALERT_RETENTION_DAYS=365`); `0` keeps archives forever. Raise it
  if you keep the long archive described under
  [Keep cheap archives longer](#keep-cheap-archives-longer-than-the-searchable-index).

## Verification

After each deployment, run something that fails if it did not work.

```bash
# manager reachable from a sandbox host, proving the firewall allow rule works
timeout 5 bash -c 'echo > /dev/tcp/<sec-wazuh>/1514' && echo ok || echo BLOCKED

# agents actually reporting, on the manager
/var/ossec/bin/agent_control -l        # every host listed, state Active

# the resolver clients actually get, run from a client, not from the server
resolvectl status | grep 'DNS Servers'

# ntopng capturing, on the firewall's shell; then find a sandbox host
# with a recent last-seen time under Hosts in its UI
/usr/local/etc/rc.d/ntopng status

# CrowdSec making decisions, on sec-crowdsec
cscli decisions list
cscli metrics
```

!!! tip "A check you have never seen fail proves nothing"
    Do not accept "the service is running" as evidence that monitoring works.
    Confirm events arrive from an agent, then **stop that agent**, confirm it
    goes Disconnected in the dashboard, and start it again. A pipeline you have
    only ever seen green is untested infrastructure.
