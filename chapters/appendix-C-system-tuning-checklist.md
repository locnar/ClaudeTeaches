# Appendix C — System Tuning Checklist

> **Consolidates:** Ch. 6 (system setup), Ch. 15 (TLB/huge pages), Ch. 41 (context switching), Ch. 42 (thread/IRQ pinning), Ch. 43 (SMT), Ch. 45 (RT scheduling / kernel tuning), Ch. 46 (warming), Ch. 51 (socket/TCP tuning), Ch. 55 (NIC features). This is the *operational* companion to those chapters: an ordered, copy-paste-oriented checklist for turning a stock Linux x86-64 box into a quiet, deterministic, low-jitter trading machine. Each item links to the chapter that explains **why**.

**How to use this:** work top-down — BIOS first (C.1), then kernel cmdline (C.2, requires reboot), then runtime knobs (C.3), then per-process (C.4), then *verify* (C.5). Lower layers gate higher ones: there's no point tuning `sysctl` if C-states still fire in firmware. **Measure before and after every change** (Ch. 3) — this checklist is a starting point, not a guarantee; the verification pass (C.5) is what proves the box is actually quiet. Values shown are sensible defaults for a single-socket trading box; adapt core numbers to your topology (`lscpu`, `lstopo`). Treat this as a template to version-control alongside the application, not a one-time ritual (Ch. 76 — tuning is defended continuously).

---

## C.1 BIOS/firmware (C-states, P-states/turbo, SMT, NUMA/snoop mode)

Firmware settings the OS can't override — set these first, in the BIOS/UEFI/BMC. **Why:** these are the deepest sources of jitter and frequency variation (Ch. 6); an un-tuned BIOS undoes everything above it.

- [ ] **Disable deep C-states** (allow only C0/C1; disable C1E, C3, C6+). A core waking from a deep sleep state takes microseconds — a cold-start stall on the first packet (Ch. 6, 46). Keep cores busy and shallow. *(Pair with kernel `intel_idle.max_cstate=1 processor.max_cstate=1` in C.2.)*
- [ ] **Fix P-states / disable frequency scaling and turbo variability** (Ch. 6). Either lock to a fixed high frequency, or disable Turbo Boost so the clock doesn't ramp/throttle unpredictably (turbo *raises* throughput but *adds* jitter as it engages/disengages and thermally throttles). For determinism, a *fixed* frequency beats a higher-but-variable one. Disable SpeedStep/Cool'n'Quiet.
- [ ] **Disable SMT / Hyper-Threading** (Ch. 43) — or plan to leave siblings idle (C.4). On hot cores, a sibling competing for front-end/execution/L1 is a jitter source; HFT hot cores usually run with HT off or the sibling empty (Ch. 43). Decide per the chapter; disabling in BIOS is the simplest.
- [ ] **NUMA / snoop mode** (Ch. 16): enable NUMA (don't interleave/flatten in firmware — you want node locality visible to the OS), and set the snoop mode for lowest local-access latency on your platform (e.g. Intel's "Home Snoop" vs cluster-on-die / sub-NUMA-clustering — measure; SNC can lower local latency but complicates topology). Disable node interleaving.
- [ ] **Disable unnecessary devices and their interrupts** (onboard audio, unused NICs/USB controllers, etc.) — fewer interrupt sources, fewer jitter sources (Ch. 42).
- [ ] **Disable power-management / "green" features**, set the firmware power profile to "maximum performance" / "low latency," disable processor power-capping (RAPL limits that throttle).
- [ ] **Disable SMIs where possible** (System Management Interrupts — firmware can steal the core invisibly for ms; the worst hidden jitter). Disable thermal/legacy-USB SMIs if the vendor allows; some BMCs expose an "SMI-free" or "low-latency" profile.

## C.2 Kernel cmdline (`isolcpus`, `nohz_full`, `rcu_nocbs`, `intel_pstate`, `mitigations=`, huge pages)

Boot parameters (edit GRUB `GRUB_CMDLINE_LINUX`, `update-grub`/`grub2-mkconfig`, reboot). **Why:** these carve out cores the kernel won't disturb and set kernel-wide policy (Ch. 45, 15, 6). Assume hot cores are `2-7` and housekeeping is `0-1` (adapt to topology).

