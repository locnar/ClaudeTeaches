# Part VII — OS, Scheduling & Isolation

# Chapter 55 — Hunting Residual Noise on Isolated Cores

> **Prerequisites:** Ch. 53 (isolation and RT configuration, `cyclictest` — this chapter starts where its §53.3 table ends), Ch. 54 (power management — a C-state wake-up or a power-limit throttle is the one gap this chapter's tracers see but cannot name without that chapter's counters), Ch. 50 (IRQ affinity, managed IRQs, "stray IPIs"), Ch. 48 (what a context switch or interrupt costs the hot thread), Ch. 6 §6.4.4 (SMIs, the clocksource watchdog, `hwlat`), Ch. 19 (the TSC — the instrument every detector here spins on), Ch. 2 (`perf` basics), Ch. 1 (the tail — a 40 µs spike once a minute *is* your p99.99).
>
> **Leads into:** Ch. 56 (warming — the last Part VII enemy once the core is quiet *and* attributed), Ch. 73 (eBPF/bpftrace — the programmable version of the tracepoints used here), Ch. 103 (host health telemetry — turning this chapter's one-off hunt into a continuous signal), Appendix C.5 and Appendix I (the hunt's one-liners, consolidated). Ch. 53 made the core quiet on paper; this chapter proves what is *still* landing on it and names the knob for each source.

---

## 55.1 Why it matters: the last 40 µs has a name

Ch. 53 ended with the canonical result: pin, isolate, make tickless, route IRQs away, run `SCHED_FIFO`, and `cyclictest` max falls from ~3 ms to a few µs. Then production happens. The strategy's p99.99 shows a 38 µs spike every few minutes, `cyclictest` run for an hour shows a 61 µs max once, and `/proc/interrupts` on the hot core shows a handful of increments in columns you never looked at — `CAL`, `TLB`, `IWI`. Nothing in the Ch. 53 recipe is wrong; something is *still running on the core*, and `cyclictest` can only tell you that it happened, not what it was. A spike that lands once a minute on a path that handles ten thousand messages a second is one message in six hundred thousand — invisible in the mean, invisible in p99, and precisely the order that was in flight at the open when it mattered (Ch. 1). Until you can say *what* ran on the core, you cannot say the box is quiet; you can only say you have not caught it yet.

This is a different discipline from configuration. Ch. 53 is "make the kernel leave the core alone"; this chapter is "find out who didn't get the memo." The sources are a known, finite set — the kernel's own documentation enumerates them — and each has a **fingerprint** (which `/proc/interrupts` row moved, which tracepoint fired, which kthread was switched in) and a **knob**. The toolkit is the kernel's: the **osnoise** and **timerlat** tracers through `rtla` (the purpose-built replacements for guessing from `cyclictest`), `hwlat` for the firmware layer, raw **ftrace** with a stop-on-threshold trigger so the ring buffer holds exactly the moment of the spike, `trace-cmd`/KernelShark to read it, `perf sched` for who-ran-when, and the SMI counter for what the kernel could not see. None of it requires a kernel rebuild, all of it works on a production box in a maintenance window, and most of it costs nothing while it is not firing.

The chapter has two halves that depend on each other. §55.2 is the **noise catalog**: what can still execute on an `isolcpus`/`nohz_full` core — per-CPU kthreads, unbound workqueues, RCU, IPIs (reschedule, function-call, TLB shootdown), managed device vectors, the NMI/PMI family, the memory-management daemons, and firmware — with the signature and the knob for each. §55.3 measures: `rtla osnoise` and `timerlat` beside Ch. 53's `cyclictest`, plus a 40-line user-space detector you can run anywhere. §55.4 is the **attribution workflow**: audit → stop-on-threshold trace → read the trace → match the catalog → turn the knob → re-measure, with the exact commands. §55.5 lists the ways the hunt goes wrong (perturbing the system with the tracer, chasing the detector's own noise, "fixing" a source by hiding it), and §55.6 closes with the exercises and the sign-off checklist. Ch. 103 later turns this one-off hunt into a continuous production signal; Appendix I.6 carries the commands.

## 55.2 Mental model: the kernel noise catalog

**The isolation knobs remove classes of work, not all work.** `isolcpus=domain` stops the scheduler *placing* tasks; `nohz_full` stops the *periodic tick* when one task runs; `rcu_nocbs` moves *RCU callbacks*; `irqaffinity` and `smp_affinity` move *device interrupts you can move*. What is left is everything that is (a) explicitly per-CPU, (b) triggered by *another* CPU targeting this one (IPIs), (c) owned by a driver the kernel manages for you, or (d) below the kernel entirely. The kernel's own list lives in `Documentation/admin-guide/kernel-per-CPU-kthreads.rst`; the table below is that list reorganised by how it shows up on the core and what switches it off.

