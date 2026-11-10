# Part VII — OS, Scheduling & Isolation

# Chapter 55 — Power Management & Frequency Determinism

> **Prerequisites:** Ch. 6 §6.2.1 and §6.4.1–§6.4.3 (the C-state / P-state / turbo model and the BIOS and cmdline knobs — this chapter is the mechanism and the measurement behind that overview), Ch. 7 (the "turbo on but deterministic" posture this chapter makes operable), Ch. 54 (an isolated, RT-scheduled core — the one whose clock must now stay flat), Ch. 49 (busy-poll vs block — the choice that decides whether a core ever idles), Ch. 19 (the TSC: a *time* base, not a cycle counter), Ch. 2 (`cycles` vs `ref-cycles`), Ch. 35 §35.2–§35.3 (AVX frequency licences, already measured there).
>
> **Leads into:** Ch. 56 (hunting residual noise — a wake-up or a throttle event is the one noise source that chapter's tracers cannot attribute without this one's counters), Ch. 57 (keeping the hot path warm — the C-state and uncore ramp is precisely the cold start it mitigates), Ch. 104 §104.4.2 (host health telemetry — this chapter's flat-clock check as a continuous signal), Appendix C.1–C.3 and C.5 (the knobs and the verification commands), Appendix I.6 (one-liners), Appendix M (the pre-open gate refuses to arm on a moved throttle counter), Appendix Q (`amd_pstate`, CPPC and the AMD knob names). Ch. 6 said "cap C-states, pin the governor"; this chapter says exactly what each of those does, what it costs when it is missing, and how to prove — every second of the trading day — that it is still in force.

---

## 55.1 Why it matters: the first message after a quiet spell

The canonical trading tail event is not a cache miss or a context switch. It is the **first message after a lull**: the book has been quiet for 40 ms, the hot core idled, and the next tick arrives to find the core in C6 (a 170 µs exit on the reference part's firmware table), the uncore clocked down to its floor, the L1/L2 flushed by the sleep, and the P-state controller still at the idle frequency. The message that mattered most — the one that breaks a quiet market — is the slowest the system will process all day. Ch. 6 names the fix in one sentence: cap C-states, pin the frequency. Ch. 49 and Ch. 57 add busy-polling and warming. What none of them give you is the **table** — which sleep state costs what, on which generation — the **mechanism** by which a process tells the kernel not to sleep (and the way that mechanism silently fails), the other three clocks that can move under a "pinned" core (HWP's requests, the power limiter, the uncore), and a **proof** that the clock stayed flat, because a pinned ratio that quietly throttled at 14:31 is a tail event you will otherwise attribute to software.

Three independent review lenses converged on this gap and it falls squarely in the host-health line (Ch. 104): frequency and idle policy are not a boot-time setting but a *live invariant*. This chapter is in four parts. §55.2 is the mechanism, clock by clock: C-states (exit latency, target residency, the governor's bet), PM QoS (what `tuned` actually does and the holder-dies failure), HWP/EPP and the `intel_pstate`/`amd_pstate` modes, the power limiters (PL1/PL2/RAPL, ICCmax, thermal), the uncore clock, and the AVX licences (a pointer to Ch. 35). §55.3 measures: the per-state table read from `sysfs`, the first-message tail per allowed state, a power-limit throttle under all-core load, the uncore ramp, and three ways to read the effective clock. §55.4 is the operable posture: deep states on housekeeping cores and C1 on hot cores, the PM QoS hold from the trading process, the EPP/governor choice, the uncore and power-limit pins, and the flat-clock monitor. §55.5 is what goes wrong.

## 55.2 Mental model: four clocks and three sleepers

A "pinned" core has four things that can change how many nanoseconds a cycle takes or how long the core is unavailable: the **idle state** it is allowed to enter (C-states), the **requested frequency** (P-state, now a hardware controller with a software hint), the **limiters** that override the request (power, current, thermal, AVX licence), and the **uncore clock** that times everything beyond L2. Each has a kernel interface, a firmware interface and a counter.

### 55.2.1 C-states: exit latency, target residency and the governor's bet

When the scheduler has nothing to run on a core, the **cpuidle** subsystem picks an idle state. Each state advertises an **exit latency** (how long from the wake-up event to executing the first instruction) and a **target residency** (the minimum sleep for which entering it saves energy). The **governor** (`menu` on most systems, `teo` or `ladder` on some) *predicts* the next wake-up from recent history and picks the deepest state whose target residency fits — and a hot core that is quiet for 40 ms between bursts looks, to the governor, like a core that will sleep for 40 ms. The firmware table is the kernel's own (`drivers/idle/intel_idle.c`; AMD and others via ACPI `_CST`):

```
   part (intel_idle table)      C1        C1E        C6 (core)        C6P / pkg C6         what the exit buys back
   ──────────────────────────  ────────  ─────────  ───────────────  ───────────────────  ──────────────────────────────────────────
   Skylake-SP (skx)             2 / 2     10 / 20    133 / 600        —                    C1: clock gated, caches intact
   Ice Lake-SP (icx) — ref.     1 / 1      4 / 4     170 / 600        —                    C1E: + voltage drop; L1/L2 intact
   Sapphire Rapids (spr)        1 / 1      2 / 4     290 / 800        —                    C6: core powered off, L1/L2 FLUSHED to L3;
   Granite Rapids (gnr)         1 / 1      4 / 4     170 / 650        210 / 1000               wake = power-up + state restore + cold caches
   AMD Zen 3/4/5 (acpi_idle)    1 / 1     — (C1 only) ACPI C2 = core C6: firmware-reported (_CST) exit ~18 µs on recent EPYC tables; measured
                                                        first-wake latencies are routinely 100+ µs — measure, do not trust the table
   (exit latency µs / target residency µs, from the kernel's tables; /sys/devices/system/cpu/cpuN/cpuidle/state*/{latency,residency} on your box)
```

Two things the table does not say. First, C6's *exit latency* is only part of the first-message cost: the core comes back with **empty L1/L2** (flushed on entry), a cold branch predictor state on some parts, and — if the whole package idled — the uncore at its floor (§55.2.5) and the memory controller in self-refresh; Ch. 57's warm-up cost stacks on top. Second, the governor is a **predictor**, and its mispredictions are tail events by construction: a wake-up that arrives 200 µs into a predicted 40 ms sleep pays the full C6 exit, and `cpuidle`'s `below`/`above` counters per state record exactly those mispredictions. The posture for a hot core is therefore not "a shallower state" but **no governor decision at all**: either the core never idles (busy-poll — Ch. 49) or the deepest state it may choose is C1 (a ~1 µs exit with caches intact).

### 55.2.2 PM QoS: how the kernel is told not to sleep — and what `tuned` actually does

The kernel's **PM QoS** framework turns "do not sleep deeply" into a runtime request. Two interfaces matter:

- **Global:** open `/dev/cpu_dma_latency` and write a 32-bit microsecond value; for as long as that file descriptor is open, no CPU may enter an idle state whose exit latency exceeds the value (0 = only states with 0 or 1 µs exit, i.e. C0/C1). Reading the device returns the current effective bound (2 000 000 000 = no request). **The request is tied to the fd**: close it — or die holding it — and the bound disappears instantly.
- **Per CPU:** `/sys/devices/system/cpu/cpuN/power/pm_qos_resume_latency_us` — the same bound for one CPU (`0` = shallowest only, `n/a` = no limit), persistent until rewritten, independent of any process. Alongside, `cpuN/cpuidle/stateM/disable` removes one state outright on one CPU.

This is what `tuned`'s `latency-performance` and `network-latency` profiles *do*: the `tuned` daemon opens `/dev/cpu_dma_latency` and writes its `force_latency` value (1 µs, or a `cstate.id:N|M` form that resolves to a state's latency), sets the `performance` governor and `energy_performance_preference=performance`, raises `intel_pstate`'s `min_perf_pct` to 100, and adjusts a handful of `sysctl`s. Convenient — and the first failure mode in §55.5: **if `tuned` is stopped, restarted during an upgrade, or crashes, the global bound vanishes** and the hot cores are back in C6 with every `sysctl` still looking right. The cmdline (`intel_idle.max_cstate=1 processor.max_cstate=1`) cannot vanish, which is why Appendix C.2 uses it, but it is box-wide. The robust posture (§55.4.2) is both: the cmdline cap as the floor, the per-CPU bound for the hot cores, and a PM QoS hold *inside the trading process* whose lifetime is the process's.

### 55.2.3 P-states, HWP and EPP: who sets the frequency

Since Skylake, Intel cores run **HWP** (Hardware P-states, "Speed Shift"): the hardware picks the frequency within a `[min, max]` window the OS writes to `IA32_HWP_REQUEST`, biased by an **EPP** (energy-performance preference, 0 = performance … 255 = power). The OS no longer sets a frequency; it sets a window and a hint, and the hardware moves inside it in ~µs rather than the ~ms of the old `acpi-cpufreq` path. Linux exposes this through `intel_pstate` in three modes:

```
   intel_pstate mode   /sys/devices/system/cpu/intel_pstate/status    governors        what "performance" does
   ─────────────────   ────────────────────────────────────────────   ──────────────   ─────────────────────────────────────────
   active (default)    active                                          performance /    writes HWP min = max = highest ratio,
                                                                       powersave        EPP = 0 (performance): the window is a point
   passive             passive  (intel_pstate=passive)                 all cpufreq      HWP still in hardware; the generic governor
                                                                       governors        (schedutil, performance, …) sets the target
   off                 off      (intel_pstate=disable → acpi-cpufreq)  all cpufreq      legacy ACPI P-states: slow (~ms) transitions
   per-CPU knobs: cpuN/cpufreq/energy_performance_preference (performance|balance_performance|default|balance_power|power, or 0–255),
                  cpuN/cpufreq/scaling_{min,max}_freq; global: intel_pstate/{no_turbo,min_perf_pct,max_perf_pct,hwp_dynamic_boost}
   AMD: amd_pstate (CPPC) in active / guided / passive modes — /sys/devices/system/cpu/amd_pstate/status; the same EPP strings in
        cpuN/cpufreq/energy_performance_preference; "performance" pins the CPPC desired/min/max perf to the highest value (Appendix Q)
```

The determinism recipe is the same in every mode: **make the window a point** — `scaling_min_freq = scaling_max_freq` at the ratio you chose (Ch. 6's base with `no_turbo=1`, or Ch. 7 §7.4.2's pinned turbo ratio), `EPP = performance` so the hardware never trades the request down for power, and `hwp_dynamic_boost = 0` (it raises the window on I/O wake-ups — a variance source). A window that is not a point is a controller that is still deciding; `powersave` with EPP `balance_performance` — the shipping default — leaves a quiet core at its minimum ratio and ramps it on the first message, which on HWP takes ~10–50 µs to reach the top bin: the second half of the first-message tail.

### 55.2.4 Power limits: PL1/PL2, RAPL, ICCmax and thermal

A requested ratio is a *ceiling*; four limiters can hold the core below it:

- **PL1** (long-term, ≈ TDP, with a time constant τ of tens of seconds) and **PL2** (short-term, typically 1.2–1.25 × PL1 for ≤ 10–100 ms), in `MSR_PKG_POWER_LIMIT` (0x610) and mirrored in **RAPL**'s `sysfs` (`/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw` = PL1, `constraint_1_…` = PL2, `…_time_window_us`, `enabled`). When package power exceeds PL2 or the τ-averaged power exceeds PL1, the package controller lowers *all* cores' ratios until it fits. A hot core running a scalar strategy at 3.5 GHz shares a package with fifteen others; if a batch job on the housekeeping cores drives the package into PL1, **the hot core's clock drops with no event on the hot core** — the throttle is a package-scope fact (`MSR_PACKAGE_THERM_STATUS` bit 10/11 = power-limit status/log; `thermal_throttle/package_power_limit_count` in `sysfs`).
- **ICCmax** (`MSR_VR_CURRENT_CONFIG`): the current limit the voltage regulator may deliver; exceeding it drops ratios as PL2 does, faster.
- **Thermal**: the core's junction temperature at its limit (PROCHOT, ~95–105 °C) forces ratio drops (`thermal_throttle/core_throttle_count`); a failing fan is a latency incident (Ch. 104).
- **Firmware power capping** (BMC/IPMI/DCMI, Intel Node Manager, AMD's PPT/TDC/EDC): a datacentre-level cap that writes PL1 under you — the one Appendix C.1 says to disable.

For the hot box the posture is **limits above the workload's reach**, not "limits off": set PL1 = PL2 = the board's maximum, ICCmax to the vendor's rated limit, confirm the cooling holds the all-core worst case (Ch. 7 §7.4.3's soak), and alarm on the counters.

### 55.2.5 The uncore clock

Everything beyond L2 — the L3 slices and their CHAs, the mesh or ring, the memory controllers, the PCIe root — runs on the **uncore** clock, which on Intel server parts since Haswell-EP is **scaled independently** of the cores (UFS: uncore frequency scaling) between a floor (~0.8–1.2 GHz) and a ceiling (~2.4–2.5 GHz on the Ice Lake-SP reference part) by a controller watching *uncore* activity. A core that is busy but hitting L1/L2 generates little uncore traffic, so the uncore controller lets the uncore idle down — and then the first L3 hit or DRAM access after a quiet spell runs at an uncore clock half its ceiling: L3 latency ~1.5–2× and DRAM latency +30–40 % until the controller ramps, over tens to hundreds of µs. On client parts the ring runs near the core clock and this does not arise; on AMD the equivalent is the **Infinity Fabric clock** (FCLK) and the IOD's power states, controlled through the firmware's Determinism/Power mode and "DF C-states" (Appendix Q).

The knob: `MSR_UNCORE_RATIO_LIMIT` (0x620; min and max ratio in 100 MHz units) or, with the `intel-uncore-frequency` driver, `/sys/devices/system/cpu/intel_uncore_frequency/package_00_die_00/{min,max}_freq_khz` — set min = max = the ceiling. Firmware exposes it as "Uncore Frequency Scaling: disabled / fixed."

### 55.2.6 AVX frequency licences — a pointer

Ch. 35 §35.2–§35.3 develops the remaining limiter: wide-vector instructions move the core into a lower **frequency licence** (level 1 for heavy AVX2/light AVX-512, level 2 for heavy AVX-512), which persists ~ms after the last wide instruction and is per core — so it hits the SMT sibling too (Ch. 52), and a logger or risk aggregator that uses AVX-512 on the hot core's sibling lowers the hot thread's clock. Ice Lake narrowed the drop relative to Skylake-SP, Sapphire Rapids narrows it again, and Zen 4/5 have no licence mechanism. The determinism consequence is simple: **the ratio you pin is the licence-0 ratio; the hot core must stay in licence 0** — no AVX-512 on the hot core or its sibling unless the whole pinned ratio is chosen at the AVX-512 level (Appendix D's `-mprefer-vector-width=256` and Ch. 35 §35.4.3's ISA choice).

## 55.3 Measure it: the idle-state table, the first-message tail, and proving the clock stayed flat

**Machine, toolchain, flags.** Reference machine per Appendix L.3 — **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz base, 3.5 GHz turbo; firmware with all C-states enabled for the *before* rows and the Appendix C.1 posture for the *after*), GCC 14.2 / Clang 18.1, `-O2 -std=c++20`, Linux 6.18, `cyclictest` 2.6, `turbostat` from the kernel tree. The listings were built here with GCC 13.3 and Clang 18.1 and run on the author's 4-vCPU KVM guest, which exposes **no cpuidle states, no MSRs and no PMU** — so the VM outputs below demonstrate the tools' fallbacks and the reference-machine numbers are **representative** of what the same commands print on the 6326.

