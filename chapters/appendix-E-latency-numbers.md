# Appendix E — Latency Numbers Every Trading Developer Should Know

> **Ties:** Ch. 3 (how to measure these), Ch. 7 (cache hierarchy), Ch. 13 (branch mispredict), Ch. 16 (NUMA-remote DRAM), Ch. 17 (the timer you measure with), Ch. 32–33 (mutex / cross-core HITM), Ch. 41 (context switch / syscall), Ch. 47 (kernel-stack NIC RX), Ch. 55 & 52 (bypass NIC RX), Ch. 66 (PCIe round-trip). This is the trading-annotated refresh of the Norvig / Jeff-Dean "latency numbers every programmer should know" — the costs that set the *floor* under every decision in this book.

The single most useful thing in a low-latency engineer's head is a **calibrated sense of what things cost** — so that when you look at a tick-to-trade budget (Ch. 76) you *know*, without measuring, that an L3 miss to DRAM is ~20× an L1 hit, that a cross-core cache-line transfer (false sharing — Ch. 33) costs more than a branch mispredict, that a syscall dwarfs both, and that anything touching the kernel network stack (Ch. 47) is in a different universe from kernel bypass (Ch. 62). These ratios are *why* the book is structured as it is: every Part attacks a row in this table.

**Two caveats, loudly** (Ch. 3): (1) these are **representative** numbers for modern x86-64 server hardware (~Ice Lake / Sapphire Rapids class, ~3 GHz, DDR4/5); *your* hardware differs, and the only number that counts for a decision is the one *you measured on your box* (Ch. 3). Treat these as orders of magnitude and *ratios*, not constants. (2) The **distribution matters more than the point value** (Ch. 1): "DRAM ≈ 100 ns" is the *typical* cost; the *tail* (a TLB miss on top — Ch. 15, a NUMA-remote access — Ch. 16, contention) is what actually bites. Memorize the table for intuition; measure for decisions.

## E.1 The table

Modern x86-64 server, ~3 GHz (so **~0.33 ns/cycle**; cycles ≈ ns × 3). Representative; **measure your own** (Ch. 3).

| Operation | ~Latency | ~Cycles | Trading relevance / notes |
|---|---:|---:|---|
| **1 CPU cycle** | 0.33 ns | 1 | the unit; ~3 cycles/ns at 3 GHz |
| **L1 cache hit** | ~1 ns | ~4 | the hot-path ideal — keep the working set here (Ch. 7–8) |
| **L2 cache hit** | ~4 ns | ~12-14 | still cheap; book/strategy hot data should fit L2 (Ch. 7, 25) |
| **L3 cache hit** | ~15-20 ns | ~45-60 | shared across cores; HITM-prone (Ch. 7, 33) |
| **Branch mispredict** | ~3-5 ns | ~10-20 | pipeline flush (Ch. 13) — the cost of an unpredictable branch on the hot path |
| **L3 miss → local DRAM** | ~80-100 ns | ~250-300 | the cache-miss cliff — ~20-100× an L1 hit (Ch. 7, 15); a cold book entry |
| **DRAM, NUMA-remote** | ~130-180 ns | ~400-550 | cross-socket penalty — ~1.5-2× local (Ch. 16); why NIC-NUMA locality matters |
| **TLB miss (page walk)** | ~10-100+ ns | ~30-300+ | on top of the access; huge pages cut it (Ch. 15) |
| **Cross-core cache-line transfer (HITM)** | ~40-100 ns | ~120-300 | **false sharing** (Ch. 33) / contended atomic (Ch. 30) — a line bouncing between cores; the hidden killer of "lock-free" code |
| **Uncontended mutex lock+unlock** | ~15-25 ns | ~45-75 | the fast path (futex, no syscall — Ch. 32); *contended* is far worse (syscall + sleep) |
| **`std::atomic` RMW (uncontended)** | ~5-10 ns | ~15-30 | a `CAS`/`fetch_add` with no contention (Ch. 30); contended → HITM row |
| **`rdtsc`/`rdtscp`** | ~5-10 ns | ~15-30 | the measurement instrument itself (Ch. 17) — cheap enough to timestamp every event |
| **Null syscall** (e.g. `getuid`) | ~50-100 ns | ~150-300 | the user/kernel transition alone (Ch. 41) — *higher* with spec-exec mitigations (Ch. 6); why the hot path makes none (Ch. 23) |
| **Context switch** | ~1-5 µs | ~3,000-15,000 | direct + indirect (cache/TLB pollution — Ch. 41); a single one blows the budget — pin & don't block (Ch. 42, 45) |
| **Page fault (minor)** | ~1-5 µs | — | first touch of a page (Ch. 23) — why `mlockall` + pre-fault (Ch. 26, 46) |
| **Kernel-stack NIC RX (wire→app)** | ~5-15 µs | — | the full kernel network path (Ch. 47) — interrupts, copies, stack |
| **Kernel-bypass NIC RX (wire→app)** | ~1-2 µs | — | poll-mode, zero-copy userspace (Ch. 62, 55) — ~5-10× better than kernel stack |
| **FPGA inline (wire→wire)** | ~tens-100s ns | — | hardware on the NIC (Ch. 69) — below software bypass, with a *flat tail* |
| **PCIe round-trip (device register read)** | ~300 ns-1 µs | — | non-posted read = full round-trip (Ch. 66) — the floor under every offload (GPU/Ch. 67); why accelerators stay off the order path |
| **SSD/NVMe random read** | ~10-100 µs | — | off-hot-path only (Ch. 75 capture) — never on the path |
| **Spinning disk seek** | ~5-10 ms | — | historical reference; the "1 ms = eternity" scale |
| **Inter-DC round-trip (same metro)** | ~0.1-1 ms | — | why colocation (Ch. 6, Appendix A.7) — *physical distance* dominates |
| **Cross-continent round-trip** | ~50-150 ms | — | speed of light; no software fixes this |

