# Part II — CPU Microarchitecture

# Chapter 17 — Timekeeping: TSC, rdtsc & Clock Sources

> **Reference machine note:** back to the single-socket **Xeon Gold 6326** (Ice Lake-SP) of Ch. 7–15, with **invariant TSC** (constant-rate, not affected by P-states/turbo). All conventions (flags, pinning, turbo off for measurement) carry over.
>
> **Prerequisites:** Ch. 3 (micro-benchmarking — this chapter is the *instrument* every benchmark in the book relies on; coordinated omission and the measurement overhead caveats live here too), Ch. 11 (out-of-order execution — why an *unserialized* `rdtsc` measures the wrong thing), Ch. 6 (system setup — turbo/C-states/`tsc` clocksource the OS exposes).
>
> **Leads into:** Ch. 58 (PTP & hardware timestamping — distributing time *across hosts* and measuring true wire-to-wire, the multi-machine sequel to this single-host chapter), Ch. 55 (NIC hardware timestamps), Ch. 71 (logging — every timestamped log line uses this clock), Ch. 76 (end-to-end latency measurement). Closes **Part II**.

---

## 17.1 Why it matters: you measure nanoseconds, so the clock matters

This entire book asks you to reason in nanoseconds — a cache miss is ~80 ns, a branch mispredict ~6 ns, a remote-DRAM access ~150 ns (Ch. 7, 13, 16). But *how* do you measure a nanosecond? The answer turns out to matter enormously, because at these scales the **measurement instrument itself** is a significant fraction of what you're measuring. A careless `std::chrono::high_resolution_clock::now()` can cost *tens to hundreds of nanoseconds* per call — more than the operation you're trying to time — and a careful `rdtsc` reads the CPU's cycle counter in ~6–25 ns. Get the clock wrong and every measurement in your book of optimizations is corrupted: you'll "optimize" the timer overhead, miss real regressions in the noise, or mis-attribute latency. The clock is the ruler; a warped ruler invalidates every length.

For HFT this is not just about benchmarking — **timestamping is a first-class product feature.** A tick-to-trade measurement needs a timestamp on the inbound packet and another on the outbound order, differenced to nanoseconds; a feed handler stamps every message for sequencing, latency monitoring, and compliance capture (Ch. 75); strategies reason about the *age* of a quote. These timestamps are taken on the **steady-state hot path**, where the rules of this book apply: no syscalls, no locks, no surprises. A timestamp that secretly traps into the kernel, or that jumps backward when the thread migrates cores, or that drifts as the CPU changes frequency, injects exactly the jitter (Ch. 1) you're trying to eliminate — *and* produces wrong numbers. So timekeeping is two problems at once: a **correct, cheap on-host clock** for hot-path timestamps and measurement, and (Ch. 58) **synchronized clocks across hosts** for end-to-end latency.

The good news is that modern x86 gives you an excellent on-host clock for free: the **invariant TSC**, a per-core cycle counter that ticks at a constant rate regardless of frequency scaling, readable in user space with the `rdtsc`/`rdtscp` instructions in a handful of nanoseconds. The bad news is a thicket of pitfalls — out-of-order reordering of the read, core-to-core skew, the difference between counting *cycles* and measuring *time*, and the several-orders-of-magnitude cost difference between clock sources — that this chapter exists to navigate. Use the TSC correctly and you have a near-free nanosecond ruler; use it carelessly and you measure noise.

---

## 17.2 Mental model

### 17.2.1 Invariant TSC; `rdtsc` vs `rdtscp` and serialization

The **Time Stamp Counter (TSC)** is a 64-bit counter the CPU increments at a fixed rate, read with the `rdtsc` instruction (returns the count in `edx:eax`). Two properties make it usable as a clock, and two instructions read it:

- **Invariant TSC.** On modern CPUs (since ~Nehalem/Sandy Bridge) the TSC is **invariant**: it ticks at a *constant* nominal frequency — the CPU's base frequency — **independent of the current P-state, turbo, or C-state.** This is the property that makes it a clock rather than a cycle-rate gauge: it advances at the same rate whether the core is at 800 MHz or 3.5 GHz, and keeps advancing in idle. (Confirm: `constant_tsc` and `nonstop_tsc` in `/proc/cpuinfo` flags; Ch. 6.) **Crucially, the TSC frequency is *not* the core frequency** — it's the base/reference frequency, so you cannot derive wall-clock time by assuming the TSC ticks once per core cycle. You must **calibrate** the TSC-ticks-to-nanoseconds factor (§17.3).

