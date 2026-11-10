# Appendix E — Latency Numbers Every Trading Developer Should Know

> **Ties:** Ch. 3 (how to measure these), Ch. 8 (cache hierarchy), Ch. 15 (branch mispredict), Ch. 18 (NUMA-remote DRAM), Ch. 20 (the timer you measure with), Ch. 43–44 (mutex / cross-core HITM), Ch. 54 (context switch / syscall), Ch. 63 (kernel-stack NIC RX), Ch. 75 & 82 (bypass NIC RX), Ch. 87 (PCIe round-trip). This is the trading-annotated refresh of the Norvig / Jeff-Dean "latency numbers every programmer should know" — the costs that set the *floor* under every decision in this book.

The single most useful thing in a low-latency engineer's head is a **calibrated sense of what things cost** — so that when you look at a tick-to-trade budget (Ch. 119) you *know*, without measuring, that an L3 miss to DRAM is ~50× an L1 hit, that a cross-core cache-line transfer (false sharing — Ch. 44) costs more than a branch mispredict, that a syscall costs at least as much as a DRAM miss and a context switch dwarfs all of them, and that anything touching the kernel network stack (Ch. 63) is in a different universe from kernel bypass (Ch. 82). These ratios are *why* the book is structured as it is: every Part attacks a row in this table.

**Two caveats, loudly** (Ch. 3): (1) these are **representative** numbers for the book's reference machine — a Xeon Gold 6326, Ice Lake-SP, turbo off at 2.9 GHz, DDR4-3200 (Appendix L.3) — and its contemporaries; *your* hardware differs, and the only number that counts for a decision is the one *you measured on your box* (Ch. 3). Treat these as orders of magnitude and *ratios*, not constants. (2) The **distribution matters more than the point value** (Ch. 1): "DRAM ≈ 100 ns" is the *typical* cost; the *tail* (a TLB miss on top — Ch. 17, a NUMA-remote access — Ch. 18, contention) is what actually bites. Memorize the table for intuition; measure for decisions.

## E.1 The table

The reference machine of Appendix L.3 — Xeon Gold 6326 (Ice Lake-SP), turbo off at the 2.9 GHz base, so **0.345 ns/cycle; cycles ≈ ns × 2.9** — the ns↔cycles basis used book-wide. Representative; **measure your own** (Ch. 3).

