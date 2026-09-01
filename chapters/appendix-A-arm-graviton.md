# Appendix A — ARM / Graviton & Non-x86 Targets

> **Relates to:** Ch. 30–36 (atomics / lock-free / seqlock — the memory-model chapter that changes most on ARM), Ch. 29 (SIMD), Ch. 33 (false sharing), Ch. 17 (timers), Ch. 32 (spin/backoff), Ch. 43 (SMT), Ch. 22 (build tuning), Ch. 6 (spec-exec mitigations). The main chapters assume x86-64; this appendix ports the low-latency toolkit to **AArch64** (AWS Graviton, Ampere Altra) and names exactly what changes.

The main chapters target modern x86-64 (Intel/AMD). AArch64 is increasingly relevant to latency-sensitive work — AWS Graviton and Ampere Altra offer many cores, good perf-per-watt, and competitive cloud economics — and most of this book *transfers*: caches, branches, NUMA, lock-free design, kernel bypass, and the measurement discipline are architecture-independent in *principle*. But the **details that matter for the last nanoseconds** differ, and a few of them — chiefly the **weaker memory model** — change how you *reason about correctness*, not just performance. This appendix walks the differences chapter by chapter.

The one-line summary: **ARM is a weakly-ordered, load/store, fixed-width-instruction architecture with a different cache-line size on server parts.** Each of those clauses has consequences below.

## A.1 The ARM memory model (`LDAR`/`STLR`, `DMB`) vs x86-TSO

This is the biggest change, and it affects **correctness**, not just speed — re-read Ch. 30–36 with it in mind.

x86 is **TSO (Total Store Order)**: a strong model where the only reordering the hardware permits is store→load (a store buffer that lets a later load bypass an earlier store to a different address). Loads are acquire-ish and stores are release-ish *for free*; a plain `mov` already has most of the ordering you want, and `std::memory_order_acquire`/`release` on x86 often compile to **plain loads/stores with no barrier** (Ch. 30). This is why a lot of subtly-wrong lock-free code *appears* to work on x86 — the hardware is forgiving.

ARM (AArch64) is **weakly ordered**: the hardware may reorder loads and stores far more aggressively (load→load, load→store, store→store, store→load — almost anything to different addresses, subject to dependencies). The C++ memory model (Ch. 30) is the *same* portable abstraction, but on ARM the orderings you request are *not* free — they emit real instructions:

- **`memory_order_acquire`/`release`** → `LDAR` (load-acquire) / `STLR` (store-release) — dedicated instructions that enforce the one-way barrier. They're cheaper than a full fence but not free (an x86 plain `mov` becomes an `LDAR`/`STLR` on ARM).
- **`memory_order_seq_cst`** → `LDAR`/`STLR` (which on ARMv8 are sequentially-consistent w.r.t. each other) or an explicit `DMB ISH` (data memory barrier) for the stronger cases. Full `seq_cst` fences (`DMB`) are notably more expensive than on x86 — another reason to use the *weakest sufficient* ordering (Ch. 30).
- **`memory_order_relaxed`** → plain `LDR`/`STR`, truly free — and on ARM the gap between relaxed and acquire/release is *visible*, so the discipline of using `relaxed` where correct (counters, statistics — Ch. 30) pays more than on x86.

**Consequences for the concurrency chapters (Ch. 30–37):**

- **Bugs hidden on x86 surface on ARM.** A seqlock (Ch. 35), lock-free queue (Ch. 34), or RCU/hazard-pointer scheme (Ch. 36) with a *missing or too-weak* `memory_order` may pass every test on x86 (TSO papers over it) and **fail on ARM** (the reordering actually happens). Porting to Graviton is an excellent way to *find* latent ordering bugs — and a reason to test on ARM even if you deploy on x86. Run the data-structure stress tests (Ch. 40) on ARM.
- **Get the orderings *right*, not "it works on x86."** The portable rule was always "use the correct `memory_order`"; ARM *enforces* it. Tools like TSan (Ch. 40) and model checkers (CDSChecker, Ch. 40) catch what x86 hides.
- **Fences cost more — minimize them.** `DMB`/seq_cst is pricier on ARM; the Ch. 30–32 advice to prefer acquire/release over seq_cst, and relaxed where sound, is more impactful here. A spinlock or queue tuned with full fences on x86 may want re-tuning to acquire/release on ARM.
- **ARMv8.1-LSE atomics.** Older ARM did atomics with `LDXR`/`STXR` (load-exclusive/store-exclusive) retry loops; ARMv8.1 adds **LSE** (Large System Extensions) — single-instruction atomics (`LDADD`, `CAS`, `SWP`) that scale far better under contention. Build with `-march=armv8.2-a+lse` (or `-mcpu=neoverse-n1`/`-mcpu=neoverse-v1`) so your atomics (Ch. 30–34) use LSE — a real contended-throughput win on Graviton2/3.

