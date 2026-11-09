# Proxmox guests

One script, `pve-guest.sh`, that builds a reusable VM template and creates
two kinds of guest from it:

- **Your own** VMs and LXC containers, the way the rest of the fleet expects
  them: unprivileged containers with `nesting=1`, cloud-init VMs with your SSH
  key and no password, DHCP, start on boot. Onboard them with Ansible after.
- **Leased VMs for other people** (`tenant`), which are built on the opposite
  assumption: whoever holds one is hostile. Each goes on its own network, the
  LEASE segment (`vmbr5`), apart from a work VM of your own (`tenant
  --uncapped`, on the WORK segment, `vmbr2`). See [How leased VMs are
  isolated](#how-leased-vms-are-isolated).

It uses **only the images already on the host** and never downloads anything.
Fetch a new image with `pveam download` or the web UI first, and it becomes
usable.

Every command is a **dry run by default**: it prints the preflight checks and
the exact commands it would run, and changes nothing. Read the plan, then
rerun the same line with `--apply`. `./pve-guest.sh --help` lists every option.

## Setup

One time. Part 4 is only needed if you lease VMs to other people.

### 1. Put the script on the hypervisor

It runs on the Proxmox host, as root.

```bash
# from the repository root, on your laptop
scp proxmox/pve-guest.sh root@t7920:/root/

# then on the host
ssh root@t7920
chmod +x /root/pve-guest.sh
export SSH_PUBKEY=/root/.ssh/harshit-laptop.pub   # the key your own guests trust
```

Put the `export` in `/root/.bashrc` to keep it. Copy the script again
whenever it changes in the repository.

### 2. Build the VM template

```bash
./pve-guest.sh images             # what is cached and usable
./pve-guest.sh template           # read the plan
./pve-guest.sh template --apply   # builds VMID 9000, tmpl-debian-13
```

`images` must list a `debian-*-genericcloud-amd64.qcow2` under cloud images.
If it doesn't, download one to `/var/lib/vz/template/iso` first. When it's
done, `qm list` shows 9000, and every VM from now on is a clone of it. LXC
containers need no template step: they use the cached tarballs directly.

### 3. Firewall the hypervisor

Before this, nothing filters traffic to the host itself: SSH, the web UI,
rpcbind and an unauthenticated netdata dashboard are open to the whole LAN,
and through OPNsense's NAT to every guest on `vmbr1`. See [How the
hypervisor is protected](#how-the-hypervisor-is-protected).

1. **Write the rules**, from a Tailscale session:

   ```bash
   ./pve-guest.sh firewall
   ./pve-guest.sh firewall --apply
   ```

   The dry run checks that your session would survive, and prints the host's
   rules exactly as PVE compiles them. `--apply` writes
   `/etc/pve/firewall/cluster.fw` (who may manage the host, the gateway
   aliases, and the `tenant` and `lease` groups) and the node's `host.fw`,
   then checks both with PVE's own parser. The datacenter firewall stays
   **off**, so nothing changes yet.

   **`cluster.fw`** is only ever created, never merged into or rewritten.
   - One without the `tenant` and `lease` groups (a hand-written one, or one
     from before the `lease` group existed) is refused, and the dry run prints
     what to merge by hand.
   - One with both groups is compared with what the script writes now, ignoring
     comments, blank lines, the datacenter switch and the order of options,
     aliases and ipset entries (PVE re-sorts those). If it matches, the output
     says `cluster.fw is current`. If it does not, the script prints a diff,
     says the file is not rewritten because it may hold your edits, and goes
     on: merge the `+` lines by hand, or move the file aside and rerun. That is
     how an older install finds out about ranges added later, such as the IPv6
     ones in `[IPSET private]`. The order of the rules in a group does count,
     since the first match wins.

   **`host.fw`** is the script's own when it carries the header line `# Managed
   by proxmox/pve-guest.sh`, or when its rules equal what the script writes
   (PVE drops the comments when it rewrites a file, header included).
   - A missing one is written (`wrote host.fw`), even when `cluster.fw` is
     already complete.
   - One that is the script's own and differs from what the script now writes
     is rewritten after its diff is printed (`updated host.fw`), so a file from
     an older version gets what was added since.
   - One without the header whose rules differ is yours: it is never touched,
     and the script prints what to merge.

2. **Make sure OPNsense's LAN address (`10.10.0.114`) is a DHCP reservation**
   on the home router. The rules single it out, and a new lease would let
   every sandbox guest and tenant back in.

3. **Turn it on**, from a Tailscale session, with an automatic undo in case
   anything cuts you off:

   ```bash
   systemd-run --on-active=10min --unit=pve-fw-undo pvesh set /cluster/firewall/options --enable 0
   pvesh set /cluster/firewall/options --enable 1
   pve-firewall status        # enabled/running
   ```

   From a **new** session, check:

   - SSH and `https://<host>:8006` over Tailscale, and over the LAN
   - that the LAN still reaches the internet through OPNsense
   - that Grafana still receives from every host
   - that `curl -m5 http://10.10.0.109:19999` from a `vmbr1` guest times out

   If all of that works, keep it with `systemctl stop pve-fw-undo.timer`. If
   you lose access, wait: ten minutes after enabling, the firewall turns
   itself back off.

### 4. Prepare for leased VMs

1. **Set up OPNsense's LEASE interface** as described in
   [OPNsense](#opnsense) below, and its TENANT interface too if you want work
   VMs. Each needs a bridge that only OPNsense is on (`vmbr5` and `vmbr2`);
   the script refuses to create a VM when its bridge is missing.

2. **Test with a canary lease.** Create one for yourself as in
   [Lease a VM](#lease-a-vm-to-a-friend), but join it to a throwaway tailnet
   (not your main one). Run the [isolation checks](#isolation-checks) from
   inside it, then [end its lease](#end-a-lease). Only then lease one to
   anybody else.

## Usage

### Create a VM of your own

```bash
./pve-guest.sh vm --name app01 --cores 2 --memory 2048 --disk 16
./pve-guest.sh vm --name app01 --cores 2 --memory 2048 --disk 16 --apply
```

Unset options default to 1 vCPU, 1024 MB RAM, an 8 GB disk, `vmbr0` and the
next free VMID. Use `--bridge vmbr1` for the sandbox segment behind OPNsense,
and `--no-start` to create it and leave it stopped.
When it's done, the script prints these steps with the MAC filled in:

1. Give it a DHCP reservation: the home router serves `vmbr0`, OPNsense serves
   `vmbr1`.
2. `ssh admin@<ip>`, then `sudo apt-get install -y qemu-guest-agent`, so
   Proxmox can show its IP and shut it down cleanly.
3. Add it to `ansible/inventory/hosts.local.yml` and onboard it from
   `ansible/` with `ansible-playbook playbooks/onboard.yml --limit app01`.
   That installs Alloy and proves its metrics and logs reach Grafana.

### Create a container of your own

```bash
./pve-guest.sh lxc --name svc01 --memory 512
./pve-guest.sh lxc --name svc01 --memory 512 --image ubuntu-24.04-standard --apply
```

The same next steps apply, except that you log in as `root@<ip>` and there is
no guest agent to install. Containers are for your own services only, never
for tenants.

### Lease a VM to a friend

You set the VM up with your own key, join it to **their** Tailscale account,
then hand it over.

1. **Create it**, with your key for setup:

   ```bash
   ./pve-guest.sh tenant --name alice-vm --user alice --key $SSH_PUBKEY \
       --ip 10.10.90.10 --cores 2 --memory 2048 --disk 20
   ```

   Rerun with `--apply`. `--user` is the login the friend will use, and
   `--ip` is a free address in `10.10.90.2` to `.254`. A lease goes on the
   LEASE segment (`vmbr5`, `10.10.90.0/24`) with the firewall group `lease`.
   Addresses there are static: there is no DHCP, so the address is whatever you
   pass. The script refuses one outside that range and one already pinned to
   another tenant VM, on either segment. It tags the VM `tenant;lease`. Note
   the VMID it prints.

   The first `--apply` for a lease also installs the packet-rate hookscript,
   and may add `snippets` to the content of the `local` storage; the dry run
   says so first. See [Packet-rate limit](#packet-rate-limit).

   **The CPU model.** A lease gets `x86-64-v2-AES` by default
   (`LEASE_CPU_TYPE`). It hides AVX and AVX2 from the guest, so a tenant cannot
   run the gather instructions that attacks such as GDS (Downfall) use against
   other guests on the same CPU. That is a mitigation on top of the host's
   microcode and kernel updates, not a replacement for them.

   A lease may only use a model that hides AVX, and the script keeps a list of
   them rather than a list of the bad ones: `x86-64-v2-AES`, `x86-64-v2`,
   `kvm64`, `qemu64`, `Nehalem`, `Westmere`, `Penryn` and `Conroe`. Any other
   `--cpu` is refused for a lease, with a message saying so. That includes
   `max` and `host` (under KVM both are the host's own CPU), `custom-*` models
   (defined on the host, unknown to the script) and models such as
   `SandyBridge`, `IvyBridge` or `Opteron_G4`, which have AVX without AVX2:
   any VEX-encoded instruction, gathers included, runs once a guest can enable
   AVX state, so AVX2 is not the line. `--uncapped` VMs are not held to the
   list; `--cpu host` is still refused for them.

2. **Log in through OPNsense**, the only host a tenant VM accepts SSH from:

   ```bash
   ssh -J root@10.10.0.114 alice@10.10.90.10
   ```

   `-J`, not `ssh -A` and a second hop: your key stays on your laptop
   instead of being forwarded into OPNsense.

3. **Join it to their tailnet:**

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --hostname alice-vm
   ```

   Send the login URL it prints to your friend, and have them log in with
   **their** account. **Never log it in with yours**: your tailnet routes
   `10.10.0.0/24` and its ACLs allow everything, so one login would give
   them your whole network. `tailscale status` must show their account before
   you go on. Log out of the VM.

4. **Hand it over.** Get their SSH public key, copy it to the host, and:

   ```bash
   ./pve-guest.sh handover --vmid <vmid> --key /root/tenants/alice.pub
   ./pve-guest.sh handover --vmid <vmid> --key /root/tenants/alice.pub --apply
   ```

   This swaps the key in the VM's cloud-init config and reboots it. It refuses
   any key this hypervisor's root trusts, which would be yours. The reboot is
   deliberate: cloud-init treats the changed config as a new instance, adds
   their key and regenerates the SSH host keys once. Doing it now, before
   their first login, means they never see a host key change.

5. **They connect** over their tailnet with `ssh alice@alice-vm`. Once
   they're in, have them (or do it yourself before you leave) replace
   `~/.ssh/authorized_keys` with only their key. The handover output prints
   the exact command.

Removing your key from `authorized_keys` alone isn't enough: the VM's config
would still hold it, and cloud-init re-adds it whenever that config is
regenerated. That's why step 4 comes first.

### What to tell your friend

- Connect with `ssh <login>@<name>` over your own tailnet. Nothing is reachable
  from the public internet.
- You have `sudo`. The VM is yours to configure, and keeping it patched is on
  you. Automatic security updates are a good start:
  `sudo apt install unattended-upgrades`, if it isn't there already.
- It can reach the internet but none of the home network. Outbound mail on
  port 25 is blocked.
- CPU, disk and network are capped: about 100 Mbit/s of network and 100 MB/s
  of disk, and at most 20000 packets per second sent, on top of the byte rate.
  The firewall also limits open connections (10000 states per source in the
  TCP rule and 10000 in the rule for every other protocol, so up to 20000 in
  all) and new TCP connections (300 per 10 seconds); a scan or a flood past
  that gets the VM blocked from the internet for up to an hour.
- AVX and AVX2 are not available in the VM, on purpose: the CPU model hides
  them so that one guest cannot attack another through those instructions.
  Software built for them will not start (a Bun binary is one). If you need
  them, ask: the owner can change the VM's CPU model by hand, or make it a
  work VM, which has `x86-64-v3` like the owner's own.
- Plain DNS (port 53, TCP and UDP) goes to the firewall's resolver: one
  hard-coded in the VM (such as `8.8.8.8`) is redirected to it. DNS over TLS
  (TCP 853) and DNS over QUIC (UDP 853) are blocked. DNS over HTTPS, and DNS
  inside your own Tailscale tunnel, can't be seen.
- The firewall logs the new connections from the VM (addresses, ports, time)
  and the DNS lookups it answers, and keeps those records for 30 days. It does
  not see the content of the traffic, and traffic inside your own Tailscale
  tunnel, or through an exit node, shows only as one connection to a relay or
  peer. See [Monitoring tenants](#monitoring-tenants).
- Please don't make it a Tailscale exit node. Your browsing would leave from
  my home IP.

### A work VM of your own

A VM you use for work gets a tenant's isolation, so work software (VPN
clients, EDR, whatever the employer installs) never sees the homelab, and the
homelab never sees work data. Since it's yours, it skips the caps and the
handover, and stays out of the fleet: no Ansible, Alloy or Wazuh.

1. **An Ubuntu template, once.** Download the cloud image (not the live
   server ISO) on the host, check it against `SHA256SUMS` from the same
   directory, then build it next to the Debian one:

   ```bash
   cd /var/lib/vz/template/iso
   wget https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
   cd /root
   ./pve-guest.sh template --image ubuntu-26.04-server-cloudimg-amd64.img --vmid 9001
   ./pve-guest.sh template --image ubuntu-26.04-server-cloudimg-amd64.img --vmid 9001 --apply
   ```

   That builds `tmpl-ubuntu-26.04`. The image can be deleted afterwards, as
   the template holds its own copy.

2. **Create it** as a tenant, with your key, and `--uncapped`:

   ```bash
   ./pve-guest.sh tenant --name optimuslabs --user harshit --key $SSH_PUBKEY \
       --ip 10.10.60.20 --template 9001 --cores 4 --memory 8192 --disk 32 --uncapped
   ```

   Rerun with `--apply`. `--uncapped` puts it on the WORK segment (`vmbr2`,
   `10.10.60.0/24`, so `--ip` is in that range), never on the same network as
   a lease. It gets the firewall group `tenant`: the internet, and nothing
   private. `--uncapped` drops only the CPU weight, the disk and the network
   limits, and tags it `tenant;uncapped`. It keeps the CPU model `x86-64-v3`
   (AVX2), since the VM is yours.

3. **Join your tailnet with a tag, and give that tag nothing.** Not untagged:
   your tailnet routes `10.10.0.0/24` and its ACLs allow everything, so an
   untagged login would undo the isolation. In the admin console, own the tag
   and let only your own devices start connections:

   ```jsonc
   "tagOwners": { "tag:work": ["autogroup:admin"] },
   "grants": [
     // was "src": ["*"]. Tagged devices aren't members, so tag:work gets
     // nothing. Add any other tag that still needs to start connections.
     { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] }
   ]
   ```

   Then, through OPNsense as in [Lease a VM](#lease-a-vm-to-a-friend):

   ```bash
   sudo tailscale up --hostname optimuslabs --advertise-tags=tag:work
   ```

   From the VM, `tailscale ping <any of your devices>` must fail, and
   `curl -m5 -sk https://10.10.0.109:8006` must time out.

4. **Skip `handover`.** Your key stays. Connect with `ssh harshit@optimuslabs`, or
   VS Code's Remote-SSH to the same host.

Run the [isolation checks](#isolation-checks) from inside it too, all but
the last line: `tailscale status` lists your devices here, and the `tailscale
ping` above is what proves the tag holds.

### Look after tenants

```bash
grep -l '^tags:.*tenant' /etc/pve/qemu-server/*.conf   # every tenant VM
grep -l '^tags:.*lease' /etc/pve/qemu-server/*.conf    # the leases only
grep -A1 ipfilter-net0 /etc/pve/firewall/<vmid>.fw     # its IP
qm status <vmid>
```

Both segments work the same way: a VM's address and firewall file are found by
its VMID. A VM made before the LEASE segment existed is tagged `tenant` alone
and is on `vmbr2`; the script does not move a VM between segments, so recreate
it as a lease to move it. [Packet-rate limit](#packet-rate-limit) has the
manual route for one that is already on the LEASE segment.

- **More RAM or CPU:** `qm set <vmid> --memory 4096 --cores 4`, then
  `qm reboot <vmid>`.
- **More disk:** `qm disk resize <vmid> scsi0 +10G`. The guest grows its
  filesystem on the next boot.
- **A different network cap:** keep the MAC and the bridge, or cloud-init
  treats the VM as new and regenerates its host keys:
  `qm set <vmid> --net0 virtio=<MAC>,bridge=<bridge>,firewall=1,rate=25`, with
  the MAC and the bridge (`vmbr5` for a lease) from `qm config <vmid>`. On a
  running VM PVE applies that by removing the whole ingress qdisc on its tap,
  and the packet-rate filter goes with it until the next stop and start. So
  finish by putting it back, either by running the hookscript by hand,
  `/var/lib/vz/snippets/lease-limits.pl <vmid> post-start`, or by restarting
  the VM with `qm shutdown <vmid>` and `qm start <vmid>`. Check with
  `tc -s filter show dev tap<vmid>i0 parent ffff:`, which must show
  `pkts_rate`. The monitoring raises an alert for a lease with no filter: the
  metric `fleet_pve_tenant_packet_limit_active` is 1 when the filter exists
  and 0 when a running lease lacks it.
- **A second NIC:** don't add one. A lease has `net0` only: the script builds
  just that, and its firewall file pins only `ipfilter-net0`. A `net1` and up
  would get no packet ceiling, no IP pinning and no metrics.
- **A lease that needs AVX or AVX2:** the script refuses a lease on any model
  that has them, so this is a decision by hand, for somebody you trust:
  `qm set <vmid> --cpu x86-64-v3`, then `qm shutdown <vmid>` and
  `qm start <vmid>` (a reboot from inside doesn't apply it). It gives that
  one VM the instructions the default hides. For a VM of your own, use a work
  VM (`tenant --uncapped`) instead.
- **They lost their key:** run `handover` again with the new one. The old
  key stays in `authorized_keys` until they remove it.

### End a lease

```bash
qm set <vmid> --protection 0 && qm stop <vmid> && qm destroy <vmid> --purge
```

`--protection 1` is set on every tenant, so it can't be destroyed by
accident. Destroying it also removes its firewall file. Ask the friend to
remove the machine from their tailnet, and remove any `--allow` port
forwards.

### Rebuild the template

When the cloud image gets old, download a fresh one to
`/var/lib/vz/template/iso`, then:

```bash
qm destroy 9000
./pve-guest.sh template --apply
```

Existing guests are full clones and don't depend on the template, so this is
safe at any time.

## How the hypervisor is protected

| From | SSH, web UI, consoles | Anything else (rpcbind, netdata, ...) |
|---|---|---|
| Your tailnet | yes | yes |
| The home LAN (`10.10.0.0/24`) | yes, as a way in when Tailscale is down | no |
| OPNsense (`10.10.0.114`), and so every `vmbr1` guest and every tenant behind it | no | no |
| Anywhere else | no | no |

The host still gets its DHCP lease, Tailscale's direct connections and replies
to anything it started itself.

- **Tailscale can't lock you out.** Tailscale installs its own chain first in
  the host's `INPUT` and accepts everything on `tailscale0` before any PVE rule
  runs. The script checks that before enabling. It also means every device on
  your tailnet can reach every port on the host, so your Tailscale ACLs are
  what limits that.
- **OPNsense is dropped twice**: excluded from the management ipset, and
  dropped by an explicit rule ahead of everything else. The ipset exclusion is
  real in the kernel (`nomatch`), but PVE's own simulator ignores it, so the
  explicit rule doesn't depend on it.
- **PVE's automatic "local network" doesn't cover the LAN here.** `/etc/hosts`
  maps `t7920` to an old address, `192.168.0.4`, so PVE only treats
  `127.0.0.0/8` as local. The explicit management list is what lets the LAN in.
  The stale entry is worth fixing on its own.
- **Conntrack has headroom.** The node's `host.fw` sets
  `nf_conntrack_allow_invalid: 1` (OPNsense routes between bridges on this host,
  so one flow crosses it twice and strict conntrack would drop the second leg),
  and, because a tenant can open flows faster than the default table (262144)
  holds them, `nf_conntrack_max: 1048576` and
  `nf_conntrack_tcp_timeout_syn_recv: 30`. A larger table and a shorter
  half-open timeout buy time while the per-guest alerts fire. OPNsense's
  per-source limits apply after this table, not before it.
- **netdata and rpcbind stay running**, but nothing outside your tailnet can
  reach them any more. If netdata is left over from before Alloy, remove it.
  If no NFS is used, disable rpcbind. Both are separate decisions.

To change who may manage the host, set `MGMT_LAN` and `OPNSENSE_LAN_IP`
before running `firewall`. To make management Tailscale-only, remove the LAN
line from `[IPSET management]` in `cluster.fw` by hand afterwards. That
leaves the physical console as the only way in if Tailscale fails.

## How leased VMs are isolated

A tenant is root inside their VM, so anything placed in it belongs to them.
Everything here follows from that.

There are two segments, so a lease never shares a layer-2 segment with a VM of
your own. Which one a VM is on depends on what it is:

| Segment | For | Bridge and network | Firewall group | OPNsense interface |
|---|---|---|---|---|
| LEASE | a VM leased to somebody else (the default) | `vmbr5`, `10.10.90.0/24` | `lease` | LEASE (`opt4`, `vtnet5`) |
| WORK | a VM of your own, `tenant --uncapped` | `vmbr2`, `10.10.60.0/24` | `tenant` | TENANT (`vtnet2`) |

| Risk | What stops it |
|---|---|
| Escaping to the hypervisor | VMs only, never containers: a container shares the host's kernel, so one kernel bug would give a tenant the host. No guest agent, no passthrough, a generic CPU model (`x86-64-v2-AES` for a lease, `x86-64-v3` for a work VM; `--cpu host` is refused). |
| Attacking other guests through the CPU | A lease's CPU model hides AVX and AVX2, so its guest cannot run the gather instructions that GDS (Downfall) style attacks need. This adds to the host's microcode and kernel mitigations and does not replace them. The script refuses any model for a lease that is not on its list of ones that hide AVX. |
| Joining your tailnet | Tailscale in the VM is logged in with **the tenant's** account, never yours. Your tailnet routes `10.10.0.0/24` and its ACLs allow everything, so one login with your account would hand them your network. |
| Reaching your network | A lease is on `vmbr5`, a bridge only OPNsense is on, never `vmbr0` (which carries the hypervisor's own address). The `lease` firewall group drops every private range in both directions, except the lease gateway, and logs each drop; the ranges are listed below, IPv6 included. OPNsense blocks the same ranges. |
| Reaching each other | A lease shares no segment with a work VM (`vmbr2`). Every tenant's inbound policy is DROP, the only exception being SSH from OPNsense (your setup path), and other leases and work VMs are private addresses. `ipfilter` pins each VM to its one IP, and `macfilter` to its MAC, so it can't pose as a neighbour. `dhcp: 0` and `radv: 0` stop it handing out DHCP leases or router advertisements. |
| Starving your services | Reserved RAM (no ballooning), half the default CPU weight, disk capped at 100 MB/s and 2000 IOPS each way, network at 12.5 MB/s (`TENANT_*` to change). `--uncapped` drops all but the reserved RAM, so it's for your own VMs only. |
| Filling the host's conntrack table | A packets-per-second ceiling on each lease's tap (see [Packet-rate limit](#packet-rate-limit)), which the byte cap on the NIC does not give. `host.fw` also raises the table size and shortens the half-open timeout. |
| Abuse from your home IP | Outbound port 25 is blocked, in the hypervisor group and on OPNsense. On the LEASE interface OPNsense allows each source 10000 states in the TCP pass rule and 10000 in the rule for every other protocol, so up to 20000 in all, and 300 new TCP connections per 10 seconds; a source over the connection rate is banned for up to an hour. DNS over TLS (TCP 853) and DNS over QUIC (UDP 853) are blocked. Suricata on OPNsense sees their traffic. |
| Dodging the resolver's log | OPNsense redirects plain DNS (TCP/UDP port 53) from the lease network, to anything but the gateway, to its own Unbound, so a hard-coded resolver is still logged. DNS over TLS and over QUIC (port 853) are blocked. DNS over HTTPS, and DNS inside a tenant's own Tailscale tunnel, are not visible. |
| Doing it unseen | Most of what a tenant does is recorded and kept, but not all of it: see [Monitoring tenants](#monitoring-tenants) for the gaps. |
| A second NIC | A lease has `net0` only. The script builds just that, and its firewall file pins only `ipfilter-net0`. A `net1` and up would get no packet ceiling, no IP pinning and no metrics, so nobody should add one. |
| Stealing fleet secrets | Nothing of the fleet's goes in: no fleet key, no Ansible, no Wazuh, no Alloy with the collector password. Your key is only there during setup, and `handover` removes it from the VM's config. |
| Being reached from the internet | Nothing is published. Tenants come in over their own tailnet, which the firewall only ever sees as outbound traffic, so there are no port forwards and your router stays closed. |
| A typo opening a hole | PVE silently skips a rule it can't parse. The script parses every rule with PVE's own parser before writing it, and again after, and refuses on any complaint. |

What `private` holds, in `cluster.fw`'s `[IPSET private]`, which both groups
drop in both directions:

| Range | What it is |
|---|---|
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | RFC 1918 private addresses |
| `100.64.0.0/10` | Tailscale, and carrier-grade NAT |
| `169.254.0.0/16` | IPv4 link-local |
| `fe80::/10` | IPv6 link-local: another guest on the same bridge, which no IPv4 range would stop |
| `fc00::/7` | IPv6 unique local, which includes the tailnet's `fd7a::/48` |

The script refuses to create a tenant while either firewall group (`tenant` or
`lease`) is missing from `cluster.fw`, while the lease gateway alias
(`lease_gw`) is missing or points elsewhere, or while the datacenter firewall
is off, so there is never a moment when a tenant VM is unfiltered.

What you can't prevent from the outside: a tenant can make their VM an exit
node on their own tailnet, and then their browsing leaves from your home IP.
With friends, agree on it rather than try to block it.

### Packet-rate limit

The NIC cap (`TENANT_NET_RATE`) limits bytes, so a lease sending a flood of
tiny packets could still fill the host's conntrack table. Each lease's tap
therefore gets a packets-per-second ceiling on what the guest sends:
`LEASE_PPS`, default `20000`, with a burst of twice that. PVE already puts an
ingress qdisc with its own `basic police` byte-rate filter (priority 50) on a
rate-limited tap; the extra filter is a `police` action with `pkts_rate` at
priority 5, with `conform-exceed drop/continue`. That means a packet over the
packet rate is dropped, and a packet within it is passed on to PVE's byte-rate
filter, so **both limits apply**. With the default conform action, `ok`, a
conforming packet would end classification at priority 5 and the byte cap would
never see it, leaving the guest bound only by packets per second. The tap is
recreated at every start, so a Proxmox hookscript sets it again each time.

- **What the script does.** For a lease, never for `--uncapped`, it writes the
  hookscript to `LEASE_HOOKSCRIPT` (default
  `/var/lib/vz/snippets/lease-limits.pl`, mode 0755, `LEASE_PPS` filled in) and
  runs `qm set <vmid> --hookscript local:snippets/lease-limits.pl`. The dry run
  prints both steps. The hookscript acts only after a start and always exits 0,
  so a failure inside it never stops a VM from starting. The file itself is
  another matter: PVE runs the hookscript's pre-start phase with stop-on-error,
  so if the file is missing or not executable the lease fails to start,
  including at boot. To recover, run `./pve-guest.sh hookscript` (the dry run)
  and then `./pve-guest.sh hookscript --apply`: it installs the file, mode
  0755, and the `snippets` setting if that is missing too, with the same code
  `tenant` uses and no VM needed. Or detach the hookscript from the lease with
  `qm set <vmid> --delete hookscript`.
- **The `hookscript` subcommand.** `./pve-guest.sh hookscript` does the
  host-wide half on its own, without creating a VM. With `--vmid <vmid>` it
  also runs `qm set <vmid> --hookscript local:snippets/lease-limits.pl`, after
  checking that the VM is a QEMU VM tagged `lease`; it refuses any other VM,
  and one that already has a different hookscript. Dry run by default, like
  every command.
- **The snippets storage.** Proxmox only takes a hookscript from a storage
  whose content list includes `snippets`. If `local` lacks it, the dry run says
  that `--apply` will run `pvesm set local --content <the existing list>,snippets`.
  The list the storage already has is read and extended, never replaced, and
  `--apply` does this before it creates the directory and installs the
  hookscript. With no storage named `local` the script stops and says so. The
  hookscript's directory must be `local`'s `snippets` directory.
- **One file for every lease.** The file is rewritten only when its content
  differs. A lease created with another `LEASE_PPS` rewrites it, and the
  leases already running pick up the new rate at their next start.
- **Leases made before this existed: recreate them with the new script.** An
  old lease is tagged `tenant` alone, so the exporter counts it as a work VM:
  the alert "Lease Packet Limit Missing" can never fire for it, and its CPU is
  still `x86-64-v3`. The manual route is only for a VM that is already a lease
  in every other way, on `vmbr5` with the `lease` firewall group and a
  10.10.90.x address (an old lease on `vmbr2` stays on the work segment and is
  not a lease in this sense, whatever its tags say):
  `qm set <vmid> --tags 'tenant;lease' --cpu x86-64-v2-AES`, then
  `./pve-guest.sh hookscript --vmid <vmid> --apply`, then one restart for both,
  `qm shutdown <vmid>` and `qm start <vmid>` (a reboot from inside doesn't
  recreate the tap or change the CPU).
- **Checking it:** `tc -s filter show dev tap<vmid>i0 parent ffff:` shows the
  priority-5 filter with `pkts_rate` and a dropped count. The
  `pve_guest_metrics` role exports that count as
  `fleet_pve_tenant_policed_packets_total{vmid,guest,segment}`, and whether
  the filter exists as `fleet_pve_tenant_packet_limit_active` (1 when it does,
  0 when a running lease lacks it, which the monitoring alerts on).
- **It does not survive a hot `--net0` change.** See [Look after
  tenants](#look-after-tenants).

### Host hardening for leasing

Two settings on the hypervisor itself, made once and by hand. `pve-guest.sh`
does not make them.

**Turn SMT off.** With SMT on, the CPU's L1TF, MDS, MMIO and TAA mitigations
are incomplete across sibling threads, and threads of different guests can
share a core. On the reference host the kernel also reports
`gather_data_sampling` as Vulnerable with no command-line option to blame,
which is why leases also get a CPU model without AVX. The price is the sibling
threads. Put this in `/etc/systemd/system/smt-off.service`:

```ini
[Unit]
Description=Turn SMT off: leased guests share this host, and with SMT on the CPU's L1TF, MDS, MMIO and TAA mitigations are incomplete
Before=pve-guests.service
After=local-fs.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo off > /sys/devices/system/cpu/smt/control'
ExecStop=/bin/sh -c 'echo on > /sys/devices/system/cpu/smt/control'
[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now smt-off.service
cat /sys/devices/system/cpu/smt/active                      # 0
cat /sys/devices/system/cpu/vulnerabilities/gather_data_sampling
```

`systemctl disable --now smt-off.service` turns SMT back on. Turning SMT off
does not change the `gather_data_sampling` line: what covers that here is that a
lease's CPU model cannot run gather instructions at all.

**Free half-open connections sooner.** `host.fw` already shortens the
`syn_recv` timeout. This sysctl does the same for connections a guest started
and nothing answered. Put it in `/etc/sysctl.d/91-tenant-conntrack.conf`:

```ini
# Half-open outbound connections (SYN sent, no answer yet) stay in conntrack for
# 120 s by default. A tenant scanning or flooding fills the table with them, so
# free the slots sooner. Pairs with nf_conntrack_tcp_timeout_syn_recv in host.fw.
net.netfilter.nf_conntrack_tcp_timeout_syn_sent = 30
```

```bash
sysctl --system
sysctl net.netfilter.nf_conntrack_tcp_timeout_syn_sent     # after a reboot too
```

**What `--apply` does not do.** `pve-guest.sh` writes `cluster.fw`, the node's
`host.fw` (with the conntrack options above; created if missing, rewritten if
it is the script's own and differs, never if it is yours), each VM's firewall
file, the packet-rate hookscript, and the `snippets` content of `local` when it
is missing. It never rewrites a `cluster.fw` that has both groups. It does not:

- install the SMT unit or the sysctl file above;
- touch OPNsense: the LEASE and TENANT interfaces, their rules, the DNS
  redirect, Unbound logging, Suricata, the syslog forwarding, the SSH setup path;
- configure Alloy or Loki, or run the `pve_guest_metrics` role: the log and
  metric pipeline is Ansible's;
- create the bridges (`vmbr5`, `vmbr2`) or attach OPNsense's NICs to them;
- join the VM to a tailnet or set anyone's Tailscale ACLs.

### OPNsense

OPNsense (VM 102) has one interface per segment: LEASE for leases, and TENANT
for work VMs. Each needs a bridge that only OPNsense is on. Set up LEASE first;
TENANT is only needed for `tenant --uncapped`.

#### LEASE interface

The `vmbr5` NIC is attached to OPNsense, where it shows up as `vtnet5`. On that
interface:

- Assign it as **LEASE** (`opt4`), static IPv4 `10.10.90.1/24` (or change
  `LEASE_PREFIX` to match yours). No DHCP: leases get static addresses from
  cloud-init.
- **Unbound** listening on LEASE, so leases resolve through `10.10.90.1`, with
  query and reply logging on.
- Rules on LEASE, in sequence order, every one with source LEASE net:

  | Seq | Action | Protocol | Destination | Port | Log | Why |
  |---|---|---|---|---|---|---|
  | 100 | pass | TCP/UDP | LEASE address (`opt4ip`) | 53 | no | DNS |
  | 110 | block | TCP | any | 853 | yes | DNS over TLS would skip the resolver |
  | 111 | block | UDP | any | 853 | yes | DNS over QUIC would skip the resolver |
  | 152 | block | UDP | This Firewall | 5351 | no | UPnP chatter, not worth logging |
  | 154 | block | UDP | This Firewall | 1900 | no | UPnP chatter, not worth logging |
  | 200 | block | any | This Firewall | any | yes | its web UI and SSH |
  | 300 | block | any | `RFC1918` | any | yes | an alias of `10/8`, `172.16/12`, `192.168/16`, `100.64.0.0/10` and `169.254.0.0/16` |
  | 400 | block | TCP | any | 25 | yes | no spam from the home IP |
  | 490 | pass | TCP | any | any | yes | at most 10000 states per source and 300 new connections per 10 s per source; exceeding that bans the source through the `virusprot` table for up to an hour |
  | 500 | pass | any | any | any | yes | everything else, at most 10000 states per source |

  Interface (rule) is LEASE, never `any`. Rule 100 must come before 200: This
  Firewall includes `10.10.90.1`.
- A **NAT redirect** (Firewall > NAT > Port Forward) on LEASE: TCP/UDP port 53
  from LEASE net to anything but the LEASE address, redirected to `10.10.90.1`
  port 53. A resolver hard-coded in a lease is answered by Unbound instead, so
  the query is still logged, and rule 100 lets it through.
- **Suricata** on the LEASE interface too. Its alerts carry the interface in
  `in_iface`: `vtnet5` for a lease, `vtnet2` for a work VM.
- **Logs to Loki.** The firewall's filter log, the resolver log and a
  per-minute `pfstats` line go by syslog to the host that runs Alloy's syslog
  listener, which stores them in Loki. See
  [the wiring page](../security/docs/wiring/index.md) for the receiving side
  and [Monitoring tenants](#monitoring-tenants) for what ends up where.

#### TENANT interface

The `vmbr2` NIC is already attached to OPNsense (VM 102, `net2`). On that
interface:

- Assign it as **TENANT**, static IPv4 `10.10.60.1/24` (or change
  `TENANT_PREFIX` to match yours). No DHCP: tenants get static addresses from
  cloud-init.
- **Unbound** listening on TENANT, so tenants resolve through `10.10.60.1`.
- Rules on TENANT, in order, every one with source TENANT net:
  1. pass → TENANT address, TCP/UDP 53 (DNS)
  2. block → This Firewall, UDP 5351 and UDP 1900, not logged (seq 152 and
     154: UPnP chatter, not worth logging)
  3. block → This Firewall (its web UI and SSH), logged
  4. block → `RFC1918`, logged. An alias of `10/8`, `172.16/12`,
     `192.168/16`, `100.64.0.0/10` and `169.254.0.0/16`
  5. block → any, TCP 25, logged
  6. pass → any, TCP (seq 490), logged, at most 50000 states per source and
     2000 new connections per 10 s per source; exceeding that bans the source
     through the `virusprot` table
  7. pass → any, every other protocol (seq 500), logged, at most 50000 states
     per source

  Interface (rule) is TENANT, never `any`. Rule 1 must come before rules 2 and
  3: This Firewall includes `10.10.60.1`. Rule 2 must come before rule 3, or
  the chatter is logged.
- A **NAT redirect** (Firewall > NAT > Port Forward) on TENANT: TCP/UDP port 53
  from TENANT net to anything but the TENANT address, redirected to
  `10.10.60.1` port 53, as on LEASE.
- Suricata on the TENANT interface too, if it isn't already.

#### SSH on OPNsense, for the setup hop

Key-only (System > Settings > Administration > Secure Shell), listening only on
the interface that faces the home LAN. Here that is OPNsense's **WAN**
(`vtnet0`, `10.10.0.114`): its LAN is the `vmbr1` sandbox. Allow TCP 22 to it
from `10.10.0.0/24` only. No port forwards: tenants arrive over Tailscale,
which only needs outbound UDP and HTTPS. A tenant created with `--allow` needs
a forward for those ports, and only those.

These rules repeat the Proxmox groups on purpose, so a mistake in either
one alone doesn't open anything.

### Isolation checks

From inside the canary lease (for a work VM, use `10.10.60.x` for its own
addresses and `10.10.90.1` as the other segment):

```bash
curl -m5 -sk https://10.10.0.109:8006 && echo LEAK     # hypervisor UI
curl -m5 -sk https://10.10.0.129     && echo LEAK      # central ingest
ping -c2 -W2 10.10.0.1               && echo LEAK      # home router
ping -c2 -W2 10.10.50.1              && echo LEAK      # vmbr1 sandbox
ping -c2 -W2 10.10.60.1              && echo LEAK      # the work segment
nc -zw5 smtp.gmail.com 25            && echo LEAK      # outbound mail
sudo ip addr add 10.10.90.99/24 dev eth0; ping -c2 -W2 -I 10.10.90.99 1.1.1.1 && echo LEAK; sudo ip addr del 10.10.90.99/24 dev eth0
curl -m5 -sI https://deb.debian.org | head -1           # must answer
dig +short @9.9.9.9 example.com                         # must answer, from the firewall's Unbound (needs dnsutils)
grep -c avx /proc/cpuinfo                               # a lease: 0
tailscale status | head -3                              # the tenant's tailnet, not yours
```

Nothing should print `LEAK`, the `curl` should return a status line,
`grep -c avx` should print `0` in a lease (and a count above 0 in a work VM),
and `tailscale status` should list only the tenant's own devices. The `dig`
query should also show up in Unbound's log on the firewall, since the redirect
sends it there. From your laptop, `ssh <login>@<its address>` must fail: your
LAN has no way in.

### Monitoring tenants

Not from inside. Alloy in a tenant VM would need the fleet's collector
password, which the tenant could read and use to forge metrics or logs for any
host, and a path from its segment to the ingest endpoint. Watch them from the
outside instead.

**The hypervisor sees each VM's CPU, memory, disk and network, and the
per-VM metrics are in Grafana.** The `pve_guest_metrics` Ansible role
([`ansible/roles/pve_guest_metrics`](../ansible/roles/pve_guest_metrics)) names
each guest's NIC, so the
[Guest Traffic](../monitoring/docs/monitoring/dashboards.md#guest-traffic)
dashboard (`guest-traffic`) can show what each one sends and receives. For
tenant VMs it also reports how many connections each holds open and how many
packets the `tenant` and `lease` groups dropped.

**OPNsense and the hypervisor firewall keep the records of what a VM did.**
They reach Loki as these streams:

| Stream | What it holds | How to find a VM |
|---|---|---|
| `job="filterlog"` | the new flows of the TENANT and LEASE interfaces, passed or blocked, that the rules log (the two quiet UPnP drops are not logged); labels `iface` and `action` | the source IP |
| `job="unbound"` | the DNS queries and replies Unbound handled, with the client IP | the client IP |
| `job="pve-firewall"` | what the hypervisor firewall dropped (its rules log at `info`, SMTP at `warning`); labels `chain` and `vmid` | the `vmid` label |
| `job="suricata"` | IDS alerts; the field `in_iface` is `vtnet2` for a work VM and `vtnet5` for a lease | the source IP |
| `job="pfstats"` | the state table and the interface counters, once a minute | not per VM |

In Grafana's Explore view, on the Loki data source:

```logql
{job="filterlog"} |= "10.10.90.10"
{job="unbound"} |= "10.10.90.10"
{job="pve-firewall", vmid="113"}
```

Loki keeps 30 days on the live install: it takes the retention from
`LOKI_RETENTION_PERIOD` in `.env` on the central node, set to `720h` there; see
[Retention](../monitoring/docs/operations/retention.md#loki-retention-for-the-tenant-logs).
The firewall also keeps the TLS server names and HTTP hosts a VM connects to in
its local Suricata `eve.json`, which is not shipped to Loki.

What none of this covers, or covers only in part:

- **The content of the traffic.** The records say which addresses talked, when
  and how much, and which names were looked up. Nothing reads the payload.
- **Tailscale.** What runs inside a tenant's own Tailscale tunnel, or through
  their exit node, is encrypted: the firewall sees one flow to a relay or a
  peer, not where it was going, and a DNS lookup inside the tunnel is not seen
  at all. DNS over HTTPS is invisible for the same reason.
- **Gaps in the logs themselves.** The log lines for a source the overload ban
  blocks are rate-limited on the way into Loki (the firewall's own log has all
  of them). The hypervisor's log is limited by PVE to one line a second per
  rule. Inbound flows to `--allow` ports are not logged on the hypervisor.
- **Flows that never leave the guest.** Whatever happens between processes
  inside the VM does not cross a segment, so no log here sees it.

## Reference

### What it builds from

| Kind | Source on the host | Default |
|---|---|---|
| VM template | cloud images (`.qcow2`, `.img`, `.raw`) in `/var/lib/vz/template/iso` or `/var/lib/vz/import` | newest `debian-*-genericcloud-amd64.qcow2`, built as `tmpl-debian-13` at VMID 9000 |
| VM, tenant | a VM template, cloned in full | template 9000 |
| LXC | the tarballs in `/var/lib/vz/template/cache` (`local:vztmpl`) | newest `debian-13-standard` |

Installer ISOs (Ubuntu live server, Kali, OPNsense, Windows) show up in
`images` but are never used: they need someone at the console to install, so a
guest built from one can't be created unattended.

### Choices worth knowing

- **Full clones, not linked.** A linked clone keeps its template in place for
  as long as the clone exists. With full clones you can destroy the template
  and rebuild it from a newer image at any time. The cost is the base disk,
  about 3 GB per VM.
- **The template holds only what every clone shares.** Name, size, bridge and
  SSH key are set on each clone. The template's description records which
  image built it and that image's date, so you can tell when it's stale.
- **Every VM gets a named CPU model, `x86-64-v3` by default.** Left unset,
  `qm` falls back to `kvm64`, which hides SSE4.2, POPCNT and AVX2. Modern
  runtimes need those: Claude Code (a Bun binary) just spins at startup on
  `kvm64`. `x86-64-v3` has AVX2 and still hides the exact host CPU. Change it
  with `--cpu` on `template`, `vm` and `tenant`, or `CPU_TYPE` for every run;
  the script refuses a model the host can't run. To fix an existing VM:
  `qm set <vmid> --cpu x86-64-v3`, then `qm shutdown` and `qm start` (a reboot
  from inside doesn't apply it). The exception is a lease, which defaults to
  `x86-64-v2-AES` (`LEASE_CPU_TYPE`) because it has no AVX, and may only be given
  a model that has none; see [Lease a VM](#lease-a-vm-to-a-friend). The template's own CPU model doesn't matter:
  every clone gets `--cpu` set explicitly.
- **Consoles need a password to log in.** Every VM has a serial port, for
  the web UI's xterm.js and `qm terminal <vmid>`. The display is the default
  VGA, so the noVNC console shows tty1, except on Debian genericcloud, which
  has no graphics driver and only talks on serial. cloud-init creates the
  login with a key and a locked password, so either console stops at the
  login prompt until you run `sudo passwd <login>` inside the guest. SSH
  stays key-only. Set it inside the guest, not with `qm set --cipassword`:
  any cloud-init change makes the next boot a new instance, with new host
  keys.
- **Your VMs have no guest agent until you install one.** The Debian cloud
  image doesn't ship `qemu-guest-agent`. Until you install it, Proxmox can't
  show the VM's IP. Tenant VMs have the agent turned off on purpose.
- **Refuses rather than modifies.** A VMID or hostname already in use stops the
  run. If a step fails partway through, the partial guest is destroyed, so a
  rerun starts clean instead of skipping over a broken guest.
- **Capacity is checked first.** A request for more RAM than the host has free,
  or more disk than the pool has free, is refused before anything is created.

### Environment

| Variable | Default | For |
|---|---|---|
| `STORAGE` | `local-lvm` | guest disks |
| `STORAGE_TMPL` | `local` | LXC tarballs |
| `SSH_PUBKEY` | `~/.ssh/id_ed25519.pub` | the key your own guests trust |
| `MGMT_LAN` | `10.10.0.0/24` | the LAN allowed to manage the host |
| `OPNSENSE_LAN_IP` | `10.10.0.114` | OPNsense's address on `vmbr0`; never allowed in |
| `TENANT_BRIDGE` | `vmbr2` | the WORK segment, for `--uncapped` |
| `TENANT_PREFIX` | `10.10.60` | the work /24; OPNsense is `.1` |
| `TENANT_NET_RATE` | `12.5` | MB/s per tenant NIC |
| `TENANT_DISK_MBPS` | `100` | MB/s each way per tenant disk |
| `TENANT_DISK_IOPS` | `2000` | IOPS each way per tenant disk |
| `CPU_TYPE` | `x86-64-v3` | CPU model for templates, VMs and `--uncapped` work VMs |
| `TENANT_CPUUNITS` | `50` | CPU weight; your guests have 100 |
| `LEASE_BRIDGE` | `vmbr5` | the LEASE segment, where a lease goes |
| `LEASE_PREFIX` | `10.10.90` | the lease /24; OPNsense is `.1` |
| `LEASE_CPU_TYPE` | `x86-64-v2-AES` | CPU model of a lease; must be one that hides AVX (see [Lease a VM](#lease-a-vm-to-a-friend)) |
| `LEASE_PPS` | `20000` | packets per second a lease's guest may send; the burst is twice that |
| `LEASE_HOOKSCRIPT` | `/var/lib/vz/snippets/lease-limits.pl` | where the packet-rate hookscript is written; must be in `local`'s `snippets` directory |

## Tests

Run from this directory. Neither needs a hypervisor, and neither contacts a
host or changes anything outside a temporary directory:

```bash
./tests/pve-guest-test.sh            # the script in a sandbox, with shims for qm, pvesh and the rest
./tests/docs-consistency-test.sh     # this README and the usage block against the script
```

The first runs a copy of `pve-guest.sh` with its host paths moved into a
temporary directory, shims for the Proxmox commands, and a stand-in for PVE's
Perl firewall parser (`tests/fake-pve`), so its own `fw_check` runs. It checks
the generated `cluster.fw`, both segments, the CPU rule, the per-VM firewall
file, the preflight refusals and `handover`. The stand-in is not PVE's parser:
the script's own dry run on the hypervisor is what checks the rules with the
real one. The second fails when a flag, a subcommand, an environment variable
or a default in this README, the usage block and the script disagree.