- **`rdtsc` is not serializing — and that's a trap.** `rdtsc` is an ordinary instruction the out-of-order engine (Ch. 11) can **reorder** with the code around it: the CPU may execute the `rdtsc` *before* the work you meant to time finishes, or *after* later work starts, so your interval brackets the wrong instructions. To time a region you must *fence* the reads:
  - **`rdtscp`** reads the TSC *and* an auxiliary value (the core id) and has weak ordering guarantees — it waits for prior instructions to retire (a partial serialization), so it's the standard choice for the **end** of a timed region. Pair with an `lfence` before the *start* read.
  - **`lfence; rdtsc`** (load-fence then read) is the common idiom to stop the *start* read from floating earlier. The robust pattern is `lfence; rdtsc` to begin and `rdtscp; lfence` (or `lfence; rdtsc`) to end — fencing both edges so the counter reads bracket exactly your region. (Avoid the heavier `cpuid` serialization of older guides; `lfence` is the modern, cheaper fence for this.) The cost of the fence is real (it limits overlap — Ch. 11), so there's a tension: the *more* you serialize for accuracy, the *more* the measurement perturbs a tiny region (§17.4.1, Ch. 3).

- **`rdtscp` also gives you the core id.** The aux value lets you detect **core migration** (§17.2.2): if the start and end reads ran on different cores, the interval may be invalid (skew), and you can discard or correct it.

The model: **`rdtsc`/`rdtscp` reads a constant-rate counter cheaply, but you must (1) fence it to time the right instructions and (2) calibrate ticks→ns because TSC rate ≠ core rate.**

### 17.2.2 Clock sources and their cost

"What time is it?" can be answered by several mechanisms spanning *three orders of magnitude* in cost — the single most important practical fact about timestamping on the hot path:

```
   mechanism                    typical cost     properties
   ─────────────────────────────────────────────────────────────────────────────
   rdtsc / rdtscp (+ lfence)    ~6–25 ns         userspace, per-core counter, must calibrate
   clock_gettime via vDSO       ~15–30 ns        no syscall (mapped page), MONOTONIC, ns API
   clock_gettime (real syscall) ~hundreds of ns  kernel trap — if vDSO/clocksource not tsc
   gettimeofday / HPET / ACPI   ~hundreds-1000 ns slow hardware clock sources
```

The key ideas:

- **The Linux `clocksource` matters enormously.** `clock_gettime(CLOCK_MONOTONIC)` is fast **only if** the kernel's selected clocksource is `tsc` *and* it's served via the **vDSO** (a kernel page mapped into your process so the read happens in user space with **no syscall**). Check `/sys/devices/system/clocksource/clocksource0/current_clocksource` — it must read `tsc`. If it has fallen back to `hpet` or `acpi_pm` (slow hardware clocks), *every* `clock_gettime` becomes a real, expensive access (sometimes a syscall), silently adding hundreds of ns and jitter to your timestamps (§17.5, Ch. 6).
- **vDSO `clock_gettime` vs raw `rdtsc`.** vDSO `clock_gettime` is the *convenient correct default*: it returns nanoseconds directly, is monotonic, handles calibration/scaling and core-skew for you (the kernel maintains the TSC→ns transform), and on a healthy box costs only ~2–3× a raw `rdtsc`. Raw `rdtsc` is the *lowest-overhead* option when you need every nanosecond and will handle calibration/fencing/skew yourself. **Default to vDSO `clock_gettime(CLOCK_MONOTONIC)`; reach for raw `rdtsc` only when measured overhead justifies it** (§17.4.1).
- **Per-core skew and migration.** Each core has its own TSC. On a healthy modern box they're synchronized at boot (the kernel checks; `tsc` clocksource implies it trusts them), but reading the raw counter on two different cores can still show small offsets, and a thread that **migrates** mid-measurement can see the TSC jump (forward or backward). Pin the measuring thread (Ch. 42), and/or use `rdtscp`'s core id to detect migration (§17.2.1). The vDSO path handles this for you.
- **`CLOCK_MONOTONIC` vs `CLOCK_REALTIME`.** For *intervals* always use a **monotonic** clock — `CLOCK_REALTIME` can jump (NTP steps, leap seconds, admin changes) and produce negative or absurd durations. `CLOCK_MONOTONIC`/`_RAW` never goes backward. (For cross-host *wall-clock* comparison you need synchronized real time — PTP, Ch. 58.)

