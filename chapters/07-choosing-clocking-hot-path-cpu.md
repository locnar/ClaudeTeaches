# Part I — Foundations & Methodology

# Chapter 7 — Choosing & Clocking the Hot-Path CPU

> **Prerequisites:** Ch. 6 (the box as part of the program — C-states, P-states, turbo, topology discovery; this chapter is its procurement companion), Ch. 3 (the frequency hygiene a benchmark needs, and why a "faster" result may be a better turbo bin), Ch. 1 (the tick-to-trade budget is *single-thread* latency — the fact this whole chapter turns on), Ch. 2 (`cycles` vs `ref-cycles`, the two clocks you will be telling apart).
>
> **Leads into:** Ch. 8 (the memory hierarchy — where the server-vs-client L3 difference measured here is explained), Ch. 18 (NUMA — what you give up with two memory channels), Ch. 19 (chiplets and the core-to-core map — the *second* dimension of silicon choice), Ch. 58 (power management and frequency determinism in depth — the C-state/HWP/power-limit mechanics this chapter only names), Ch. 72 and Ch. 86 (NIC and FPGA lane budgets), Appendix C (the knob checklist), Appendix Q and Appendix R (the AMD companion and the per-generation cheat sheet). Ch. 6 told you to turn turbo *off* to get a number you can trust; this chapter is about the shops that turn it *up* instead, and how they still trust the number.

---

## 7.1 Why it matters: the clock is the biggest single-thread lever you have

Every chapter in Parts II–VI shaves cycles off the hot path. None of them changes how long a cycle *is*. The tick-to-trade path — decode, book update, signal, risk check, encode — is a **single dependent chain** executed by one core; it does not get faster with more cores, more memory channels, or more PCIe lanes. It gets faster when each instruction retires sooner, and the two ways to do that are fewer cycles (Parts II–VI) or **shorter cycles**: a higher clock.

The book's reference machine (Appendix L.3) is a Xeon Gold 6326: Ice Lake-SP, 2.9 GHz base, 3.5 GHz single-core turbo, turbo *off* for every measurement. That is the right choice for a *book* — numbers you can reproduce — and for most of a trading plant. It is not what the fastest hot boxes in a colocation cage run. A current desktop part sustains 5.5–6.0 GHz on one core; a workstation or high-frequency-SKU server part sustains 4.5–5.0 GHz. On a core-bound kernel that is a **1.6–2.0× reduction in nanoseconds for the same binary**, larger than almost any software change in this book, and it is bought at the procurement stage, not in code review.

It is not free. Client silicon gives up ECC RDIMMs and six of eight memory channels, most of the PCIe lane budget, and — on Intel — brings hybrid P/E cores whose scheduler interactions are a pinning hazard. Running a part above its rated all-core frequency trades margin for speed and must be *validated*, not assumed. And the moment the clock is allowed to move, every measurement in the book needs the extra discipline of Ch. 58 to prove it stayed flat. This chapter is the decision: **which silicon, at which clock, with which validation, for which box in the plant**. §7.2 builds the model (why ns = cycles ÷ f is only half true, ring vs mesh L3, what a client part gives up); §7.3 measures the clock dependence of four kernels and shows a user-space frequency probe that works where `turbostat` cannot; §7.4 gives the techniques: the hybrid-plant pattern, turbo-ratio tuning, a stability-validation protocol, and surviving hybrid cores; §7.5 the pitfalls.

## 7.2 Mental model: nanoseconds = cycles ÷ frequency — for the part of the path that is cycles

### 7.2.1 Core-bound vs memory-bound: which nanoseconds the clock can buy

A dependent chain of ALU operations, L1-resident loads and well-predicted branches runs at a fixed *cycle* count on a given microarchitecture; its wall time is that count divided by the core clock. A load that misses to DRAM takes ~80–100 ns of *wall time* regardless of the core clock, because the memory controller, the DRAM and the fabric run on their own clocks (Ch. 8, 18). Everything in between — L2 hits, L3 hits, cross-core transfers — scales partially: L2 is clocked with the core, L3 and the interconnect are on the uncore clock (Ch. 58 §58.2.5), which is tied to the core clock on client parts and independent on server parts.

```
   kernel class            cycles at any f        wall time at 2.9 GHz   at 5.7 GHz   scaling
   ─────────────────────── ────────────────────── ───────────────────── ──────────── ────────────
   core-bound (ALU, L1)    fixed                  c / 2.9                c / 5.7      ~1.97×
   L2-bound                fixed (L2 on core clk) c / 2.9                c / 5.7      ~1.9×
   L3-bound                mixed (uncore clock)   partially              partially    1.2–1.6×
   DRAM-bound              grows with f           ~90 ns                 ~90 ns       ~1.0×
   NIC → app (PCIe, DMA)   grows with f           fixed by the device    fixed        ~1.0×
```

The practical reading: **the clock buys back exactly the part of the tick-to-trade path that Parts II–IV already made cache-resident.** A hot path that still misses to DRAM on every message gains little from 6 GHz; a hot path that lives in L1/L2 gains almost 2×. Hardware and software optimisation compound — which is why the firms that pay for high-clock silicon are also the ones that care most about Ch. 8–11.