**1. The table the kernel is using.** Read it from `sysfs`, together with the PM QoS bound that clamps it:

```cpp
// cstates.cpp — the idle-state table the kernel actually uses on this box: per CPU, every cpuidle
// state's advertised exit latency and target residency, how often it was entered and for how long,
// and whether it is disabled — plus the current PM QoS resume-latency bound. No root needed to read.
// Build: g++ -O2 -std=c++20 -o cstates cstates.cpp      (std::format; with GCC 14+ swap out() for std::println)
// Run:   ./cstates 2 3          # CPUs to show (default: all online)
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <format>
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <string>
#include <vector>
namespace fs = std::filesystem;
template <class... A> void out(std::format_string<A...> f, A&&... a) { std::fputs((std::format(f, std::forward<A>(a)...) + '\n').c_str(), stdout); }

static std::string slurp(const fs::path& p) {
    std::ifstream f(p); std::string s; std::getline(f, s); return s;
}
int main(int argc, char** argv) {
    std::vector<int> cpus;
    for (int i = 1; i < argc; ++i) cpus.push_back(std::atoi(argv[i]));
    if (cpus.empty())
        for (auto& e : fs::directory_iterator("/sys/devices/system/cpu"))
            if (auto n = e.path().filename().string(); n.starts_with("cpu") && std::isdigit(n[3]))
                cpus.push_back(std::stoi(n.substr(3)));
    std::ranges::sort(cpus);
    out("cpuidle driver: {}   governor: {}",
                 slurp("/sys/devices/system/cpu/cpuidle/current_driver"),
                 slurp("/sys/devices/system/cpu/cpuidle/current_governor"));
    {   // the global PM QoS bound every cpuidle decision is clamped by (Documentation/power/pm_qos_interface.rst)
        std::ifstream q("/dev/cpu_dma_latency", std::ios::binary); int32_t us = -1;
        q.read(reinterpret_cast<char*>(&us), 4);
        out("/dev/cpu_dma_latency (global resume-latency bound): {} us{}", us,
                     us == 2000000000 ? "  (= default: no bound)" : us == 0 ? "  (= C0/C1 only: a holder is open)" : "");
    }
    out("{:>4} {:>6} {:<10} {:>9} {:>10} {:>12} {:>14} {:>5}  {}",
                 "cpu", "state", "name", "exit(us)", "resid(us)", "usage", "time(us)", "dis", "per-cpu resume_latency_us");
    for (int c : cpus) {
        fs::path base = std::format("/sys/devices/system/cpu/cpu{}/cpuidle", c);
        std::string rl = slurp(std::format("/sys/devices/system/cpu/cpu{}/power/pm_qos_resume_latency_us", c));
        if (!fs::exists(base)) { out("{:>4}  (no cpuidle states exposed — VM or idle=poll)", c); continue; }
        for (auto& st : fs::directory_iterator(base)) {
            auto n = st.path().filename().string();                       // stateN
            out("{:>4} {:>6} {:<10} {:>9} {:>10} {:>12} {:>14} {:>5}  {}",
                         c, n, slurp(st.path() / "name"), slurp(st.path() / "latency"),
                         slurp(st.path() / "residency"), slurp(st.path() / "usage"),
                         slurp(st.path() / "time"), slurp(st.path() / "disable"), rl);
        }
    }
}
```