The model: **timestamping cost spans 6 ns to 1000 ns depending on the clocksource and whether you stay in user space; verify the box uses the `tsc` clocksource + vDSO, default to vDSO `clock_gettime`, and drop to fenced `rdtsc` only when you've measured that you need to.**

---

## 17.3 Measure it: TSC calibration against a reference clock

Two things to establish empirically: (1) the **cost** of each timestamp mechanism (so you pick the right one — §17.2.2), and (2) the **TSC→nanoseconds** conversion factor (since TSC rate ≠ core rate — §17.2.1). Calibrate the TSC by reading it at the endpoints of a known `CLOCK_MONOTONIC` interval.

```cpp
// tsc.cpp — TSC read cost, vDSO clock_gettime cost, and TSC->ns calibration.
// Build: g++ -O2 -std=c++20 -march=native tsc.cpp -o tsc
// Run pinned (avoid migration), turbo off:  taskset -c 2 ./tsc
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <x86intrin.h>     // __rdtsc, __rdtscp, _mm_lfence

static inline std::uint64_t rdtsc_start() {     // fence BEFORE so it can't float earlier
    _mm_lfence(); std::uint64_t t = __rdtsc(); _mm_lfence(); return t;
}
static inline std::uint64_t rdtsc_end() {       // rdtscp waits for prior insns to retire
    unsigned aux; std::uint64_t t = __rdtscp(&aux); _mm_lfence(); return t;
}
static inline std::uint64_t mono_ns() {
    timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);   // vDSO path on a healthy box
    return std::uint64_t(ts.tv_sec) * 1'000'000'000ull + ts.tv_nsec;
}

int main() {
    // (1) Calibrate TSC->ns: bracket a ~200 ms monotonic interval with TSC reads.
    std::uint64_t ns0 = mono_ns(),  c0 = rdtsc_start();
    timespec req{0, 200'000'000};  nanosleep(&req, nullptr);
    std::uint64_t c1 = rdtsc_end(), ns1 = mono_ns();
    double tsc_hz = double(c1 - c0) / (double(ns1 - ns0) / 1e9);
    double ns_per_tsc = 1e9 / tsc_hz;
    std::printf("TSC freq ~ %.4f GHz   (%.6f ns / tsc tick)\n", tsc_hz / 1e9, ns_per_tsc);

    // (2) Cost of each mechanism: median-ish over many back-to-back reads.
    constexpr int R = 2'000'000;
    std::uint64_t s = 0;
    std::uint64_t a = rdtsc_start();
    for (int i = 0; i < R; ++i) s += __rdtsc();                 // raw rdtsc (no fence)
    std::uint64_t b = rdtsc_end();
    std::printf("raw rdtsc        : %.2f ns/call\n", (b - a) * ns_per_tsc / R);

    a = rdtsc_start();
    for (int i = 0; i < R; ++i) s += rdtsc_end();               // fenced rdtscp
    b = rdtsc_end();
    std::printf("fenced rdtscp    : %.2f ns/call\n", (b - a) * ns_per_tsc / R);

    a = rdtsc_start();
    for (int i = 0; i < R; ++i) s += mono_ns();                 // vDSO clock_gettime
    b = rdtsc_end();
    std::printf("clock_gettime    : %.2f ns/call  (sink=%llu)\n",
                (b - a) * ns_per_tsc / R, (unsigned long long)s);
    return 0;
}
```

Before running, **verify the clocksource** (this is the measurement that decides everything):