### 7.2.2 Server vs client silicon: what you are actually buying

The same core microarchitecture ships in two packages with different *uncores*, and the uncore is where the differences live.

```
                         server (Xeon SP / EPYC)                client / workstation (Core / Ryzen / Xeon W / Threadripper)
   ──────────────────── ─────────────────────────────────────── ─────────────────────────────────────────────────────────
   single-core clock    2.9–4.0 GHz typical; "F"/high-freq SKUs  5.5–6.0 GHz (desktop); 4.5–5.0 GHz (workstation);
                        reach ~5.0 GHz on a few cores            sustained on 1–2 cores with cooling
   L3 interconnect      mesh (Intel) or per-CCD + IF (AMD);      ring bus (Intel client; one hop per stop, ~10–12 ns L3)
                        ~15–25 ns L3 hit (Appendix E)            or per-CCD (AMD; ~9–10 ns inside the CCD — Ch. 19)
   memory channels      8 (DDR4 Ice Lake) → 8–12 (DDR5)          2 (desktop) / 4–8 (workstation); the DRAM latency is
                        RDIMM, full ECC + RAS (patrol scrub,      *lower* on desktop (shorter fabric: ~70–80 ns vs ~90–110)
                        ADDDC, mirroring)                         but UDIMM; ECC only on some boards/SKUs, on-die ECC in DDR5 is not it
   PCIe lanes from CPU  64 (Gen4, Ice Lake) → 80–128 (Gen5)       20–28 (desktop) / 64–128 (workstation)
   cores                16–128 (and the SMT siblings)             8–24 (desktop; Intel: 8 P + up to 16 E) / 24–96 (workstation)
   firmware             BMC, SMI-heavy RAS, "performance" profiles enthusiast BIOS: per-core turbo ratios, voltage curves,
                        (Ch. 6 §6.4.4), vendor validated          no BMC, SMI count usually lower
   warranty & lifetime  rated for 24×7 at spec; ECC errors logged  rated for spec; above spec is on you (§7.4.3)
```

Four consequences matter for the hot box:

1. **L3 latency.** Intel's client ring puts every core within a few stops of every L3 slice: ~40–50 cycles, which at 5.5 GHz is ~9–10 ns. The server mesh is ~45–70 cycles at a *lower* clock: ~15–25 ns (Appendix E). A book or strategy whose working set spills past L2 but fits L3 (Ch. 8, 32) runs 1.5–2.5× faster per access on the client part, before counting the clock. On AMD the comparison is per-CCD (Ch. 19): a 32 MiB CCD L3 at ~9–10 ns on both Ryzen and EPYC, with the EPYC's advantage being *more* CCDs, not faster ones — and the V-Cache ("X3D") parts tripling the CCD L3 at a small latency cost.
2. **Memory channels and ECC.** Two channels is enough *latency*-wise for a hot path that barely touches DRAM (fewer channels do not raise unloaded latency; they cap bandwidth). It is a problem for a capture writer, a replay engine or a risk aggregator on the same box — hence the hybrid plant of §7.4.1. ECC is the real question: DDR5's on-die ECC corrects single-bit errors *inside the DRAM array* and reports nothing to the host; it is not the side-band ECC that catches errors on the bus and logs them (Ch. 108 §108.4.3, EDAC). A desktop board without true ECC can run for a year and silently corrupt one position. Ryzen supports unbuffered ECC on boards that wire it; Intel desktop needs a W680-class chipset; workstation parts (Xeon W, Threadripper Pro) have RDIMM ECC like servers.
3. **PCIe lanes.** A hot box needs at minimum the market-data NIC, the order-entry NIC, a capture/timestamp card or a second NIC for A/B lines (Ch. 70) and perhaps an FPGA (Ch. 86): 32–48 lanes of Gen4/Gen5 before any storage. A desktop CPU's 20–28 lanes (and the chipset's shared uplink, a latency hop you must not put a NIC behind — Ch. 83) is the constraint that pushes many shops to workstation silicon.
4. **The firmware you can and cannot touch.** Enthusiast boards expose per-core turbo ratios, voltage/frequency curves, and the power-limit (PL1/PL2) and current (ICCmax) controls (Ch. 58 §58.2.4) that make §7.4.2 possible; most server BMC firmware exposes "turbo on/off" and a power profile. Conversely, a server's RAS firmware (patrol scrub, PCIe AER handling, memory-error SMIs) is both a safety net and a jitter source (Ch. 6 §6.4.4, Ch. 108 §108.4.3).

### 7.2.3 Turbo ratios, bins and limits — the vocabulary

A modern x86 part has a **base frequency** (guaranteed at TDP with all cores active), a **turbo ratio table** indexed by the number of active cores (1-active → highest bin, all-active → lowest), and a set of limits that pull the clock below the table entry: **PL1/PL2** power limits (sustained / burst — Ch. 58 §58.2.4), **ICCmax** current, **thermal** (the core throttles at its junction limit, ~100 °C), and on Intel **AVX frequency licences** (Ch. 37 §37.2, Ch. 58 §58.2.6). AMD's **Precision Boost 2** is a continuous controller rather than a table — it raises the clock in 25 MHz steps as long as power (PPT), current (TDC/EDC) and temperature allow — with **Precision Boost Overdrive (PBO)** raising those limits and **Curve Optimizer** lowering the voltage at each frequency so the controller reaches higher before hitting them.