| Operation | ~Latency | ~Cycles | Trading relevance / notes |
|---|---:|---:|---|
| **1 CPU cycle** | 0.345 ns | 1 | the unit; 2.9 cycles/ns at the 2.9 GHz base (0.29 ns/cycle at the 3.5 GHz turbo) |
| **L1 cache hit** | ~1.7 ns | 5 | the hot-path ideal — keep the working set here (Ch. 8–10); 5-cycle load-to-use on Ice Lake (4 cycles, ~1 ns, on Skylake-class cores) |
| **L2 cache hit** | ~5 ns | ~14 | still cheap; book/strategy hot data should fit L2 (Ch. 8, 32) |
| **L3 cache hit** | ~15-25 ns | ~45-70 | shared across cores — slice distance sets the spread; HITM-prone (Ch. 8, 44) |
| **Branch mispredict** | ~5-7 ns | ~15-20 | pipeline flush (Ch. 15) — the cost of an unpredictable branch on the hot path |
| **L3 miss → local DRAM** | ~80-100 ns | ~230-290 | the cache-miss cliff — ~50-60× an L1 hit here, ~50-100× across current cores: two orders of magnitude (Ch. 8, 17); a cold book entry |
| **DRAM row hit vs row conflict** (the two modes inside the row above) | ~70 ns vs ~100 ns | ~200 vs ~290 | tCL only vs tRP + tRCD + tCL at DDR4-3200 plus ~55 ns of fabric; no cache counter distinguishes them — the per-miss distribution does (Ch. 9 §9.2.2, §9.3) |
| **DRAM miss under a streaming neighbour** (controller at the knee) | ~140–200 ns | ~400–580 | the same miss, queued behind a capture writer/replay on the socket's channels (Ch. 9 §9.2.3); the hot core's miss *count* does not change |
| **DRAM refresh stall** (miss lands on a refreshing rank) | + ~0.3–0.55 µs | + ~900–1,600 | tRFC for 8–16 Gb DDR4; a ~1-in-20-per-rank tail mode, doubling above 85 °C (Ch. 9 §9.2.4) |
| **DRAM, NUMA-remote** | ~130-180 ns | ~380-520 | cross-socket penalty — ~1.5-2× local (Ch. 18); why NIC-NUMA locality matters |
| **CXL-attached memory** (a CPU-less NUMA node) | ~170–250 ns | ~500–720 | a remote hop and a half; capacity, not latency — keep it off the hot process with `--membind` (Ch. 9 §9.2.5, Ch. 18 §18.2.4) |
| **TLB miss (page walk)** | ~10-100+ ns | ~30-300+ | on top of the access; huge pages cut it (Ch. 17) |
| **Cross-core cache-line transfer (HITM)** | ~40-100 ns | ~120-290 | **false sharing** (Ch. 44) / contended atomic (Ch. 41) — a line bouncing between cores; the hidden killer of "lock-free" code |
| **Uncontended mutex lock+unlock** | ~15-25 ns | ~45-70 | the fast path (futex, no syscall — Ch. 43); *contended* is far worse (syscall + sleep) |
| **`std::atomic` RMW (uncontended)** | ~5-10 ns | ~15-30 | a `CAS`/`fetch_add` with no contention (Ch. 41); contended → HITM row |
| **libm `exp` / `erf`, scalar** | ~2.8 ns thr, ~10 ns lat / ~13 ns thr, ~27 ns lat | ~8 / 28 ; ~37 / 78 | glibc 2.39 with `-fno-math-errno`; a hand-written minimax `exp` is ~5 / 32 cycles with no slow path and vectorizes 8-wide to ~1.3 cycles; the **tail**: `exp(-745)` (subnormal result) ~126 cycles, `log` of a denormal ~118 — a software slow path FTZ does not fix (Ch. 36 §36.3) |
| **Black-Scholes call + vega** | ~30 ns libm / ~18 ns fast / ~4.5 ns vectorized | ~87 / 53 / ~13 per series | the pricing kernel of a quoting engine (Ch. 36 §36.4.4, Ch. 107); implied vol by Newton is 170–550 cycles depending on the iteration budget and converges non-uniformly across strikes (Ch. 36 §36.3) |
| **Hash of an 8-byte key** | ~1.3 ns multiply / ~3.5 ns mixer / ~8.5 ns SipHash | ~3.5 / ~9–10 lat / ~23–31 | Fibonacci multiply-shift vs splitmix/wyhash vs SipHash-2-4; the probe that follows is the real cost (0.9 slots at 65% load with any decent hash, 37 with a masked multiplicative hash, 18,000 with identity on symbols); a compile-time perfect hash resolves in ~3 cycles with no probe; `crc32q` is 0.26 cycles/byte (Ch. 38 §38.3) |
| **`rdtsc`/`rdtscp`** | ~6 ns raw `rdtsc`; ~12-25 ns fenced `rdtscp` | ~18 / ~35-75 | the measurement instrument itself (Ch. 20, §20.3: ~6 ns raw, ~12 ns with `lfence`) — cheap enough to timestamp every event |
| **Null syscall** (e.g. `getuid`) | ~50-100 ns with `mitigations=off`; ~150-400 ns with KPTI/retpoline on | ~150-300 / ~450-1,150 | the user/kernel transition alone (Ch. 54) — the mitigation setting (Ch. 6) picks the band; why the hot path makes none (Ch. 30) |
| **C ABI call through the PLT (shared object)** | ~0.3–0.4 ns over the inlined code (2.9 vs 2.5 ns for a 2.5 ns body) | ~1 | the cost of a language boundary from an AOT language (C++/Rust/Zig/OCaml): a `call`/`ret` and a `jmp` through the GOT, plus lost inlining; a call *into a managed runtime* is 10–470 ns (Ch. 95) |
| **Context switch** | ~1-5 µs direct; **+5-50 µs indirect** | ~3,000-15,000 direct | the direct switch is the small part; the cold-restart penalty (cache/TLB/predictor pollution — Ch. 54 §54.3, 46) is the dominant one; a single one blows the budget — pin & don't block (Ch. 56, 59) |
| **Signal delivery round trip** (sigframe `XSAVE` + handler + `sigreturn`) | ~1.1 µs | ~3,000 | a context-switch-class event that lands on the hot thread unless it is masked and funnelled (Ch. 54 §54.4.3); `SA_ONSTACK` adds ~9 %; the frame is full-size even with clean AVX-512 state (Ch. 108 §108.3) |
| **Cross-call IPI landing on an isolated core** (`CAL`/`TLB`/`RES`) | ~2-3 µs stolen from the running thread | ~6,000-9,000 | an MSR read, a `perf` attach, a `text_poke` (kprobe arming), an expedited RCU grace period or a `munmap` on another core interrupts the hot core — the residual-noise unit (Ch. 61 §61.3, 79 §114.3); the only fix is at the sender |
| **C-state wake-up** (C1 / C6) | ~1-2 µs / ~50-150 µs | — | the first event after an idle spell pays the exit latency (Ch. 6 §6.2.1, 46); why Appendix C.1 caps at C1 or busy-polls (`idle=poll`) |
| **Page fault (minor)** | ~0.3-1 µs for a 4 KiB page; several µs for a zeroed 2 MiB THP page | ~1,000-3,000 | first touch of a page, zeroing included (Ch. 30) — why `mlockall` + pre-fault (Ch. 34, 62) |
| **Kernel-stack NIC RX (wire→app)** | ~5-15 µs | — | the full kernel network path (Ch. 63) — interrupts, copies, stack |
| **Kernel-bypass NIC RX (wire→app)** | ~0.5-1 µs p50, ~1-2 µs p99 | — | poll-mode, zero-copy userspace (Ch. 82, 75): ef_vi/ExaNIC-class ~0.5-0.8 µs, DPDK ~1 µs — ~5-10× better than kernel stack |
| **FPGA inline (wire→wire)** | ~tens-100s ns | — | hardware on the NIC (Ch. 91) — below software bypass, with a *flat tail* |
| **PCIe round-trip (device register read)** | ~300 ns-1 µs | — | non-posted read = full round-trip (Ch. 87) — the floor under every offload (GPU/Ch. 89); why accelerators stay off the order path |
| **SSD/NVMe random read** | ~10-100 µs | — | off-hot-path only (Ch. 109 capture) — never on the path |
| **Spinning disk seek** | ~5-10 ms | — | historical reference; the "1 ms = eternity" scale |
| **Inter-DC round-trip (same metro)** | ~0.1-1 ms | — | why colocation (Appendix K.1–K.2; Ch. 80) — *physical distance* dominates |
| **Cross-continent round-trip** | ~50-150 ms | — | speed of light; no software fixes this |

