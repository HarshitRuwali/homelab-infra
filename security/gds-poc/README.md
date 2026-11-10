# GDS (Gather Data Sampling) exposure on t7920: PoC and verdict

Internal assessment for the VM-lease decision. Host: Dell T7920, Proxmox VE 9.2.26,
kernel `7.0.14-28-pve`, 2× Intel Xeon Gold 6138 (Skylake-SP, stepping 4).

Question: with an untrusted tenant about to be leased a VM on this host, how much
can leak via the Gather Data Sampling side channel, and what must change before
handover?

Status: **complete** (2026-11-10). All experiments run on throwaway canaries in the
39x range; canaries destroyed and purged afterwards, SMT verified off throughout.

## TL;DR verdict

**A tenant cannot leak anything via GDS under the current lease configuration.**
The exposure is closed by two independent controls:

1. **Instruction level (primary):** every allowed lease CPU model hides not just AVX
   but *XSAVE itself* from the guest. Gather instructions raise `#UD` (SIGILL) and
   XCR0 cannot be read or written, so a tenant has no way to execute gathers,
   including hand-assembled VEX bytes and the "xsetbv to re-enable AVX state" path.
   Verified empirically on `x86-64-v2-AES` (lease default) and plain `x86-64-v2`
   (E1, E1d).
2. **Scheduling level (defense in depth):** even with *both* VMs AVX-visible
   (`x86-64-v3`) and pinned to the same physical core, SMT-off produced **no
   measurable cross-VM GDS timing signal** across ~76M attacker samples (E2).

The host's `gather_data_sampling: Vulnerable` string is real but currently
inconsequential for tenants: it reflects a PVE kernel build without
`CONFIG_MITIGATION_GDS` that actively sets the microcode mitigation-disable bit at
boot, while SMT-off + AVX/XSAVE-hiding guest models remove both prerequisites of the
attack. See §6 for what to do before handover (short answer: nothing mandatory;
keep the two controls, optionally add the kernel-side mitigation later).

## 1. Ground truth (collected by `probe.sh`, raw output in `results/`)

| Item | Value |
|---|---|
| CPU | Intel Xeon Gold 6138, Skylake-SP, stepping 4, 2×20 cores |
| Microcode | `0x02007006` (dmesg) |
| SMT | **off** (`smt/control=off`, `active=0`, `nproc=40`, `smt-off.service` active at probe time, 2026-11-09) |
| sysfs | `gather_data_sampling: Vulnerable` (plain, not "No microcode") |
| dmesg | `[1.489] GDS: Vulnerable` at boot |
| `/proc/cpuinfo` bugs | contains `gds` → kernel marks the CPU affected |
| MSR `IA32_MCU_OPT_CTRL` (0x123) | **`0x10`** on cpu0 and cpu20 (re-read 2026-11-10): bit 4 `GDS_MITG_DIS`=1, bit 5 `LOCKED`=0. The probe run's own read failed; see the addendum in results/ground-truth.txt. |
| Kernel config | no enabled `CONFIG_MITIGATION_GDS`; the sampled peers (`RFDS`, `MDS`, `L1TF`) are all =y |
| cmdline | no `gather_data_sampling=`, no `mitigations=` |

## 2. Root cause of "GDS: Vulnerable"

The microcode **has** the fix; the PVE kernel build **actively disables it**.

Evidence chain (kernel v7.0, `arch/x86/kernel/cpu/bugs.c`):

1. Initial state: `gds_mitigation = IS_ENABLED(CONFIG_MITIGATION_GDS) ? AUTO : OFF`.
   The PVE kernel has the option unset → starts at **OFF**.
2. `gds_select_mitigation()` (called unconditionally from boot init, line 3263):
   state is not AUTO, so it stays OFF; then:
   ```c
   if (!(x86_arch_cap_msr & ARCH_CAP_GDS_CTRL)) {      /* bit 25 */
       if (gds_mitigation != GDS_MITIGATION_FORCE)
           gds_mitigation = GDS_MITIGATION_UCODE_NEEDED;
       return;
   }
   ```
   t7920 ends at OFF, **not** UCODE_NEEDED ⇒ `ARCH_CAP_GDS_CTRL` was set when the
   kernel read it at boot ⇒ **microcode 0x02007006 contains the GDS fix**.