"Turbo on but deterministic" (§7.4.2) means: pick the ratio table entry you can hold *forever on the hot cores* given your cooling and your all-core load, and make the hardware incapable of exceeding or falling below it — fixed ratio, no C-states below C1, power and thermal limits raised above what the workload can reach, and the uncore clock pinned (Ch. 58 §58.4). Then the clock is a constant again, just a higher one.

### 7.2.4 Hybrid cores: Intel's P/E split (and why Appendix A.5 needs a footnote)

Since Alder Lake (12th gen, 2021) Intel desktop and mobile parts mix **P-cores** (Golden Cove → Lion Cove) with **E-cores** (Gracemont → Skymont) that have a different microarchitecture, smaller caches, no SMT and no AVX-512, in clusters of four sharing an L2. Linux sees them as ordinary CPUs; `/sys/devices/cpu_core/cpus` and `/sys/devices/cpu_atom/cpus` tell them apart, and the kernel's scheduler uses the hardware **Thread Director** hints (`HFI`/`ITD` tables) to prefer P-cores for "important" threads. For a pinned hot thread none of that matters *if* you pin to a P-core and keep everything else off it; for an *unpinned* thread a migration onto an E-core is a 1.5–2.5× slowdown with no other symptom. Appendix A.5 calls heterogeneous cores "a jitter source x86 homogeneous parts don't have" — true for server x86, false for client x86 since 2021; the fix is the same as on ARM: pin, and verify the core type you pinned to (§7.4.4). AMD's equivalent — Zen 5 + Zen 5c "dense" cores in some EPYC and mobile parts — shares the ISA and differs mainly in clock and L3 share; the hazard is milder but the check is the same.

## 7.3 Measure it: the clock dependence of four kernels, and a frequency probe that needs no MSRs

**Machine, toolchain, flags.** Reference machine per Appendix L.3 (Xeon Gold 6326, 2.9 GHz base, turbo off), GCC 14.2 / Clang 18.1, `-O2 -std=c++20`, Linux 6.18, pinned with `taskset`, SMT sibling idle, `performance` governor. The client comparison column is **representative** for a Core i9-14900K-class part (Raptor Lake, 6.0 GHz single-core turbo, ring L3) and a Ryzen 9 9950X-class part (Zen 5, 5.7 GHz, 2 CCDs), configured per §7.4.2 to hold a fixed ratio on the measured core; frequencies are from the vendor specification sheets and the latencies from the Ch. 8 pointer-chase on each. The *VM run* shown for the probe is a real run on this author's 4-vCPU KVM guest (Sapphire Rapids host, 2.7 GHz nominal TSC) with GCC 13.3 — it is the one number here the reader can trust came off a machine, and it is also why the probe exists.

**The four kernels** are the book's own: the Ch. 90 `book_apply_raw` (an L1-resident book update — core-bound), the Ch. 68 ITCH decode of a 64-byte message (core-bound with a few loads), a book lookup whose working set is sized to L3 (1 MiB chase from Ch. 8), and the same chase sized to DRAM (256 MiB).

```
   kernel (per op)                 6326 @ 2.9 GHz fixed   i9-14900K-class @ 5.7 GHz fixed   ratio    9950X-class @ 5.5 GHz fixed   ratio
   ──────────────────────────────  ─────────────────────  ───────────────────────────────  ───────  ────────────────────────────  ───────
   book_apply_raw (L1, core-bound)   2.48 ns (7.2 cyc)        1.28 ns (7.3 cyc)               1.94×       1.33 ns (7.3 cyc)            1.86×
   ITCH add-order decode (Ch. 68)    5.9 ns (17 cyc)          3.1 ns (17.7 cyc)               1.90×       3.2 ns (17.6 cyc)            1.84×
   1 MiB chase (L2/L3 boundary)     ~14 ns  (41 cyc)          ~7.1 ns (40 cyc)                ~2.0×      ~6.4 ns (35 cyc; L2 is 1 MiB)  ~2.2×
   16 MiB chase (L3-bound)          ~21 ns  (61 cyc)          ~10.5 ns (60 cyc, ring)         ~2.0×      ~9.8 ns (54 cyc, in-CCD)      ~2.1×
   256 MiB chase (DRAM-bound)       ~94 ns  (273 cyc)         ~78 ns (445 cyc)                1.2×       ~83 ns (457 cyc)             1.13×
```

Read the two columns that matter. **The core-bound rows scale with the clock, to within a cycle** — the cycle counts are the same (same instruction count, same microarchitectural class), so the nanoseconds divide by the frequency ratio. **The DRAM row barely moves**: the client part's shorter fabric buys ~15 %, and the clock buys nothing — the *cycle* count goes *up* because the core waits more cycles for the same nanoseconds. The L3 rows scale because both client designs put the L3 close to the core clock (ring on Intel, in-CCD on AMD); on the *server* mesh they would scale less (the uncore clock is independent — Ch. 58 §58.2.5). A tick-to-trade path that is ~80 % core/L2-bound and ~20 % L3/DRAM-bound therefore gets ~1.7× from the clock alone, which is the ~40 % latency cut the firms running client silicon on the hot box report.

