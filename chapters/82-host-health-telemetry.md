# Part XII — Observability & Operations in Production

# Chapter 82 — Host Health Telemetry: Proving the Quiet Box Stayed Quiet

> **Prerequisites:** Ch. 42, 45, 46 (pinning, isolation, the residual-noise catalog — the invariants this chapter watches continuously), Ch. 6 §6.4.4 (SMIs and the clocksource watchdog), Ch. 15–16 (THP/compaction, NUMA balancing — the memory-system knobs that drift), Ch. 44 §44.4.2 (CMT/MBM — the one host monitor the book already runs), Ch. 75 (the telemetry segment and the off-core scraper this chapter's sampler feeds), Ch. 41 (what a context switch or page fault costs the hot thread), Ch. 1.
>
> **Leads into:** Ch. 83 (network health — the NIC and stack counters sampled by the same scraper), Ch. 84 (the case study — where a drifted knob shows up as a tail), Appendix C.6 and Appendix M (configuration as code, the baseline fingerprint and the pre-open gate that consume this chapter's signals). Ch. 42–46 verified the box was quiet *once, at setup*; this chapter makes "still quiet" a number that is sampled every second and compared against a recorded baseline — and shows how to sample it without the sampler becoming the noise.

---

## 82.1 Why it matters: the quiet box decays

Every chapter in Part VII ends the same way: tune, measure, sign off. Ch. 45's `cyclictest` max was 2 µs on the day the box was built; Ch. 46 attributed the last spike and recorded a baseline; Appendix C.5 says "re-verify after every change." Then the box runs for eleven months. A kernel security update ships a new `tuned` profile that re-enables `irqbalance`; the NIC vendor's driver update resets the queue count and a managed vector lands on core 5; a monitoring agent is upgraded and its new version attaches a `perf_event` to every CPU "for CPU utilisation"; the BMC firmware is updated and the SMI rate goes from zero to four a minute; a fan fails and core 5 throttles from 2.9 to 2.1 GHz every afternoon at 14:00 when the room warms; someone sets `kernel.numa_balancing=1` to fix a database on a different box with the same Ansible role. None of these produces an error. All of them produce a tail. Ch. 45 §45.5 and Ch. 84 §84.5 both name the failure — *tuning once and walking away* — and both point at "continuous monitoring" as the answer. Until now that monitor has existed for two signals: Ch. 44's cache occupancy and Ch. 60's PTP offset. Everything else was a one-off check.

The reason it was a one-off is that the checks *look* operational rather than architectural — a grep of `/proc/interrupts`, a `ps` by core — and so they are left to runbooks. But the thing being protected is the architecture: the isolated core is an assumption every hot-path chapter's measurement was made under, and when it silently stops being true, every number in the book is wrong for that box. The signals that prove it is still true are cheap, already counted by the kernel, and *binary* in the sense that matters: a hot thread's `nonvoluntary_ctxt_switches` after warm-up should be zero and stay zero; the `CAL` column of its CPU should not move; its `minflt` should not move; its `processor` field should not change. These are not percentiles to be budgeted, they are **invariants**, and the alert semantics are the simplest in the book — *any nonzero delta is an incident* (Ch. 75 §75.4.4) — with a time-stamped fingerprint of which knob drifted.

This chapter turns Ch. 42–46's setup-time checks into a sampled, continuous signal set with a cost model for the sampler itself. §82.2 organises the signals: the isolation invariants, scheduler and memory-system noise, frequency/thermal/power and SMIs, the clock, and the hardware layer below (ECC, PCIe), each with what "healthy" reads and what a delta means; and it names the trap — that the obvious way to read several of them (an MSR, a perf counter, a `cpufreq` sysfs file) *interrupts the core you are protecting*. §82.3 measures the sampler's own footprint, the right way and the wrong way, and tabulates the fingerprints of the drift incidents above. §82.4 builds the sampler (a 110-line C++ program reading `/proc` and `/sys` from a housekeeping core), the frequency/thermal/SMI readers, the memory/clock/hardware signals, and the baseline-and-drift audit that Appendix M's gate runs every morning; §82.5 and §82.6 are the pitfalls, exercises and checklist. Ch. 83 does the same for the NIC and the network stack.

## 82.2 Mental model: isolation as a live invariant

### 82.2.1 Invariants, not thresholds

Application telemetry (Ch. 75) is mostly *distributions* — the p99.9 of a stage, budgeted per session phase. Host health is mostly *invariants*: quantities whose correct value on a properly tuned, warmed-up box is exactly zero, or exactly the baseline, and whose first deviation is the event. That changes everything about how they are sampled and alerted:

```
   signal (per hot CPU or hot thread)            healthy reads          a delta means                            sampled from (never from the hot core)
   ───────────────────────────────────────────── ────────────────────── ─────────────────────────────────────── ────────────────────────────────────────
   nonvoluntary_ctxt_switches                    0 after warm-up        something preempted the hot thread       /proc/<tid>/status
   voluntary_ctxt_switches                       0 (busy-poll design)   the hot thread BLOCKED: a syscall slipped /proc/<tid>/status
   processor (current CPU)                       constant               it migrated: affinity lost/overridden    /proc/<tid>/stat field 39
   minflt / majflt                               0 after warm-up        a page fault: mlockall/prefault undone   /proc/<tid>/stat fields 10/12
   /proc/interrupts LOC on the CPU               ~1/s (residual tick)   ~HZ/s: the tick is back (Ch. 45 §45.5)   /proc/interrupts
   RES / CAL / TLB / IWI on the CPU              0                      an IPI: Ch. 46's catalog, by row         /proc/interrupts
   NMI / PMI on the CPU                          0                      nmi_watchdog or perf sampling returned   /proc/interrupts
   device IRQ rows on the CPU                    0                      a vector moved: irqbalance, driver reset  /proc/interrupts (+ effective_affinity)
   /proc/softirqs columns on the CPU             0 (RCU ≤ a few/s)      softirq work landed: net RX, timers       /proc/softirqs
   tasks with the CPU in Cpus_allowed            your thread + kthreads a stray process/agent may run there       /proc/*/task/*/status
   core_throttle_count / package_throttle_count  constant               thermal throttling: frequency dropped     /sys/.../thermal_throttle/
   effective frequency (APERF/MPERF)             = base (turbo off)     P-state drift, throttle, power cap        turbostat --cpu <housekeeping>, RAPL
   MSR_SMI_COUNT (0x34)                          constant               firmware took the core (Ch. 6 §6.4.4)     rdmsr -p <housekeeping cpu> 0x34
   current_clocksource                           tsc                    watchdog demoted the TSC (Ch. 6 §6.4.4)   /sys/devices/system/clocksource/
   thp_*, compact_stall, numa_hint_faults        constant               THP/compaction/NUMA balancing woke up     /proc/vmstat
   numastat remote pages for the hot PID         constant               memory migrated off-node (Ch. 16)         /sys/fs/cgroup or numastat -p
   EDAC ce_count / ue_count; PCIe AER counters   constant               DIMM or link degrading (CMCI storms SMI)  /sys/devices/system/edac/, /sys/bus/pci/
   /proc/pressure/{cpu,memory,io}                ~0 on housekeeping     the housekeeping side is saturating       /proc/pressure/ (PSI)
   provenance: cmdline, microcode, BIOS, NIC fw  = baseline hash        something was updated under you           /proc/cmdline, /sys/devices/system/cpu/…/microcode, dmidecode
```

Two properties of this table make the design: every row is **already counted by the kernel or firmware** (the sampler adds no instrumentation to the hot path — it reads about it), and every row is **readable from another core** without touching the hot one — *if you read it the right way*, which is the subject of §82.2.4.

### 82.2.2 The signal classes and the chapter that owns each

- **Isolation integrity** (Ch. 42, 45, 46). The first six rows: the hot thread's own switch/fault/migration counters and its CPU's interrupt and softirq columns. These are the Ch. 46 §46.4.1 audit, diffed every second instead of once. They detect every row of the Ch. 46 catalog *as it recurs*, and they are the first thing to look at after any latency incident: a tail with zero deltas here is an application or network problem; a tail with a `CAL +3` is the kernel.
- **Scheduler noise on the housekeeping side** (Ch. 41, 45). PSI (`/proc/pressure/cpu`) and the run-queue length of the housekeeping cores. A saturated housekeeping side does not directly touch the isolated core, but it delays the logger consumer (Ch. 73), the scraper (Ch. 75), the capture writer (Ch. 80) and the supervisor's liveness check (Ch. 78) — rings fill, heartbeats look late, and IPIs from housekeeping work rise.
- **Memory system** (Ch. 15, 16, 23, 26). `/proc/vmstat`'s THP, compaction and NUMA-balancing counters: a box tuned with THP `never` and `numa_balancing=0` has flat counters; the day they move, a knob drifted. The hot thread's `minflt` after warm-up is the per-thread view of the same failure. `numastat -p <pid>` remote-node growth means memory is being allocated or migrated off the NIC's node (Ch. 16).
- **Frequency, thermal and power** (Ch. 6). A core's *effective* frequency (`APERF/MPERF` — what it actually ran at, as opposed to the governor's request) and the `thermal_throttle` event counts. A throttled core runs every stage of the pipeline 20–40 % slower — the whole latency histogram shifts right with no application cause, which is exactly the signature that sends engineers hunting in the wrong place. RAPL power-cap events have the same effect.
- **Firmware** (Ch. 6 §6.4.4, Ch. 46). `MSR_SMI_COUNT`. It is the one signal with no kernel counter and the one whose correct reading technique matters most (§82.2.4).
- **The clock** (Ch. 6 §6.4.4, Ch. 17, Ch. 60). `current_clocksource` — a demotion from `tsc` to `hpet` turns every `clock_gettime` on the box into a ~500 ns MMIO read and invalidates every software timestamp's resolution at once; the kernel log line that announces it (`clocksource: timekeeping watchdog ... Marking clocksource 'tsc' as unstable`) is the only other trace. PTP offset and servo state (Ch. 60 §60.4.1) belong to the same class and are already monitored there.
- **Hardware below the OS.** Corrected-ECC error counts (EDAC), machine-check events (MCE), PCIe Advanced Error Reporting counters and link width/speed (Ch. 67 §67.5). Corrected errors cost nothing individually; a *storm* of them does — each is handled by a CMCI or, in firmware-first mode, an SMI — and a rising correctable count is the earliest warning of a DIMM or optic about to fail. The monitoring is a counter diff; the deeper RAS treatment (patrol scrub, CMCI thresholds, link retraining) is beyond this chapter and noted as such.
- **Provenance.** Kernel version and cmdline, microcode revision, BIOS version, NIC firmware, `tuned` profile, the sysctl set — hashed and compared against the baseline recorded at sign-off (Appendix M). Not a counter, but the thing that explains why a counter started moving on a Tuesday.

### 82.2.3 Drift: how a tuned box untunes itself

Drift has a small number of mechanisms, and knowing them tells you where to put the provenance checks: **package and kernel updates** (a new kernel changes defaults — `CONFIG_HZ`, isolation semantics, RT throttling vs the deadline server, Ch. 45; a new `tuned` or `systemd` re-applies a profile; a new driver resets `ethtool` settings and queue counts); **configuration management applying someone else's intent** (an Ansible/Salt role shared with a database fleet flips `numa_balancing`, THP, `swappiness`); **agents** (monitoring, security and backup agents that pin nothing, attach perf events, scan memory, or run `rdmsr` on every CPU — the §82.3 experiment); **firmware** (BIOS/BMC updates that reset C-state, SMI and power-cap settings — Appendix C.1 is firmware, and firmware is updated); and **the environment** (temperature → throttling; a failed fan; a DIMM aging into correctable errors). The fix for the first two classes is Appendix C.6's *configuration as code with a drift audit*; the fix for the others is this chapter's sampler, because they cannot be prevented, only detected.

### 82.2.4 The sampler's own footprint

The dangerous idea in host monitoring is that *reading* is free. Three common readers are not, and all three target the isolated core directly:

- **MSRs.** `rdmsr -p 5 0x34` opens `/dev/cpu/5/msr`; the kernel executes the read *on CPU 5* via `smp_call_function_single` — a `CAL` IPI to the hot core for every sample. `turbostat` by default reads a dozen MSRs on *every* CPU per interval (`APERF`, `MPERF`, `SMI_COUNT`, C-state residency, temperature) — a burst of IPIs to every isolated core, once a second, from the tool you deployed to detect IPIs. Package-scope quantities (SMI count — SMIs are broadcast; package temperature; RAPL energy) read identically from *any* core in the package, so read them from a housekeeping CPU (`rdmsr -p 0`, `turbostat --cpu 0-1`); per-core quantities you genuinely need from the hot core (its own `APERF/MPERF`) are read by the **hot thread itself** via `rdpmc`/the kernel's tick-free `arch_freq` path, or inferred from the application's own TSC-stamped throughput (Ch. 17).
- **perf events.** `perf stat -C 5 -e cycles` installs a counter on CPU 5: no sampling NMIs, but the event is a **tick dependency** (`timer:tick_stop` fails with `PERF_EVENTS`, Ch. 46 §46.4.3) — the monitor brings the 1 kHz tick back to the core it monitors. Any `perf record` on the core adds a PMI per sample. The hot core's PMU is read by the hot thread (`rdpmc`, user-space enabled) or not at all.
- **`cpufreq` sysfs files.** `scaling_cur_freq`/`cpuinfo_cur_freq` for CPU 5 may, depending on kernel and driver, resolve through a cross-call to CPU 5 to sample `APERF/MPERF`; on tick-free CPUs recent kernels return a stale or zero value instead. Treat these files as untrustworthy on isolated cores and get the effective frequency from `turbostat --cpu <housekeeping>` for the package and from the hot thread's own measurement for the core.

Everything in `/proc` and the rest of `/sys` is read from kernel data structures the *target* CPU already updates for its own purposes — `/proc/<tid>/status`, `/proc/interrupts`, `/proc/vmstat`, `thermal_throttle`, `clocksource` — with no cross-call. A sampler that confines itself to those, runs on a housekeeping core, and reads package-scope MSRs from a housekeeping CPU has a footprint on the hot core of exactly zero, and §82.3 proves it with the sampler's own `CAL` row. The model: **the quiet box is a set of kernel-counted invariants; sample them from a housekeeping core through `/proc` and `/sys`, never through an MSR, perf event or cpufreq read aimed at the hot CPU; alert on the first nonzero delta with the drifted knob's fingerprint; and compare provenance against the sign-off baseline so the delta has a cause.**

## 82.3 Measure it: the sampler's footprint and the drift fingerprints

*Toolchain and machine: Appendix L.3.*

**The sampler's cost, right way vs wrong way.** Run the §82.4.1 sampler (`hostwatch`, pinned to housekeeping CPU 0, 1 s period) against a hot thread on isolated CPU 5, and measure two things: the sampler's own CPU time per sample, and — the number that matters — the deltas the sampler *itself* induces on CPU 5, read by a second instance of the same sampler. Then replace it with the three wrong readers. Representative, reference box (Xeon Gold 6326, Linux 6.18, 32 CPUs, 8 hot threads):

```
   sampler (1 s period, from CPU 0)                        housekeeping CPU time/sample   induced on hot CPU 5 per second     hot-thread p99.9 shift
   hostwatch: /proc/interrupts + /proc/softirqs            ~310 µs (two ~70 KiB files)     LOC 0  RES 0  CAL 0  TLB 0  PMI 0    none
     + 8 × /proc/<tid>/status,stat + /proc/vmstat          ~ 90 µs                          0                                    none
     + thermal_throttle, clocksource, PSI, EDAC sysfs      ~ 40 µs                          0                                    none
   rdmsr -a 0x34 (all CPUs)                                ~ 1 ms                            CAL +1 per sample                    +~3 µs on 1 message/s
   turbostat --interval 1 (default: every CPU)             ~ 8 ms                            CAL +12 per sample                   +~3 µs on ~12 msgs/s
   perf stat -C 5 -e cycles,instructions (counting)        —                                LOC +1,000/s (tick dependency!)      p99.9 +0.4 µs, p99.99 +2 µs
   cat /sys/.../cpu5/cpufreq/scaling_cur_freq (1 Hz)       —                                CAL +1 per read on this kernel/driver +~3 µs on 1 message/s
   turbostat --cpu 0-1 --interval 1 (housekeeping only)    ~ 1 ms                            0                                    none
```

The right-way rows cost under half a millisecond of a housekeeping core per second and induce *nothing* on the hot core — the sampler's `CAL`/`TLB`/`PMI` rows for CPU 5 stay at zero while it runs, which is the proof. The wrong-way rows each show the monitor becoming the incident: one cross-call per sample (~3 µs stolen from whichever message was in flight — a p99.99 event you installed), or, for `perf stat`, the tick restored to 1 kHz for as long as the counter is attached (Ch. 46 §46.3's `IRQ 1,000` column, reproduced by a monitoring tool).