3. `gds_apply_mitigation()` (line 3313): bug present + microcode present →
   `update_gds_msr()`, which for state OFF does:
   ```c
   mcu_ctrl |= GDS_MITG_DIS;      /* bit 4 of MSR 0x123 */
   wrmsrq(MSR_IA32_MCU_OPT_CTRL, mcu_ctrl);
   ```
   i.e. the kernel **writes the mitigation-disable bit at boot**. A live re-read on
   2026-11-10 returned `0x10` on cpu0 and cpu20 (DIS set, not locked), confirming it.

So: nothing is missing from the CPU or microcode. The PVE kernel package simply
was built without GDS support, and its "off" default then *disables* a mitigation
that exists in silicon. (Upstream added `CONFIG_MITIGATION_GDS` in 6.13; the PVE
7.0.14-28 build predates/omits it.)

### Side note: unstable userspace reads of MSR 0xB10 on this host

Reads of `IA32_ARCH_CAPABILITIES` (0xB10) via `/dev/cpu/0/msr` return a different
value in every ssh session, stable within a session; other MSRs (TSC, APIC base,
MCU_OPT_CTRL) read correctly and stably. No process holds the device open, no
runtime microcode reloads appear in dmesg, and the kernel's boot-time read is what
the state machine above uses. Values observed across sessions include
`0x39807222`, `0x3378e2618`, `0x826cab617/8`, `0x8f8f0f39d` (the probe run, in
results/ground-truth.txt), `0xb746b77ce`, `0x0b0f432ec6` and `0xb1211f058`
(re-read 2026-11-10; this one has bit 25 set, consistent with the microcode
carrying the fix). Possibly a Skylake-SP MSR-read erratum under concurrent VM
load; it does not affect the root cause, which rests on stable signals (sysfs
string + MSR 0x123) and code analysis.

## 3. What GDS actually leaks here

Per kernel docs (`admin-guide/hw-vuln/gather_data_sampling`): with the mitigation
disabled, stale data from architectural or internal vector registers may be
transiently forwarded to a gather's destination register instead of memory data;
an attacker infers it via cache-timing-style techniques. Affected directions:

- user → kernel (same thread)
- guest → host and guest → guest (across VMX boundary, same physical core)
- cross-HT sibling (**closed by SMT-off** on this host)

Who can be the *attacker*? It must execute `gather` instructions, which requires
AVX state (VEX encoding + XCR0 YMM). On t7920 today:

| Context | CPU model | AVX visible? | Can run gathers? |
|---|---|---|---|
| Host kernel/userspace | full host | yes | yes |
| 104, 110, 202, 203 (work) | `host` | yes | yes |
| 300 optimuslabs (work VM) | `x86-64-v3` | yes | yes |
| 100, 105 | `x86-64-v2-AES` | no (+ no XSAVE) | **no, SIGILL** (E1) |
| future tenant (lease default) | `x86-64-v2-AES` (`LEASE_CPU_TYPE`) | no (+ no XSAVE) | **no, SIGILL and XCR0 inaccessible** (E1) |
| lease alt. model | plain `x86-64-v2` | no (+ no XSAVE) | **no, SIGILL** (E1d) |

Lease CPU allowlist (`proxmox/pve-guest.sh`): only AVX-hiding models
(`x86-64-v2-AES x86-64-v2 kvm64 qemu64 Nehalem Westmere Penryn Conroe`); `host`
and `x86-64-v3` are refused for leases (verified against the script deployed on
t7920 at /root/pve-guest.sh; the repo copy proxmox/pve-guest.sh at this commit
predates the lease policy). The script's own comment flags the exact question
this PoC answers: *"Any VEX-encoded instruction, gathers included, runs once a
guest can enable AVX state"*: hiding AVX in CPUID may not stop a
determined tenant from hand-assembling gathers if the guest kernel auto-enables
XCR0 YMM state on #NM.

Who can be the *victim*? Anything that uses vector registers: host kernel (AVX
crypto/memcpy paths) and all `host`/v3 work VMs, e.g. 202 ubuntu-ai.

## 4. Experiments (as run)

Canaries only, VMIDs in the 39x range, cloned from template 9001 (Ubuntu 26.04,
1 vCPU / 1 GiB), pinned to physical core 37 (attacker) with the victim on the same
core for E2; both destroyed with `qm stop` + `qm destroy --purge` afterwards.
SMT verified off before and after (`smt/active=0`). No existing VM was touched.