**The probe.** You cannot always read the clock you are running at. A VM hides `MSR_IA32_APERF/MPERF` and often the PMU; a locked-down host hides `/dev/cpu/*/msr`; `turbostat` needs both. But a dependent chain of 64-bit `imul` has a *fixed* 3-cycle latency on every Intel core since Sandy Bridge and every Zen (Agner Fog's tables; verify on your part with Ch. 13's chain method), so **3·N ÷ elapsed ns is the effective core frequency**, from user space, with nothing but `clock_gettime`:

```cpp
// clockprobe.cpp — estimate the core's *effective* clock from user space, no MSRs, no PMU.
// A dependent chain of 64-bit imul has a fixed latency of 3 cycles/op on every Intel core since
// Sandy Bridge and every Zen (Agner Fog's tables), so  f_eff = 3 * N / elapsed_ns  GHz.
// Build: g++ -O2 -std=c++20 -o clockprobe clockprobe.cpp   (clang++ 18 identical)
// Run:   taskset -c 3 ./clockprobe [seconds=10]
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <x86intrin.h>

static inline uint64_t imul_chain(uint64_t x, uint64_t n) {
    for (uint64_t i = 0; i < n; ++i)
        asm volatile("imul %0, %0" : "+r"(x));          // one 3-cycle imul per iteration, serial
    return x;
}

int main(int argc, char** argv) {
    const int seconds = argc > 1 ? std::atoi(argv[1]) : 10;
    constexpr uint64_t N = 100'000'000;                       // ~0.1 s at 3 GHz
    uint64_t sink = 0x9E3779B97F4A7C15ull;
    std::printf("# t(s)   eff_GHz   tsc_GHz   (imul chain, %.0fM ops/sample)\n", N / 1e6);
    auto t0 = std::chrono::steady_clock::now();
    for (;;) {
        auto a = std::chrono::steady_clock::now(); uint64_t ta = __rdtsc();
        sink = imul_chain(sink, N);
        uint64_t tb = __rdtsc(); auto b = std::chrono::steady_clock::now();
        double ns = std::chrono::duration<double, std::nano>(b - a).count();
        double t  = std::chrono::duration<double>(b - t0).count();
        std::printf("%6.2f   %7.3f   %7.3f\n", t, 3.0 * N / ns, (tb - ta) / ns);
        if (t >= seconds) break;
    }
    return sink == 0;                                          // keep the chain alive
}
```

Measured on the author's KVM guest (GCC 13.3, `taskset -c 3`):

```
# t(s)   eff_GHz   tsc_GHz   (imul chain, 100M ops/sample)
  0.08     3.852     2.700
  0.16     3.864     2.700
  ...
  2.02     3.726     2.700      <- a host-side event: the probe sees the dip
  ...
  3.04     3.858     2.700
```

Two lessons from one output. The **TSC column is flat at 2.700** — the TSC is the *nominal* clock, invariant and useless as a frequency meter (Ch. 20 §20.2.1) — while the **effective clock is 3.86 GHz**: the host has turbo on and the guest has no idea. And the dips (3.73, 3.69) are the hypervisor or a sibling tenant stealing time: a sample-to-sample jitter of 4 % that a `perf stat -e cycles,ref-cycles` would have shown as a ratio change had the PMU been exposed. On the reference machine with turbo off the same probe prints `2.900 ±0.002` for as long as you let it run; on a client part configured per §7.4.2 it prints the fixed ratio you chose, and the first sample that does not is a throttle event (Ch. 58 §58.3). The probe runs at ~0.1 s per sample and is itself a 100 % load on the core, so it is a *qualification* tool and a *sidecar* on a spare core, never something to run on the hot core during trading — there, Ch. 58's `aperf/mperf` or `cycles/ref-cycles` sampling from a housekeeping core is the production method.

## 7.4 Techniques

### 7.4.1 The hybrid plant: client silicon on the hot box, server silicon for everything else

The plant pattern that reconciles §7.2.2's table with a trading system's needs is **not** "all desktops"; it is one or two **hot boxes** on high-clock silicon and server silicon around them:

```
   role                          silicon                           why
   ───────────────────────────── ───────────────────────────────── ───────────────────────────────────────────────────────────
   tick-to-trade hot box         client/workstation, 5.5–6 GHz,    single-thread bound; ≤ 8 hot threads; L1/L2-resident data;
   (feed → book → strategy →     fixed ratio (§7.4.2), 2–8 ch       2 NICs + timestamp card fit the lane budget on workstation
    gateway, Ch. 98)             DDR5, true ECC where available     parts; the ~40 % latency cut lands here and nowhere else
   feed capture / line arb       server (lanes, channels, ECC)      bandwidth- and I/O-bound (Ch. 103, 104); needs 100G capture
   replay, backtest, research    server, many cores                 throughput; MPI/GPU (Ch. 84–85); nobody waits on one core
   risk aggregation / drop copy  server, ECC mandatory              correctness over speed; RAS firmware welcome
   sequencer / journal (Ch. 105)  server with NVMe lanes             fsync-bound, fail-over bound
```