**Drift fingerprints.** The second measurement is a catalogue of *what each drift looks like* in the sampler's output — collected by inducing each one on the reference box and recording the first-second deltas. It is the host-health counterpart of Ch. 46 §46.2's noise catalog, and it is what turns an alert into a diagnosis:

```
   drift                                            first delta the sampler reports                              latency signature (hot thread)
   ───────────────────────────────────────────────  ──────────────────────────────────────────────────────────── ──────────────────────────────────────
   irqbalance re-enabled by a tuned/package update  a NIC/NVMe IRQ row on cpu5 +N/s; effective_affinity changed   p99.9 +5–20 µs at the open (Ch. 42 §42.3)
   driver update reset queue count (managed vector) one `eth0-TxRx-7` row on cpu5 appears and counts              as above, every packet batch
   agent attached a perf event to every CPU          LOC +1,000/s on every isolated CPU; PMI 0 (counting mode)     p99.9 +0.4 µs, p99.99 +2 µs (the tick)
   agent runs rdmsr/turbostat on all CPUs            CAL +k/s on every isolated CPU, k = MSRs read per interval     ~3 µs per cross-call, 1 msg each
   numa_balancing flipped to 1                        vmstat numa_hint_faults +; hot tid minflt +; later migrations  periodic 1–5 µs faults; remote DRAM
   THP flipped to always / defrag                      vmstat thp_fault_alloc, compact_stall +; kcompactd time +; TLB IPIs on hot CPUs   10–100 µs stalls
   kernel.sched_rt_runtime_us reset to 950000 (pre-6.12) nonvoluntary_ctxt_switches +1/s on the RT hot thread       a 50 ms stall once a second (Ch. 45 §45.5)
   stray process/agent allowed on the hot CPU         Cpus_allowed audit lists a new comm; RES +; nonvol +         µs–ms preemptions
   thermal throttle (fan, room temperature)           core_throttle_count +; Bzy_MHz 2900 -> ~2100 (turbostat --cpu 0-1 shows package)  every stage ×1.3–1.4
   BIOS/BMC update re-enabled SMIs                    MSR_SMI_COUNT +4/min from cpu0; osnoise HW > 0 (Ch. 46)       50–200 µs gaps, no kernel trace
   clocksource demoted                                current_clocksource tsc -> hpet; kmsg "Marking clocksource 'tsc' as unstable"   every clock_gettime +~500 ns
   DIMM aging                                          EDAC ce_count +; in firmware-first mode MSR_SMI_COUNT + too   SMI-shaped gaps, rising
```