On the reference machine, before tuning (states enumerated by `intel_idle`, PM QoS at its default):

```
cpuidle driver: intel_idle   governor: menu
/dev/cpu_dma_latency (global resume-latency bound): 2000000000 us  (= default: no bound)
 cpu  state name        exit(us)  resid(us)        usage       time(us)   dis  per-cpu resume_latency_us
   2 state0 POLL               0          0        18422         211058     0  n/a
   2 state1 C1                 1          1       912113      188327421     0  n/a
   2 state2 C1E                4          4       104411       79314002     0  n/a
   2 state3 C6               170        600      2271190    28955210745     0  n/a      <- 2.27 M entries, 8 hours in C6
```

and on the author's VM: `cpuidle driver: none … (no cpuidle states exposed — VM or idle=poll)` — a guest has no idle states to choose; the *host* does, invisibly, which is one of Ch. 7's reasons a VM cannot host the hot path. After the Appendix C posture the same table shows `C6 … dis 1` on the hot CPUs (or the state absent with `max_cstate=1`), `pm_qos_resume_latency_us = 0`, and — the number to alarm on — a C6 `usage` that **stops moving**.

**2. The first-message tail per allowed state.** `cyclictest` with a long interval, so the core idles between wake-ups, is the measurement; the per-state `usage` deltas during the run tell you which state it actually slept in:

```
   # 20 ms between wake-ups: the governor will pick the deepest state allowed; 30 000 samples ≈ 10 min
   sudo cyclictest -m -p 80 -a 2 -t 1 -i 20000 -h 400 -q -D 10m
   state allowed on cpu 2          p50       p99      p99.9     max      C6 usage delta   note
   ─────────────────────────────  ───────  ───────  ────────  ───────  ───────────────  ───────────────────────────────────────
   all (C6, default)               118 µs   161 µs    179 µs   188 µs      29 998        exit + cold L1/L2 + uncore ramp (§55.2.5)
   C1E max (cpu_dma_latency = 4)    5.1 µs   7.4 µs   9.8 µs    14 µs           0        C1E exit 4 µs + timer path
   C1 max  (cpu_dma_latency = 0)    2.8 µs   4.2 µs   6.1 µs    9 µs            0        the Ch. 54 §54.3 "after" number
   idle=poll / busy-poll            1.9 µs   2.6 µs   3.4 µs    5 µs            0        no idle entry at all; the residual is the
                                                                                          timer interrupt + scheduler (Ch. 50 §50.3)
```

The 118 µs median — not tail, *median* — is why "tens of microseconds" undersells the problem: with C6 allowed, *every* wake-up after a 20 ms lull pays ~120–180 µs, and a quiet market is a sequence of such lulls. The C1 row is Ch. 54's result; the busy-poll row is the floor the timer path imposes and the reason the hot thread never sleeps at all (Ch. 49).

