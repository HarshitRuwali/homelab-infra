# Proxmox guests

One script, `pve-guest.sh`, that builds a reusable VM template and creates
two kinds of guest from it:

- **Your own** VMs and LXC containers, the way the rest of the fleet expects
  them: unprivileged containers with `nesting=1`, cloud-init VMs with your SSH
  key and no password, DHCP, start on boot. Onboard them with Ansible after.
- **Leased VMs for other people** (`tenant`), which are built on the opposite
  assumption: whoever holds one is hostile. See [How leased VMs are
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
   `/etc/pve/firewall/cluster.fw` (who may manage the host, and the `tenant`
   group) and the node's `host.fw`, then checks both with PVE's own parser.
   The datacenter firewall stays **off**, so nothing changes yet. It refuses
   to merge into either file if one already exists.

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

1. **Set up OPNsense's TENANT interface** as described in
   [OPNsense](#opnsense) below.

2. **Test with a canary tenant.** Create one for yourself as in
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
next free VMID. Use `--bridge vmbr1` for the sandbox segment behind OPNsense.
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
       --ip 10.10.60.10 --cores 2 --memory 2048 --disk 20
   ```

   Rerun with `--apply`. `--user` is the login the friend will use, and
   `--ip` is a free address in `10.10.60.2` to `.254` (the script refuses one
   already in use). Note the VMID it prints.

2. **Log in through OPNsense**, the only host a tenant VM accepts SSH from:

   ```bash
   ssh -J root@10.10.0.114 alice@10.10.60.10
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
  of disk.
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

   Rerun with `--apply`. It gets the same firewall as any tenant: the
   internet, and nothing private. `--uncapped` drops only the CPU weight, the
   disk and the network limits, and tags it `uncapped`.

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
grep -l '^tags:.*tenant' /etc/pve/qemu-server/*.conf   # every leased VM
grep -A1 ipfilter-net0 /etc/pve/firewall/<vmid>.fw     # its IP
qm status <vmid>
```

- **More RAM or CPU:** `qm set <vmid> --memory 4096 --cores 4`, then
  `qm reboot <vmid>`.
- **More disk:** `qm disk resize <vmid> scsi0 +10G`. The guest grows its
  filesystem on the next boot.
- **A different network cap:** keep the MAC, or cloud-init treats the VM as
  new and regenerates its host keys:
  `qm set <vmid> --net0 virtio=<MAC>,bridge=vmbr2,firewall=1,rate=25`, with the
  MAC from `qm config <vmid>`.
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

| Risk | What stops it |
|---|---|
| Escaping to the hypervisor | VMs only, never containers: a container shares the host's kernel, so one kernel bug would give a tenant the host. No guest agent, no passthrough, a generic CPU type. |
| Joining your tailnet | Tailscale in the VM is logged in with **the tenant's** account, never yours. Your tailnet routes `10.10.0.0/24` and its ACLs allow everything, so one login with your account would hand them your network. |
| Reaching your network | `vmbr2`, the bridge only OPNsense is on, never `vmbr0` (which carries the hypervisor's own address). The `tenant` firewall group drops every private range in both directions, except the tenant gateway. |
| Reaching each other | Every tenant's inbound policy is DROP, the only exception being SSH from OPNsense (your setup path), and other tenants are private addresses. `ipfilter` pins each VM to its one IP, and `macfilter` to its MAC, so it can't pose as a neighbour. |
| Starving your services | Reserved RAM (no ballooning), half the default CPU weight, disk capped at 100 MB/s and 2000 IOPS each way, network at 12.5 MB/s (`TENANT_*` to change). `--uncapped` drops all but the reserved RAM, so it's for your own VMs only. |
| Abuse from your home IP | Outbound port 25 is blocked. Suricata on OPNsense sees their traffic. |
| Stealing fleet secrets | Nothing of the fleet's goes in: no fleet key, no Ansible, no Wazuh, no Alloy with the collector password. Your key is only there during setup, and `handover` removes it from the VM's config. |
| Being reached from the internet | Nothing is published. Tenants come in over their own tailnet, which the firewall only ever sees as outbound traffic, so there are no port forwards and your router stays closed. |
| A typo opening a hole | PVE silently skips a rule it can't parse. The script parses every rule with PVE's own parser before writing it, and again after, and refuses on any complaint. |

The script refuses to create a tenant while the tenant group is missing or the
datacenter firewall is off, so there is never a moment when a tenant VM is
unfiltered.

What you can't prevent from the outside: a tenant can make their VM an exit
node on their own tailnet, and then their browsing leaves from your home IP.
With friends, agree on it rather than try to block it.

### OPNsense

The `vmbr2` NIC is already attached to OPNsense (VM 102, `net2`). On that
interface:

- Assign it as **TENANT**, static IPv4 `10.10.60.1/24` (or change
  `TENANT_PREFIX` to match yours). No DHCP: tenants get static addresses from
  cloud-init.
- **Unbound** listening on TENANT, so tenants resolve through `10.10.60.1`.
- Rules on TENANT, in order, every one with source TENANT net:
  1. pass → TENANT address, TCP/UDP 53 (DNS)
  2. block → This Firewall (its web UI and SSH), logged
  3. block → `RFC1918`, logged. An alias of `10/8`, `172.16/12`,
     `192.168/16`, `100.64.0.0/10` and `169.254.0.0/16`
  4. block → any, TCP 25, logged
  5. pass → any

  Interface (rule) is TENANT, never `any`. Rule 1 must come before rule 2:
  This Firewall includes `10.10.60.1`.
- Suricata on the TENANT interface too, if it isn't already.
- **SSH on OPNsense** (System > Settings > Administration > Secure Shell),
  key-only, for the setup hop, listening only on the interface that faces
  the home LAN. Here that is OPNsense's **WAN** (`vtnet0`, `10.10.0.114`):
  its LAN is the `vmbr1` sandbox. Allow TCP 22 to it from `10.10.0.0/24`
  only. No port forwards: tenants
  arrive over Tailscale, which only needs outbound UDP and HTTPS. A tenant
  created with `--allow` needs a forward for those ports, and only those.

These rules repeat the Proxmox group on purpose, so a mistake in either
one alone doesn't open anything.

### Isolation checks

From inside the canary tenant:

```bash
curl -m5 -sk https://10.10.0.109:8006 && echo LEAK     # hypervisor UI
curl -m5 -sk https://10.10.0.129     && echo LEAK      # central ingest
ping -c2 -W2 10.10.0.1               && echo LEAK      # home router
ping -c2 -W2 10.10.50.1              && echo LEAK      # vmbr1 sandbox
nc -zw5 smtp.gmail.com 25            && echo LEAK      # outbound mail
sudo ip addr add 10.10.60.99/24 dev eth0; ping -c2 -W2 -I 10.10.60.99 1.1.1.1 && echo LEAK; sudo ip addr del 10.10.60.99/24 dev eth0
curl -m5 -sI https://deb.debian.org | head -1           # must answer
tailscale status | head -3                              # the tenant's tailnet, not yours
```

Nothing should print `LEAK`, the `curl` should return a status line, and
`tailscale status` should list only the tenant's own devices. From your
laptop, `ssh <login>@10.10.60.X` must fail: your LAN has no way in.

### Monitoring tenants

Not from inside. Alloy in a tenant VM would need the fleet's collector
password, which the tenant could read and use to forge metrics or logs for any
host, and a path from `vmbr2` to the ingest endpoint. Watch them from the
outside instead: the hypervisor sees each VM's CPU, memory, disk and network,
and OPNsense sees their traffic. Per-VM metrics from the hypervisor into
Grafana are not built yet; they are the next piece.

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
| `TENANT_BRIDGE` | `vmbr2` | the tenant segment |
| `TENANT_PREFIX` | `10.10.60` | the tenant /24; OPNsense is `.1` |
| `TENANT_NET_RATE` | `12.5` | MB/s per tenant NIC |
| `TENANT_DISK_MBPS` | `100` | MB/s each way per tenant disk |
| `TENANT_DISK_IOPS` | `2000` | IOPS each way per tenant disk |
| `TENANT_CPUUNITS` | `50` | CPU weight; your guests have 100 |