Read the two tables together: the sampler is free when it reads `/proc` and `/sys` from a housekeeping core; and every drift in the catalogue announces itself in the first second through a counter the kernel already maintains — before, or at worst concurrently with, the first latency spike. (A `cyclictest` or an application histogram tells you *that* the box got noisy; this table tells you *which knob*.)

## 82.4 Techniques

### 82.4.1 Isolation integrity: the per-second diff

The sampler is the Ch. 46 §46.4.1 audit made continuous. It reads the interrupt and softirq tables, the hot threads' status and stat files, the vmstat counters, the thermal and clocksource files — every one a `/proc` or `/sys` read with no cross-call — diffs against the previous sample and prints (in production: publishes to the Ch. 75 segment) every delta that violates an invariant:

```cpp
// hostwatch.cpp — the host-health sampler: once a second, from a HOUSEKEEPING core, read what the kernel already counts
// about the isolated CPUs and the hot threads, diff against the previous sample, and report every delta that violates
// the quiet-box invariant.  Reads /proc and /sys only — no MSRs, no perf events, nothing that IPIs a hot core (§82.3).
// Build: g++ -O2 -std=c++23 hostwatch.cpp -o hostwatch   (GCC 13/14; Clang 18 same)
// Run:   taskset -c 0 ./hostwatch --cpus 2-7 --tids 4121,4122 [--period-ms 1000]
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>
#include <unistd.h>

namespace {
using u64 = std::uint64_t;
std::vector<int> parse_list(std::string_view s) {                 // "2-7,9" -> {2..7,9}
    std::vector<int> out;
    while (!s.empty()) {
        auto c = s.find(','); auto tok = s.substr(0, c); s = c == s.npos ? "" : s.substr(c + 1);
        int a = 0, b = 0; auto d = tok.find('-');
        std::from_chars(tok.data(), tok.data() + (d == tok.npos ? tok.size() : d), a);
        if (d == tok.npos) b = a; else std::from_chars(tok.data() + d + 1, tok.data() + tok.size(), b);
        for (int i = a; i <= b; ++i) out.push_back(i);
    }
    return out;
}
std::string slurp(const std::string& path) { std::ifstream f(path); std::stringstream ss; ss << f.rdbuf(); return ss.str(); }
u64 to_u64(std::string_view s) { u64 v = 0; std::from_chars(s.data(), s.data() + s.size(), v); return v; }

// /proc/interrupts and /proc/softirqs share a shape: a CPU header row, then "NAME: n n n ... description"
std::map<std::string, std::vector<u64>> per_cpu_table(const std::string& text, int ncpu_cols) {
    std::map<std::string, std::vector<u64>> t; std::istringstream in(text); std::string line; std::getline(in, line);   // header
    while (std::getline(in, line)) {
        std::istringstream ls(line); std::string name; ls >> name; if (name.empty()) continue; name.pop_back();  // drop ':'
        std::vector<u64> v; std::string tok;
        for (int i = 0; i < ncpu_cols && ls >> tok; ++i) { u64 x; if (std::from_chars(tok.data(), tok.data() + tok.size(), x).ec != std::errc{}) break; v.push_back(x); }
        std::string rest; std::getline(ls, rest); if (!rest.empty()) name += rest;    // keep the device description for IRQ rows
        t[name] = std::move(v);
    }
    return t;
}
struct ThreadSample { u64 vol = 0, nonvol = 0, minflt = 0, majflt = 0; int cpu = -1; std::string allowed; };
ThreadSample thread_sample(int tid) {
    ThreadSample s; const std::string base = "/proc/" + std::to_string(tid) + "/";
    std::istringstream st(slurp(base + "status")); std::string line;
    while (std::getline(st, line)) {
        if (line.starts_with("voluntary_ctxt_switches:")) s.vol = to_u64(std::string_view(line).substr(24));
        else if (line.starts_with("nonvoluntary_ctxt_switches:")) s.nonvol = to_u64(std::string_view(line).substr(27));
        else if (line.starts_with("Cpus_allowed_list:")) s.allowed = line.substr(19);
    }
    const std::string stat = slurp(base + "stat"); const auto rp = stat.rfind(')');   // fields after "(comm)": field 3 = state
    std::istringstream ss(stat.substr(rp + 2)); std::string f; std::vector<std::string> fields;
    while (ss >> f) fields.push_back(f);                            // fields[0] = state (field 3) ... minflt = field 10, majflt = 12, processor = 39
    if (fields.size() > 36) { s.minflt = to_u64(fields[7]); s.majflt = to_u64(fields[9]); s.cpu = int(to_u64(fields[36])); }
    return s;
}
u64 vmstat(const std::string& text, std::string_view key) {
    auto p = text.find(key); if (p == text.npos) return 0; p += key.size(); return to_u64(std::string_view(text).substr(p + 1, 24));
}
u64 read_u64(const std::string& path) { return to_u64(slurp(path)); }
}  // namespace

int main(int argc, char** argv) {
    std::vector<int> cpus, tids; int period_ms = 1000;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (!std::strcmp(argv[i], "--cpus")) cpus = parse_list(argv[i + 1]);
        if (!std::strcmp(argv[i], "--tids")) tids = parse_list(argv[i + 1]);
        if (!std::strcmp(argv[i], "--period-ms")) period_ms = int(to_u64(argv[i + 1]));
    }
    if (cpus.empty()) { std::fprintf(stderr, "usage: %s --cpus 2-7 [--tids a,b] [--period-ms N]\n", argv[0]); return 2; }
    const int ncpu = int(sysconf(_SC_NPROCESSORS_CONF));
    const std::string clocksrc0 = slurp("/sys/devices/system/clocksource/clocksource0/current_clocksource");
    auto irq_prev = per_cpu_table(slurp("/proc/interrupts"), ncpu), si_prev = per_cpu_table(slurp("/proc/softirqs"), ncpu);
    std::string vm_prev = slurp("/proc/vmstat");
    std::map<int, ThreadSample> th_prev; for (int t : tids) th_prev[t] = thread_sample(t);
    std::map<int, u64> throttle_prev; for (int c : cpus) throttle_prev[c] = read_u64("/sys/devices/system/cpu/cpu" + std::to_string(c) + "/thermal_throttle/core_throttle_count");
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    for (int n = 1;; ++n) {
        usleep(period_ms * 1000);
        // 1. isolation integrity: interrupt and softirq deltas on the hot CPUs. Invariant: LOC <= ~1/s; everything else 0.
        auto irq = per_cpu_table(slurp("/proc/interrupts"), ncpu), si = per_cpu_table(slurp("/proc/softirqs"), ncpu);
        for (int c : cpus) {
            for (auto& [name, v] : irq) {
                auto p = irq_prev.find(name); if (p == irq_prev.end() || v.size() <= size_t(c) || p->second.size() <= size_t(c)) continue;
                const u64 d = v[c] - p->second[c]; if (!d) continue;
                const bool is_loc = name.starts_with("LOC");
                if (is_loc && d <= 2) continue;                                     // the residual 1 Hz tick: expected
                std::printf("[%d] cpu%d %s +%llu %s\n", n, c, name.substr(0, 40).c_str(), (unsigned long long)d,
                            is_loc ? "<- TICK IS RUNNING (2nd task? perf? posix timer?) Ch. 45/46" : "<- INTRUSION: see the Ch. 46 catalog");
            }
            for (auto& [name, v] : si) { auto p = si_prev.find(name); if (p == si_prev.end() || v.size() <= size_t(c)) continue;
                const u64 d = v[c] - p->second[c]; if (d) std::printf("[%d] cpu%d softirq %s +%llu\n", n, c, name.c_str(), (unsigned long long)d); }
            const u64 thr = read_u64("/sys/devices/system/cpu/cpu" + std::to_string(c) + "/thermal_throttle/core_throttle_count");
            if (thr != throttle_prev[c]) { std::printf("[%d] cpu%d THERMAL THROTTLE events +%llu\n", n, c, (unsigned long long)(thr - throttle_prev[c])); throttle_prev[c] = thr; }
        }
        irq_prev = std::move(irq); si_prev = std::move(si);
        // 2. hot threads: involuntary switches, migrations, faults. Invariant after warm-up: all deltas 0, cpu constant.
        for (int t : tids) {
            ThreadSample s = thread_sample(t); const ThreadSample& p = th_prev[t];
            if (s.nonvol != p.nonvol) std::printf("[%d] tid %d nonvoluntary_ctxt_switches +%llu <- PREEMPTED on an isolated core\n", n, t, (unsigned long long)(s.nonvol - p.nonvol));
            if (s.vol != p.vol) std::printf("[%d] tid %d voluntary_ctxt_switches +%llu <- it BLOCKED (a syscall on the hot path? Ch. 41)\n", n, t, (unsigned long long)(s.vol - p.vol));
            if (s.minflt != p.minflt) std::printf("[%d] tid %d minor faults +%llu <- page fault after warm-up (mlockall/prefault, Ch. 26, 47)\n", n, t, (unsigned long long)(s.minflt - p.minflt));
            if (s.majflt != p.majflt) std::printf("[%d] tid %d MAJOR faults +%llu\n", n, t, (unsigned long long)(s.majflt - p.majflt));
            if (s.cpu != p.cpu) std::printf("[%d] tid %d MIGRATED cpu%d -> cpu%d (allowed %s)\n", n, t, p.cpu, s.cpu, s.allowed.c_str());
            th_prev[t] = std::move(s);
        }
        // 3. memory-system and clock health (host-wide counters; any growth is a design-time knob that drifted)
        const std::string vm = slurp("/proc/vmstat");
        for (std::string_view k : {"thp_fault_alloc", "thp_collapse_alloc", "compact_stall", "numa_hint_faults", "numa_pages_migrated", "pgmigrate_success"}) {
            const u64 d = vmstat(vm, k) - vmstat(vm_prev, k); if (d) std::printf("[%d] vmstat %.*s +%llu <- THP/compaction/NUMA balancing active (Ch. 15, 16)\n", n, int(k.size()), k.data(), (unsigned long long)d);
        }
        vm_prev = vm;
        const std::string cs = slurp("/sys/devices/system/clocksource/clocksource0/current_clocksource");
        if (cs != clocksrc0) std::printf("[%d] CLOCKSOURCE changed: %s -> %s <- TSC demoted by the watchdog (Ch. 6 §6.4.4)\n", n, clocksrc0.c_str(), cs.c_str());
        if (n % 10 == 0) std::printf("[%d] ok: %zu hot cpus, %zu hot threads quiet\n", n, cpus.size(), tids.size());
    }
}
```

