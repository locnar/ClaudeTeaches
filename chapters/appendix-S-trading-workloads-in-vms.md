# Appendix S — Trading Workloads in Virtual Machines

> **Relates to:** Appendix A.7 (the tier decision — the order path stays bare-metal; this appendix is for the workloads that *do* run virtualised beside it), Ch. 59 §59.4.4 (containers — the other half of the "adjacent workload" question), Ch. 6 §6.2.3 (the VM-entry mitigations), Ch. 17 (huge pages — the two-dimensional page walk), Ch. 20 (timekeeping — `kvm-clock` vs the TSC), Ch. 54 (wake-ups — a guest's cost a VM exit), Ch. 88 (VFIO — the same binding passes a device *into* a guest), Ch. 76 (DDIO with a passed-through VF), Ch. 113 (host-health telemetry — steal time).

Most firms run something latency-adjacent in a VM: the risk service a strategy consults before the open, an order gateway that owns an SR-IOV virtual function, the monitoring stack, research workers, a venue simulator. None of it is the tick-to-trade path — Appendix A.7 settled that the order path is bare-metal colocated, and nothing here reverses it — but all of it has a latency budget, shares a host with other guests, and fails in ways the bare-metal chapters never see: a wake-up that costs 11 µs instead of 2, a clock that is a hypercall, a `cpuid` that costs six microseconds, memory whose first touch is a two-dimensional page walk. This appendix is the mechanics behind those numbers and the knobs that take a guest from "cloud-like" to "within 2× of the host", measured where the test guest allows and representative for the reference machine elsewhere (Appendix L.3).

*Measurements in this appendix are from the 4-vCPU KVM guest the book's other guest runs use (a Firecracker/KVM microVM on a 2.7 GHz Xeon host, nested paging, kernel 6.18, `clocksource=tsc`, no cpuidle driver, `constant_tsc nonstop_tsc`), g++ 13.3 `-O2`; clang++ 18.1 within noise.*

## S.1 Where a VM belongs, and the question it has to answer

*(A cloud instance is this appendix's VM operated by someone else; Ch. 86 §86.4.6 applies it to trading from a cloud host — placement, ENA/SR-IOV adapters, the cloud PTP clock, steal time — against the venues reached over TLS/WebSocket/REST.)*

The tier model of Ch. 97 §97.2 and Appendix A.7 puts the tick-to-trade processes on bare metal and everything else where it is cheapest to operate. A VM is a reasonable home for the **adjacent tier** — latency-tolerant to ~50–500 µs, syscall-making, shared-host — if, and only if, its tail is *bounded* and *understood*: a risk check that takes 20 µs p50 and 5 ms p99.9 because another guest's `memcpy` evicted its EPT entries is not adjacent, it is a Ch. 50 flow-control problem for the strategy that waits on it. So the question for each VM is the same as for each core in Ch. 61: **what enters it, how often, and what does each entry cost** — with the hypervisor as one more source of entries.

Three rules the rest of the appendix justifies: a guest that must be quiet gets **pinned vCPUs on isolated host cores, huge pages on both levels, a passed-through device and the TSC as its clock** (§S.4–§S.5); a guest that need not be quiet is left alone and *kept away from the hot host* (the host that runs the order path runs no guests at all — the VM-entry mitigations, the `vhost` threads and the EPT flushes are all noise on its cores); and **every number is re-measured inside the guest** — the host's `cyclictest` says nothing about the guest's.

## S.2 Where the exits are: EPT, MSRs, `cpuid`, `HLT` and `PAUSE`

A guest runs natively until it does something the hardware cannot virtualise in place; then it **exits** to the hypervisor (`VM-exit` → KVM handles it → `VM-entry`), 1–2 µs round-trip on bare metal plus the handler, with the L1TF/MDS-class flushes of Ch. 6 §6.2.3 on affected silicon added to every entry. The exits that matter for latency:

- **EPT violations — the memory half.** Guest-physical → host-physical translation goes through the **extended page tables** (EPT; AMD: NPT). A guest TLB miss is a **two-dimensional walk**: each of the four guest page-table levels is itself a guest-physical address that needs an EPT walk — up to 24 memory references instead of 4 (Ch. 17 §17.2). A guest page *first touch* is worse: a guest page fault *and* an EPT violation exit for the host to allocate the backing page — **8.6 µs per 4 KiB page** on the test guest for a user-space touch loop, against ~0.25 µs on bare metal (Ch. 34 §34.4.3's table; the in-kernel `MAP_POPULATE` path is 0.78 µs/page because it batches the exits). The fix is on both levels (§S.4): host huge pages backing the guest (one EPT level fewer, and the host never faults), guest huge pages (fewer guest walks), and pre-faulting (`MADV_POPULATE_WRITE`) so the exits happen before the readiness gate.
- **Instruction exits.** `cpuid` always exits (it is how the hypervisor presents a CPU model) — **6,033 ns** on the test guest for one `cpuid(0)`, against ~100 ns on bare metal — so a library that calls `cpuid` to pick a kernel per call (some `memcpy` dispatchers, `__builtin_cpu_supports` at first use only — fine) is a latency bug in a guest. `rdmsr`/`wrmsr` exit (every MSR not passed through: the `IA32_TSC_DEADLINE` timer write on a guest without the TSC-deadline passthrough, perf counters, `0x1A4` — Ch. 12 §12.4.3 cannot be done from inside). `rdtsc` does **not** exit with TSC offsetting/scaling (`-cpu host`): **12.2 ns** here, bare-metal cost. `rdpmc` exits unless `kvm.enable_pmu` and the vPMU is passed through — and the vPMU is a sampling-only facility; the Ch. 2 counters are host-side.
- **`HLT` and `PAUSE`-loop exiting — the scheduling half.** A guest vCPU that idles executes `HLT`, which exits so the host can run something else; waking it is a host-side IPI, a scheduler wake-up of the vCPU thread and a VM entry — the **11.4 µs** one-way eventfd wake-up of Ch. 88 §88.3, and the **60–75 µs** timer-wake floor Ch. 59 §59.2.1 measured, against 2–4 µs and ~5 µs on bare metal. `PAUSE`-loop exiting (PLE) is the hypervisor's spinlock-holder-preemption heuristic: a guest spinning on `pause` for longer than `ple_window` cycles is *descheduled* — which is exactly what a Ch. 43 spin-wait or a Ch. 45 SPSC consumer does. The knobs: `kvm.halt_poll_ns` (the host polls before descheduling an idle vCPU — 200 µs default, raise it to the inter-arrival time), the guest's `cpuidle-haltpoll` driver / `idle=poll` (the guest never executes `HLT`), and `kvm_intel.ple_gap=0` / `ple_window=0` (PLE off) for a guest whose vCPUs are 1:1 pinned.
- **Interrupts and I/O.** Every device interrupt for a guest is an exit unless **posted interrupts** (Intel APICv + VT-d posted interrupts; AMD AVIC) deliver it directly to the running vCPU — `kvm_intel.enable_apicv=1` (default on Ice Lake+) and interrupt remapping on. A `virtio-net` packet is a `vhost` kernel thread on the host, a softirq on the host (Ch. 64), a virtqueue notification (an exit, or an eventfd/irqfd with APICv) and the guest's own stack; an SR-IOV VF with VFIO (§S.5) is the device's own MSI-X, posted, into the guest driver — no host software in the path.

The accounting is visible from inside: `/proc/stat`'s eighth field is **steal time** (jiffies the vCPU was runnable but the host ran something else — 2,096 on the test guest's uptime, i.e. the host *does* deschedule it), `perf kvm stat` on the *host* names the exits by reason and count (`EPT_VIOLATION`, `HLT`, `MSR_WRITE`, `CPUID`, `PAUSE_INSTRUCTION`, `EXTERNAL_INTERRUPT`), and a guest `cyclictest`/Ch. 61 `osnoise` run shows the host's interruptions as unexplained gaps — which is the first check for any guest that claims a latency budget.

## S.3 Time in a guest: `kvm-clock` vs the invariant TSC

Ch. 20's instrument is the invariant TSC; in a guest it is one of two clocks:

- **`kvm-clock`** (the paravirtual clocksource): a per-vCPU page the host updates with (TSC offset, scale, system time), read by the guest vDSO as `tsc × scale + offset` — safe across migration and host TSC differences, and the default on most distros. Cost here: `clock_gettime(CLOCK_MONOTONIC)` **24.8 ns** via `kvm-clock` against **20.9 ns** via the `tsc` clocksource and 12.2 ns for a raw `rdtsc` — a small tax, but the real difference is **what the value means**: `kvm-clock` can be stepped by the host, is not the host's PHC, and will not take a PTP servo (Ch. 78).
- **The guest TSC as the clocksource** (`clocksource=tsc`, with `-cpu host,+invtsc` so the guest sees `constant_tsc nonstop_tsc` and `tsc=reliable` where the hypervisor's TSC is trustworthy): `rdtsc` is native (TSC offsetting in hardware, no exit), the guest's `tsc_khz` is the host's (pin it with `tsc-frequency=` so a migration cannot change it), and Ch. 20's calibration applies unchanged. This is the configuration for any guest that timestamps.
- **PTP in a guest.** The host's NIC PHC is not visible to a guest unless the NIC *function* is (an SR-IOV VF with PTP support — §S.5 — gives `/dev/ptpN` in the guest and `ptp4l` runs as on bare metal); otherwise **`ptp_kvm`** exposes the *host's* clock as `/dev/ptp_kvm` (a hypercall per read, ~1–2 µs, steered by `chrony` with `refclock PHC /dev/ptp_kvm` to ~10 µs offsets — fine for logs, not for Ch. 78's wire-to-wire budget), and `virtio-rtc` (6.x) is the coming replacement. A guest that needs sub-µs time needs the device, not a paravirtual clock.

## S.4 Making a guest quiet: pinning, huge pages, APICv, idle polling

The host-side recipe, in libvirt terms (QEMU flags in parentheses), for a guest that must hold a latency budget:

```xml
<domain type='kvm'>
  <vcpu placement='static'>4</vcpu>
  <cputune>                                                     <!-- 1:1 vCPU -> isolated host core (host: isolcpus=…,nohz_full=… for 8-11) -->
    <vcpupin vcpu='0' cpuset='8'/>  <vcpupin vcpu='1' cpuset='9'/>
    <vcpupin vcpu='2' cpuset='10'/> <vcpupin vcpu='3' cpuset='11'/>
    <emulatorpin cpuset='2-3'/>                                 <!-- QEMU's own threads (the exit handlers' slow paths, monitor, migration) off the vCPU cores -->
    <iothreadpin iothread='1' cpuset='2-3'/>                    <!-- and the I/O threads; vhost-net/vhost-user threads likewise (vhost_* in the host's `ps`) -->
    <vcpusched vcpus='0-3' scheduler='fifo' priority='50'/>     <!-- the vCPU threads SCHED_FIFO on the host (Ch. 59 §59.4.2's caveats apply on the host) -->
  </cputune>
  <memoryBacking><hugepages><page size='1' unit='GiB'/></hugepages><locked/><nosharepages/></memoryBacking>   <!-- 1 GiB host pages, mlocked, no KSM -->
  <numatune><memory mode='strict' nodeset='0'/></numatune>      <!-- the guest's memory on the node of its cores and its VF -->
  <cpu mode='host-passthrough' check='none'>                    <!-- -cpu host: the real model, invtsc, the real cache topology -->
    <feature policy='require' name='invtsc'/>
    <topology sockets='1' cores='4' threads='1'/>
    <cache mode='passthrough'/>
  </cpu>
  <clock offset='utc'>
    <timer name='tsc' present='yes' mode='native' frequency='2900000000'/>   <!-- pinned TSC frequency: no change on migration -->
    <timer name='kvmclock' present='no'/>                        <!-- the guest uses clocksource=tsc (§S.3) -->
    <timer name='hpet' present='no'/> <timer name='pit' tickpolicy='discard'/>
  </clock>
  <features><apic/><pmu state='off'/><kvm><hint-dedicated state='on'/></kvm></features>   <!-- hint-dedicated: the guest knows its vCPUs are 1:1 -->
  <devices>
    <hostdev mode='subsystem' type='pci' managed='yes'>          <!-- the SR-IOV VF, via vfio-pci (§S.5, Ch. 88 §88.4.1) -->
      <source><address domain='0' bus='0x3b' slot='0x1' function='0x2'/></source>
    </hostdev>
  </devices>
</domain>
```

And on the host and in the guest:

- **Host kernel:** `isolcpus=…,nohz_full=…,rcu_nocbs=…` for the vCPU cores (the vCPU thread is the one task the core runs — Ch. 59 §59.4.1's rule holds for it exactly); `intel_iommu=on iommu=pt` (VFIO needs the IOMMU; `pt` keeps the host's own devices on identity maps); `kvm_intel enable_apicv=1 ple_gap=0` (posted interrupts; no PAUSE-loop exiting for pinned guests); `kvm.halt_poll_ns=0` *if* the guest polls its own idle (next bullet) or raised to the inter-arrival time if it does not; `transparent_hugepage=never` and `ksm` off on the host (a KSM merge or a THP collapse of guest memory is an EPT flush — Ch. 34 §34.5's shootdown, one level up); the host's own housekeeping (`vhost-*`, `kvm-pit`, `kworker`s) confined to the housekeeping cores. The host is tuned per Appendix C *as if* each pinned vCPU were a hot thread.
- **Guest kernel:** `clocksource=tsc tsc=reliable` (§S.3); `idle=poll` or the `cpuidle-haltpoll` driver (`haltpoll` governor: the vCPU polls for `guest_halt_poll_ns` before executing `HLT`, so short idles never exit); `nohz_full`/`isolcpus` inside the guest for its own hot vCPU (the guest scheduler is a scheduler, Ch. 59 applies twice); guest huge pages (`hugepagesz=1G hugepages=N` in the guest for the segments — the two-dimensional walk shrinks on *both* axes); `MADV_POPULATE_WRITE` + `mlockall` in the process (Ch. 34) so every EPT violation happens before the gate; `mitigations=` evaluated with the *guest* `vulnerabilities/*` (the test guest reports `BHI: Vulnerable` and `Not affected` for almost everything else — the host owns the rest).
- **What a tuned guest buys — representative, reference-machine host (Appendix L.3), `cyclictest -m -p 95 -i 200 -h 400 -D 10m`:**

```
   configuration                                                  avg       max        kernel-stack UDP RX one-way (Ch. 64's udp_rtt shape)
   bare-metal isolated core (Ch. 59 §59.3)                        1.0 µs     ~2 µs     ~6 µs
   guest, pinned vCPU on an isolated core, huge pages, APICv,
     idle=poll, VF passthrough, TSC clock                          1.8 µs     ~9 µs     ~8 µs   (the VF's MSI-X is posted; no host in the path)
   guest, pinned vCPU, defaults otherwise (kvm-clock, HLT, THP)   4 µs      ~60 µs    ~25 µs  (virtio-net: vhost thread + host softirq + notification exit)
   guest, unpinned, shared host                                   15 µs   ~2,000 µs    ~80 µs  (steal; another guest's EPT flushes; the host scheduler)
```

  The ordering is the lesson: pinning removes the milliseconds, the device and the idle policy remove the tens of microseconds, and the floor stays ~2× bare metal (the exits that remain — a timer, an EPT entry on a new page, the odd MSR). A guest that needs *better* than that row needs the host.

## S.5 SR-IOV, VFIO passthrough and what the guest's driver sees

A **virtual function** is a lightweight PCIe function the NIC carves out of its physical function (PF): its own BARs, MSI-X vectors and queues, its own RSS/flow-steering slice (Ch. 77), sharing the PF's link, DDIO window (Ch. 76) and — on most NICs — its PTP hardware clock. Passing it to a guest is Ch. 88's binding applied by the host: `driverctl set-override <VF-BDF> vfio-pci`, the `<hostdev>` above, and the guest boots with the VF as a native PCIe device driven by the vendor's VF driver (`iavf`, `mlx5_core`), or by DPDK/`ef_vi` inside the guest for a bypass path. What is preserved and what is not:

- **Preserved:** zero host software in the data path; the device's own MSI-X, delivered by posted interrupt straight to the pinned vCPU (interrupt remapping + APICv — without them every VF interrupt is an exit); DMA by the device through the IOMMU into guest memory (the IOMMU holds the guest-physical → host-physical map, so the guest's DMA addresses are *guest-physical* — the guest driver's "physical" addresses are correct by construction and the host's IOMMU enforces the boundary); DDIO into the host L3 slice the vCPU's core sits on, if the VF, the vCPU's core and the memory are on one node (`numatune` + the VF's `numa_node` — Ch. 18 §18.4.1).
- **Not preserved:** the PF's control plane (link settings, `ethtool -K` offloads, flow-steering rules on the PF, the VF's MAC/VLAN/rate — set from the *host* with `ip link set <pf> vf N …`); PTP on VFs that lack a PHC (most Intel VFs do not; Mellanox VFs do on recent firmware — check `ethtool -T` in the guest); hugepage-backed DMA buffers still matter *in the guest* for the IOTLB (Ch. 88 §88.4.4 applies verbatim); and a VF reset on the host (`FLR` from a PF driver reload) takes the guest's link down — the Ch. 88 §88.5 reset hazard, now triggered from another kernel.
- **Against `virtio-net`/`vhost`:** the paravirtual path is a `vhost` kernel thread on the host (pin it; it is a softirq consumer — Ch. 64 §64.2.2 on the host), a virtqueue notification (an exit unless `ioeventfd`/`irqfd` with APICv), and the host's bridge/OVS — ~25 µs one-way in the representative table against ~8 µs for the VF. `vhost-user`/DPDK on the host with a polling datapath narrows it; passthrough removes it. For a gateway VM that talks to a venue, the VF is the only configuration worth measuring.

## S.6 Checklist — a VM with a latency budget

- [ ] It is an **adjacent-tier** workload (Appendix A.7, Ch. 97 §97.2); the tick-to-trade processes are bare-metal and the hot host runs **no guests**.
- [ ] vCPUs are **1:1 pinned to isolated host cores**, emulator/iothreads/`vhost` threads pinned to host housekeeping cores, vCPU threads `SCHED_FIFO` (§S.4); the host's `isolcpus`/`nohz_full` cover the vCPU cores.
- [ ] Memory is **1 GiB-backed on the host, locked, no KSM/THP**, NUMA-strict to the VF's node; guest huge pages + `MADV_POPULATE_WRITE` + `mlockall` so no EPT violation survives into the session (§S.2, §S.4).
- [ ] **APICv/posted interrupts on, PLE off**, `halt_poll`/`idle=poll` chosen; `perf kvm stat` on the host shows no `HLT`/`PAUSE`/`EPT_VIOLATION` exits during a steady-state run (§S.2).
- [ ] The guest clock is the **TSC** (`-cpu host,+invtsc`, pinned `tsc-frequency`, `clocksource=tsc`); `kvm-clock` is absent; time that matters comes from a **VF's PHC** or is not promised (§S.3).
- [ ] Network is an **SR-IOV VF via VFIO** (interrupt remapping on, VF on the vCPU's node), not `virtio-net`, for anything that talks to a venue or a feed (§S.5).
- [ ] The guest's own `cyclictest`/`osnoise`, steal time and the **in-guest** application tail are measured and fingerprinted (Appendix M); a VM migration, a host kernel update or a new co-tenant re-runs them.

## S.7 References

- Intel, *64 and IA-32 Architectures Software Developer's Manual*, vol. 3C — VMX: VM exits and their reasons, EPT, APICv and posted interrupts, TSC offsetting/scaling (§S.2–§S.3); AMD, *APM vol. 2* — SVM, NPT, AVIC.
- Linux kernel documentation: `Documentation/virt/kvm/` (the API, `halt_poll_ns`, `cpuidle-haltpoll`, `ptp_kvm`), `Documentation/admin-guide/pm/cpuidle.rst` (the `haltpoll` governor), `Documentation/driver-api/vfio.rst` (passthrough — Ch. 88), `tools/perf/Documentation/perf-kvm.txt` (`perf kvm stat`).
- libvirt, *Domain XML format* (`cputune`, `memoryBacking`, `numatune`, `clock`, `hostdev`) and QEMU's `-cpu host` / `invtsc` / `tsc-frequency` documentation — the §S.4 recipe's vocabulary.
- Red Hat, *Virtualization Tuning and Optimization Guide* and *Real-Time KVM* (RT-KVM) documentation; the OpenStack/OVS-DPDK NFV tuning guides — the vendor-blessed versions of §S.4–§S.5, written for packet-processing guests.
- PCI-SIG, *Single Root I/O Virtualization and Sharing Specification*; Intel, *VT-d* specification (interrupt remapping, posted interrupts) — the §S.5 mechanisms.
- Firecracker and `kvm-clock` design notes; the `ptp_kvm` and `virtio-rtc` patch series — guest time (§S.3).

## S.8 Additional Reading

- The KVM Forum talks on real-time KVM (Rik van Riel, Marcelo Tosatti, Luiz Capitulino) and the `tuned` `realtime-virtual-host`/`realtime-virtual-guest` profiles — the configuration this appendix condenses.
- Ch. 59 §59.4.4 (*containers*) — the other adjacent-workload container; Ch. 88 (*userspace drivers*) — VFIO; Ch. 20 (*timekeeping*) — the TSC the guest inherits; Ch. 6 §6.2.3 — the VM-entry mitigations; Ch. 61 — attributing what still enters a (virtual) core.
- **Appendix A.7** — the cloud-vs-bare-metal tier decision this appendix assumes; **Appendix C** — the host is tuned by it; **Appendix M** — the gate a latency-budgeted guest must pass like any host.

---

*Next: the detailed, searchable topic index — every chapter and appendix expanded into its sub-topics — is [`README.md`](README.md).*