The hot box's data comes in over the wire (Ch. 72–74) and goes out over the wire; the only shared-memory neighbours on it are the processes in Ch. 98's topology, which were designed to fit in a handful of cores. What the hot box must *not* host is the capture writer (bandwidth), the research Python (noise — Ch. 59), or anything whose correctness depends on ECC the board does not have. Appendix K's cross-connect and the venue's equalised cabling do not care which SKU is at the end of the fibre.

**Choosing the SKU.** Compare parts on *single-core sustained frequency at your cooling*, L3 latency (not size), memory latency (Ch. 8's chase on a demo unit), lane count, and ECC. Intel's high-frequency Xeon SKUs (the "F" and workstation parts) and AMD's EPYC "F" high-frequency parts exist precisely for this market and give you ECC, lanes and 4.5–5.0 GHz in one package — at a price. Appendix R lists the current generations with the figures that matter.

### 7.4.2 Turbo on but deterministic: fixing the ratio

Ch. 6 §6.2.1's advice — turbo off, governor `performance` — removes the *variance* by taking the *lowest* fixed clock. The alternative takes the **highest fixed clock the core can hold**:

1. **Find the sustained ceiling.** In firmware, set the per-core (or all-core) turbo ratio to the 1-active-core table entry minus one bin (e.g. 57× on a 6.0 GHz part), raise PL1 = PL2 to the board's maximum and ICCmax to the vendor's limit, set the thermal target to the default (do not raise it), and disable C-states below C1 on the hot cores (Ch. 58 §58.4.1). Run `soak` (§7.4.3) for 30 minutes on *all* hot cores simultaneously while `clockprobe` or Ch. 58's `turbostat` sidecar records the effective clock. If the clock sags or a core throttles (`thermal_throttle/core_throttle_count` moves — Ch. 58 §58.3), drop one bin and repeat. The ceiling is the highest ratio that holds for the full soak at the plant's worst-case ambient.
2. **Pin it.** Set min ratio = max ratio = the ceiling for the hot cores (Intel: per-core ratio limits in firmware or `MSR_TURBO_RATIO_LIMIT` via the board's tools; AMD: a fixed all-core "manual OC" ratio, which disables Precision Boost — or PBO with a *negative* max-boost offset and a Curve Optimizer undervolt, which keeps the controller but caps it; the fixed ratio is the deterministic one). Leave the *housekeeping* cores on the stock controller — they do not need determinism and they stay cooler.
3. **Pin the uncore.** On Intel server and workstation parts the uncore (L3/mesh/memory controller) clock is independently scaled; fix it at its maximum (`UNCORE_RATIO_LIMIT` MSR 0x620, or the firmware "uncore frequency scaling = disabled" — Ch. 58 §58.4.4). On client parts the ring runs near the core clock and follows it.
4. **Prove it every day.** The pre-open gate (Appendix M) runs the Ch. 58 flat-clock check: effective frequency on each hot core = the pinned ratio ± 0.1 %, zero throttle counts since boot, uncore at its pin. A ratio that drifted is a failed gate, not a slower day.

The result is a core that runs at, say, 5.7 GHz, forever, with the same variance as Ch. 6's 2.9 GHz — because the mechanisms that *varied* the clock have been removed, not because turbo was. The cost is power and heat (a fixed 5.7 GHz core draws its peak continuously; the plant's cooling is part of the design) and the validation debt of §7.4.3.

### 7.4.3 Overclocking and the stability-validation protocol

Above the vendor's rated single-core turbo you are **overclocking**: the part is no longer validated by the vendor at that operating point, and the failure mode is not a crash but a **wrong answer** — a bit flip in an ALU result or an L1 line that passes every parity check the core has (which, on client parts, is often none). A trading system that computes a position wrong is worse than one that crashes. Hence the protocol:

- **Detection before speed.** The workload under test must *check its own answers*: a deterministic kernel with a golden checksum (the `soak` harness below), the Ch. 110 property tests, or a replay (Ch. 104) whose output digest is known. "It didn't crash in Prime95" is not a stability result; a silent single-bit error in a 20-minute run is a *failed* result.
- **Soak under the real load shape.** Run the actual hot-path binary on a replayed open (Ch. 104) with the actual neighbour processes, for hours, at the plant's worst ambient. The Intel *and* AMD turbo controllers are thermal and current controllers: a synthetic all-core load is both too hard (it hits PL1 and sags) and too easy (it never has the hot path's L1-resident, high-IPC shape that draws the most current per core).
- **Margin.** Having found the highest *error-free* ratio, run one bin lower. Silicon ages (electromigration raises the voltage a given ratio needs); a part that was marginal on day one fails on day 400, in production.
- **Record it.** The fixed ratio, voltage, PL/ICC settings, the soak duration and its digest go in the host fingerprint (Appendix M.2) so a BIOS reset or a board swap is caught by attestation rather than by a bad fill.