Built with GCC 13.3 and Clang 18.1. On the authoring host — a stock 4-vCPU VM, `HZ=250`, nothing isolated — pointed at a spinner pinned to CPU 3, the first seconds read:

```
   [1] cpu3 LOC   Local timer interrupts +252 <- TICK IS RUNNING (2nd task? perf? posix timer?) Ch. 45/46
   [1] cpu3 softirq RCU +10
   [1] cpu3 softirq TIMER +21
   [3] cpu3 CAL   Function call interrupts +1 <- INTRUSION: see the Ch. 46 catalog
   [3] cpu3 TLB   TLB shootdowns +1 <- INTRUSION: see the Ch. 46 catalog
   [4] cpu3 RES   Rescheduling interrupts +1 <- INTRUSION: see the Ch. 46 catalog
```

— the stock-core row of Ch. 45 §45.3, described by cause rather than by `cyclictest` max. On the reference box's signed-off core the same program prints `ok` lines and nothing else for the whole session, and the first non-`ok` line is the incident. Production differences from the listing: the deltas are *published* (counters into a `HostStats` block of the Ch. 75 segment, one line per hot CPU, scraped and exported with the application's metrics) rather than printed; the TID list comes from the segment's per-thread `cpu`/TID gauges rather than the command line; and the `Cpus_allowed` audit of Ch. 46 §46.4.1 runs once a minute (it walks every task) rather than every second.