```
$ cat /sys/devices/system/clocksource/clocksource0/current_clocksource
tsc                         # must be 'tsc'; 'hpet'/'acpi_pm' means clock_gettime is SLOW
$ grep -o 'constant_tsc\|nonstop_tsc' /proc/cpuinfo | sort -u
constant_tsc                # invariant TSC present
nonstop_tsc
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, base ~2.9 GHz), `tsc` clocksource + vDSO, pinned, turbo off (illustrative):

```
TSC freq ~ 2.9000 GHz   (0.344828 ns / tsc tick)        <- calibrated; ~base freq, NOT turbo
raw rdtsc        : ~6 ns/call
fenced rdtscp    : ~12 ns/call                          <- fences cost ~2x, but measure the right thing
clock_gettime    : ~18 ns/call                          <- vDSO: convenient, monotonic, ~3x raw rdtsc
```

The lessons land immediately: the calibrated TSC frequency comes out near the **base** clock (~2.9 GHz), *not* the turbo frequency — confirming TSC ticks ≠ core cycles (§17.2.1), so timing in "TSC ticks" and multiplying by `ns_per_tsc` is correct while assuming "one tick = one core cycle" is wrong. The **fences roughly double** the raw `rdtsc` cost — the price of measuring the *right* instructions (§17.2.1) — which matters when you bracket a region only a few ns long (the measurement perturbs it; Ch. 3). And vDSO `clock_gettime` at ~18 ns is only ~3× a raw `rdtsc` while giving you nanoseconds, monotonicity, and migration-safety for free — which is why it's the sensible default (§17.4.1). If your box instead shows `clock_gettime` at *hundreds* of ns, your clocksource is **not** `tsc`/vDSO — fix that (Ch. 6) before trusting any timing.

---

## 17.4 Techniques

### 17.4.1 Reliable nanosecond timestamping on the hot path

Choosing and using the clock correctly on the steady-state hot path:

- **Default to vDSO `clock_gettime(CLOCK_MONOTONIC)`.** It's monotonic, returns nanoseconds, handles calibration/scaling/skew, and on a healthy box costs ~15–25 ns with **no syscall**. For the vast majority of hot-path timestamping (stamping a message, measuring a stage) this is the right tool — correct by construction, cheap enough. **Verify it's the vDSO/`tsc` path** (§17.3); a fallback clocksource silently turns it into a syscall.
- **Use fenced `rdtsc`/`rdtscp` when you need the last few nanoseconds.** For timing very short regions, ultra-tight measurement loops, or when ~10 ns/timestamp matters at your volume, read the TSC directly: `lfence; rdtsc` to start, `rdtscp; lfence` to end, convert with the calibrated `ns_per_tsc`. Accept the responsibilities: calibrate once at startup, pin the thread (or check `rdtscp`'s core id for migration), and fence appropriately.
- **Right-size the serialization.** Fences cost overlap (Ch. 11). For *coarse* intervals (µs+), a plain `rdtsc` pair without fences is fine — the reorder window is tiny relative to the interval. For *nanosecond* intervals, fence both edges or the measurement is meaningless. Match fencing to the interval length; don't over-serialize coarse measurements or under-serialize fine ones.
- **Subtract the measurement overhead.** The timestamp call itself has a cost (§17.3); for tiny regions, measure an empty bracket (`t1 = clock(); t2 = clock();`) and subtract its median, or amortize by timing *N* iterations and dividing (Ch. 3). Don't report timer overhead as the thing's latency.
- **Stamp, don't format, on the hot path.** Capture the raw timestamp (a TSC tick or ns integer) inline and defer any conversion/formatting to off the hot path (Ch. 71's logging discipline). A timestamp is one cheap read; turning it into a human string is not hot-path work.
- **Beware coordinated omission (Ch. 3).** When timestamping request latencies, a stalled system that stops *issuing* requests hides its own worst latencies. Timestamp by *intended* arrival time, not just service time — the clock is correct, but *what* you time must avoid the coordinated-omission trap (Ch. 3 in full).

### 17.4.2 On-host hardware timestamping

When software timestamps (even `rdtsc`) aren't precise or comparable enough — notably for **wire-to-wire** latency — push timestamping into hardware:

- **NIC hardware timestamps (Ch. 55).** Modern NICs can stamp each packet with a hardware clock at the moment it crosses the wire (RX) or is sent (TX), via `SO_TIMESTAMPING` (`SOF_TIMESTAMPING_RX_HARDWARE`/`TX_HARDWARE`) or the driver's facilities. This removes the software stack's variable latency from the measurement: you get the *true* time the bytes arrived, not when your code got around to reading them. Essential for honest tick-to-trade numbers (Ch. 76).
- **Correlating the NIC clock with the TSC.** The NIC's PHC (PTP Hardware Clock) and the host TSC are *different* clocks; to compare a hardware RX timestamp with a software TSC stamp you must relate them (cross-timestamping: the kernel's `PTP_SYS_OFFSET`/`get_device_system_crosststamp` reads both near-simultaneously). This is the bridge into Ch. 58.
- **PTP for cross-host time (Ch. 58).** The on-host TSC tells you *intervals on this box*; to compare a timestamp taken *here* with one taken on *another machine* (the exchange, a matching engine, a peer) you need the clocks **synchronized** — PTP (IEEE 1588) disciplines the NIC PHC to a grandmaster to sub-microsecond (sub-ns with enhancements), and that's the entire subject of Ch. 58. This chapter is the single-host foundation; Ch. 58 is the distributed sequel.

The principle: **the closer to the wire you timestamp, the more honest the latency number** — software TSC for on-host stage timing, NIC hardware timestamps for wire-to-wire, PTP-disciplined clocks for cross-host (Ch. 55, 58).

---

## 17.5 Pitfalls & anti-patterns: unserialized `rdtsc`, frequency drift

- **Unserialized `rdtsc` timing a tiny region.** The headline trap: `rdtsc` reorders (Ch. 11), so an unfenced start/end pair brackets the wrong instructions and your "5 ns" measurement is noise. Fence both edges (`lfence; rdtsc` / `rdtscp; lfence`) for nanosecond intervals (§17.2.1).
- **Assuming TSC ticks = core cycles.** The invariant TSC runs at the **base** frequency, not the current (turbo-boosted or throttled) core frequency (§17.2.1, §17.3). Converting ticks→ns with the core's *current* clock — or counting "cycles" via TSC while turbo varies frequency — is wrong. Calibrate ticks→ns against a monotonic reference.
- **Wrong clocksource silently slow.** If `/sys/.../current_clocksource` is `hpet`/`acpi_pm` (not `tsc`), every `clock_gettime` is hundreds of ns and jittery (§17.2.2). This sabotages every timestamp on the box. Verify and fix the clocksource (Ch. 6) — a top item before any latency work.
- **TSC read across a core migration.** Reading the raw TSC on different cores (thread migrated mid-measurement) can show skew or a backward jump. Pin the thread (Ch. 42) and/or check `rdtscp`'s core id; or use the vDSO path which is migration-safe (§17.2.2).
- **Using `CLOCK_REALTIME` for intervals.** Real-time can step (NTP/PTP corrections, leap seconds) and yield negative/absurd durations. Use `CLOCK_MONOTONIC`/`_RAW` for intervals; reserve real time for cross-host wall-clock comparison (Ch. 58).
- **Ignoring measurement overhead on tiny regions.** Timing a 3 ns operation with a 12 ns timer pair without subtracting the ~12 ns overhead reports mostly the timer. Subtract an empty bracket or amortize over N iterations (§17.4.1, Ch. 3).
- **`std::chrono::high_resolution_clock` assumptions.** It may alias `system_clock` (non-monotonic!) on some implementations; prefer `std::chrono::steady_clock` (monotonic) — but know it ultimately calls the same `clock_gettime`, so its cost/quality is the clocksource's.
- **Old `cpuid`-serialization advice.** Bracketing `rdtsc` with `cpuid` (a full serialize) is the legacy idiom; it's far heavier than needed and perturbs more. Use `lfence`/`rdtscp` on modern CPUs (§17.2.1).
- **Comparing timestamps across hosts without synchronization.** Two unsynchronized TSCs/clocks on different machines are not comparable to nanoseconds; differencing them yields garbage. That requires PTP (Ch. 58), not this chapter's on-host clock.

---

## 17.6 Exercises & checklist

**Exercises**

1. **Calibrate and cost the clocks.** Build `tsc.cpp` pinned/turbo-off. Confirm the calibrated TSC frequency ≈ your CPU's **base** (not turbo) clock. Record ns/call for raw `rdtsc`, fenced `rdtscp`, and vDSO `clock_gettime`. By what factor do the fences and the vDSO add cost?
2. **Prove `rdtsc` reorders.** Time a tiny region (a few dependent adds) with *unfenced* vs *fenced* `rdtsc`. Show the unfenced version gives unstable/implausible (even zero/negative) intervals while the fenced version is stable. Explain via out-of-order execution (Ch. 11).
3. **Break the clocksource.** Temporarily switch the clocksource to `hpet` (`echo hpet > /sys/.../current_clocksource`, root, test box only) and re-run the `clock_gettime` cost. How much slower? Switch back to `tsc`. Why does this silently corrupt latency work (§17.5)?
4. **Detect migration.** Use `rdtscp`'s aux (core id) to flag when start/end reads occur on different cores. Run *unpinned* under load and count invalid intervals; then pin (Ch. 42) and confirm they vanish.
5. **Overhead subtraction.** Time an empty `clock_gettime` bracket; then time a known ~50 ns operation with and without subtracting the timer overhead. Quantify the error the overhead introduces, and relate to amortizing over N (Ch. 3).

**Checklist — timekeeping**

- [ ] I verified `/sys/.../current_clocksource` is **`tsc`** and `constant_tsc`/`nonstop_tsc` are present (Ch. 6) — `clock_gettime` is on the **vDSO** fast path.
- [ ] On the hot path I **default to vDSO `clock_gettime(CLOCK_MONOTONIC)`**, dropping to fenced `rdtsc`/`rdtscp` only where measured overhead justifies it.
- [ ] Nanosecond-scale `rdtsc` intervals are **fenced both edges** (`lfence`/`rdtscp`); coarse intervals aren't over-serialized.
- [ ] I **calibrated** TSC ticks→ns against a monotonic reference and do **not** assume TSC ticks = core cycles (it's the **base** frequency).
- [ ] Measuring threads are **pinned** (Ch. 42) and/or I check `rdtscp` core id for migration; I use a **monotonic** clock for intervals, never `CLOCK_REALTIME`.
- [ ] I **subtract timer overhead** (or amortize over N) for tiny regions, and stamp-not-format on the hot path (Ch. 71).
- [ ] For wire-to-wire / cross-host latency I use **hardware timestamps** (Ch. 55) and **PTP** (Ch. 58), not bare on-host TSC differences.
- [ ] I accounted for **coordinated omission** (Ch. 3) in *what* I timestamp, separate from the clock's correctness.

---

## 17.7 References

- Intel, *64 and IA-32 Architectures Software Developer's Manual* (Vol. 3, "Time-Stamp Counter") and the *Optimization Reference Manual* — invariant TSC semantics, `rdtsc`/`rdtscp`, and the recommended `lfence`/`rdtscp` serialization (§17.2.1).
- G. Paoloni, *"How to Benchmark Code Execution Times on Intel IA-32 and IA-64"* (Intel white paper) — the canonical `rdtsc`/`rdtscp` + serialization methodology behind §17.2–§17.4.
- The Linux documentation and man pages — `clock_gettime(2)`, `vdso(7)`, `time(7)`, the `clocksource` sysfs interface, and `SO_TIMESTAMPING`/`Documentation/networking/timestamping` (the clocksource and hardware-timestamp controls of §17.2.2, §17.4.2).
- A. Fog, *Optimizing software in C++* and *Instruction Tables* — `rdtsc`/`rdtscp`/`lfence` latency and the cost of serialization (the numbers in §17.3).
- D. Terpstra et al. / the PAPI and `perf` timing documentation — alternatives and cross-checks for cycle/time measurement (ties Ch. 2–3).

## 17.8 Additional Reading

- The `tsc` kernel documentation and LWN articles on TSC reliability, the vDSO, and clocksource selection — background on why a box might fall off the `tsc` clocksource.
- Carl Cook, *"When a Microsecond Is an Eternity"* (CppCon) — measurement discipline at HFT latency scales, complementing Ch. 3 and this chapter.
- Ch. 55 (*NIC Features & Offloads*) — NIC hardware timestamping; Ch. 58 (*Clock Synchronization & Hardware Timestamping (PTP)*) — the distributed, cross-host sequel to this chapter; Ch. 3 (*Micro-benchmarking*) — the statistical methodology this clock feeds; Ch. 71 (*Zero-Overhead Logging*) — stamp-not-format timestamping in logs.
- **Appendix E** — the latency numbers (and the measurement notes) this chapter's clock produces; **Appendix C** — the clocksource/`tsc` system-tuning settings consolidated.

---

*Next: Ch. 18 — Compile-Time Mechanics, opening Part III. Having spent Part II on what the hardware does at runtime, we turn to moving work *off* runtime entirely: `constexpr`/`consteval`, computing at compile time what would otherwise cost cycles on the hot path, and the cost model of templates.*