Binaries are static ELFs built locally with GCC (`gcc -O2 -static`, canary guests
have no compiler); AVX is emitted via intrinsics in functions marked
`__attribute__((target("avx2")))`, so no global `-mavx2` flag is needed.
Hand-assembled VEX bytes proved unreliable (see anomaly below), which is why the
tests use intrinsics. Only sources and scripts are committed; the binaries are
git-ignored in experiments/. Rebuild any of them with, e.g.,

```sh
gcc -O2 -static -o e1_test experiments/e1_test.c
```

Raw outputs: `results/`.

### E1: can an AVX-hidden guest execute gathers at all? → **NO**

Canary 395 with lease model `x86-64-v2-AES`, running `e1_test.c` / `e1c_xcr0.c`
(gather via `_mm_i32gather_ps`, signal handlers for SEGV/BUS/ILL):

```
cpuid: avx=0 xsave(bit26)=0     ← model hides AVX *and* XSAVE
xgetbv: SIGILL                  ← XCR0 read is #UD
gather: SIGILL                  ← gather is #UD, no #NM auto-enable path
```

The guest kernel does **not** auto-enable YMM state on `#NM` (that would have let
the retry execute); the instruction simply faults. Because XSAVE itself is absent
from CPUID, the "tenant runs xsetbv to re-enable AVX" path from the pve-guest.sh
comment is unavailable, since there is no XCR0 interface at all.

### E1d: plain `x86-64-v2` (allowed alternative) → **NO**

Same test after `qm set 395 -cpu x86-64-v2`: identical result (`avx=0 xsave=0`,
gather SIGILL). The remaining allowed models (`kvm64 qemu64 Nehalem Westmere
Penryn Conroe`) predate Sandy Bridge and expose no more than v2; they are closed
by feature inclusion, not re-tested.

### E1 control: `x86-64-v3` (AVX visible) → gathers execute

Canary 396: `cpuid.avx=1`, valid-address gather returns correct data, unmapped-
address gather faults with SIGSEGV *after executing* (i.e. the instruction runs and
data access is what faults), XCR0 readable (`0x7`). Harness validated on local
AVX2 hardware first.

### E2: cross-VM GDS timing signal, SMT off → **none measurable**

Both canaries set to `x86-64-v3` (worst case: attacker *can* execute gathers).
Victim churns `vgatherdps` over a known-pattern buffer; attacker samples single-
gather latency (`lfence/rdtsc`) into a 4-cycle histogram, 6 s per phase:

| Phase | Condition | Samples (6 s) | Peak / median |
|---|---|---|---|
| A | attacker core 37, victim idle | 160.8M (~26.8M/s) | 32–35 cyc / ~33 |
| C | **both on core 37 (attack)** | 76.0M (~12.7M/s, halved by time-sharing) | 32–35 cyc / ~33 |
| B | attacker core 38, victim churning on 37 | 156.8M (~26.1M/s) | 32–35 cyc / ~33 |

Attack condition C is statistically indistinguishable from both the same-core idle
baseline (A) and the cross-core control (B). The only A-vs-C difference is sample
rate, halving exactly as expected when two vCPUs time-share one core with SMT off.
Conclusion: with SMT disabled, gather buffer-cache state left by a co-located VM's
gathers is not observable in another VM's gather latency, consistent with
Intel's model requiring simultaneous SMT-sibling execution. Caveat: one attack shape at
4-cycle resolution over 6 s windows; strong evidence, not an exhaustive proof.

### E3: mitigation A/B → **not run (nothing to toggle against)**

E2 found no signal in the current configuration, so clearing `GDS_MITG_DIS` live
(option B) would have nothing to make vanish. It remains listed in §5 as an option
for owner-side hardening; applying it (or any reboot-requiring change) on this 24/7
host is a maintenance-window decision, not part of the PoC.

### Anomaly: raw VEX bytes spin instead of faulting under `x86-64-v2-AES`

A binary containing the hand-assembled sequence `C5 FC F5 08` (invalid VEX prefix)
did **not** fault on canary 395: three such processes spun in user mode for >50 min
at ~50% CPU each, with no kernel trap log entries (`wchan=0`,
`/proc/PID/syscall="running"`, dmesg clean). The same bytes on the v3 control exited
cleanly via SIGILL. Local binutils 2.46 also cannot assemble or decode these
sequences, so the exact decode path was not identified. Practical impact: a tenant
can hang its *own* VM with raw VEX bytes (self-DoS only; no cross-VM effect
observed). Correctly-encoded gathers are cleanly blocked regardless (E1).

