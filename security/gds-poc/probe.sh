#!/usr/bin/env bash
# probe.sh: read-only GDS ground-truth collector for the t7920 Proxmox host.
#
# Run locally on the host, or from anywhere with ssh:
#   ssh -i ~/.ssh/t7920 root@t7920 'bash -s' < probe.sh > results/ground-truth.txt
#
# It changes nothing: no MSR writes, no VM changes, no config reads beyond
# what is needed to describe the CPU side-channel state.

set -u

hr() { printf '\n=== %s ===\n' "$*"; }

hr "identity"
hostname; uname -r; date -u +"%Y-%m-%dT%H:%M:%SZ"

hr "cpu summary"
lscpu | grep -E "^CPU\(s\)|Model name|Socket|Core\(s\) per|Thread\(s\) per|^Stepping" || true
echo "microcode (dmesg): $(dmesg 2>/dev/null | grep -m1 'microcode: Current revision' || echo n/a)"

hr "kernel cmdline"
cat /proc/cmdline

hr "cpu vulnerabilities (sysfs)"
for f in /sys/devices/system/cpu/vulnerabilities/*; do
  printf '%-38s %s\n' "$(basename "$f"):" "$(cat "$f")"
done

hr "cpuinfo bugs line"
grep -m1 '^bugs' /proc/cpuinfo || true

hr "SMT state"
printf 'control: %s\nactive:  %s\nnproc:   %s\n' \
  "$(cat /sys/devices/system/cpu/smt/control 2>/dev/null)" \
  "$(cat /sys/devices/system/cpu/smt/active 2>/dev/null || echo n/a)" \
  "$(nproc)"
systemctl is-active smt-off.service 2>/dev/null | sed 's/^/smt-off.service: /' || true

hr "GDS kernel boot message"
dmesg 2>/dev/null | grep -E 'GDS|gather_data_sampling' || echo "(none in dmesg buffer)"

hr "MSR_IA32_MCU_OPT_CTRL (0x123): GDS mitigation control"
# bit4 = GDS_MITG_DIS (mitigation disabled), bit5 = GDS_MITG_LOCKED.
python3 - <<'PY' || echo "(msr read failed)"
import struct, glob
def rd(cpu, addr):
    with open(f"/dev/cpu/{cpu}/msr", "rb") as f:
        f.seek(addr)
        return struct.unpack("<Q", f.read(8))[0]
cpus = sorted(int(p.split("/")[-2]) for p in glob.glob("/dev/cpu/*/msr"))
sample = [cpus[0]] + ([cpus[len(cpus)//2]] if len(cpus) > 1 else [])
for c in sample:
    v = rd(c, 0x123)
    print(f"cpu{c}: {v:#06x}  GDS_MITG_DIS={(v>>4)&1}  GDS_MITG_LOCKED={(v>>5)&1}")
PY

hr "IA32_ARCH_CAPABILITIES (0xB10): read stability check"
# bit25 = ARCH_CAP_GDS_CTRL (microcode has the fix), bit26 = ARCH_CAP_GDS_NO.
python3 - <<'PY' || echo "(msr read failed)"
import struct
vals = []
for i in range(3):
    with open("/dev/cpu/0/msr", "rb") as f:
        f.seek(0xB10)
        vals.append(struct.unpack("<Q", f.read(8))[0])
print("reads:", [hex(v) for v in vals])
if len(set(vals)) == 1:
    v = vals[0]
    print(f"GDS_CTRL(bit25)={(v>>25)&1}  GDS_NO(bit26)={(v>>26)&1}")
else:
    print("UNSTABLE READS: userspace MSR reads of 0xB10 are unreliable on this host.")
    print("Authoritative source is the kernel boot-time check (see sysfs + dmesg above).")
PY

hr "kernel config: mitigation options"
grep -E 'CONFIG_MITIGATION_(GDS|RFDS|MDS|L1TF)=' /boot/config-$(uname -r) 2>/dev/null || echo "(no /boot/config)"

hr "VM inventory and CPU models"
qm list | sed 's/^/  /'
for id in $(qm list | awk 'NR>1{print $1}'); do
  c=$(qm config "$id" 2>/dev/null | grep -E '^cpu:' || echo 'cpu: <none>')
  n=$(qm config "$id" 2>/dev/null | grep -cE '^net[0-9]+:.*bridge=vmbr5')
  if [ "${n:-0}" -gt 0 ]; then seg="tenant-segment(vmbr5)"; else seg="other"; fi
  echo "  VMID $id: $c [$seg]"
done

hr "kernel GDS code path (reference, v7.0 upstream)"
echo 'bugs.c: gds_mitigation init = IS_ENABLED(CONFIG_MITIGATION_GDS) ? AUTO : OFF'
echo '        state OFF -> sysfs "Vulnerable" and update_gds_msr() WRITES GDS_MITG_DIS=1'
echo '        cmdline gather_data_sampling=force -> FULL when ARCH_CAP_GDS_CTRL is set'