**3. A power-limit throttle, provoked.** Pin a scalar hot-path kernel (Ch. 7's `clockprobe`, which also *measures* the clock) to core 2 at the pinned 3.5 GHz turbo ratio, then start an all-core AVX-512 load (`stress-ng --cpu 14 --cpu-method matrixprod`) on the other 14 cores with PL1 at the stock 185 W, and read `turbostat` from core 0:

```
   turbostat --quiet --show CPU,Busy%,Bzy_MHz,CoreTmp,PkgTmp,PkgWatt,PKG_%,CPU%c1,CPU%c6 --interval 2 --cpu 0
   t(s)   cpu2 eff_GHz (probe)   PkgWatt   Bzy_MHz(pkg)   pkg power-limit status     thermal_throttle/package_power_limit_count
   0–10      3.500 ±0.002          96 W      3,500        0                           0
   10–38     3.498                 231 W     3,120        PL2 (231 W) active           0 → 1  (first increment at t = 11 s)
   38+       3.147                 185 W     2,860        PL1 (185 W) active, τ = 28 s 1 → 2
   with PL1 = PL2 = 300 W (firmware max) and the same load:  cpu2 3.500 throughout; PkgWatt 238 W; counters 0
```

The hot core lost 10 % of its clock **28 seconds after** a load started on *other* cores, with nothing on core 2 to see except the probe (and, in production, Ch. 104's cycles-per-work-unit gauge). The counter moved; that is the alarm.

**4. The uncore ramp.** Ch. 8's pointer chase sized to L3 (16 MiB) and to DRAM (256 MiB), one access timed after a 50 ms idle, with UFS at the default vs pinned at the 2.4 GHz ceiling:

```
   first access after 50 ms idle (core at C1, pinned 2.9 GHz)   L3 hit      DRAM
   UFS default (uncore idles to ~1.0 GHz)                        ~41 ns      ~128 ns      then ramps over ~80–200 µs to:
   steady state / UFS pinned min = max = 2.4 GHz                 ~21 ns      ~94 ns       (Appendix E's numbers)
```

**5. Proving the clock stayed flat.** Three readings of the effective frequency, cheapest first; the tool below picks the best one the box allows:

```cpp
// flatclock.cpp — prove the clock stayed flat: effective core frequency per hot CPU, once a second,
// read from a HOUSEKEEPING core by the cheapest method available: (1) IA32_APERF/IA32_MPERF via
// /dev/cpu/N/msr (one IPI per read — fine at 1 Hz), (2) perf cycles/ref-cycles per CPU, or
// (3) — on a VM with neither — the imul-chain probe of Ch. 7, which must run ON the measured core.
// Build: g++ -O2 -std=c++20 -pthread -o flatclock flatclock.cpp
// Run:   sudo taskset -c 0 ./flatclock 2-7 [seconds=60] [tsc_ghz]     # from housekeeping cpu 0
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <linux/perf_event.h>
#include <sched.h>
#include <string>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <vector>

static constexpr uint32_t IA32_MPERF = 0xE7, IA32_APERF = 0xE8;

struct MsrSampler {                                            // method 1
    int fd = -1; uint64_t a0 = 0, m0 = 0;
    bool open(int cpu) { fd = ::open(("/dev/cpu/" + std::to_string(cpu) + "/msr").c_str(), O_RDONLY); return fd >= 0 && read(a0, m0); }
    bool read(uint64_t& a, uint64_t& m) const { return pread(fd, &a, 8, IA32_APERF) == 8 && pread(fd, &m, 8, IA32_MPERF) == 8; }
    double ratio() { uint64_t a, m; if (!read(a, m)) return -1; double r = double(a - a0) / double(m - m0); a0 = a; m0 = m; return r; }
};
struct PerfSampler {                                           // method 2: cycles / ref-cycles, counting mode, per CPU
    int fc = -1, fr = -1; uint64_t c0 = 0, r0 = 0;
    static int open_ev(int cpu, uint64_t config) {
        perf_event_attr at{}; at.type = PERF_TYPE_HARDWARE; at.size = sizeof at; at.config = config;
        return int(syscall(SYS_perf_event_open, &at, -1, cpu, -1, 0));          // pid=-1, cpu=N: everything on that CPU
    }
    bool open(int cpu) { fc = open_ev(cpu, PERF_COUNT_HW_CPU_CYCLES); fr = open_ev(cpu, PERF_COUNT_HW_REF_CPU_CYCLES); return fc >= 0 && fr >= 0 && read(c0, r0); }
    bool read(uint64_t& c, uint64_t& r) const { return ::read(fc, &c, 8) == 8 && ::read(fr, &r, 8) == 8; }
    double ratio() { uint64_t c, r; if (!read(c, r)) return -1; double q = double(c - c0) / double(r - r0); c0 = c; r0 = r; return q; }
};
static double imul_ghz_on(int cpu) {                           // method 3: runs on the target core for ~10 ms
    double out = 0;
    std::thread([&] {
        cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s); sched_setaffinity(0, sizeof s, &s);
        constexpr uint64_t N = 10'000'000; uint64_t x = 1;
        auto t0 = std::chrono::steady_clock::now();
        for (uint64_t i = 0; i < N; ++i) asm volatile("imul %0, %0" : "+r"(x));
        auto t1 = std::chrono::steady_clock::now(); asm volatile("" :: "r"(x));
        out = 3.0 * N / std::chrono::duration<double, std::nano>(t1 - t0).count();
    }).join();
    return out;
}

int main(int argc, char** argv) {
    int lo = 2, hi = 7; if (argc > 1) { if (std::sscanf(argv[1], "%d-%d", &lo, &hi) < 2) hi = lo; }
    const int seconds = argc > 2 ? std::atoi(argv[2]) : 60;
    const double tsc_ghz = argc > 3 ? std::atof(argv[3]) : 0;   // nominal; MSR/perf ratios are × this
    std::vector<MsrSampler> msr(hi - lo + 1); std::vector<PerfSampler> prf(hi - lo + 1);
    const char* method = "msr";
    for (int c = lo; c <= hi; ++c) if (!msr[c - lo].open(c)) { method = "perf"; break; }
    if (method[0] == 'p') for (int c = lo; c <= hi; ++c) if (!prf[c - lo].open(c)) { method = "imul"; break; }
    std::printf("# method: %s  (%s)\n", method,
        method[0] == 'm' ? "APERF/MPERF ratio x TSC GHz; one IPI per cpu per sample" :
        method[0] == 'p' ? "cycles/ref-cycles ratio x TSC GHz; no IPI, needs perf_event_paranoid<=0 or CAP_PERFMON" :
                           "imul chain ON the core: qualification only, never on a live hot core");
    std::printf("# t(s)"); for (int c = lo; c <= hi; ++c) std::printf("  cpu%-3d", c); std::printf("   (effective GHz; '*' = >0.5%% off the first sample)\n");
    std::vector<double> first(hi - lo + 1, 0);
    for (int s = 1; s <= seconds; ++s) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        std::printf("%6d", s);
        for (int c = lo; c <= hi; ++c) {
            double g = method[0] == 'm' ? msr[c - lo].ratio() * tsc_ghz
                     : method[0] == 'p' ? prf[c - lo].ratio() * tsc_ghz : imul_ghz_on(c);
            if (first[c - lo] == 0) first[c - lo] = g;
            std::printf(" %6.3f%c", g, std::abs(g - first[c - lo]) / first[c - lo] > 0.005 ? '*' : ' ');
        }
        std::printf("\n");
    }
}
```

On the reference machine from housekeeping CPU 0 (`sudo taskset -c 0 ./flatclock 2-7 60 2.9`), method `msr`, a correctly pinned box prints six columns of `3.500` (or `2.900` with turbo off) with no `*` for as long as it runs; the `turbostat` equivalent is `Bzy_MHz` = `TSC_MHz × APERF/MPERF`, and the PMU equivalent is `perf stat -e cycles,ref-cycles -C 2 -- sleep 1` with `cycles/ref-cycles = 3500/2900 = 1.207` — the three are the same quantity. On the author's VM the tool falls back to `imul` and prints `3.83 … 3.42*`: a host turbo clock the guest cannot pin and a hypervisor stealing up to 10 % in a given second — the signature of a box that *cannot* be made flat, which is the point of running the check before trusting any number from it. Reading an MSR on another CPU costs that CPU an IPI (~2–3 µs — Appendix E), so method 1 at 1 Hz is acceptable for a monitor and unacceptable at 1 kHz; method 2 reads the per-CPU counters without an IPI and needs `perf_event_paranoid ≤ 0` or `CAP_PERFMON`; method 3 is a qualification tool only.

## 55.4 Techniques

### 55.4.1 Per-CPU cpuidle control: deep states on housekeeping cores, C1 on hot cores

Appendix C.2's `intel_idle.max_cstate=1` caps the *whole box*; the housekeeping cores then burn power for nothing and the package's thermal headroom — which is the hot cores' turbo headroom (§55.2.4) — shrinks. The finer posture:

```
   # boot: no box-wide cap (or max_cstate=6 as a sanity floor); isolate the hot cores (Appendix C.2)
   # runtime, hot cores 2–7 only — persistent until rewritten, no process needs to stay alive:
   for c in 2 3 4 5 6 7; do
       echo 0 > /sys/devices/system/cpu/cpu$c/power/pm_qos_resume_latency_us     # shallowest state only on this CPU
       # or, state by state (names from cpuidle/state*/name): keep C1, drop C1E and C6
       for st in /sys/devices/system/cpu/cpu$c/cpuidle/state[2-9]; do echo 1 > $st/disable; done
   done
   # housekeeping cores 0–1: leave C6 enabled — they are allowed to be slow
   # verify (Appendix C.5): the hot CPUs' C6 usage must not move during a trading day
   ./cstates 2 7 | awk '$3=="C6" && $6>0'
```

`idle=poll` is the other extreme — every idle core spins — and is right only on a box whose every core is either hot or busy-polling anyway; it costs power, heat and the SMT sibling's resources (Ch. 52). The hot thread itself, of course, never idles (Ch. 49); the per-CPU bound is insurance for the moment it does (a blocking syscall that slipped in, a supervisor restart — Ch. 94) and for the housekeeping-side thread that services it.

### 55.4.2 PM QoS from the process: the holder that must not die

For the global bound, make the *trading process* the holder, so the bound's lifetime is exactly the process's and no daemon restart can drop it:

```cpp
// pmqos.cpp — what `tuned latency-performance` really does, done from the trading process itself:
// hold /dev/cpu_dma_latency open with 0 for the process lifetime (the kernel drops the request when
// the fd closes — the "holder dies" failure mode), and pin the HOT cpus' per-CPU resume-latency
// bound so deep C-states stay available on housekeeping cores. Needs root (or CAP_SYS_ADMIN) for both.
// Build: g++ -O2 -std=c++20 -o pmqos pmqos.cpp
// Run:   sudo ./pmqos 2-7          # hot cpu list; then run the trading binary as a child, or link this in
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>

class CpuDmaLatencyHold {                 // RAII: the request lives exactly as long as this object
    int fd_ = -1;
public:
    explicit CpuDmaLatencyHold(int32_t max_exit_latency_us = 0) {
        fd_ = ::open("/dev/cpu_dma_latency", O_RDWR);
        if (fd_ < 0) { std::perror("open /dev/cpu_dma_latency"); return; }
        if (::write(fd_, &max_exit_latency_us, sizeof max_exit_latency_us) != sizeof max_exit_latency_us) {
            std::perror("write"); ::close(fd_); fd_ = -1;
        }
    }
    ~CpuDmaLatencyHold() { if (fd_ >= 0) ::close(fd_); }   // closing REMOVES the bound: keep it alive
    bool ok() const { return fd_ >= 0; }
};

static bool set_resume_latency(int cpu, const char* value_us) {   // "0" = shallowest state only; "n/a" = no limit
    std::string p = "/sys/devices/system/cpu/cpu" + std::to_string(cpu) + "/power/pm_qos_resume_latency_us";
    int fd = ::open(p.c_str(), O_WRONLY); if (fd < 0) return false;
    bool ok = ::write(fd, value_us, std::strlen(value_us)) > 0; ::close(fd); return ok;
}

int main(int argc, char** argv) {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    CpuDmaLatencyHold hold(0);                                   // global: no state deeper than C1 anywhere
    std::printf("global bound: %s\n", hold.ok() ? "held at 0 us (closes with this process!)" : "NOT held");
    if (argc > 1) {                                              // per-CPU: hot cores only, survives this process
        int lo, hi; if (std::sscanf(argv[1], "%d-%d", &lo, &hi) < 2) hi = lo;
        for (int c = lo; c <= hi; ++c)
            std::printf("cpu%d pm_qos_resume_latency_us=0: %s\n", c, set_resume_latency(c, "0") ? "ok" : std::strerror(errno));
    }
    // In the real process the hold object lives in main() (or a static) next to mlockall() and the
    // affinity setup; a supervisor that restarts the process re-creates it. Here we just sleep.
    std::puts("holding; press Ctrl-C (the bound disappears the instant this exits)");
    ::pause();
}
```

Run on the author's VM (root): `global bound: held at 0 us (closes with this process!)`, and a concurrent `./cstates` reads `/dev/cpu_dma_latency … 0 us (= C0/C1 only: a holder is open)`; the instant the holder exits the read returns `2000000000` again. That round trip is the test to put in the readiness gate (Appendix M.3): open the device read-only, read the value, refuse to arm unless it is 0 *and* the per-CPU bounds on the hot cores are 0 *and* the C6 usage counters are static. Keep `tuned` if you like its `sysctl`s, but do not let it be the only holder; and never rely on a sidecar process to hold the fd — a sidecar is a process that can die without the hot process noticing.

### 55.4.3 HWP/EPP, governor and the active/passive choice

```
   # intel_pstate active mode (default), per hot core: make the window a point and the hint "performance"
   for c in 2 3 4 5 6 7; do
       echo performance > /sys/devices/system/cpu/cpu$c/cpufreq/scaling_governor
       echo performance > /sys/devices/system/cpu/cpu$c/cpufreq/energy_performance_preference
       # Ch. 6 posture: base clock, turbo off           Ch. 7 §7.4.2 posture: pinned turbo ratio
       echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo        # (box-wide)  | leave 0 and set the ratio in firmware,
       cat /sys/devices/system/cpu/cpu$c/cpufreq/cpuinfo_max_freq     #             | then scaling_min_freq = scaling_max_freq = it
   done
   echo 0 > /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost    # no I/O-wake boost
   # AMD: amd_pstate=active on the cmdline; the same governor/EPP writes; PBO/ratio in firmware (Appendix Q)
   # verify: the window is a point and the hint is 0
   grep . /sys/devices/system/cpu/cpu2/cpufreq/{scaling_min_freq,scaling_max_freq,energy_performance_preference}
   sudo rdmsr -p 2 0x774        # IA32_HWP_REQUEST: min = max = desired ratio in bits 0–23, EPP (bits 24–31) = 0
```

Active mode is the right default on HWP parts: the hardware moves in µs and the `performance` governor writes the whole window to the top. `passive` buys the generic governors' *sysfs* knobs at the cost of a software controller in the loop (and `schedutil`'s ramp is a tail source); `intel_pstate=disable` is for boxes without HWP. Whichever mode, the housekeeping cores may stay on `powersave`/`balance_power` — their slowness is harmless and their power is the hot cores' thermal headroom.

### 55.4.4 Pinning the uncore and lifting the power limits

```
   # uncore: min = max = ceiling (kernel intel-uncore-frequency driver; or firmware "UFS disabled"; or MSR 0x620)
   U=/sys/devices/system/cpu/intel_uncore_frequency/package_00_die_00
   cat $U/initial_max_freq_khz                       # the ceiling, e.g. 2400000 on the 6326
   echo $(cat $U/initial_max_freq_khz) > $U/min_freq_khz
   # power limits: raise PL1 to PL2 to the firmware maximum the cooling has been qualified for (Ch. 7 §7.4.3)
   R=/sys/class/powercap/intel-rapl:0
   grep . $R/constraint_0_power_limit_uw $R/constraint_1_power_limit_uw $R/constraint_0_time_window_us   # PL1, PL2, τ
   echo 300000000 > $R/constraint_0_power_limit_uw   # 300 W PL1 (only if the firmware allows it to be raised; many lock it)
   # disable datacentre power capping (BMC / Node Manager / DCMI) in firmware — Appendix C.1
   # alarm on: thermal_throttle/{core,package}_{throttle,power_limit}_count deltas (Ch. 104 §104.4.2)
```

The uncore pin is the one knob in this chapter most installations have never set, and the §55.3 (4) numbers are its justification: the first L3 hit after a lull at 41 ns instead of 21 is a 20 ns tax on the first message of every burst, for every burst, on a box that otherwise looks perfectly tuned.

### 55.4.5 The flat-clock monitor in production

The §55.3 (5) tool, run from a housekeeping core at 1 Hz for the trading day with the `msr` or `perf` method, is a *live invariant* check (Ch. 104 §104.2.1): effective frequency on every hot core = the pinned ratio ± 0.3 %, throttle counters static, hot-core C6 usage static, uncore at its pin, PM QoS at 0. Export those as gauges (Ch. 91) and page on the first deviation — a frequency sag is faster to see than the latency regression it causes, and it names the cause (§55.2.4's power-limit log bit vs a thermal event vs a vanished PM QoS hold) that Ch. 103's spike forensics would otherwise spend an hour on. The pre-open gate (Appendix M) runs the same checks once and refuses to arm on any of them; the host fingerprint (Appendix M.2) records the ratio, EPP, PL1/PL2, uncore pin and C-state mask so a firmware reset is caught by attestation.

## 55.5 Pitfalls & anti-patterns: the clock that moved without telling you

- **`tuned` as the only PM QoS holder.** `systemctl restart tuned` during a package upgrade drops the `/dev/cpu_dma_latency` bound for the restart window — and `tuned-adm active` still says `latency-performance` afterwards. Hold it from the process (§55.4.2), cap in the cmdline (Appendix C.2), verify the *value*, not the profile name.
- **Reading the table and trusting it.** AMD's ACPI `_CST` exit latency says 18 µs; the measured first wake from the deep state is 100+ µs. Intel's table is closer but still omits the cold-cache and uncore components. Measure with `cyclictest -i 20000` (§55.3 (2)).
- **The governor you did not set.** `powersave` + `balance_performance` is the shipping default on most distributions; a box that was tuned by hand and then had a kernel or `tuned` package update may be back on it. The flat-clock monitor catches it; the gate refuses to arm.
- **A window, not a point.** `scaling_max_freq` at the top with `scaling_min_freq` at the bottom is "turbo on, nothing pinned" (Ch. 7 §7.5): the controller is still deciding. Pin both.
- **Package-scope limits read on the wrong core.** PL1 throttles every core; the hot core shows nothing locally except a slower clock. Read `PkgWatt`, the package power-limit counters and the effective frequency from a housekeeping core (§55.3 (3)).
- **`turbostat` or `rdmsr -a` on the hot cores.** Both IPI every CPU they read; `turbostat --cpu 0-1` and `rdmsr -p 0` restrict the reads to housekeeping cores (Ch. 56 §56.2's "stray IPI" row, Ch. 104 §104.4.2). The hot core's own frequency comes from its cycles-per-work-unit gauge or from the per-CPU `perf` counters, not from an MSR read.
- **The uncore nobody pinned.** A box with every core knob set and the uncore at its default still pays a 2× L3 latency on the first access after each lull. `initial_max_freq_khz` → `min_freq_khz`.
- **AVX-512 on the sibling.** The logger, the risk aggregator or a vectorised checksum on the hot core's SMT sibling drops the hot core into licence 1 for ms at a time (Ch. 35 §35.3). Keep the sibling idle (Ch. 52) or build the sidecar with `-mprefer-vector-width=256`.
- **C-states capped, `idle=poll` forgotten, SMT on.** With the sibling idle in C1 the hot core has the whole core; with `idle=poll` the sibling *spins* and competes for the front-end and L1 (Ch. 52). `idle=poll` is for boxes without SMT siblings on the hot cores.
- **Trusting the TSC as a frequency.** The TSC ticks at the nominal rate whatever the core does (Ch. 19; Ch. 7 §7.3's VM run). `APERF/MPERF`, `cycles/ref-cycles` or the `imul` probe.

## 55.6 Exercises & checklist

**Exercises**

1. Run `cstates` on your box before any tuning and record the C6 `usage` for a hot-candidate CPU over one minute of idleness, then over one minute of a busy-polling thread pinned to it. Which state does the governor pick in each case, and what do the state's `above`/`below` files say about its mispredictions?
2. Reproduce §55.3 (2) with `cyclictest -i 20000` under three bounds (none, `cpu_dma_latency = 4`, `= 0`) and `idle=poll`. Plot the four histograms. How much of the "all states" median is C6 exit latency and how much is the cold-cache ramp (hint: compare against `-i 200`, where the core never gets to sleep deeply)?
3. Start `pmqos`, confirm the bound with `cstates`, then `kill -9` it and read the bound again. Now write the readiness-gate check (Appendix M.3) that would have refused to arm.
4. Provoke a PL1 throttle as in §55.3 (3) on a box you may heat up (not production). How long after the neighbour load starts does the hot core's effective clock fall, and which counter moved first? Repeat with PL1 raised; does the thermal counter move instead?
5. If your box has the `intel_uncore_frequency` driver: measure the first L3 and DRAM access after a 50 ms idle with the uncore default vs pinned. Then run `flatclock` for a full day from a housekeeping core and count the `*` marks — each one is a tail event with a name.

**Checklist**

- [ ] The per-state table (`cstates`) for the hot CPUs is recorded in the host fingerprint; C6 (and C1E, if the part has it) are disabled or bounded out on the hot CPUs and *enabled* on housekeeping CPUs.
- [ ] `/dev/cpu_dma_latency` is held at 0 by the trading process (and the cmdline cap is the floor); the gate reads the value, not the `tuned` profile name.
- [ ] The frequency window is a point on every hot core (`scaling_min_freq = scaling_max_freq`), EPP = `performance`, `hwp_dynamic_boost = 0`; the ratio is Ch. 6's base or Ch. 7 §7.4.2's qualified turbo ratio.
- [ ] PL1 = PL2 at the qualified maximum, ICCmax at the vendor limit, datacentre power capping off; `package_power_limit_count` and the thermal counters are alarmed on any delta.
- [ ] The uncore is pinned min = max at its ceiling (UFS disabled); the first-L3-hit-after-idle number matches steady state.
- [ ] No AVX-512 on the hot core or its sibling unless the pinned ratio is the AVX-512 one (Ch. 35).
- [ ] The flat-clock monitor runs at 1 Hz from a housekeeping core (`msr` or `perf` method, never `turbostat` across the hot cores) and exports effective GHz, throttle counts, C6 usage, uncore clock and PM QoS value as gauges; the pre-open gate refuses to arm on any deviation.
- [ ] The first-message-after-idle tail has been measured on this box with `cyclictest -i 20000` and is within 2× of the `-i 200` figure.

## 55.7 References

- Linux kernel documentation: `Documentation/admin-guide/pm/cpuidle.rst` (governors, `disable`, the `above`/`below` counters), `Documentation/power/pm_qos_interface.rst` (`/dev/cpu_dma_latency`, `pm_qos_resume_latency_us`), `Documentation/admin-guide/pm/intel_pstate.rst` (active/passive, HWP, EPP, `no_turbo`, `hwp_dynamic_boost`), `Documentation/admin-guide/pm/amd-pstate.rst`, `Documentation/admin-guide/pm/intel_uncore_frequency_scaling.rst`, `Documentation/power/powercap/powercap.rst` (RAPL `sysfs`); `drivers/idle/intel_idle.c` for the per-part exit-latency/residency tables quoted in §55.2.1.
- Intel, *Intel® 64 and IA-32 Architectures Software Developer's Manual*, Vol. 3B ch. 15 "Power and Thermal Management": HWP (`IA32_HWP_REQUEST`, `IA32_HWP_CAPABILITIES`), `IA32_APERF`/`IA32_MPERF`, package RAPL (`MSR_PKG_POWER_LIMIT`, `MSR_PKG_ENERGY_STATUS`), `MSR_PACKAGE_THERM_STATUS` power-limit bits; Vol. 4 for `MSR_UNCORE_RATIO_LIMIT` (0x620) and `MSR_VR_CURRENT_CONFIG`.
- AMD, *Processor Programming Reference (PPR) for AMD Family 19h/1Ah*, the CPPC and P-state sections; *AMD EPYC™ 9004/9005 Series Tuning Guides* (Determinism mode, DF C-states, Infinity Fabric clock, "Global C-state Control").
- `turbostat` (Len Brown, `tools/power/x86/turbostat` in the kernel tree) — the `Bzy_MHz`/`Avg_MHz`/`CPU%c1`/`CPU%c6`/`Pkg%pc6`/`PkgWatt`/`SMI` columns and the `--cpu` restriction; `cpupower` (`tools/power/cpupower`) — `idle-info`, `idle-set -d`, `frequency-info`.
- `tuned` project documentation, the `latency-performance` and `network-latency` profile sources (`force_latency`, `min_perf_pct`, EPP settings) — what the daemon actually writes.
- rt-tests, `cyclictest(8)` — the `-i` interval that makes wake-up-after-idle the quantity measured.

## 55.8 Additional Reading

- Ch. 6 (*System Setup*) — the overview this chapter deepens; Ch. 7 (*Choosing & Clocking the Hot-Path CPU*) — the pinned-turbo posture and the soak that qualifies a ratio; Ch. 35 §35.2–§35.3 — the AVX licence measurement; Ch. 57 (*Keeping the Hot Path Warm*) — the cache/TLB/predictor half of the first-message cost; Ch. 104 §104.4.2 — the production sampler.
- Brendan Gregg, *Systems Performance* (2nd ed.), ch. 6 "CPUs" — `cycles` vs `ref-cycles`, turbostat, and the frequency-aware interpretation of utilisation; "CPU Utilization is Wrong" (blog).
- Travis Downs, *Performance Matters* blog, "Gathering Intel on Intel AVX-512 Transitions" — the licence-transition mechanism and measurement, complementary to Ch. 35.
- Intel, *Intel® Xeon® Processor Scalable Family — BIOS and Workload Tuning Guides* (per generation): the firmware names for C-state, P-state, UFS, power-limit and SNC knobs that Appendix C.1 lists.
- Appendix C (the checklist, with this chapter's PM QoS and uncore lines), Appendix I.6 (one-liners), Appendix Q (AMD), Appendix R (per-generation C-state and licence behaviour).

---

*Next: Ch. 56 — Hunting Residual Noise on Isolated Cores, the continuation of Ch. 54's `cyclictest` loop for when the max is *still* not flat and the clock *is*: the catalog of what can still run on an `isolcpus`/`nohz_full` core — per-CPU kthreads, workqueues, RCU, IPIs, managed IRQs, NMIs, firmware — each with its fingerprint and its knob, and the `rtla osnoise`/`timerlat` stop-on-threshold hunt that catches the spike in the act.*