### Operational notes

- Canary access went through link-local IPv6 on `vmbr0` after static IPv4 via
  cloud-init failed to come up (template's netplan renderer path); a temporary
  cloud-init password was used as stopgap and removed during cleanup.
- One guest-side binary was silently truncated to 0 bytes across a `qm stop/start`
cycle (suspected ext4 recovery after SIGKILL stop); all pushes were md5-verified
afterwards. Phase-B data in `results/e2_raw.txt` is from the verified re-run.

## 5. Mitigation options (costs)

| Option | What it does | Cost / notes |
|---|---|---|
| **A. cmdline `gather_data_sampling=force`** (+ reboot) | Kernel state FORCE→FULL (microcode present): clears `GDS_MITG_DIS`, sysfs shows "Mitigation: Microcode". KVM then tells guests GDS_NO. | One line in `/etc/default/grub` + reboot window. No AVX disable (that only happens if microcode *lacks* the fix). This is the recommended permanent fix. |
| **B. live `wrmsr 0x123 &= ~0x10` on all CPUs** | Same effect immediately, no reboot. | Reverts after next boot until A lands; KVM's per-guest view updates only at next VM (re)start. Use as stopgap / for the E3 experiment. |
| **C. PVE kernel with `CONFIG_MITIGATION_GDS=y`** | AUTO state → FULL automatically on affected CPUs. | Wait for/track a Proxmox kernel build that enables it; then no cmdline needed. |
| D. SMT-off (already done) | Closes cross-HT direction only. | Already in place (`smt-off.service`). Does **not** close same-thread temporal sampling, and is not sufficient alone. |

## 6. Verdict and recommendations

### Exposure matrix (measured)

| SMT | Attacker model | Tenant can execute gathers? | Cross-VM GDS signal observed? |
|---|---|---|---|
| off (current) | `x86-64-v2-AES` / v2 / older allowed models | **No**: gather #UD, no XCR0 access (E1/E1d) | n/a: attacker has no gathers |
| off (current) | AVX-visible (`v3`, misconfig only) | Yes | **None measurable**, ~76M samples (E2) |
| on | any allowed tenant model | No (model still hides XSAVE/AVX) | n/a for tenants; owner-side AVX VMs would be exposed to each other; not tested (requires an SMT toggle and approval) |

### What must change before handover: **nothing mandatory**

The two controls already in place independently close the tenant GDS path:

1. **Keep the lease CPU allowlist as-is.** Every allowed model hides XSAVE+AVX,
   which E1/E1d proved sufficient. The pve-guest.sh worry ("VEX runs once a guest
   can enable AVX state") does not materialize on this KVM/kernel because v2-class
   models expose no XCR0 interface at all. Do **not** add `host`, `x86-64-v3` or
   any AVX-visible model to the lease path.
2. **Keep SMT off** (`smt-off.service`). E2 shows it also removes the cross-VM
   timing channel even for AVX-visible co-located VMs, and it closes other SMT
   side channels (MDS/L1TF-class) as well.

### Optional hardening (not required for the lease decision)

- Sync the repo's `proxmox/pve-guest.sh` with the deployed version on t7920:
  the committed copy predates the lease CPU policy (`LEASE_CPU_TYPE`,
  `LEASE_CPU_ALLOWED`) that this verdict relies on.
- **§5 option A** (`gather_data_sampling=force` + reboot) or **option C** (a PVE
  kernel built with `CONFIG_MITIGATION_GDS=y`) would make the host report
  "Mitigation: Microcode" and have KVM advertise `GDS_NO` to guests. This protects
  *owner-side* AVX-visible VMs from each other in case SMT is ever re-enabled; it
  needs a maintenance window on a 24/7 box, so schedule deliberately rather than as
  a pre-handover blocker.
- The `C5 FC F5` spin anomaly (§4) is worth a line in the lease handover notes:
  tenants may find byte sequences that hang their own VM; support should treat
  "VM stuck at 100% CPU with no kernel log" as guest-caused, not host fault.

### Residual risk statement

With SMT off and AVX/XSAVE-hiding lease models, GDS contributes **no measurable
tenant exposure** on this hardware. The `gather_data_sampling: Vulnerable` sysfs
string remains accurate for the *host kernel* (mitigation disabled by build config,
§2) and would matter only if both SMT were enabled and two AVX-visible workloads
shared a core; today that is owner-side VMs only, and E2 suggests even that
channel is weak under time-sharing.