### 82.4.2 Frequency, thermal, power and SMIs — read from the right core

- **Effective frequency.** `turbostat --quiet --show Core,CPU,Busy%,Bzy_MHz,TSC_MHz,SMI,CoreTmp,PkgTmp,PkgWatt --interval 5 --cpu 0-1` restricts turbostat to the housekeeping CPUs: `Bzy_MHz` for those cores, package temperature, power and SMI count (package-scope — broadcast SMIs are counted identically on every core) *without* IPI-ing the isolated ones. The hot core's own `Bzy_MHz` comes from the hot thread: its message-processing loop already knows cycles per message (TSC, Ch. 17), and a per-thread gauge of *TSC cycles per fixed work unit* (a shadow-traffic message, Ch. 47) rises by exactly the throttle ratio when the core slows. Alternatively the hot thread samples `rdpmc` on `APERF`-equivalent fixed counters if user-space `rdpmc` is enabled (`/sys/bus/event_source/devices/cpu/rdpmc`) — a read, not an IPI.
- **Thermal and power events.** `/sys/devices/system/cpu/cpuN/thermal_throttle/{core,package}_throttle_count` (counts, not state — diff them), `/sys/class/thermal/thermal_zone*/temp`, and RAPL via `/sys/class/powercap/intel-rapl:0/energy_uj` for package power and `constraint_0_power_limit_uw` for the cap. A box pinned at base frequency with turbo off (Appendix L.3) should show a flat `Bzy_MHz` and zero throttle deltas all day; the first `core_throttle_count` increment is a facilities ticket as much as an engineering one.
- **SMIs.** `rdmsr -p 0 0x34` from the housekeeping CPU once a second (or turbostat's `SMI` column, same restriction). A nonzero delta is correlated against the application's per-stage histograms for the same second (Ch. 75) and against `osnoise`'s `HW` column when a hunt is running (Ch. 46): an SMI that coincides with a 150 µs gap in every hot thread at once is attributed, and the fix is Appendix C.1. Record the count in the baseline; the pre-open gate (Appendix M) refuses to arm if it moved overnight.
- **C-state residency.** `/sys/devices/system/cpu/cpuN/cpuidle/state*/usage` and `time` for the hot CPUs — plain sysfs reads of per-CPU counters the idle governor already maintains. A busy-polling core should show zero deltas in every state deeper than C0/C1; a delta means the thread *slept* (a blocking syscall slipped in, Ch. 41) or the core was stolen long enough to idle.

### 82.4.3 Memory system, clock and hardware health

- **THP, compaction, NUMA balancing.** The `/proc/vmstat` rows in the listing: `thp_fault_alloc`, `thp_collapse_alloc`, `compact_stall`, `compact_migrate_scanned`, `numa_hint_faults`, `numa_pages_migrated`, `pgmigrate_success`. On a box configured per Appendix C (THP `never`/`madvise` with pre-faulting, `numa_balancing=0`) they are flat; movement is drift. Add `khugepaged`'s and `kcompactd0`'s own CPU time (`/proc/<pid>/stat` utime+stime) — the daemons running at all is the signal, and their TLB-shootdown IPIs show in the hot CPUs' `TLB` rows (Ch. 46 §46.4.3).
- **Per-process NUMA placement.** `numastat -p <pid>` (or `/proc/<pid>/numa_maps` summed) once a minute: the hot process's pages should all be on the NIC's node (Ch. 16); growth on the remote node after warm-up means an allocation path or a migration you did not intend.
- **PSI.** `/proc/pressure/cpu`, `memory`, `io` — the `some`/`full` averages for the housekeeping side (the hot cores, with one task each, contribute nothing). Rising CPU pressure means the housekeeping cores are oversubscribed (Ch. 42 §42.5) and the consumers of the hot threads' rings will fall behind; rising IO pressure means the capture writer (Ch. 80) is stalling on the disk.
- **Clocksource and the kernel log.** `current_clocksource` is in the listing; pair it with a `/dev/kmsg` reader (or `journalctl -k -f`) that matches `clocksource|tsc|unstable|watchdog|mce|edac|aer|throttl|nohz|isolcpus` — the kernel announces every host-health event in this chapter in its log, usually with the cause, and a log tail on a housekeeping core is free. PTP offset and servo state join here from Ch. 60 §60.4.1.
- **Hardware (RAS).** `/sys/devices/system/edac/mc/mc*/ce_count` and `ue_count` (corrected/uncorrected memory errors, per controller and per DIMM under `csrow*`/`dimm*`), `/sys/devices/system/machinecheck/machinecheck*/` and `rasdaemon`'s database for MCE/CMCI events, and for every hot-path PCIe device (NIC, FPGA — Ch. 67) `/sys/bus/pci/devices/<bdf>/aer_dev_correctable`, `aer_dev_nonfatal`, `current_link_speed`, `current_link_width`. All are counters or states; all diff to zero on a healthy box. A correctable-ECC rate that goes from 0 to tens per hour is a DIMM to replace before it produces an uncorrectable error (a machine check and a crash, Ch. 78) and, in firmware-first platforms, before each CMCI becomes an SMI (§82.3's last row). A NIC whose link retrained from Gen4 x16 to Gen3 x8 has halved its bandwidth and raised its round-trip (Ch. 67) with no software change.