A few ratios worth burning in (Ch. 1's "reason about the distribution and the ratios"):

- **L1 : DRAM ≈ 1 : 100.** A cache miss is ~100 cycles you didn't budget — the single most important number in Parts II/IV. The whole cache-aware/data-oriented agenda (Ch. 7–10, 25) exists to keep you out of the DRAM row.
- **Branch mispredict ≈ an L2/L3-ish hit, ~10-20 cycles** — cheap *individually*, but on a hot path executed millions of times an unpredictable branch is a real tax (Ch. 13) — hence branchless techniques.
- **Cross-core HITM (~40-100 ns) > branch mispredict, ≈ a DRAM access** — false sharing (Ch. 33) and contended atomics (Ch. 30) cost like a cache miss *every time the line bounces*. This is why "lock-free" with a contended hot line can be *slower* than a well-tuned lock (Ch. 32).
- **Syscall (~50-100 ns) ≫ everything microarchitectural; context switch (µs) ≫ syscall.** The user/kernel boundary is a cliff (Ch. 41); crossing it on the hot path (a `write` to log — Ch. 71, a blocking call — Ch. 47) is why Parts VII-VIII work so hard to avoid it.
- **Kernel stack (µs) : kernel bypass (~1 µs) : FPGA (~100 ns).** The three network tiers (Ch. 47, 62, 69) — each ~5-10× the next. The tick-to-trade budget (Ch. 76) is dominated by which tier you're on.
- **PCIe round-trip (~µs) is the floor under offload** (Ch. 66) — it's why a GPU (Ch. 67), however fast its kernel, can't be on the order path, and why the FPGA goes *inline* (Ch. 69) instead of behind PCIe.

## E.2 How each was measured, with pointers to the deriving chapter

The numbers are only trustworthy if you know *how they're obtained* — and reproducing them is the best way to calibrate your own box (Ch. 3). Briefly, per row:

- **Cache/DRAM latencies (L1/L2/L3/DRAM)** — a **pointer-chasing** microbenchmark (a randomized linked list sized to each level, so the prefetcher can't hide the latency — Ch. 7), or `lmbench`'s `lat_mem_rd`, or Intel MLC (Memory Latency Checker). Read the *dependent-load* latency, not bandwidth. **Ch. 7** derives these; **Ch. 15** adds the TLB component; **Ch. 16** adds the NUMA-remote row (run MLC across sockets, or `numactl --membind` to force remote).
- **Branch mispredict** — a benchmark with a *deliberately unpredictable* branch (random data) vs a predictable one, differencing the cost; cross-check with `perf stat`'s `branch-misses` and the cycles attributable (Ch. 2 top-down). **Ch. 13** derives it.
- **Cross-core HITM** — two threads on different cores hammering the *same* cache line (a shared atomic or false-shared struct — Ch. 33); measure the per-op cost and confirm with `perf c2c` (cache-to-cache / HITM detection — Ch. 33) or the `MEM_LOAD_*.HITM` PMU events (Ch. 2). **Ch. 33** (false sharing) and **Ch. 30** (atomics) derive it.
- **Mutex / atomic RMW** — a tight loop of lock/unlock (uncontended: one thread; contended: N threads) and of `fetch_add`/`CAS`, timed with `rdtsc` (Ch. 17). **Ch. 32** (spinlocks/mutex) and **Ch. 30** (atomics) derive these; the *contended* numbers are workload-dependent — measure yours.
- **`rdtsc`/syscall/context-switch** — `rdtsc` back-to-back to find its own overhead (Ch. 17); a null syscall (`getuid`/`getpid` pre-cache, or `syscall(SYS_...)`) in a loop for the syscall cost (Ch. 41); a two-thread/two-process ping-pong (pipe or futex) divided by two for the context-switch cost (Ch. 41), or `perf bench sched pipe`. **Ch. 17** (timer), **Ch. 41** (syscall/switch) derive these. Note the syscall number *moves with `mitigations=`* (Ch. 6) — measure with your production mitigation setting.
- **Page fault** — touch freshly-`mmap`'d pages and time the first access (minor fault), vs pre-faulted/`mlock`'d (Ch. 26). **Ch. 23** (page faults) and **Ch. 26** (mmap/mlock) derive it.
- **NIC RX (kernel stack vs bypass)** — **wire-to-application** timing using **NIC hardware timestamps / PTP** (Ch. 55, 58) so you capture the true wire time, not when software looked: a loopback or a known-cadence feed, differencing the hardware RX timestamp from the app's receive timestamp. Kernel-stack via the normal socket path (Ch. 47); bypass via `ef_vi`/DPDK/`AF_XDP` poll-mode (Ch. 55, 61, 62). **Ch. 47** (native I/O), **Ch. 62** (bypass), **Ch. 58** (the timestamping method) derive these — and **Ch. 76** composes them into the tick-to-trade budget.
- **FPGA inline (wire-to-wire)** — measured *externally* with a tap/PTP-timestamped capture (Ch. 58, 75), since the point is there's no host software to instrument — packet-in to packet-out across the FPGA-NIC (Ch. 69). **Ch. 69** derives it; the headline is the *flat tail* (p99 ≈ p50), which a single point value hides — measure the distribution (Ch. 1, 3).
- **PCIe round-trip** — time a **non-posted read** of a device register (MMIO read — Ch. 66), which forces a full round-trip (a posted *write* doesn't — Ch. 66's asymmetry); or use a vendor's PCIe latency tool. **Ch. 66** derives it and explains why the *read* is the expensive direction.
- **Storage / network-distance rows** — `fio` for NVMe (Ch. 75 context), `ping`/PTP for network round-trips; these are off-hot-path or physical-floor references (colocation — Appendix A.7), included for scale.

The meta-point (Ch. 3, 76): **reproduce the rows that matter for your decisions on your own hardware**, with your production build (Appendix D) and tuning (Appendix C) and `mitigations=` setting (Ch. 6) — a table from a book is for intuition; a number from your box is for engineering. Then fold the measured rows into the tick-to-trade decomposition (Ch. 76.3) so the budget is built from *your* costs, not these representative ones.

## E.3 References

- Jeff Dean's "Numbers Everyone Should Know" / Peter Norvig's "Teach Yourself Programming in Ten Years" latency table — the original this refreshes; and Colin Scott's interactive "Latency Numbers Over Time" visualization (E.1).
- **Intel Memory Latency Checker (MLC)**, **`lmbench`**, and **`stress-ng`** — tools for measuring the cache/DRAM/NUMA rows (E.2; Ch. 7, 16).
- `perf` (`stat`, `c2c`, `mem`), the Intel SDM/optimization manual, and Agner Fog's instruction tables (Appendix G) — the microarchitectural latencies behind the cache/branch/atomic rows (E.2; Ch. 2, 7, 13, 30, 33).
- Ch. 3 (microbenchmarking — *how* to measure), Ch. 7/13/15/16/17/30/32/33/41/47/55/62/66/69 (the chapters that derive each row), Ch. 76 (composing them into tick-to-trade).
- Carl Cook, "When a Microsecond Is an Eternity" (CppCon) — the trading framing of why these numbers matter (E.1).

## E.4 Additional Reading

- Brendan Gregg's *Systems Performance* (the latency-analysis methodology and the orders-of-magnitude tables) — and his blog's latency-heatmap material.
- The "Latency Numbers Every Programmer Should Know" GitHub gists and their periodic refreshes — community-maintained updates to the original table (E.1).
- Vendor microarchitecture deep-dives (Intel/AMD optimization manuals, AnandTech/Chips-and-Cheese latency measurements) for current-silicon cache/DRAM numbers (E.1).
- **Appendix C** (System Tuning) — the knobs that move several rows (C-state exit, page fault, context switch); **Appendix D** (Compiler Flags) — the build that affects the branch/inline rows; **Appendix F** (Glossary) — HITM/TLB/futex/MMIO terms; **Appendix G** (Bibliography) — the manuals behind the measurements; **Ch. 1** — why the *distribution* behind each point value is what you actually trade.

---

*Next: Appendix F — Glossary (HFT & Microarchitecture Terms), an alphabetized quick reference for the acronyms and jargon used throughout — trading & market-structure, microarchitecture, memory & OS, concurrency, and C++/toolchain — each entry a line or two with a pointer to the chapter that develops it.*