```cpp
// soak.cpp — stability validation for a tuned/overclocked hot core: run a deterministic workload
// for N minutes, pinned, and fail on the first wrong answer; record per-iteration latency and the
// effective clock so a sagging ratio or a throttle event is visible in the log, not just a crash.
// Build: g++ -O2 -std=c++20 -pthread -o soak soak.cpp
// Run:   chrt -f 50 taskset -c 3 ./soak 600            # 10-minute soak on core 3
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <vector>
#include <x86intrin.h>

// --- the workload: an order-book-shaped mix — integer hash chain (ALU), a pointer chase sized to
//     L2 (cache), and an FMA dot product (vector units) — each with a known-good checksum.
struct Work {
    std::vector<uint32_t> chase;      // 1 MiB cyclic permutation: L2-resident on server, L2/L3 on client
    std::vector<double>   a, b;       // 4096 doubles each
    uint64_t golden = 0;
    explicit Work(uint64_t seed) : chase(1u << 18), a(4096), b(4096) {
        std::mt19937_64 g(seed);
        std::vector<uint32_t> p(chase.size()); for (uint32_t i = 0; i < p.size(); ++i) p[i] = i;
        std::shuffle(p.begin(), p.end(), g);
        for (size_t i = 0; i + 1 < p.size(); ++i) chase[p[i]] = p[i + 1];
        chase[p.back()] = p[0];
        for (auto& x : a) x = std::uniform_real_distribution<>(-1, 1)(g);
        for (auto& x : b) x = std::uniform_real_distribution<>(-1, 1)(g);
        golden = run();                                   // first run defines "correct"
    }
    uint64_t run() const {
        uint64_t h = 0x243F6A8885A308D3ull; uint32_t idx = 0;
        for (int i = 0; i < 1 << 16; ++i) {               // 65k dependent loads through the chase
            idx = chase[idx];
            h ^= idx; h *= 0x9E3779B97F4A7C15ull; h ^= h >> 29;
        }
        double acc0 = 0, acc1 = 0, acc2 = 0, acc3 = 0;    // 4 independent FMA chains
        for (size_t i = 0; i < a.size(); i += 4) {
            acc0 = __builtin_fma(a[i], b[i], acc0);     acc1 = __builtin_fma(a[i+1], b[i+1], acc1);
            acc2 = __builtin_fma(a[i+2], b[i+2], acc2); acc3 = __builtin_fma(a[i+3], b[i+3], acc3);
        }
        double d = (acc0 + acc1) + (acc2 + acc3);
        uint64_t bits; __builtin_memcpy(&bits, &d, 8);
        return h ^ bits;
    }
};

static double eff_ghz() {                                   // 3-cycle imul chain, see clockprobe.cpp
    constexpr uint64_t N = 3'000'000; uint64_t x = 1;
    auto t0 = std::chrono::steady_clock::now();
    for (uint64_t i = 0; i < N; ++i) asm volatile("imul %0, %0" : "+r"(x));
    auto t1 = std::chrono::steady_clock::now();
    asm volatile("" :: "r"(x));
    return 3.0 * N / std::chrono::duration<double, std::nano>(t1 - t0).count();
}

int main(int argc, char** argv) {
    const double seconds = argc > 1 ? std::atof(argv[1]) : 60;
    Work w(42);
    std::vector<uint32_t> lat; lat.reserve(1 << 22);
    uint64_t iters = 0, errors = 0; double ghz_min = 1e9, ghz_max = 0;
    auto start = std::chrono::steady_clock::now(); auto next_report = start + std::chrono::seconds(10);
    for (;;) {
        uint64_t t0 = __rdtsc(); uint64_t r = w.run(); uint64_t t1 = __rdtsc();
        lat.push_back(uint32_t(t1 - t0)); ++iters;
        if (r != w.golden) { ++errors; std::fprintf(stderr, "MISMATCH at iter %llu: %016llx != %016llx\n",
                             (unsigned long long)iters, (unsigned long long)r, (unsigned long long)w.golden); }
        auto now = std::chrono::steady_clock::now();
        if (now >= next_report) {
            double g = eff_ghz(); ghz_min = std::min(ghz_min, g); ghz_max = std::max(ghz_max, g);
            std::printf("t=%5.0fs iters=%llu errors=%llu eff=%.3f GHz\n",
                        std::chrono::duration<double>(now - start).count(),
                        (unsigned long long)iters, (unsigned long long)errors, g);
            next_report += std::chrono::seconds(10);
        }
        if (std::chrono::duration<double>(now - start).count() >= seconds) break;
    }
    std::sort(lat.begin(), lat.end());
    auto pct = [&](double p) { return lat[std::min(lat.size() - 1, size_t(p * lat.size()))]; };
    std::printf("iterations %llu  errors %llu  eff GHz [%.3f, %.3f]\n"
                "iteration TSC ticks: p50 %u  p99 %u  p99.9 %u  max %u\n",
                (unsigned long long)iters, (unsigned long long)errors, ghz_min, ghz_max,
                pct(0.50), pct(0.99), pct(0.999), lat.back());
    return errors ? 2 : 0;                                   // non-zero = the box is not stable
}
```