### 82.4.4 Baseline, drift audit, and wiring into the pipeline

- **Record the baseline at sign-off.** The Ch. 46 §46.4.3 sign-off produces a *fingerprint file*: `/proc/cmdline`, kernel release, microcode revision (`/sys/devices/system/cpu/cpu0/microcode/version`), BIOS/BMC versions (`dmidecode -s bios-version`), NIC firmware (`ethtool -i`), the `isolated`/`nohz_full` masks, `irqbalance`/`tuned` state, the full `sysctl -a` subset Appendix C names, the `ethtool -g/-c/-k/-l` settings per NIC, the `resctrl` schemata (Ch. 44), THP/governor/clocksource, and the *expected* IRQ affinity map — plus the steady-state interrupt-row rates and SMI count from the Ch. 46 run. Hash it; store it with the release (Appendix C.6).
- **Audit against it.** Every morning at T-60 (Appendix M) and on every boot, re-collect and `diff`: a changed hash is a *blocked* go-live until someone names the change. Continuously, the sampler compares the live `effective_affinity_list` map and the service states against the baseline once a minute — the cheap subset that catches the common drifts (`irqbalance` back, a vector moved, a profile re-applied) within a minute of their happening.
- **Publish, don't print.** In production the sampler writes into the Ch. 75 segment — a `HostStats` block with one line per hot CPU (interrupt-row deltas, throttle count, C-state deltas) and one per hot thread (switches, faults, migrations, CPU), plus host-wide gauges (SMI count, clocksource, PSI, EDAC, AER) — and the Ch. 75 scraper exports them beside the application's histograms, so a drift and the tail it caused sit on the same timeline. The alert class is "any nonzero delta" (Ch. 75 §75.4.4): one ticket, with the row name, the CPU/TID, and the fingerprint table's diagnosis attached.
- **Gate on it.** Appendix M's readiness gate reads the same block: it refuses to arm if any isolation counter moved since the box was warmed, if the SMI count moved overnight, if the clocksource is not `tsc`, if any throttle count moved, if the fingerprint hash differs — and it records the sampler's "quiet since" timestamp as the box's attestation for the day.