- [ ] **`isolcpus=2-7`** (Ch. 45) — remove these cores from the kernel scheduler's general balancing; only threads explicitly pinned there run. The foundation of a quiet core.
- [ ] **`nohz_full=2-7`** (Ch. 45) — tickless operation on the isolated cores: stop the periodic scheduler tick (the ~1000 Hz timer interrupt) when one task runs, eliminating that recurring jitter (Ch. 41, 45). Needs `CONFIG_NO_HZ_FULL`.
- [ ] **`rcu_nocbs=2-7`** (Ch. 45, 36) — offload RCU callback processing off the isolated cores onto the housekeeping cores, so RCU grace-period work (Ch. 36) doesn't fire on the hot core. Pair with `rcu_nocb_poll` if appropriate.
- [ ] **`irqaffinity=0-1`** — default IRQ affinity to the housekeeping cores so interrupts don't land on hot cores (Ch. 42; reinforce at runtime in C.3).
- [ ] **`intel_pstate=disable`** (or `=passive`) plus **`cpufreq` governor** management (C.3) — take fine frequency control, consistent with the BIOS P-state decision (Ch. 6). On AMD, the analog is `amd_pstate`. *(If you locked frequency in BIOS, ensure the kernel doesn't re-introduce scaling.)*
- [ ] **`processor.max_cstate=1 intel_idle.max_cstate=1`** (Ch. 6) — kernel-side enforcement of the shallow-C-state decision from C.1 (belt and suspenders).
- [ ] **`mitigations=...`** (Ch. 6, 72) — the speculative-execution mitigation knob. On a **single-tenant, network-restricted, code-controlled** box (Ch. 42, 43, 45), the spec-exec mitigations (Spectre/Meltdown/retpoline — Ch. 6) can be a measured latency cost you choose to reduce (`mitigations=off`, or selectively). **This is a deliberate risk decision** (Ch. 72.4.4) given your threat model — not a default; document it, measure it (C.5), and only on a box whose attack surface justifies it.
- [ ] **Huge pages** (Ch. 15): reserve explicit huge pages at boot — **`default_hugepagesz=1G hugepagesz=1G hugepages=N`** (1 GiB pages for the largest mappings) and/or `hugepagesz=2M hugepages=M`. Boot-time reservation avoids fragmentation failures later (Ch. 15, 26). Decide explicit-vs-transparent (THP) per workload (THP knob in C.3).
- [ ] **`nosoftlockup nmi_watchdog=0`** — disable watchdog timers that periodically interrupt cores (Ch. 45 jitter).
- [ ] **`skew_tick=1`** — de-synchronize remaining ticks across cores so they don't all fire together (jitter smoothing).
- [ ] **`audit=0 selinux=0`** (if your security policy permits) — remove audit-subsystem overhead from the hot path; weigh against Ch. 72 requirements.

## C.3 Runtime knobs (governor, IRQ affinity/`irqbalance`, `tuned`, RPS/XPS, NIC ring/coalescing, `sysctl`, THP, `mlockall`)

Applied after boot (scriptable; put in a systemd unit / startup script, version-controlled). **Why:** the runtime-adjustable layer of Ch. 42, 51, 55, 15.

- [ ] **CPU governor → `performance`** (Ch. 6): `cpupower frequency-set -g performance` (every core), so cores don't downclock when "idle." Consistent with C.1/C.2 frequency policy.
- [ ] **Stop `irqbalance`** (Ch. 42): `systemctl stop irqbalance && systemctl disable irqbalance` — it would re-spread IRQs onto your hot cores. Then **pin IRQs to housekeeping cores manually**: for each device IRQ, write the housekeeping mask to `/proc/irq/<N>/smp_affinity`. Pin the **NIC's** IRQs deliberately (Ch. 42, 55) — RX queues to the cores that will poll/process them (or onto housekeeping if busy-polling on the hot core, Ch. 47/55).
- [ ] **`tuned` profile** (optional): apply `tuned-adm profile latency-performance` (or `network-latency`/`realtime`) as a baseline, then layer manual tweaks. Convenient, but understand what it sets — don't let it fight your explicit choices.
- [ ] **NIC ring buffers and coalescing** (Ch. 53, 55): `ethtool -G <if> rx <N>` to size RX rings for microburst absorption (Ch. 53 — avoid hardware drops at the open); `ethtool -C <if> rx-usecs 0 adaptive-rx off` to **disable interrupt coalescing** for lowest latency (coalescing trades latency for throughput/CPU — wrong trade on the hot path, Ch. 55). Enable hardware timestamping (Ch. 55, 58) if used.
- [ ] **RPS/XPS** (Ch. 51, 55): Receive/Transmit Packet Steering — steer packet processing to chosen cores. For a busy-polled kernel-bypass path (Ch. 62) this may be moot; for a kernel-stack path (Ch. 47), steer RX to the processing cores and keep it off hot cores you've isolated.
- [ ] **Disable Transparent Huge Pages (THP)** if using *explicit* huge pages (Ch. 15): `echo never > /sys/kernel/mm/transparent_hugepage/enabled` — THP's background `khugepaged` compaction is a jitter source (Ch. 15), and you've reserved explicit pages in C.2. *(If you rely on THP instead, set `madvise` and pre-fault — Ch. 15, 26.)*
- [ ] **Network `sysctl`** (Ch. 51): size socket buffers (`net.core.rmem_max`/`wmem_max`, `net.ipv4.tcp_rmem`/`tcp_wmem`), `net.core.netdev_max_backlog`, `net.ipv4.tcp_low_latency` (legacy), busy-poll (`net.core.busy_poll`/`busy_read` — Ch. 47, 55) for the kernel-stack path. Set `TCP_NODELAY` in the *app* (Ch. 51), not sysctl. For multicast market data (Ch. 51), size `net.core.rmem_max` generously and tune `igmp`/reverse-path as needed.
- [ ] **VM `sysctl`** (Ch. 15, 23): `vm.swappiness=0` (don't swap — Ch. 23/26), `vm.zone_reclaim_mode`, `vm.stat_interval` high (less vmstat housekeeping). **Disable swap entirely** (`swapoff -a`) on a trading box — a swapped-out hot page is a multi-ms fault (Ch. 23, 26).
- [ ] **`mlockall`** (Ch. 26): have the application call `mlockall(MCL_CURRENT|MCL_FUTURE)` to lock all pages resident — no page faults, no swapping on the hot path (Ch. 23, 26). (This is application code, but it's the runtime memory-locking counterpart to the huge-page reservation.) Pre-fault and pre-touch (Ch. 46).
- [ ] **Turn off / pin background services** (Ch. 41, 45): cron, monitoring agents, `systemd` timers, log rotation, package-update daemons — confine them (and all non-hot processes) to the housekeeping cores via cgroup/`systemctl set-property AllowedCPUs`, or stop them. A surprise cron job on a hot core is a classic jitter incident.
- [ ] **`writeback`/dirty-page tuning** if the box does capture (Ch. 75): keep dirty-page writeback off the hot cores and on isolated storage (Ch. 75), so flushing doesn't stall.

## C.4 Per-process (affinity/`taskset`, RT priority, `numactl`)

Applied when launching the trading process(es) (Ch. 42, 45, 16). **Why:** pin the hot threads to the cores you isolated, give them RT priority, and keep their memory NUMA-local.

- [ ] **Pin hot threads to isolated cores** (Ch. 42): `taskset -c 2-7 <app>`, or — better — per-thread `pthread_setaffinity_np`/`sched_setaffinity` in the app so each hot thread owns exactly one isolated core (Ch. 42), one thread per core, never sharing. Keep housekeeping threads (logger consumer — Ch. 71, capture writer — Ch. 75, control plane) on the housekeeping cores (`0-1`).
- [ ] **Leave SMT siblings idle** (Ch. 43) if HT is *on*: pin only one thread per physical core and ensure the sibling logical CPU runs nothing (don't pin work to it; isolcpus + manual placement). If HT is off (C.1), moot.
- [ ] **RT scheduling priority** (Ch. 45): `chrt -f 80 <app>` (or `SCHED_FIFO`/`SCHED_RR` via `sched_setscheduler` per thread) so the hot thread isn't preempted by normal tasks. **Caution** (Ch. 45): an RT thread that busy-spins can starve kernel housekeeping on its core — combine with `isolcpus`/`nohz_full` (C.2) so there's nothing to starve, and set `kernel.sched_rt_runtime_us=-1` *only* on a properly isolated box (otherwise the RT throttle is a safety net you may want).
- [ ] **NUMA-local memory** (Ch. 16): `numactl --cpunodebind=0 --membind=0 <app>` to bind the process to one NUMA node's cores *and* memory, so allocations are node-local (Ch. 16 — cross-socket access is a latency penalty). For multi-socket boxes, place each hot process's memory on the node whose cores and NIC it uses (Ch. 16, 42, 55 — NIC-NUMA locality). First-touch the memory on the right node (Ch. 16).
- [ ] **Pre-fault and warm** (Ch. 46): at startup, touch all huge pages, prime caches/TLB/branch predictors with shadow traffic, establish connections — so the *first real tick* is warm (Ch. 46). This is application logic but belongs in the launch/readiness procedure.
- [ ] **cgroup isolation** (Ch. 45): place the trading process in a cgroup with exclusive `cpuset` of the hot cores, and *all other* processes in a cgroup confined to housekeeping cores — enforcing the separation system-wide.

## C.5 Verification pass (`cyclictest`, jitter measurement)

**Never trust the checklist — measure that the box is actually quiet** (Ch. 3, 45, 76). This pass is what turns "I set the knobs" into "the box is deterministic."

- [ ] **`cyclictest`** (Ch. 45): the canonical jitter measurement. Run pinned to a hot core, RT priority, for a meaningful duration under representative load: `cyclictest -m -p 80 -a 2 -t 1 -i 200 -d 0 -D 1h`. Read the **max** latency (the tail — Ch. 1), not the avg. A well-tuned isolated core should show single-digit-microsecond (often sub-µs avg, low-µs max) wake-up latency with a *tight* distribution; a fat max means residual jitter to hunt (an SMI, a missed isolation, a stray IRQ). Compare against an *un*-tuned core to see the improvement.
- [ ] **Hunt residual jitter sources** (Ch. 41, 45): if `cyclictest` max is high, find the culprit — `perf` / `ftrace` / the `hwlat` detector (catches SMIs and hardware latency the OS can't see — Ch. 6's hidden firmware jitter), check `/proc/interrupts` for IRQs landing on hot cores (C.3), check for stray processes (`ps -eLo psr,...` to see what's on each core). Iterate until the tail is flat.
- [ ] **Verify the settings actually took:** C-states (`cpupower idle-info`), frequency/governor (`cpupower frequency-info`), isolation (`cat /sys/devices/system/cpu/isolated`, `/proc/cmdline`), IRQ affinity (`/proc/irq/*/smp_affinity`), huge pages (`/proc/meminfo` HugePages_*), THP (`/sys/kernel/mm/transparent_hugepage/enabled`), NUMA (`numactl -H`, `numastat`), mitigations (`/sys/devices/system/cpu/vulnerabilities/*`). A setting you *meant* to apply but didn't (a typo'd cmdline, a service that reset it) is the most common "why is it still jittery."
- [ ] **Measure the application's own tail** (Ch. 3, 76): `cyclictest` proves the *box* is quiet; the real test is the *application's* tick-to-trade distribution (Ch. 76) under a microburst (Ch. 53) — run the §76.3 decomposition and confirm the per-stage tails are flat. The box being quiet is necessary, not sufficient.
- [ ] **Re-verify after every change** (Ch. 76): a kernel update, a BIOS update, a config drift, a new background agent can silently re-introduce jitter. Bake this verification into a periodic check (and ideally a pre-trading-day readiness gate) — tuning is defended continuously, not set once.

## C.6 References

- The Linux kernel real-time and `nohz_full`/`isolcpus`/`rcu_nocbs` documentation (`Documentation/admin-guide/kernel-parameters.txt`, the NO_HZ and RT docs) — the cmdline knobs of C.2 (Ch. 45).
- The Red Hat / SUSE **low-latency tuning guides** and `tuned` profile documentation — a vendor-blessed consolidation of C.1-C.3.
- `cyclictest` / `rt-tests` and the `hwlat` detector documentation — the verification pass (C.5; Ch. 45).
- `ethtool`, `cpupower`, `numactl`, `chrt`, `taskset` man pages — the runtime/per-process tools of C.3-C.4.
- Intel/AMD BIOS low-latency tuning guides and your server vendor's (Dell/HPE/Supermicro) "low latency" BIOS whitepapers — C.1.
- The chapters this consolidates: Ch. 6 (setup/mitigations), Ch. 15 (huge pages), Ch. 41–46 (context-switch/pinning/SMT/RT/warming), Ch. 51 (TCP), Ch. 55 (NIC).

## C.7 Additional Reading

- The Red Hat *Low Latency Performance Tuning* and *Realtime* guides, and the CERN/CMS and trading-community tuning write-ups — real deployed checklists (C.1-C.5).
- Talks on building deterministic Linux boxes for trading (e.g. the Carl Cook / Mechanical Sympathy material — Ch. 76) — the *why* behind the knobs.
- **Appendix A** (ARM/Graviton) — the ARM-server analogs of these knobs (often fewer: fixed frequency, no SMT — A.5-A.6); **Appendix D** (Compiler Flag Reference) — the *build-side* counterpart to this *system-side* checklist; **Appendix E** (Latency Numbers) — the costs (page fault, context switch, C-state exit) these knobs eliminate.

---

*Next: Appendix D — Compiler Flag Reference, a categorized GCC/Clang flag cheat sheet with the latency-relevant ones called out: optimization levels, arch/tuning, FP/math, codegen, inlining/layout, LTO/PGO/BOLT, diagnostics, and sanitizers — what each costs and when to drop it on the hot path (consolidating Ch. 6, 17, 20–22, 27).*