```
   source                           fingerprint on the hot core                          the knob
   ───────────────────────────────  ───────────────────────────────────────────────────  ──────────────────────────────────────────
   residual timer tick              /proc/interrupts LOC increments; timer:tick_stop     a 2nd runnable task, perf event, posix timer or
                                    tracepoint fires with success=0 + a dependency mask  unstable clocksource on the CPU — remove it (Ch. 53)
   ksoftirqd/N, softirqs            sched_switch -> ksoftirqd/N; irq:softirq_entry       softirq *raised on this CPU* — route the IRQ/net
                                                                                         queue away (Ch. 50, 58 §58.4.2, 67); RT: ksoftirqd at RT
   kworker/N:* (per-CPU work)       sched_switch -> kworker/N:x; workqueue:* events      per-CPU work is queued *from* this CPU (a timer,
                                                                                         a driver, vmstat) — find the queuer in the trace
   unbound kworker/u*               sched_switch -> kworker/uNN:x                        /sys/devices/virtual/workqueue/cpumask = housekeeping
   RCU: rcuc/N, rcuo*/N, rcu_sched  sched_switch -> rcuc/rcuop; rcu:* events; CAL IPIs   rcu_nocbs= (Ch. 53); rcutree.rcu_nocb_poll (no wake
   expedited grace periods          (expedited GPs IPI every CPU running in kernel mode)  IPIs); rcupdate.rcu_normal=1 (no expedited GPs)
   reschedule IPI                   /proc/interrupts RES; irq_vectors:reschedule_entry   something woke a task *onto* this CPU — a 2nd task,
                                                                                         a wrongly-affined thread, a workqueue (above)
   function-call IPI                /proc/interrupts CAL; irq_vectors:call_function_*    smp_call_function from another CPU: perf, RDT/MSR
                                                                                         writes, CPU-hotplug, cpufreq, text_poke (live
                                                                                         patching, kprobes, ftrace enable!), RCU expedited
   TLB shootdown IPI                /proc/interrupts TLB; tlb:tlb_flush reason=REMOTE    *your own process* unmapped/protected memory on
                                                                                         another core (munmap, madvise(DONTNEED), THP split,
                                                                                         compaction, KSM) — no free/unmap in housekeeping
                                                                                         threads of the hot process (Ch. 28–29, 93)
   irq_work IPI                     /proc/interrupts IWI; irq_vectors:irq_work_entry     perf, printk from NMI, RCU — usually a symptom of
                                                                                         one of the above
   managed device vectors           /proc/interrupts: a NIC/NVMe queue row counting on   isolcpus=managed_irq (Ch. 53), fewer queues
   (NIC, NVMe — ignore smp_affinity) the hot CPU; effective_affinity_list includes it    (ethtool -L), NVMe irq policy; Ch. 50
   NMI watchdog / perf PMI          /proc/interrupts NMI, PMI; nmi:nmi_handler           nmi_watchdog=0 (Appendix C.2); no perf sampling on
                                                                                         the hot core in production (Ch. 2, 108)
   watchdog/N (soft lockup)         sched_switch -> watchdog/N                           watchdog_cpumask excludes isolated CPUs;
                                                                                         nosoftlockup
   khugepaged / kcompactd / kswapd  sched_switch -> those (they run on housekeeping      THP never or madvise + no khugepaged defrag
   / ksmd                           CPUs but IPI/shoot down the hot CPU)                 (Ch. 16); vm.compaction_proactiveness=0; KSM off;
                                                                                         mlockall + pre-fault (Ch. 31, 56)
   vmstat_update                    kworker/N doing vmstat_update                        vm.stat_interval high; nohz_full CPUs defer it
                                                                                         (quiet_vmstat) — check it actually went quiet
   NUMA balancing hinting faults    page faults with no mmap/first touch; sched:*numa*   kernel.numa_balancing=0 (Ch. 17, Appendix C.3)
   clocksource watchdog timer       a timer on an isolated CPU every 500 ms; dmesg       tsc=nowatchdog / tsc=reliable (Ch. 6 §6.4.4)
   cpufreq / thermal / RAPL         call_function IPIs from cpufreq governor; THR/TRM    performance governor, fixed P-state, thermal
                                    rows; thermal_apic_entry                              headroom (Ch. 6)
   SMI (firmware)                   NOTHING in /proc/interrupts; MSR_SMI_COUNT (0x34)     BIOS (Appendix C.1); hwlat / rtla osnoise HW column
                                    increments; osnoise HW column; hwlat gaps            (Ch. 6 §6.4.4)
   hardware stalls below firmware   osnoise HW; hwlat; no counter                        DRAM patrol scrub / ECC CMCI storms, PCIe link
                                                                                         retraining — Ch. 103's hardware-health signals
```

Three ideas organise the table:

- **Noise is either *local* or *remote*.** Local noise is work the hot CPU does to itself: a timer it armed, a softirq raised by an interrupt it took, a per-CPU work item queued from its own context. Remote noise is work another CPU *forced* on it through an IPI: a reschedule, a cross-call, a TLB flush, an expedited RCU grace period. The distinction tells you where to look. Local noise means something on the hot core still touches a kernel facility (a syscall, a perf event, a timer — Ch. 48's hunt); remote noise means the housekeeping side — your own process's other threads, system daemons, the monitoring agent — is *reaching into* the core, and the fix is on *their* side.
- **Every kernel-side source has a trace fingerprint; only firmware does not.** `ftrace` sees every interrupt entry, every context switch, every IPI vector on the core. Anything it cannot see yet which the TSC says happened is firmware (SMI) or hardware — the `HW` column of `osnoise` and the `hwlat` tracer exist exactly to measure that residue (Ch. 6 §6.4.4 counts the SMIs).
- **The most embarrassing source is your own process.** A TLB-shootdown IPI from a housekeeping thread of the *same* process that freed memory (Ch. 28–29), a `perf stat` someone left attached to the hot thread, a stats thread calling `sched_setaffinity` or `mprotect`, a `kprobe` being armed (`text_poke` cross-calls every CPU), a thread created without affinity landing on the hot core (Ch. 50 §50.5): the box is quiet until the application wakes it. Process-per-role (Ch. 93) is partly a noise-isolation decision for this reason.

The model: **after Ch. 53, what remains on the core is a finite catalog of local and remote intrusions, each with a counter or tracepoint that names it; the hunt is to catch one in a trace, match it to the catalog, and turn its knob — and whatever has no kernel fingerprint is firmware or hardware, counted separately.**

## 55.3 Measure it: `rtla osnoise` and `timerlat` beside `cyclictest`

*Toolchain and machine: Appendix L.3.*

`cyclictest` (Ch. 53 §53.3) measures *wake-up latency*: how late a sleeping thread was woken. It catches noise only if the noise happened to delay a wake-up, and it says nothing about who caused it. The kernel ships two tracers that are built for this hunt, driven by one front end, `rtla` (`tools/tracing/rtla`, packaged as `rtla` since 5.17):

- **`osnoise`** runs a kernel thread (or, with `-u`, a user thread) that *spins* on the CPU reading the clock exactly like your busy-poll loop, and accounts every gap in its own execution to a cause — because it is in the kernel, it sees the interrupt, softirq, NMI and context-switch entry/exit hooks around each gap. The gap it cannot attribute is reported as `HW` (hardware/firmware noise). Its output is the busy-poll trader's view of the core.
- **`timerlat`** arms a periodic timer and measures two latencies per period: how late the *IRQ* fired (hardware + interrupts-disabled regions) and how late the *thread* ran after it (scheduling). It is `cyclictest` with the latency split into its two halves and with a trace attached.

Both take a **stop-on-threshold** option that freezes the trace buffer the instant a sample exceeds a value, so the ring holds the µs before the spike rather than the hour after it. Run them on the Ch. 53 configuration, hot core 5, housekeeping 0–1:

```bash
# osnoise: spin as SCHED_FIFO 80 on CPU 5 for 10 min, 1 s periods, count noise >= 1 us,
#          stop and save a trace on the first single noise event above 20 us
sudo rtla osnoise top -c 5 -P f:80 -d 10m -p 1000000 -r 1000000 -T 1 -s 20 -t
# timerlat: 100 us period, stop + trace when IRQ or thread latency exceeds 20 us; -u measures from a user thread too
sudo rtla timerlat top -c 5 -P f:95 -p 100 -a 20 -u
# hwlat (Ch. 6 §6.4.4): the firmware floor — spins with interrupts off; a qualification tool, never left running
sudo rtla hwnoise -c 5 -d 2m -T 1
```

Representative results on the reference box (Xeon Gold 6326, Linux 6.18 `CONFIG_HZ=1000`, non-RT, rt-tests 2.x and rtla 6.18) — the same four configurations as Ch. 53 §53.3, now with attribution. `cyclictest` max is repeated from §53.3 for the row; the `osnoise` columns are per 1 s period (`Noise` = µs stolen per second, `Max Single` = the longest one gap, then counts by cause):

```
   configuration                          cyclictest  osnoise/1s:  Noise   Max Single  HW   NMI   IRQ   Softirq  Thread
                                          max (µs)                 (µs)    (µs)
   stock core, busy system                 ~3,000      ~18,400     2,870    0    0   1,012    318     1,431
   pinned + isolcpus + SCHED_FIFO           ~40           1,120       38     0    0   1,001     11        4
   + nohz_full + rcu_nocbs + IRQs away      ~5              14        4.1    0    0       3      0        1
   + mitigations=off, C1 cap, BIOS SMIs off ~2               2.3      1.6    0    0       1      0        0
   …and the same tuned core, one hour, with one spike:
   tuned core, 1 h, the "61 µs" event        61 (1×)        —        61      0    0       1      0        1   <- osnoise -s 20 fired
```

Read it the way the catalog predicts:

- **The stock core is mostly *thread* and *IRQ* noise** — 1,431 context switches and ~1,000 local-timer interrupts per second (the tick: `HZ=1000` → `IRQ ≈ 1,000`), stealing 18 ms of every second. `isolcpus` + RT priority removes the threads (4 switches/s: the residual per-CPU kthreads) but **the tick still fires 1,000×/s** — the `IRQ` column does not move until `nohz_full`. That column is the single most useful number on the table: a `nohz_full` core whose `IRQ` count is ~1,000/s is *not tickless* (Ch. 53 §53.5 — a second runnable task, a perf event, a posix timer), and no amount of other tuning will fix it.
- **The fully tuned core has single-digit noise events per second and ~2 µs of stolen time** — `IRQ 1` is `osnoise`'s own housekeeping timer plus the once-per-second residual, `Thread 0`. A `HW 0` with `NMI 0` says firmware is quiet; a non-zero `HW` on an otherwise silent core is the SMI signature (Ch. 6 §6.4.4 — confirm with `MSR_SMI_COUNT`).
- **The one-hour row is the point of the chapter.** `cyclictest` reports 61 µs once; `osnoise -s 20` *stopped the trace at that event*, and the trace (§55.4.2) showed `irq_vectors:call_function_entry` → `smp_call_function_many` originating from CPU 1 inside `__text_poke` → a `kprobe` being armed by a monitoring agent's `bpftrace` run. `Thread 1`: the hot thread was switched out for a `kworker/5:1` that the same cross-call queued. One trace, one catalog row, one fix (run tracing agents outside the trading window, Ch. 73 §73.3), and the hour-long re-run is flat.

**A user-space detector you can run anywhere.** `osnoise` needs root and tracefs; the same measurement from user space — a spin loop timing the gap between consecutive `rdtsc` reads — is forty lines, runs as the trading user, and is exactly what Ch. 103 runs continuously. It reproduces the `Max Single` column and, with `--stop-us`, drives the same stop-on-threshold ftrace trigger through `trace_marker` and `tracing_on`:

```cpp
// gapdetect.cpp — a user-space osnoise: spin on one isolated core reading the TSC, histogram every gap
// between consecutive reads, and on a gap above --stop-us write a marker and stop the kernel tracer so
// the ftrace ring holds exactly what ran on the core during the spike.
// Build: g++ -O2 -std=c++23 -march=icelake-server gapdetect.cpp -o gapdetect   (GCC 13/14, Clang 18 same)
// Run:   taskset -c 5 chrt -f 80 ./gapdetect --seconds 60 --stop-us 20
#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <format>
#include <string_view>
#include <unistd.h>
#include <x86intrin.h>

namespace {
constexpr char kTracing[] = "/sys/kernel/tracing/";
double tsc_ghz() {                                           // calibrate against CLOCK_MONOTONIC (Ch. 19)
    timespec a{}, b{}; clock_gettime(CLOCK_MONOTONIC, &a); const auto t0 = __rdtsc();
    do { clock_gettime(CLOCK_MONOTONIC, &b); } while ((b.tv_sec - a.tv_sec) * 1'000'000'000LL + (b.tv_nsec - a.tv_nsec) < 100'000'000);
    const auto t1 = __rdtsc();
    return double(t1 - t0) / ((b.tv_sec - a.tv_sec) * 1e9 + (b.tv_nsec - a.tv_nsec));
}
int open_tracefs(const char* name, int flags) {              // -1 (and no tracing) if tracefs is not mounted/permitted
    char p[128]; std::snprintf(p, sizeof p, "%s%s", kTracing, name); return ::open(p, flags);
}
int ctoi(std::string_view s) { int v = 0; std::from_chars(s.data(), s.data() + s.size(), v); return v; }
}  // namespace

int main(int argc, char** argv) {
    int seconds = 10, stop_us = 0;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!std::strcmp(argv[i], "--seconds")) seconds = ctoi(argv[i + 1]);
        if (!std::strcmp(argv[i], "--stop-us")) stop_us = ctoi(argv[i + 1]);
    }
    const double ghz = tsc_ghz();
    const int marker = stop_us ? open_tracefs("trace_marker", O_WRONLY) : -1;    // opened BEFORE the loop: no syscalls inside it
    const int tracing_on = stop_us ? open_tracefs("tracing_on", O_WRONLY) : -1;
    const std::uint64_t stop_cycles = std::uint64_t(stop_us * ghz * 1000.0);
    const std::uint64_t end = __rdtsc() + std::uint64_t(seconds * ghz * 1e9);

    std::array<std::uint64_t, 64> hist{};                     // log2 buckets of gap length in ns: bucket k holds [2^k, 2^(k+1)) ns
    std::uint64_t worst = 0, samples = 0, prev = __rdtsc();
    while (true) {
        const std::uint64_t now = __rdtsc();                  // unfenced rdtsc: a ~25-cycle self-gap floor, fine at µs thresholds
        const std::uint64_t gap = now - prev; prev = now; ++samples;
        const std::uint64_t ns = std::uint64_t(gap / ghz);
        ++hist[ns ? 63 - __builtin_clzll(ns) : 0];
        if (gap > worst) worst = gap;
        if (stop_cycles && gap > stop_cycles) [[unlikely]] {   // the only syscalls: on the spike we are hunting
            char msg[96]; const int n = std::snprintf(msg, sizeof msg, "gapdetect: %llu ns gap, stopping trace\n", (unsigned long long)ns);
            if (marker >= 0) (void)!write(marker, msg, n);
            if (tracing_on >= 0) (void)!write(tracing_on, "0", 1);
            break;
        }
        if (now > end) break;
    }
    std::fputs(std::format("samples {}  worst gap {:.2f} us  (tsc {:.3f} GHz)\n{:>14} {:>12}\n",
                           samples, worst / ghz / 1000.0, ghz, "gap >= (ns)", "count").c_str(), stdout);
    for (std::size_t k = 0; k < hist.size(); ++k)                 // std::println once the toolchain has <print> (Appendix H)
        if (hist[k]) std::fputs(std::format("{:>14} {:>12}\n", 1ull << k, hist[k]).c_str(), stdout);
}
```

Built with GCC 13.3 and Clang 18.1 as shown (libstdc++ 13 has `<format>` but not `<print>` — Appendix H). On an *untuned* 4-vCPU virtual machine (the authoring host, Linux 6.18, nothing pinned) a 3 s run recorded 1.4 × 10⁸ samples, 870 gaps in the 1–2 µs bucket, 755 in 8–16 µs, 8 above 64 µs and a worst gap of 360 µs — the stock-core row of the table, from user space, in a VM. On the reference box's tuned core the same run's histogram collapses to the 8–32 ns buckets plus a handful of 1–4 µs entries (the residual IRQs the `osnoise` table counts) and a worst gap that *is* the number to chase. What the loop deliberately does not do: no syscalls, no allocation, no `clock_gettime` inside the loop (the gaps would be the vDSO's — Ch. 19), the tracefs files opened before it starts, and the stop-trigger path taken exactly once. It is Ch. 48's "no syscalls on the hot path" applied to the instrument.

## 55.4 Techniques

### 55.4.1 Catalog audit: what is allowed on the core right now

Before tracing, audit statically — most residual sources are visible in `/proc` and `/sys` without catching one in the act. A dozen lines of shell, run for each isolated CPU (`$C`), tell you who *may* run there and which vectors *may* fire there:

```bash
C=5
cat /sys/devices/system/cpu/isolated /sys/devices/system/cpu/nohz_full      # is the CPU in both sets? (Ch. 53)
ps -eLo pid,tid,psr,cls,rtprio,comm | awk -v c=$C '$3==c'                     # every thread currently ON the core: want exactly one
grep -l -E "^Cpus_allowed_list:.*(^|[,-])$C($|[,-])" /proc/[0-9]*/task/[0-9]*/status 2>/dev/null \
  | sed 's|/status||' | xargs -r -I{} sh -c 'cat {}/comm' | sort | uniq -c | sort -rn | head -20   # every thread ALLOWED on it
cat /sys/devices/virtual/workqueue/cpumask                                     # unbound workqueues: must exclude $C
cat /proc/sys/kernel/watchdog_cpumask                                          # soft-lockup watchdog: must exclude $C
for i in /proc/irq/[0-9]*; do l=$(cat $i/effective_affinity_list 2>/dev/null); \
  echo "$l" | grep -q -E "(^|[,-])$C($|[,-])" && echo "IRQ $(basename $i) -> $l  $(cat $i/actions 2>/dev/null)"; done
cat /sys/devices/system/clocksource/clocksource0/current_clocksource          # tsc, or the watchdog timer is on your core (Ch. 6)
awk -v c=$((C+2)) 'NR==1 || /LOC|RES|CAL|TLB|IWI|NMI|PMI/ {printf "%-5s %s\n", $1, $c}' /proc/interrupts   # the IPI rows, this CPU
```

What the audit should return on a signed-off core: `isolated` and `nohz_full` both contain it; exactly one thread on it (yours, `cls FF`, your RT priority); the threads *allowed* on it are yours plus the unavoidable per-CPU kthreads (`migration/5`, `ksoftirqd/5`, `kworker/5:*`, `cpuhp/5`, and `rcuc/5` on RT); the workqueue and watchdog masks exclude it; no IRQ's effective affinity includes it except the local vectors; the clocksource is `tsc`. Then **snapshot the IPI rows, wait a minute of steady state, and diff**: `LOC` should move by about one per second (the residual tick — if it moves by a thousand, the tick is on), `RES`/`CAL`/`TLB`/`IWI` should not move at all, `NMI`/`PMI` should be zero. Any delta is a catalog row, and the diff tells you which class before you trace anything. (Ch. 103 §103.4.1 is this diff, run every second, forever.)

### 55.4.2 Stop-on-threshold tracing: catching the spike in the act

When the audit is clean and the spike still happens, catch it. The technique is always the same three steps — **arm a trace that is cheap while nothing happens, stop it the instant the detector sees the spike, read the µs before the stop** — and ftrace supports it natively:

```bash
T=/sys/kernel/tracing
echo 0 > $T/tracing_on
echo 20 > $T/tracing_cpumask           # hex mask: CPU 5 only (0x20) — trace only the hot core; nothing else is recorded
echo x86-tsc > $T/trace_clock           # timestamps in TSC so they line up with your app's rdtsc (Ch. 19)
echo 8192 > $T/buffer_size_kb           # per-CPU ring; 8 MiB holds seconds of a quiet core
echo > $T/set_event                      # the fingerprint events of the catalog (§55.2):
echo 'sched:sched_switch sched:sched_wakeup irq:irq_handler_entry irq:softirq_entry' >> $T/set_event
echo 'irq_vectors:* tlb:tlb_flush timer:tick_stop timer:hrtimer_expire_entry workqueue:workqueue_execute_start' >> $T/set_event
echo 'ipi:ipi_send_cpu ipi:ipi_send_cpumask' >> $T/set_event   # 6.4+: who sent the IPI (sender side — enable on ALL cpus for this one)
echo 1 > $T/tracing_on
taskset -c 5 chrt -f 80 ./gapdetect --seconds 3600 --stop-us 20     # §55.3: stops the trace itself via tracing_on
cat $T/trace | tail -60                                             # the last 60 events on CPU 5 before the marker
```

Three variations cover the rest:

- **`rtla ... -s/-a ... -t`** does all of the above for you — `osnoise -s 20 -t` and `timerlat -a 20` enable the right events, stop on the threshold, and save `osnoise_trace.txt`/`timerlat_trace.txt` with the stack of the offending IRQ or the name of the task that preempted the thread. Prefer it when you can run as root; keep the manual recipe for when the detector must be *your* application (a `trace_marker` write from the strategy's own watchdog branch when a tick-to-trade sample exceeds the budget — the production form of the trigger, Ch. 90 §90.4.4).
- **`trace-cmd record -M 20 -C x86-tsc -e irq -e sched -e irq_vectors -e tlb ...`** records the same events to a file instead of the ring (`-M` is the CPU mask), and `trace-cmd report` / **KernelShark** render them as a timeline where "what ran on CPU 5 between these two TSC values" is a visual question. For a spike that recurs every few minutes, record for ten minutes and look at the timeline around each `gapdetect` marker.
- **`perf sched record -C 5 -- sleep 600` then `perf sched timehist -C 5`** gives the who-ran-when table — every task switched onto the core, how long it waited, how long it ran — which is the fastest way to find a *thread* intruder (a `kworker`, a stray agent, your own mis-affined helper). `perf sched latency` summarises the same data per task.

Reading the trace against the catalog: an `irq_vectors:call_function_entry` whose matching `ipi:ipi_send_cpu` came from a housekeeping CPU inside `smp_call_function_many` ← `on_each_cpu` ← `text_poke_bp_batch` is a kprobe/ftrace/livepatch being armed; `tlb:tlb_flush reason=TLB_REMOTE_SHOOTDOWN` preceded by a `munmap` on another thread of your PID is your own allocator; `sched_switch prev=strategy next=kworker/5:1` with `workqueue_execute_start function=vmstat_update` is vmstat (raise `vm.stat_interval`, check `quiet_vmstat` engaged); `timer:tick_stop success=0 dependency=PERF_EVENTS` is a perf counter still attached to the core; a gap with *no* event inside it at all is firmware — go to the SMI counter.

### 55.4.3 Turning the knobs: one fix per catalog row

Attribution is worthless without the fix, and the fixes are specific. The ones not already in Ch. 53/Appendix C, by catalog row:

- **Expedited RCU and `rcuo*` wake-ups.** `rcu_nocbs=` moved callback *processing*; the offload kthreads are still *woken* by an IPI unless `rcutree.rcu_nocb_poll` makes them poll instead, and **expedited grace periods** (`synchronize_rcu_expedited`, used by some drivers, `membarrier`, and `cgroup`/`sysfs` churn) IPI every CPU that is in the kernel. Boot with `rcupdate.rcu_normal=1` (all GPs normal; `rcu_normal_after_boot=1` keeps boot fast) and `rcutree.rcu_nocb_poll`. Verify: `CAL` stays flat during a `cgroup` create/destroy storm on the housekeeping side.
- **Unbound workqueues and the per-CPU ones.** Write the housekeeping mask to `/sys/devices/virtual/workqueue/cpumask` (the `isolcpus=...,nohz` flag and `nohz_full` set it for you on recent kernels — *check*, don't assume). Per-CPU `kworker/5:*` work is queued from the core itself: the trace names the function (`workqueue_execute_start`), and the fix is to remove whatever on the core still queues it (a timer, a driver, `vmstat_update`).
- **Managed vectors.** `isolcpus=managed_irq` (Ch. 53) keeps multi-queue NIC/NVMe vectors off isolated CPUs *when there are enough housekeeping CPUs for the queues*; otherwise reduce queue count (`ethtool -L eth0 combined 2`; NVMe `irq_affinity`/`nvme.poll_queues` for the capture disk — Ch. 98) so each queue has a housekeeping home. `/proc/irq/N/effective_affinity_list` is the truth, not `smp_affinity`.
- **TLB shootdowns from your own process.** Every `munmap`, `mprotect`, `madvise(MADV_DONTNEED)`, THP split or page-cache eviction of a page in your address space from *any* core flushes the hot core's TLB by IPI. Pre-allocate and never free on housekeeping threads of the hot process (Ch. 28–29), `mlockall` (Ch. 31), THP `never` or pre-faulted `madvise` with `khugepaged` defrag off (Ch. 16), `vm.compaction_proactiveness=0`, and put the memory-churning roles in *other processes* (Ch. 93) — a different `mm` is not flushed. `tlb:tlb_flush` with `reason=TLB_REMOTE_SHOOTDOWN` counts the remaining ones.
- **The tick that will not stop.** `timer:tick_stop success=0 dependency=...` names the reason: `SCHED` (a second runnable task — Ch. 53 §53.5), `PERF_EVENTS` (a counter attached — `perf stat` left running, a monitoring agent's `perf_event_open`), `POSIX_TIMER` (your process armed an interval timer — move it to a housekeeping thread), `CLOCK_UNSTABLE` (the clocksource is not `tsc` — Ch. 6 §6.4.4), `RCU_EXP` (an expedited GP in flight — above). Fix the dependency; the tick stops on its own.
- **NMI, PMI and the soft-lockup watchdog.** `nmi_watchdog=0` and `nosoftlockup` (Appendix C.2) or `watchdog_cpumask=0-1`; **never** sample the production hot core with `perf record` (Ch. 2, 108 — each PMI is a ~2 µs NMI). Production profiling of the hot path is eBPF on a *housekeeping* core reading the app's own counters (Ch. 73, 90), or offline on a replay (Ch. 108).
- **Firmware.** When `osnoise` shows `HW > 0` or `gapdetect` shows a gap with nothing in the trace, read `MSR_SMI_COUNT` before and after (Ch. 6 §6.4.4, `turbostat --show SMI`) and correlate; the knobs are BIOS (Appendix C.1) and there is no software workaround — a box that takes a periodic 150 µs SMI is the wrong box for the hot path.
- **Re-measure, then sign off.** Every knob change goes back through §55.3: the `osnoise` table for an hour with `-s` set to your budget, `cyclictest` for the Ch. 53 comparison, and the `/proc/interrupts` diff. The core is signed off when `-s <budget>` never fires during a trading-length run under realistic housekeeping load — and the signature of that run (the audit output, the `osnoise` summary, the SMI count) is recorded as the baseline that Ch. 103 and Appendix M compare against every morning.

## 55.5 Pitfalls & anti-patterns: perturbing the hunt, chasing ghosts

- **Tracing everything on every CPU.** A full `function_graph` trace, or events enabled on all CPUs with a small buffer, generates enough work on the housekeeping cores to *create* noise on the hot one (IPIs to flush buffers, `kworker`s, cache pollution through shared L3 — Ch. 52) and overwrites the spike before you read it. Trace the hot CPU only (`tracing_cpumask`), enable only the catalog events, size the buffer, and stop on threshold. The tracer must cost nothing until the moment it is needed.
- **Arming the tracer *is* a cross-call.** Enabling a kprobe, `ftrace` function tracing, or a `bpftrace` probe patches kernel text with `text_poke`, which IPIs every CPU — including the one you are protecting. Arm before the window, never during trading; a monitoring agent that attaches probes ad hoc is a noise source in the catalog (§55.3's one-hour event). Tracepoints and USDT markers that are *already* enabled are cheap; *enabling* them is not (Ch. 73 §73.3).
- **Measuring the detector's own noise.** `cyclictest` sleeps and wakes (a timer IRQ and a wake-up on the core — the very events you are hunting); `osnoise` arms a housekeeping timer; `gapdetect` with a fenced `rdtscp` or a `clock_gettime` in the loop measures the instrument. Know each tool's self-noise (`osnoise`'s `IRQ 1` per second, `gapdetect`'s ~25-cycle floor) and set thresholds above it. Reproduce with a *second* instrument before declaring a source.
- **Fixing the symptom by hiding it.** Raising the detector threshold until it stops firing, dropping `vm.stat_interval` so vmstat IPIs are rarer rather than gone, or moving the spike to a time of day you do not trade. The catalog has a knob for every row; a row you cannot zero is a documented, budgeted cost (an SMI every 30 min at 40 µs), not a threshold you stopped looking at.
- **Trusting `smp_affinity` over `effective_affinity`.** Managed vectors accept no writes and the mask you wrote is not where the vector lives (Ch. 50 §50.4.2). Always read `effective_affinity_list`; always diff `/proc/interrupts` per CPU.
- **Forgetting that your process is multi-threaded.** The hot thread is pinned and isolated; the logger consumer (Ch. 88), the telemetry scraper (Ch. 90), the control thread, and the capture writer (Ch. 98) share its *address space*. Their `munmap`s shoot down its TLB, their `mprotect`s too, their `sched_setaffinity` calls reschedule-IPI it. Audit housekeeping threads for memory-management syscalls (Ch. 48's `strace -c` hunt applies to *them*), or move them to separate processes (Ch. 93).
- **Testing on an idle box.** The housekeeping side at 3 a.m. queues nothing; at the open it runs the logger, the capture writer, the risk service and the deployment agent. Remote noise is proportional to *their* activity. Run the hunt under a realistic housekeeping load — replay the open (Ch. 98, 108) on the housekeeping cores while the detector spins on the hot one.
- **Declaring victory from a short run.** A 1-in-10-minutes spike needs an hour-long run to see six of them, and a 1-in-hour SMI needs a day. Set `-s` to the budget and run for the trading-day length; the absence of a trigger over a full session is the evidence, not a flat five-minute histogram.
- **Leaving `hwlat` or `osnoise` running in production.** They monopolise the core (that is how they measure). They are qualification tools for the maintenance window; the production-time equivalent is the application's own gap detection and the counter diffs (Ch. 103), which cost nothing the hot path notices.

## 55.6 Exercises & checklist

**Exercises**

1. **Reproduce the attribution table.** On a test box, run `rtla osnoise top` on a stock core, then on each Ch. 53 configuration step, and fill in the §55.3 columns. Confirm the `IRQ` column stays at ~`HZ` until `nohz_full` engages and that the `Thread` column reflects the residual per-CPU kthreads.
2. **Plant a source, catch it.** On a tuned core run `gapdetect --stop-us 20` with the §55.4.2 events armed; from a housekeeping core, in the *same* process (a second thread) `mmap`/`munmap` 64 MiB in a loop. Catch the `tlb:tlb_flush` shootdown in the stopped trace; move the churn to a *separate* process and show the IPIs vanish. Repeat with `perf stat -C 5 sleep 10` (a `PERF_EVENTS` tick dependency), and with `bpftrace -e 'kprobe:do_sys_open {}'` armed mid-run (a `text_poke` cross-call).
3. **Expedited RCU.** Create and destroy cgroups in a loop on the housekeeping side and watch `CAL` on the isolated core. Add `rcupdate.rcu_normal=1 rcutree.rcu_nocb_poll` and repeat.
4. **The tick dependency.** Arm a `timer_create`/`setitimer` interval timer from the hot thread and observe `timer:tick_stop success=0 dependency=POSIX_TIMER`; move the timer to a housekeeping thread and confirm the tick stops again.
5. **The firmware residue.** Run `osnoise` for an hour on a signed-off core with `rdmsr -a 0x34` sampled every minute. Correlate any `HW > 0` period with an `MSR_SMI_COUNT` increment; if the box has a BIOS "SMI-free"/"low-latency" profile, toggle it and repeat (Ch. 6 §6.4.4, Appendix C.1).
6. **Detector self-noise.** Replace `__rdtsc()` in `gapdetect` with `clock_gettime(CLOCK_MONOTONIC)` and with a fenced `__rdtscp` + `lfence`; compare the 8–64 ns buckets. Then run it on a core where `current_clocksource` is `hpet` (`echo hpet > .../current_clocksource` on a test box) and explain the new floor (Ch. 6 §6.4.4, Ch. 19).

**Checklist — residual noise, attributed**

- [ ] The §55.4.1 **audit** is clean for every isolated CPU: one thread on it, only the unavoidable per-CPU kthreads allowed, workqueue/watchdog masks exclude it, no device vector's `effective_affinity` includes it, clocksource `tsc`.
- [ ] A one-minute **`/proc/interrupts` diff** on each hot core shows `LOC` ≈ 1/s and `RES`/`CAL`/`TLB`/`IWI` = 0, `NMI`/`PMI` = 0.
- [ ] **`rtla osnoise`** (or `gapdetect`) over a trading-length run under realistic housekeeping load, with `-s`/`--stop-us` set to the per-message budget, **never fires**; `HW` = 0 and `MSR_SMI_COUNT` is flat.
- [ ] Every spike that *did* fire during qualification was **caught in a stopped trace**, matched to a **catalog row** (§55.2), fixed with that row's **knob** (§55.4.3), and re-measured — not thresholded away.
- [ ] RCU is `rcu_nocbs` + `rcu_nocb_poll` + `rcu_normal`; workqueue cpumask is set; managed vectors have housekeeping homes; `nmi_watchdog=0`; no perf sampling on hot cores; the hot process's housekeeping threads make no `munmap`/`mprotect`/`madvise` calls (or live in another process — Ch. 93).
- [ ] Tracing agents (`bpftrace`, kprobes, ftrace function tracing) are **armed outside the trading window** only; what runs in production is tracepoints/USDT that are already enabled (Ch. 73).
- [ ] The signed-off core's **baseline** (audit output, `osnoise` summary, interrupt-row rates, SMI count) is recorded for Ch. 103's continuous comparison and Appendix M's pre-open gate.

## 55.7 References

- Linux kernel documentation: `Documentation/admin-guide/kernel-per-CPU-kthreads.rst` (the per-CPU kthread catalog and the knob for each — §55.2), `Documentation/trace/osnoise-tracer.rst`, `timerlat-tracer.rst`, `hwlat_detector.rst`, `Documentation/tools/rtla/` (`rtla-osnoise`, `rtla-timerlat`, `rtla-hwnoise`), `Documentation/trace/ftrace.rst` and `events.rst` (tracefs, `tracing_cpumask`, `trace_clock`, `set_event`, `trace_marker`), `Documentation/RCU/` (`rcu_nocbs`, `rcu_nocb_poll`, `rcu_normal`), `Documentation/timers/no_hz.rst` (the tick dependencies named by `timer:tick_stop`).
- Daniel Bristot de Oliveira et al., *"Demystifying the Real-Time Linux Scheduling Latency"* (ECRTS 2020) and the osnoise/timerlat design write-ups (Linux Plumbers 2021; LWN, *"Finding the source of OS noise"*) — the model of latency as the sum of attributable intervals that `osnoise`/`timerlat` implement (§55.3).
- Steven Rostedt, `trace-cmd` and KernelShark documentation and talks (*"Finding latency with ftrace"*) — recording per-CPU event streams and reading them as a timeline (§55.4.2).
- `perf sched` documentation (`perf-sched(1)`: `timehist`, `latency`) — who-ran-when on a CPU (§55.4.2).
- Intel SDM Vol. 4, `MSR_SMI_COUNT` (0x34); `turbostat(8)` — counting SMIs (§55.3, §55.4.3; Ch. 6 §6.4.4).
- Ch. 53 (the configuration this chapter verifies), Ch. 50 (IRQ affinity, managed vectors), Ch. 6 §6.4.4 (SMIs, clocksource watchdog, `hwlat`), Ch. 19 (the TSC as the instrument), Ch. 16/28–29/31 (the memory-management sources of TLB shootdowns).

## 55.8 Additional Reading

- Brendan Gregg, *Systems Performance* (2nd ed.), the CPU and tracing chapters — ftrace, perf and BPF as a jitter-attribution toolkit; and the `perf-tools`/BCC `runqlat`, `hardirqs`, `softirqs` tools (Ch. 73).
- The Red Hat Enterprise Linux for Real Time *Tuning Guide* and *Reference Guide* (the `rtla`, `trace-cmd`, `hwlat`, `tuna` chapters) — a vendor-maintained version of the hunt.
- Frederic Weisbecker's `nohz_full` and CPU-isolation work (LWN and Linux Plumbers series on "housekeeping" and `isolcpus` flags) — why each residual source exists and the road-map of knobs.
- Ch. 56 (*Keeping the Hot Path Warm*) — the next Part VII problem once the core is quiet and attributed; Ch. 73 (*eBPF/bpftrace*) — programmable tracepoints and the cost of arming them; Ch. 103 (*Host Health Telemetry*) — this chapter's audit and interrupt diffs run continuously in production; Ch. 108 (*Case Study*) — where a residual spike shows up as a p99.99 outlier in the tick-to-trade decomposition.
- **Appendix C.5** (verification pass — points here for "hunt residual jitter"), **Appendix I.6** (the `rtla`/ftrace/`perf sched` one-liners), **Appendix E** (the cost of the interrupt, IPI and context switch you are hunting), **Appendix F** (osnoise / timerlat / hwlat / IPI / shootdown glossary).

---

*Next: Ch. 56 — Keeping the Hot Path Warm, the final piece of the isolation story: once you've built a quiet, pinned, cache-partitioned, isolated core and proven nothing else runs on it (Ch. 48–55), the remaining enemy is *coldness* — the first real message after a quiet spell hits cold caches, TLBs, and predictors. How to keep the hot path warm with shadow traffic, pre-touching, and warm-up so the message that matters is never the cold one.*