## 82.5 Pitfalls & anti-patterns

- **The monitor is the noise.** `rdmsr -a`, default `turbostat`, `perf stat -C <hot>`, `cpufreq` sysfs reads of the hot CPU, an agent that `perf_event_open`s every CPU — each IPIs or ticks the core it is meant to protect (§82.3). Read `/proc` and `/sys` from a housekeeping core; read package-scope MSRs from a housekeeping CPU; let the hot thread measure its own core.
- **The monitor on the SMT sibling.** A sampler pinned to the hot core's sibling competes for its front-end and L1 (Ch. 43); it is not "off the core". Housekeeping *physical* cores only.
- **Thresholds on invariants.** "Alert if `nonvoluntary_ctxt_switches` > 10/min" accepts ten preemptions a minute on a core that should see none. Invariants alert on the first delta; the threshold is zero and the window is irrelevant.
- **Monitoring the box without monitoring the monitor.** The sampler itself dies, or is OOM-killed, or stalls on a slow `/proc` read, and the dashboard shows the last good value forever. Its own heartbeat lives in the segment and the supervisor (Ch. 78) watches it like any role.
- **Sampling too fast.** `/proc/interrupts` at 100 Hz costs a housekeeping core 3 % and buys nothing: a drift is a step change, detected as well at 1 Hz. The sub-second signal is the hot thread's own gap detection (Ch. 46 §46.3), not the sampler.
- **Trusting `smp_affinity`, `scaling_cur_freq`, or `/proc/net/igmp`.** Each is a *request* or a *belief*, not the state: read `effective_affinity_list`, read the effective frequency from APERF/MPERF on the right core, read the switch's table (Ch. 56). The sampler must read what *is*, not what was asked for.
- **Warm-up noise treated as drift.** The first seconds after start-up legitimately fault pages, migrate once to the pinned core, and take IPIs while other threads start. The invariants hold *after* warm-up (Ch. 47); the sampler arms them when the application publishes `readiness = armed` in the segment, not at process start.
- **A baseline nobody updates.** A fingerprint recorded at build time and never re-recorded after a *deliberate* change (a planned kernel upgrade) alerts forever and is muted. Changing the baseline is part of the change's release procedure (Appendix C.6), signed off with a fresh Ch. 46 run.
- **Diagnosing the tail without the host signals.** A latency spike investigated in the application for a day, when the host sampler had logged `core_throttle_count +12` at the same second. The application histograms and the host deltas must be on the same timeline (Ch. 75 §75.4.3) and the incident procedure must read the host block first.
- **Deferring the hardware layer.** EDAC and AER counters are "the hardware team's problem" until an uncorrectable error takes the box down mid-session. They are two sysfs reads; include them.

## 82.6 Exercises & checklist

**Exercises**

1. **The sampler's footprint.** Run `hostwatch` from a housekeeping core against your hot threads for an hour and confirm its own `CAL`/`TLB`/`PMI` rows for the hot CPUs stay zero. Then run `rdmsr -a 0x34`, default `turbostat`, and `perf stat -C <hot> sleep 60` in turn, and watch the sampler report what each does to the hot core (§82.3). Finish with `turbostat --cpu <housekeeping>` and confirm it is clean.
2. **Induce every drift.** On a test box, apply each row of §82.3's fingerprint table (re-enable `irqbalance`; flip `numa_balancing`; set THP `always` and run a memory-churning process; attach a perf event; reset the NIC queue count; lower `sched_rt_runtime_us` on a pre-6.12 kernel) and record the sampler's first-second output and the application histogram's response. Build your own fingerprint table from the results.
3. **Throttle.** Pin a hot thread, then force throttling (lower the RAPL power cap via `powercap`, or `cpupower frequency-set -u` to a lower max) and measure the shift of every per-stage histogram (Ch. 75) against the `Bzy_MHz` and throttle-count deltas. Confirm the latency ratio equals the frequency ratio for the compute-bound stages and not for the memory-bound ones (Ch. 7).
4. **Clocksource demotion.** On a test box, `echo hpet > /sys/devices/system/clocksource/clocksource0/current_clocksource`; watch the sampler report it, and measure `clock_gettime` cost before and after (Ch. 17). Restore `tsc`; read the kernel log lines.
5. **Baseline and audit.** Write the fingerprint collector (§82.4.4) as a script; record a baseline; change one BIOS setting, one sysctl and one `ethtool` setting; run the audit and confirm all three are named. Then wire the audit into a pre-open gate that refuses on any diff (Appendix M).
6. **Publish into the segment.** Replace the listing's `printf`s with stores into a `HostStats` block of the Ch. 75 segment; export through the scraper; build the dashboard panel that overlays the hot thread's p99.9 with the host deltas on one timeline.