On the author's KVM guest a 20 s run gives `iterations 56434 errors 0 eff GHz [3.880, 3.886]` and an iteration distribution of `p50 912500 p99 1454172 p99.9 5738980 max 21388894` TSC ticks — the p99.9 at 6× the median and the max at 23× are the hypervisor, and exactly the shape a soak log must make you *reject*: on a bare-metal hot core configured per §7.4.2 the same harness prints a p99.9 within ~2 % of p50 (the L2 chase has no other variance source), and anything else is a C-state, a throttle or a neighbour. The checksum, not the latency, is the pass/fail; the latency is the diagnostic. Replace `Work` with the real hot-path kernel and a replayed input (Ch. 104) for the production qualification.

### 7.4.4 Surviving hybrid cores: pin to the type you measured

On a hybrid Intel part, treat the E-cores as housekeeping cores and never as hot cores — then make the mistake impossible:

```
   cat /sys/devices/cpu_core/cpus          # e.g. 0-15  (8 P-cores × 2 SMT threads)
   cat /sys/devices/cpu_atom/cpus          # e.g. 16-31 (16 E-cores, no SMT)
   # the hot partition is a subset of cpu_core; SMT siblings of hot cores stay idle (Ch. 55):
   isolcpus=domain,managed_irq,2-7 nohz_full=2-7 rcu_nocbs=2-7      # P-cores 1–3, one thread each
   # housekeeping, IRQs, capture, logging: the E-cores
   echo ffff0000 > /proc/irq/default_smp_affinity                     # and per-IRQ (Ch. 54)
```