## A.2 NEON/SVE/SVE2 vs AVX

The SIMD chapter (Ch. 29) ports in *concept* — data-parallel, layout-for-vectorization, downclocking caveats — but the **ISA and width model differ**:

- **NEON** is ARM's fixed-128-bit SIMD (the analog of SSE/AVX-128) — always present on AArch64, 128-bit lanes, intrinsics in `<arm_neon.h>`. The Ch. 29 layout/auto-vectorization advice applies; the intrinsics are different (`vaddq_f32` vs `_mm256_add_ps`), and the width is 128-bit (no direct 256/512-bit NEON).
- **SVE / SVE2 (Scalable Vector Extension)** is ARM's answer to wide SIMD, with a twist: **vector-length-agnostic** code. You don't write to a fixed width; you write loops with *predicate*-driven, length-agnostic intrinsics and the *hardware* picks the vector length (128-2048 bits, implementation-defined). Graviton3 has SVE (256-bit); the *same binary* runs on a different SVE width without recompiling. This is a genuinely different model from AVX's fixed 128/256/512 — closer to "describe the data-parallel work, let the hardware size the vector."
- **Practical guidance:** auto-vectorization (Ch. 29) via `-O3 -mcpu=native` (or `-march=armv8.2-a+sve` for SVE) gets you much of the way; hand-intrinsics need rewriting per-ISA (NEON ≠ AVX ≠ SVE), so prefer portable wrappers (`std::experimental::simd`, xsimd, Highway) where you must support both. The **downclocking** caveat (Ch. 29 — AVX-512 dropping frequency on some Intel parts) is *less* of an issue on ARM, but verify per-part. Measure (Ch. 3) — SVE's length-agnostic loops can beat or lag a hand-tuned NEON kernel depending on the workload.

## A.3 Cache-line size (64 vs 128 bytes) and false sharing

A subtle, high-impact difference that directly hits Ch. 33 (false sharing) and Ch. 9 (layout):

- **x86 server: 64-byte cache lines.** Everything in Ch. 7–9, 33 assumed 64. `hardware_destructive_interference_size` is 64.
- **Some ARM server parts use 128-byte cache lines** (or a 128-byte cache-coherency/prefetch granule even where the L1 line is 64) — notably some Apple and certain server cores; Graviton/Neoverse are commonly 64 but **verify per-part** (`getconf LEVEL1_DCACHE_LINESIZE`, `/sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size`). Where it's 128:
  - **False-sharing padding must be 128 bytes** (Ch. 33), not 64 — a struct padded to a 64-byte line on x86 can *still* false-share on a 128-byte-line ARM part. Hard-coding `alignas(64)` is an x86 assumption; use `std::hardware_destructive_interference_size` (which reflects the target) or a per-arch constant.
  - **Struct sizing to "a cache line" (Ch. 9) changes** — a 64-byte hot struct is half a line on a 128-byte part; you may fit two hot structs per line and *want* to, or may need to re-pad.
  - **`hardware_destructive_interference_size` differs** — and this is exactly why the standard provides it instead of a literal 64. Code that hard-codes 64 is not portable to 128-byte-line targets (Ch. 33).
- **Action:** never hard-code the line size; query it or use the standard constants, and **re-run the false-sharing benchmarks (Ch. 33) on the actual ARM target** — padding tuned for 64 may not isolate on 128.

## A.4 Timers (`CNTVCT_EL0` vs `rdtsc`); `WFE`/`YIELD` vs `pause`

The timekeeping (Ch. 17) and spin (Ch. 32) chapters use x86-specific instructions; ARM has analogs with different properties:

- **Timestamp counter:** x86 `rdtsc`/`rdtscp` → ARM **`CNTVCT_EL0`** (the virtual count register), read via the `cntvct_el0` system register, with its frequency in **`CNTFRQ_EL0`**. Key differences (Ch. 17): ARM's counter is a *fixed-frequency* architectural counter (often ~25 MHz-1 GHz, *not* the core clock — so it's lower-resolution than `rdtsc`'s core-cycle granularity, but invariant by design and not tied to frequency scaling). You don't calibrate against frequency-scaling the way you might worry about on older x86 (Ch. 17's invariant-TSC discussion) — but the *resolution* is coarser, so very short intervals are less precise. Use `CNTFRQ_EL0` to convert ticks→ns. The Ch. 17 measurement discipline (serialize, beware reordering) applies; `CNTVCT_EL0` reads may need an `ISB` for ordering in tight measurement loops.
- **Spin-wait hint:** x86 `pause` (Ch. 32, the spinlock backoff hint) → ARM **`YIELD`** (a weak hint) or, better, **`WFE`/`SEV`** (Wait For Event / Send Event). `WFE` is more powerful than `pause`: a core can `WFE` to sleep until another core `SEV`s or a monitored memory location changes (via the exclusive monitor) — enabling a *lower-power, lower-contention* spin than busy-polling with `YIELD`. For spinlocks and queue backoff (Ch. 32), `WFE`/`WFET` (timed, ARMv8.7) can beat a `pause` loop on power and on releasing SMT/pipeline resources. Tune the backoff (Ch. 32) with `WFE` on ARM rather than a direct `pause`→`YIELD` substitution.
- **`std::this_thread::yield` / portable spin hints** map to these; for hand-tuned spins, use the ARM intrinsics (`__yield()`, `__wfe()`) under arch guards.

## A.5 big.LITTLE / heterogeneous cores and SMT differences

The pinning/SMT chapters (Ch. 42–43) assume *homogeneous* cores; ARM often isn't:

- **Heterogeneous cores (big.LITTLE / DynamIQ).** Many ARM SoCs (and Apple Silicon) mix high-performance ("big"/P) and efficiency ("LITTLE"/E) cores with *different* performance, cache, and even microarchitecture. For low latency this is a **pinning hazard** (Ch. 42): a hot thread scheduled (or migrated) onto an E-core is suddenly much slower — a jitter source x86 homogeneous parts don't have. **Pin hot threads to the big/P cores explicitly** (affinity, Ch. 42), and know your topology (the E-cores can host housekeeping, like the SMT-sibling strategy of Ch. 43). *Server* ARM (Graviton, Altra) is typically **homogeneous** (all Neoverse cores, no big.LITTLE) — so this mainly bites on client/edge SoCs, but verify.
- **SMT:** Graviton and Ampere Altra are commonly **single-threaded per core (no SMT)** — which *sidesteps* most of Ch. 43's hot-core-sibling concerns (no sibling to contend or to leave idle). That's a simplification for the HFT "quiet core" goal (Ch. 43): on a no-SMT ARM server, each core is already un-shared. (Some other ARM parts do have SMT; check.) Where there's no SMT, the Ch. 43 "disable HT / leave the sibling idle" tuning is moot — one core, one thread.
- **`YIELD` and resource sharing (Ch. 43)** matter only where SMT exists; on no-SMT ARM the spin-hint is purely about power/pipeline, not a sibling.

## A.6 `-mcpu`/`-mtune` tuning; spec-exec mitigations on ARM

The build (Ch. 22) and security-mitigation (Ch. 6) chapters need ARM-specific flags:

- **`-mcpu=` is the key ARM flag** (Ch. 22, Appendix D): it sets *both* the architecture and the tuning for a specific core — e.g. `-mcpu=neoverse-n1` (Graviton2), `-mcpu=neoverse-v1` (Graviton3), `-mcpu=ampere1` (Altra), or `-mcpu=native` on the target. This is more important on ARM than `-march`/`-mtune` are on x86 because it pulls in the right extensions (LSE atomics — A.1, SVE — A.2, the right pipeline model). **Always set `-mcpu` for the deployment target** — a generic `-march=armv8-a` build leaves LSE atomics and SVE on the table and tunes for no particular core.
- **`-march=armv8.2-a+lse+...`** when you must support a *range* of ARM parts but still want specific extensions — name them explicitly (`+lse`, `+sve`, `+crypto`).
- **Spec-exec mitigations (Ch. 6):** ARM has its own Spectre-class variants and mitigations (`CSDB`, `SB` speculation barriers, the `__builtin_load_no_speculate` pattern, and kernel `mitigations=`). The *cost/benefit* on an isolated, single-tenant box (Ch. 6, 42, 43, 45) is the same *decision* — you may tune mitigations down on a controlled box — but the specific instructions/knobs differ, and the microarchitectural exposure differs per core. The Ch. 6 reasoning ("measure the cost, decide per box given the threat model") transfers; the specifics are ARM's.
- **Frequency/turbo (Ch. 6):** server ARM (Graviton/Altra) often runs at a *fixed* frequency with *no turbo* — which is actually **good for determinism** (Ch. 6: no frequency-scaling jitter, no turbo-then-throttle), one of the quiet-box goals achieved by default. Fewer C-states/P-states to tame than x86.

## A.7 Where Graviton in the cloud fits (and where bare-metal still wins)

The strategic question: **does ARM-in-the-cloud belong in a latency-sensitive trading workload?**

- **Where Graviton/Altra fit well:** the *throughput* and *research* tiers of the book — backtesting (Ch. 75–76), Monte-Carlo/risk (Ch. 67–68), the simulation harness (Ch. 76), data processing, and *non-order-path* services. Graviton's many homogeneous, no-SMT, fixed-frequency cores with good perf-per-dollar are excellent for parallel compute (Ch. 68) and for the off-hot-path infrastructure (capture, logging, monitoring — Ch. 71, 75, 76). Cloud elasticity suits research/backtest bursts.
- **Where bare-metal colocation still wins (the order path):** the actual **tick-to-trade hot path** (Ch. 53, 55, 58, 61, 62, 76) lives or dies on *physical proximity to the exchange* (colocation — single-digit µs to the matching engine), deterministic dedicated hardware (Ch. 6, 42, 43, 45), kernel bypass / specialized NICs (Ch. 55, 62), and often FPGAs (Ch. 69) — none of which a shared cloud VM provides. **The cloud is not colocated**, and you cannot put a Solarflare/Exablaze NIC or an inline FPGA (Ch. 69) in an EC2 instance. So the order path stays on bare-metal colocated boxes (x86 or ARM); the cloud (Graviton included) hosts everything *around* it. AWS's Local Zones / outposts and exchange-proximate offerings narrow this for some venues, but the lowest-latency order path remains bare-metal-colocated.
- **The porting verdict:** if you *do* run latency-sensitive code on Graviton, this appendix is your checklist — fix the **memory-model orderings** (A.1, the correctness item), set **`-mcpu`** for LSE/SVE (A.1-2, A.6), **verify the cache-line size** and re-pad (A.3), use **`CNTVCT_EL0`/`WFE`** (A.4), pin to **big cores** if heterogeneous (A.5), and re-run the **concurrency stress tests and false-sharing/latency benchmarks** on the ARM target (don't trust x86 results). Most of the book transfers; these are the deltas.

## A.8 References

- The **ARM Architecture Reference Manual (ARMv8-A / ARMv9-A)** — the memory model, `LDAR`/`STLR`/`DMB`, LSE atomics, `CNTVCT_EL0`, `WFE`/`SEV`, NEON/SVE (A.1-A.5).
- The **ARM Neoverse** (N1/V1/N2) and **Ampere Altra** optimization/tuning guides — per-core tuning, `-mcpu` targets, pipeline models (A.6).
- *A Tutorial Introduction to the ARM and POWER Relaxed Memory Models* (Maranget, Sarkar, Sewell) and the cppmem/herd tooling — reasoning about weak-memory orderings (A.1).
- The **AWS Graviton** technical guide and the Graviton getting-started/porting documentation — extensions, `-mcpu=neoverse-*`, and where Graviton fits (A.6-A.7).
- ARM's SVE/SVE2 programming guides and the portable-SIMD libraries (Google Highway, xsimd) — length-agnostic SIMD (A.2).
- Ch. 30–36 (atomics/lock-free/seqlock/RCU — the chapters most affected by A.1), Ch. 29 (SIMD — A.2), Ch. 33 (false sharing — A.3), Ch. 17 (timers — A.4), Ch. 43 (SMT — A.5), Ch. 22 & Appendix D (build flags — A.6), Ch. 6 (mitigations — A.6).

## A.9 Additional Reading

- AWS and Ampere performance blogs on porting low-latency / HPC workloads to ARM — real-world LSE/SVE/`-mcpu` results and the memory-model gotchas (A.1, A.6).
- Talks on weak memory models and lock-free correctness across architectures (CppCon, the C++ committee's SG1 material) — why x86-tested lock-free code breaks on ARM (A.1).
- Apple Silicon performance write-ups (for the 128-byte cache line and big.LITTLE specifics) where relevant to non-server ARM (A.3, A.5).
- **Appendix D** (Compiler Flag Reference) — the `-mcpu`/`-march`/extension flags; **Appendix C** (System Tuning Checklist) — the ARM-server analogs of the x86 quiet-box knobs (often fewer, given fixed frequency / no SMT); **Appendix F** (Glossary) — LSE/SVE/`LDAR`/TSO terms.

---

*Next: Appendix B — Beyond C++: Alternative Languages for Low-Latency, surveying Rust, C, Zig, and the case against managed runtimes (Java/C#/Go GC pauses and JIT warm-up) on the hot path — FFI with a C++ core, what transfers from this book and what doesn't, and matching language to the latency tier.*