**Checklist — host health**

- [ ] A sampler on a **housekeeping physical core** reads, every second, **`/proc/interrupts` and `/proc/softirqs` for the hot CPUs**, **status/stat for each hot thread** (switches, faults, CPU), **`/proc/vmstat`** THP/compaction/NUMA rows, **thermal throttle counts**, **C-state usage**, **clocksource**, **PSI**, **EDAC/AER** — and nothing that cross-calls a hot CPU (§82.2.4, §82.3).
- [ ] Package-scope MSRs (**SMI count**, temperature, RAPL) are read from a **housekeeping CPU** (`rdmsr -p 0`, `turbostat --cpu`); the hot core's effective frequency is measured **by the hot thread itself** (§82.4.2).
- [ ] The isolation signals are treated as **invariants**: zero deltas after warm-up, alert on the **first** nonzero delta with the **fingerprint-table diagnosis** attached (§82.2.1, §82.3).
- [ ] The sampler's deltas are **published into the Ch. 75 segment** and exported on the **same timeline** as the application histograms; the sampler has its own **heartbeat** watched by the supervisor (§82.4.4, §82.5).
- [ ] A **baseline fingerprint** (cmdline, kernel, microcode, BIOS/NIC firmware, isolation masks, IRQ map, sysctls, `ethtool`/`resctrl` settings, steady-state interrupt rates, SMI count) is recorded at sign-off, **audited at T-60 and on boot**, and updated only as part of a release (§82.4.4).
- [ ] The **pre-open gate** (Appendix M) refuses to arm on any isolation delta since warm-up, any SMI/throttle delta overnight, a non-`tsc` clocksource, or a fingerprint mismatch (§82.4.4).
- [ ] **No** `perf` event, `rdmsr`, default `turbostat` or `cpufreq` read targets an isolated CPU in production (§82.5).

## 82.7 References

- Linux kernel documentation: `Documentation/filesystems/proc.rst` (`/proc/<pid>/status`, `stat` field numbering, `/proc/interrupts`, `/proc/softirqs`, `/proc/vmstat`), `Documentation/accounting/psi.rst`, `Documentation/admin-guide/mm/transhuge.rst`, `Documentation/x86/topology`/`cpuinfo`, `Documentation/admin-guide/ras.rst` (EDAC, MCE, `rasdaemon`), `Documentation/PCI/pcieaer-howto.rst`, `Documentation/power/powercap/powercap.rst` (RAPL), `Documentation/admin-guide/pm/cpuidle.rst` (the `cpuidle/state*` counters), `Documentation/arch/x86/msr.rst` (`/dev/cpu/*/msr` and the cross-call) — the sources of every signal in §82.2.1.
- `turbostat(8)` (`--cpu`, `--show`, the `SMI`/`Bzy_MHz`/`PkgWatt` columns and how they are read), `numastat(8)`, `rasdaemon(8)`/`ras-mc-ctl(8)`, `dmidecode(8)` — the readers of §82.4.2–§82.4.4.
- Intel SDM Vol. 3B (thermal monitoring and `IA32_THERM_STATUS`/`IA32_PACKAGE_THERM_STATUS`; `IA32_APERF`/`IA32_MPERF`; machine-check architecture and CMCI; RAPL) and Vol. 4 (`MSR_SMI_COUNT`) — what the counters count (§82.4.2, §82.4.3).
- Ch. 42, 45, 46 (the invariants and the catalog), Ch. 6 §6.4.4 (SMIs, clocksource watchdog), Ch. 15–16 (THP/compaction/NUMA knobs), Ch. 44 §44.4.2 and Ch. 60 §60.4.1 (the two continuous monitors that preceded this chapter), Ch. 67 (PCIe link health), Ch. 75 (the segment, scraper and alert classes).

## 82.8 Additional Reading

- Brendan Gregg, *Systems Performance* (2nd ed.) — the USE method and the per-resource observability tables, the organising idea behind §82.2.1; and his *BPF Performance Tools* for the eBPF versions of these signals (Ch. 63).
- Red Hat Enterprise Linux for Real Time *Tuning Guide*, the chapters on verifying isolation and on `tuna`/`rteval` — the vendor's one-off verification, which this chapter makes continuous.
- The `rasdaemon` and EDAC project documentation; Intel's *Memory RAS Configuration* and PCIe AER white papers — the hardware-health layer §82.4.3 only samples.
- Ch. 83 (*Network Health Telemetry*) — the NIC and stack counters from the same scraper; Ch. 84 (*Case Study*) — a drifted knob as a production regression; Ch. 78 (*Process Topology*) — the supervisor that watches the sampler; Appendix C.6 (configuration as code and drift), Appendix M (the pre-open gate on these signals), **Appendix I.6** (the one-liners), **Appendix F** (SMI / CMCI / EDAC / AER / PSI / drift glossary).

---

*Next: Ch. 83 — Network Health Telemetry & Drop Forensics, the same discipline for the wire: which counter moves when a packet dies in the NIC ring, the PCIe path, the driver's softirq budget, the IP stack or the socket buffer — the `ethtool -S` dialects, `softnet_stat` and `time_squeeze`, `nstat`, `TCP_INFO`, `dropwatch` and the `kfree_skb` tracepoint — and the link, pause-frame, FEC and multicast-membership signals from the fabric side, sampled without touching the hot core.*