In code, assert the type at start-up: read `/sys/devices/system/cpu/cpuN/topology/core_cpus_list` and check CPU *N* is in `cpu_core/cpus`; refuse to start otherwise (Appendix M's attestation does this for the whole partition). Disable the kernel's hybrid heuristics for the pinned threads — pinning already does, since Thread Director only influences *placement* and a thread with a one-CPU affinity mask has none to influence. Finally, benchmark on the P-core you will pin to, with the E-cores *busy* with the housekeeping load, because the E-core clusters share the ring and the L3 with the P-cores and their traffic is the client part's version of Ch. 18's noisy neighbour (Ch. 56's L3 partitioning is not available on client parts; measure instead).

## 7.5 Pitfalls & anti-patterns: buying GHz you cannot use, trusting a clock you cannot see

- **A DRAM-bound hot path on a 6 GHz part.** §7.3's table: the clock buys nothing below L3. If Ch. 8–11 have not been done, the procurement upgrade is a 10 % result dressed as a 2× one. Fix the working set first, then buy the clock.
- **Turbo on, nothing pinned.** Stock turbo with the stock controller is Ch. 6 §6.5's variance: the clock depends on how many cores woke up, what the neighbour's AVX load did to the licence (Ch. 37), and the room temperature. "We run turbo" without §7.4.2's ratio pin is the worst of both worlds — a higher *mean* and a wider *distribution*.
- **No ECC and no way to know.** DDR5 on-die ECC is not host-visible ECC. A desktop hot box without true ECC must at least have end-to-end checks on its outputs (Ch. 99's gate re-checks, Ch. 100's reconciliation) and a plan for the day the position is wrong; most shops decide the workstation SKU is cheaper than that plan.
- **A NIC behind the chipset.** Desktop boards route most slots through the chipset's x4/x8 uplink: a shared, buffered extra hop (Ch. 83) that adds latency and jitter to the one device that must not have either. The NIC(s) go in CPU-direct lanes, which on a desktop part means *the* x16 slot (bifurcated if the board allows).
- **Qualifying with a synthetic load.** A stress tool's all-core AVX load hits the power limit and tells you the part is stable at a clock it never reaches under the hot path's single-thread shape — and silently passes a core that flips a bit in the integer unit at the *higher* single-core ratio the hot path does reach. Soak with the real binary and a checksum (§7.4.3).
- **Trusting the TSC as a frequency.** §7.3's output: 2.700 in every row while the core ran at 3.86. The TSC is a *time* base, not a cycle counter (Ch. 20); for frequency use `aperf/mperf`, `cycles/ref-cycles` or the `imul` probe.
- **Letting the hybrid scheduler choose.** An unpinned worker on an Alder Lake-or-later part will, under load, sometimes run on an E-core; the regression shows as a bimodal latency distribution with no software cause. Pin, and assert the core type.
- **Forgetting the plant.** A single 5.7 GHz hot box among 2.9 GHz servers is correct; a *fleet* of desktops replacing the capture, replay and risk servers because "they are faster" loses the lanes, the channels and the ECC those roles need (§7.4.1).

## 7.6 Exercises & checklist

**Exercises**

1. Run `clockprobe` on your development box for 60 s with the governor at `powersave`, then `performance`, then with turbo disabled. Plot the three traces. Which one is flat, and at what frequency relative to the part's base and single-core turbo specifications?
2. Reproduce §7.3's table on your machine with the Ch. 8 pointer-chase at 32 KiB, 1 MiB, 16 MiB and 256 MiB, with turbo on and off. Fit *ns = a + b/f* per size: which sizes have *b* ≈ the whole cost (core-bound) and which have *a* ≈ the whole cost (memory-bound)?
3. If you have a hybrid Intel part: pin `soak` to a P-core, then to an E-core, 60 s each. Compare p50 and the effective clock. Then run it *unpinned* under a background load and look for the bimodal distribution.
4. Take the §7.4.2 protocol one bin at a time on a client part you are allowed to configure (not a production box): find the highest ratio that passes a 30-minute `soak` with zero errors, then run the same at one bin lower for 4 hours. Record power and temperature; that is the plant's cooling requirement.
5. Price the hybrid plant of §7.4.1 for your system — hot box(es) on workstation silicon, everything else on server — against an all-server plant with "F" SKUs. Where does the lane budget, not the price, decide?

**Checklist**

- [ ] The hot path's cycle budget is known per stage (Ch. 1), and the share that is core/L2-bound (the part the clock can buy) is measured, not assumed.
- [ ] Silicon is compared on *sustained single-core clock at the plant's cooling*, L3 latency, DRAM latency, lanes and ECC — Appendix R's table, re-measured on a demo unit with Ch. 8's chase.
- [ ] If turbo is on, the ratio is pinned (min = max), C-states ≤ C1, power/current limits above the workload's reach, uncore pinned (Ch. 58), and the flat-clock check is in the pre-open gate (Appendix M).
- [ ] Any ratio above the vendor's rating passed a checksum-verified soak of the real binary, at worst ambient, and runs one bin below the last error-free ratio; the settings are in the host fingerprint.
- [ ] On hybrid parts the hot partition is a subset of `cpu_core`, asserted at start-up; E-cores host housekeeping only.
- [ ] NICs and the timestamp card sit in CPU-direct lanes; nothing on the hot path is behind the chipset.
- [ ] ECC is host-visible (RDIMM, or UDIMM-ECC on a board that wires it) or the lack of it is a documented, mitigated risk.
- [ ] The TSC is used for *time*, `aperf/mperf` / `cycles` vs `ref-cycles` / the `imul` probe for *frequency*.

## 7.7 References

- Intel, *Intel® 64 and IA-32 Architectures Software Developer's Manual*, Vol. 3B: ch. 15 "Power and Thermal Management" (`IA32_PERF_CTL`, `MSR_TURBO_RATIO_LIMIT`, `IA32_APERF`/`IA32_MPERF`, `MSR_PACKAGE_POWER_LIMIT`), and Vol. 4 for the per-model MSR list.
- Intel, *Intel® 64 and IA-32 Architectures Optimization Reference Manual*, the chapters on the Alder Lake / Raptor Lake hybrid architecture (P-core / E-core, Thread Director) and on frequency behaviour under AVX workloads.
- AMD, *Processor Programming Reference (PPR) for AMD Family 19h / 1Ah Models* and *AMD Ryzen™ Master / Precision Boost Overdrive* documentation — the PB2 / PBO / Curve Optimizer mechanisms; `amd_pstate` and CPPC in the Linux `Documentation/admin-guide/pm/amd-pstate.rst`.
- Agner Fog, *Instruction tables* — the 3-cycle `imul r64` latency the probe depends on, per microarchitecture; *The microarchitecture of Intel, AMD and VIA CPUs* — ring vs mesh L3 and the client/server core differences.
- Linux kernel, `Documentation/admin-guide/pm/intel_pstate.rst`, `Documentation/arch/x86/topology.rst` and the `cpu_core` / `cpu_atom` sysfs devices (hybrid enumeration); `Documentation/admin-guide/pm/cpuidle.rst`.
- Intel ARK and AMD product specification pages for the SKUs named in §7.2.2 and Appendix R — the authoritative base/turbo frequencies, lane counts, channel counts and ECC support; verify before purchase, they change per stepping.

## 7.8 Additional Reading

- Carl Cook, *"When a Microsecond Is an Eternity: High Performance Trading Systems in C++"* (CppCon 2017) — the practitioner's statement that the hot box is a tuned, high-clock machine and the tick-to-trade path is one core's work.
- David Gross, *"Trading at light speed: designing low latency systems in C++"* (Meeting C++ 2022) — hardware selection and the measurement discipline around it.
- Chips and Cheese and Anandtech microarchitecture deep-dives (Raptor Lake, Zen 4/Zen 5, Sapphire/Granite Rapids) — independently measured L3/DRAM latency curves per part; use their method (a pointer chase, Ch. 8) to produce your own.
- Brendan Gregg, *"CPU Utilization is Wrong"* and *Systems Performance* (2nd ed.) ch. 6 — why `cycles`, `ref-cycles` and the TSC are three different things.
- Ch. 58 (*Power Management & Frequency Determinism*) — the mechanics behind every knob this chapter named; Ch. 19 (*Chiplets, Tiles & NUMA Within the Socket*) — the second axis of the silicon decision; Appendix Q and Appendix R — the AMD companion and the per-generation table.

---

*Next: Ch. 8 — The Memory Hierarchy & Caches, where Part II begins and we descend into the microarchitecture: why DRAM is ~55× an L1 hit (two orders of magnitude) and cannot be bought back with a higher clock, and how cache lines, associativity, MESI coherence and the hardware prefetchers decide which of your nanoseconds are cycles and which are waits.*