A few ratios worth burning in (Ch. 1's "reason about the distribution and the ratios"):

- **L1 : DRAM ≈ 1 : 50–100.** A cache miss is ~250 cycles you didn't budget — two orders of magnitude, the single most important number in Parts II/IV. The whole cache-aware/data-oriented agenda (Ch. 8–12, 32) exists to keep you out of the DRAM row.
- **Branch mispredict ≈ an L2/L3-ish hit, ~15-20 cycles (~5-7 ns)** — cheap *individually*, but on a hot path executed millions of times an unpredictable branch is a real tax (Ch. 15) — hence branchless techniques.
- **Cross-core HITM (~40-100 ns) > branch mispredict, ≈ a DRAM access** — false sharing (Ch. 44) and contended atomics (Ch. 41) cost like a cache miss *every time the line bounces*. This is why "lock-free" with a contended hot line can be *slower* than a well-tuned lock (Ch. 43).
- **Syscall (~50-400 ns, mitigation-dependent) ≈ one to several DRAM misses — before any real kernel work or the cache/TLB side effects; context switch (µs) ≫ syscall.** The user/kernel boundary is a cliff (Ch. 54); crossing it on the hot path (a `write` to log — Ch. 98, a blocking call — Ch. 63) is why Parts VII-VIII work so hard to avoid it.
- **Kernel stack (µs) : kernel bypass (~0.5-1 µs) : FPGA (~100 ns).** The three network tiers (Ch. 63, 82, 91) — each ~5-10× the next. The tick-to-trade budget (Ch. 119) is dominated by which tier you're on.
- **PCIe round-trip (~µs) is the floor under offload** (Ch. 87) — it's why a GPU (Ch. 89), however fast its kernel, can't be on the order path, and why the FPGA goes *inline* (Ch. 91) instead of behind PCIe.

## E.2 How each was measured, with pointers to the deriving chapter

The numbers are only trustworthy if you know *how they're obtained* — and reproducing them is the best way to calibrate your own box (Ch. 3). Briefly, per row:

- **Cache/DRAM latencies (L1/L2/L3/DRAM)** — a **pointer-chasing** microbenchmark (a randomized linked list sized to each level, so the prefetcher can't hide the latency — Ch. 8), or `lmbench`'s `lat_mem_rd`, or Intel MLC (Memory Latency Checker). Read the *dependent-load* latency, not bandwidth. **Ch. 8** derives these; **Ch. 9** adds the row-hit/row-conflict split (the per-hop `rdtscp` histogram of its `dram_probe`, confirmed by the uncore `ACT_COUNT:CAS_COUNT` ratio), the loaded row (`mlc --loaded_latency`, or the same chaser with streaming neighbours) and the refresh stall (tRFC from the DIMM's JEDEC class); **Ch. 17** adds the TLB component; **Ch. 18** adds the NUMA-remote row (run MLC across sockets, or `numactl --membind` to force remote).
- **Branch mispredict** — a benchmark with a *deliberately unpredictable* branch (random data) vs a predictable one, differencing the cost; cross-check with `perf stat`'s `branch-misses` and the cycles attributable (Ch. 2 top-down). **Ch. 15** derives it.
- **Cross-core HITM** — two threads on different cores hammering the *same* cache line (a shared atomic or false-shared struct — Ch. 44); measure the per-op cost and confirm with `perf c2c` (cache-to-cache / HITM detection — Ch. 44) or the `MEM_LOAD_L3_HIT_RETIRED.XSNP_FWD` PMU event (`XSNP_HITM` is its older name on Ice Lake-SP and earlier — `perf stat -e mem_load_l3_hit_retired.xsnp_hitm`; `MEM_LOAD_L3_MISS_RETIRED.REMOTE_HITM` for the cross-socket case) (Ch. 2). **Ch. 44** (false sharing) and **Ch. 41** (atomics) derive it.
- **Signal delivery round trip** — `raise(SIGUSR1)` in a loop with an `SA_SIGINFO` handler (`raise` returns after the handler ran), 200,000 iterations, `rdtscp`-timed; repeated with `SA_ONSTACK` and with AVX-512 state dirtied before the loop. Measured at 2,937 / 3,199 / 2,896 cycles on a 2.7 GHz Xeon VM (Ch. 108 §108.3) — a CPU-bound path that transfers to the reference machine within the VM's `rdtscp` overhead; **Ch. 108** derives it, **Ch. 54 §54.4.3** uses it.
- **C ABI call through the PLT** — Ch. 95 §95.3's ladder: the same `book_apply_raw` called 20 M times over an L1-resident 1024-record ring from C++ with the body inlined (2.48 ns), through the PLT (2.90 ns) and with `-fno-plt` (2.58 ns); the row is the difference, measured on the 4-vCPU test VM (Appendix L.2) with GCC 13.3 `-O2 -march=native`, pinned — **not** the reference machine, so the absolute call cost is to be re-derived there; the managed-runtime rungs (FFM 10.5 ns, JNI 29 ns, cgo 50 ns, Python 340–474 ns) are in Ch. 95 §95.3 and are architectural facts about the runtime, not the box. **Ch. 95** derives it.
- **Mutex / atomic RMW** — a tight loop of lock/unlock (uncontended: one thread; contended: N threads) and of `fetch_add`/`CAS`, timed with `rdtsc` (Ch. 20). **Ch. 43** (spinlocks/mutex) and **Ch. 41** (atomics) derive these; the *contended* numbers are workload-dependent — measure yours.
- **`rdtsc`/syscall/context-switch** — `rdtsc` back-to-back to find its own overhead (Ch. 20); a null syscall (`getuid`/`getpid` pre-cache, or `syscall(SYS_...)`) in a loop for the syscall cost (Ch. 54); a two-thread/two-process ping-pong (pipe or futex) divided by two for the context-switch cost (Ch. 54), or `perf bench sched pipe`. **Ch. 20** (timer), **Ch. 54** (syscall/switch) derive these. Note the syscall number *moves with `mitigations=`* (Ch. 6) — measure with your production mitigation setting.
- **Page fault** — touch freshly-`mmap`'d pages and time the first access (minor fault), vs pre-faulted/`mlock`'d (Ch. 34). **Ch. 30** (page faults) and **Ch. 34** (mmap/mlock) derive it.
- **Cross-call IPI** — the worst gap reported by `rtla osnoise` / the Ch. 61 `gapdetect` loop on a signed-off isolated core while a housekeeping core deliberately provokes one cross-call per second (`rdmsr -p <hot> 0x34`, `perf stat -C <hot>`, a `munmap` in another thread of the same process), correlated with the `CAL`/`TLB` row delta in `/proc/interrupts` (Ch. 61 §61.3, Ch. 114 §114.3).
- **C-state wake-up** — `cpupower idle-info` lists each state's advertised exit latency; measure the real thing with `cyclictest` at a long interval (so the core idles between wake-ups) with deep C-states allowed vs capped at C1 (Ch. 6 §6.3), and watch residency with `turbostat`. **Ch. 6** derives it; Appendix C.1–C.2 are the knobs.
- **NIC RX (kernel stack vs bypass)** — **wire-to-application** timing using **NIC hardware timestamps / PTP** (Ch. 75, 78) so you capture the true wire time, not when software looked: a loopback or a known-cadence feed, differencing the hardware RX timestamp from the app's receive timestamp. Kernel-stack via the normal socket path (Ch. 63); bypass via `ef_vi`/DPDK/`AF_XDP` poll-mode (Ch. 75, 81, 82). **Ch. 63** (native I/O), **Ch. 82** (bypass), **Ch. 78** (the timestamping method) derive these — and **Ch. 119** composes them into the tick-to-trade budget.
- **FPGA inline (wire-to-wire)** — measured *externally* with a tap/PTP-timestamped capture (Ch. 78, 109), since the point is there's no host software to instrument — packet-in to packet-out across the FPGA-NIC (Ch. 91). **Ch. 91** derives it; the headline is the *flat tail* (p99 ≈ p50), which a single point value hides — measure the distribution (Ch. 1, 3).
- **PCIe round-trip** — time a **non-posted read** of a device register (MMIO read — Ch. 87), which forces a full round-trip (a posted *write* doesn't — Ch. 87's asymmetry); or use a vendor's PCIe latency tool. **Ch. 87** derives it and explains why the *read* is the expensive direction.
- **Storage / network-distance rows** — `fio` for NVMe (Ch. 109 context), `ping`/PTP for network round-trips; these are off-hot-path or physical-floor references (colocation — Appendix K.1), included for scale.

The meta-point (Ch. 3, 119): **reproduce the rows that matter for your decisions on your own hardware**, with your production build (Appendix D) and tuning (Appendix C) and `mitigations=` setting (Ch. 6) — a table from a book is for intuition; a number from your box is for engineering. Then fold the measured rows into the tick-to-trade decomposition (§119.3) so the budget is built from *your* costs, not these representative ones.

## E.3 Per-generation deltas: see Appendix R

The table above is one machine. Appendix R.4 restates every core-dependent row — L1/L2/L3, DRAM local and remote, branch mispredict, core-to-core within and across an L3 domain and across sockets, C1/C6 exit, `pause` — for Ice Lake-SP, Sapphire Rapids, Granite Rapids, Zen 3/4/5 EPYC and a 5.7 GHz client ring part, in ns *and* cycles at each part's pinned clock; R.2–R.3 carry the geometry (cache sizes, ROB, TLB reach, channels, lanes) the rows derive from. Two rows exist only there: **core-to-core across an L3 domain** (a CCD or tile boundary — Ch. 19), which a monolithic reference machine cannot have, and the **client-ring L3 hit** at ~9–10 ns that Ch. 7 builds its hot-box argument on. Appendix Q.9 says what to expect when the deriving chapters are re-run on AMD.

## E.4 References

- Jeff Dean's "Numbers Everyone Should Know" / Peter Norvig's "Teach Yourself Programming in Ten Years" latency table — the original this refreshes; and Colin Scott's interactive "Latency Numbers Over Time" visualization (E.1).
- **Intel Memory Latency Checker (MLC)**, **`lmbench`** (`lat_mem_rd`) and a pointer-chase microbenchmark (e.g. **tinymembench**) — tools for measuring the cache/DRAM/NUMA rows (E.2; Ch. 8, 18); **`stress-ng`** only as a background-load generator when measuring the same rows *under contention*.
- `perf` (`stat`, `c2c`, `mem`), the Intel SDM/optimization manual, and Agner Fog's instruction tables (Appendix G) — the microarchitectural latencies behind the cache/branch/atomic rows (E.2; Ch. 2, 8, 15, 41, 44).
- Ch. 3 (microbenchmarking — *how* to measure), Ch. 8/15/17/18/20/41/43/44/54/63/75/82/87/91 (the chapters that derive each row), Ch. 119 (composing them into tick-to-trade).
- Carl Cook, "When a Microsecond Is an Eternity" (CppCon) — the trading framing of why these numbers matter (E.1).

## E.5 Additional Reading

- Brendan Gregg's *Systems Performance* (the latency-analysis methodology and the orders-of-magnitude tables) — and his blog's latency-heatmap material.
- The "Latency Numbers Every Programmer Should Know" GitHub gists and their periodic refreshes — community-maintained updates to the original table (E.1).
- Vendor microarchitecture deep-dives (Intel/AMD optimization manuals, AnandTech/Chips-and-Cheese latency measurements) for current-silicon cache/DRAM numbers (E.1).
- **Appendix C** (System Tuning) — the knobs that move several rows (C-state wake-up, page fault, context switch); **Appendix D** (Compiler Flags) — the build that affects the branch/inline rows; **Appendix F** (Glossary) — HITM/TLB/futex/MMIO terms; **Appendix G** (Bibliography) — the manuals behind the measurements; **Ch. 1** — why the *distribution* behind each point value is what you actually trade.

---

*Next: Appendix F — Glossary (HFT & Microarchitecture Terms), an alphabetized quick reference for the acronyms and jargon used throughout — trading & market-structure, microarchitecture, memory & OS, concurrency, and C++/toolchain — each entry a line or two with a pointer to the chapter that develops it.*
