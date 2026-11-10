# Appendix R — x86 Generation Cheat Sheet

> **Relates to:** Appendix E (the latency table this appendix restates per generation), Appendix L.3 (the reference machine), Ch. 7 (the silicon decision the tables inform), Ch. 18 (chiplets and tiles — the topology column), Ch. 54 (C-state exits and licences per generation), Ch. 8, 12–14, 16 (the microarchitectural quantities tabulated), Appendix Q (AMD specifics), Appendix D.2 (`-march=` per generation). The book's numbers are calibrated to a 2021 Ice Lake-SP; a reader on 2023–2026 silicon needs to know what moved and by how much. **Every figure here is from vendor specification sheets and architecture documents or from published independent measurements, marked (m); none was measured for this book** — reproduce the ones that matter with Ch. 8's sweep, Ch. 12's chains and Ch. 18's map.

## R.1 How to read this appendix

Three tables and a note. **R.2** is the Intel server line from the reference generation forward (Ice Lake-SP → Sapphire Rapids → Emerald Rapids → Granite Rapids) with the client parts Ch. 7 discusses; **R.3** is AMD Zen 3 → 4 → 5 (EPYC and Ryzen). Both list the quantities the book's chapters depend on: core counts and clocks (Ch. 7), cache sizes and the L3 topology (Ch. 8, 18), ROB and dispatch width (Ch. 12), µop-cache size (Ch. 13), TLB reach (Ch. 16), memory channels and PCIe lanes (Ch. 7, 78), the deep C-state exit (Ch. 54), AVX-512 behaviour (Ch. 34) and DDIO (Ch. 68). **R.4** restates Appendix E.1's latency rows per generation, in nanoseconds *and* cycles at each part's typical pinned clock, because a cycle is a different length on each. **R.5** is the accelerator inventory — AMX, DSA, IAA, QAT, DLB, AVX10, APX — and why each belongs beside the hot path rather than on it. Where a cell says "~", it is a range across SKUs; where it says (m), an independent measurement; otherwise, the vendor's specification.

## R.2 Intel: Ice Lake-SP → Sapphire → Emerald → Granite Rapids (and the client parts)

```
                         Ice Lake-SP (ref.)   Sapphire Rapids      Emerald Rapids       Granite Rapids (Xeon 6P)   client: Raptor Lake / Arrow Lake
   year / core           2021 / Sunny Cove    2023 / Golden Cove   2023-24 / Raptor Cove 2024 / Redwood Cove      2022-23 Raptor Cove+Gracemont / 2024 Lion Cove+Skymont
   -march=               icelake-server       sapphirerapids       emeraldrapids        graniterapids             raptorlake / arrowlake
   die topology          monolithic mesh      4 tiles, EMIB        2 tiles, EMIB        2–3 compute + 2 I/O tiles ring (RPL) / tiles (ARL)
   SNC modes             SNC2                 SNC2 / SNC4          SNC2                 SNC3 / HEX                —
   cores (max)           40                   60 (56 on most)      64                   128 (6900P) / 86 (6700P)  8P + 16E / 8P + 16E (no SMT on ARL)
   base / 1-core turbo   ~2.0–2.9 / 3.5–3.7   ~1.9–2.4 / 3.8–4.2   ~1.9–2.5 / 3.9–4.1   ~2.0–2.5 / 3.9–4.2        3.2 / 6.0 (14900K) ; 3.7 / 5.7 (285K)
   high-frequency SKUs   6334 (3.6/3.7, 8c)   6444Y (3.6/4.0, 16c) 6544Y (3.6/4.1, 16c) 6517P / 6737P-class        the whole line
   L1d / L2 per core     48 KiB / 1.25 MiB    48 KiB / 2 MiB       48 KiB / 2 MiB       48 KiB / 2 MiB            48 KiB / 2 MiB (P); 64 KiB / 4 MiB per 4 E
   L3 total / per core   60 MiB / 1.5 MiB     112.5 MiB / 1.875    320 MiB / 5 MiB      up to 504 MiB / ~4 MiB    36 MiB (RPL) / 36 MiB (ARL)
   L3 hit latency (m)    ~61 cyc, ~21 ns      ~100+ cyc, ~30–35 ns ~90 cyc, ~28 ns      ~110 cyc, ~35–40 ns       ~45 cyc, ~9–10 ns (RPL ring)
   ROB / dispatch        352 / 5              512 / 6              512 / 6              512 / 6                   512 / 6 (P-core)
   µop cache (DSB)       2.25K                4K                   4K                   4K                        4K (P)
   L1 DTLB / STLB        64+32+8 / 2048       96 / 2048            96 / 2048            96 / 2048                 96 / 2048
   memory                8 ch DDR4-3200       8 ch DDR5-4800       8 ch DDR5-5600       12 ch DDR5-6400 / MRDIMM-8800   2 ch DDR5-5600 / 6400
   DRAM latency (m)      ~94 ns               ~110–120 ns          ~105–115 ns          ~110–125 ns (SNC3 local ~95)   ~70–80 ns
   PCIe (CPU lanes)      64 × Gen4            80 × Gen5 + CXL 1.1  80 × Gen5 + CXL 1.1  96 × Gen5 + CXL 2.0       20 (16 Gen5 + 4 Gen4) / 24
   AVX-512 & licences    yes; L1/L2 licences  yes; narrower drops  yes; narrower        yes (+AVX10.1 announced)  no AVX-512 (fused off); AVX10 from later gens
   accelerators          —                    AMX, DSA, IAA, QAT, DLB (SKU-gated)        same                      AMX(+FP16), DSA, IAA, QAT, DLB   NPU (ARL) only
   DDIO                  yes (2 ways)         yes                  yes                  yes                       no
   deep C-state exit     C6 170 µs            C6 290 µs            C6 ~290 µs           C6 170 / C6P 210 µs       C6 ~120–150 µs (client tables)
   core-to-core (m)      45–65 ns (mesh)      55–65 same tile, 60–75 cross-tile  similar   50–70 / +EMIB            P↔P ~25–35 ns; E-cluster pairs ~30–40
```

Reading it for the book: the **L3 got bigger and slower** at every server generation (Ice Lake's ~21 ns became ~30–40 ns on the tiled meshes), so Ch. 8's "L3 hit" row and Ch. 30's "fit the book in L3" budget both move the wrong way — and Ch. 7's client ring at ~10 ns is the outlier that makes desktop silicon attractive. **L2 doubled** to 2 MiB from Sapphire Rapids, which is the level to design for. **C6 exit grew** to 290 µs on Sapphire/Emerald Rapids (Ch. 54 §54.2.1). **Memory latency rose** with DDR5 and bigger fabrics; channel count and bandwidth rose more. SNC went from two to three (Granite Rapids) and tiles made Ch. 18's map matter on Intel too. The client parts lost AVX-512 and gained E-cores (Ch. 7 §7.2.4); Arrow Lake dropped SMT.

## R.3 AMD: Zen 3 → Zen 4 → Zen 5 (EPYC and Ryzen)

```
                         Zen 3 (Milan / 5000)       Zen 4 (Genoa, Bergamo / 7000)         Zen 5 (Turin, Turin-dense / 9000)
   year                  2021 / 2020                2022 / 2023                           2024
   -march=               znver3                     znver4                                znver5
   CCD                   8 cores + 32 MiB L3        8 cores + 32 MiB (Zen 4) ; 16 cores + 16 MiB (Zen 4c)   8 + 32 MiB (Zen 5) ; 16 + 32 MiB (Zen 5c)
   EPYC cores (max)      64 (8 CCDs)                96 Genoa (12 CCDs) / 128 Bergamo       128 Turin (16 CCDs) / 192 Turin-dense
   EPYC clocks           base 2.0–3.5 / boost 3.5–4.1 (75F3: 2.95/4.0)   base 2.4–4.1 / boost 3.7–4.4 (9174F: 4.1/4.4)   base 2.2–4.0 / boost 3.7–5.0 (9575F: 3.3/5.0)
   Ryzen clocks          5950X: 3.4 / 4.9           7950X: 4.5 / 5.7 ; 7950X3D: 4.2 / 5.7  9950X: 4.3 / 5.7 ; 9950X3D: 4.3 / 5.7
   V-Cache parts         Milan-X (768 MiB), 5800X3D  Genoa-X (1152 MiB), 7950X3D (one CCD)   9950X3D (one CCD); Turin-X announced
   L1d / L2              32 KiB / 512 KiB           32 KiB / 1 MiB                        48 KiB / 1 MiB
   L3 hit latency (m)    ~46 cyc, ~13 ns            ~50 cyc, ~14 ns                       ~47 cyc, ~12 ns
   ROB / dispatch        256 / 6                    320 / 6                               448 / 8
   op cache              4K µops                    6.75K                                 6K (2-ahead fetch)
   L1 DTLB / L2 TLB      64 / 2048                  72 / 3072                             96 / 4096
   memory (EPYC)         8 ch DDR4-3200             12 ch DDR5-4800                       12 ch DDR5-6400
   memory (Ryzen)        2 ch DDR4-3200             2 ch DDR5-5200                        2 ch DDR5-5600
   DRAM latency (m)      ~100–110 ns (NPS1 EPYC)    ~105–115 NPS1 / ~95 NPS4 ; ~75 Ryzen   ~100 NPS1 / ~90 NPS4 ; ~75 Ryzen
   PCIe (CPU lanes)      128 × Gen4 (EPYC) / 24 Gen4 (Ryzen)   128 × Gen5 + CXL 1.1 / 28 Gen5   128 × Gen5 + CXL 2.0 / 28 Gen5
   AVX-512               no                         yes, 256-bit datapath, no licence     yes, 512-bit datapath (Zen 5), no licence
   deep C-state exit     ACPI C2 (CC6): table ~18 µs, measured 100+ µs (Ch. 54)  same      same
   core-to-core (m)      ~20 same CCD / ~100 cross  ~19 / ~85–100                         ~18–20 / ~80–90
   DDIO                  no (no equivalent)         no                                    no
```

Reading it: the CCD stayed 8 cores + 32 MiB for three generations (Ch. 18's block size is stable), the **L2 doubled** at Zen 4, **AVX-512 arrived without a licence** at Zen 4 and at full width at Zen 5, the **core got wider** (8-wide, 448 ROB) at Zen 5, and EPYC **boost clocks reached 5.0 GHz** on the Turin F-series — Ch. 7's "F SKU" option. V-Cache tripled the CCD L3 at every generation for the book that does not fit 32 MiB. DRAM latency is higher than Intel's monolithic reference but the L3 is much faster; the tick-to-trade path's balance between the two decides (Ch. 7 §7.2.1).

## R.4 Appendix E restated per generation

Appendix E.1's rows at each part's *typical pinned* clock (the clock named in the header; cycles = ns × GHz). Representative; vendor documents and (m) published measurements; Ch. 8/18 reproduce the cache, DRAM and core-to-core rows, Ch. 14 the mispredict, Ch. 54 the C-state rows. Rows that do not depend on the core (syscall with a given mitigation set, kernel RX, bypass RX, PCIe round trip, FPGA) are unchanged from Appendix E and omitted.

```
   operation                     Ice Lake-SP @ 2.9   Sapphire Rapids @ 3.5   Granite Rapids @ 3.2   Zen 3 EPYC @ 3.5   Zen 4 EPYC @ 3.7   Zen 5 EPYC @ 4.0   client ring @ 5.7 (RPL)
                                 ns     cycles       ns      cycles          ns      cycles         ns     cycles      ns     cycles      ns     cycles      ns     cycles
   1 cycle                       0.345  1            0.286   1               0.313   1              0.286  1           0.270  1           0.250  1           0.175  1
   L1 hit (load-to-use)          1.7    5            1.4     5               1.6     5              1.1    4           1.1    4           1.0    4           0.9    5
   L2 hit                        ~4.8   14           ~4.6    16              ~5.0    16             ~3.4   12          ~3.8   14          ~3.5   14          ~2.8   16
   L3 hit                        ~21    61           ~30–35  105–120         ~35–40  110–130        ~13    46          ~14    50          ~12    47          ~9–10  50–55
   branch mispredict             ~6     17           ~5      17              ~5.5    17             ~4.5   16          ~4.3   16          ~4     16          ~3     17
   DRAM, local (unloaded)        ~94    270          ~115    400             ~115    370            ~105   370         ~110   410 (NPS4 ~95)  ~100  400          ~75    430
   DRAM, other socket            ~150   430          ~180    630             ~185    590            ~200   700         ~190   700         ~180   720          —
   core-to-core, same L3 domain  ~55    160 (mesh)   ~60     210 (tile)      ~60     190            ~20    70 (CCD)    ~19    70          ~19    75           ~30    170 (ring)
   core-to-core, cross domain    —      —            ~70     245 (EMIB)      ~70     225            ~100   350 (IOD)   ~90    330         ~85    340          —
   core-to-core, cross socket    ~125   360          ~140    490             ~140    450            ~200   700         ~180   670         ~170   680          —
   C1 exit / C6 exit             1 µs / 170 µs       1 / 290                 1 / 170 (C6P 210)      1 / 100+ (m)       1 / 100+ (m)       1 / 100+ (m)       1 / ~130
   pause (m)                     ~40 cyc, 14 ns      ~45 cyc, 13 ns*         ~45 cyc, 14 ns*        ~65 cyc, 19 ns      ~65, 18 ns         ~65, 16 ns         ~45, 8 ns
   imul r64 (Ch. 7 probe)        3 cyc               3                       3                      3                  3                  3                  3
```

(\* `pause` moved between ~10 and ~140 cycles across Intel generations — Skylake raised it to ~140, Ice Lake brought it back to ~40, and the Golden/Redwood Cove figures are published measurements on client parts; measure it on the server part with Ch. 37's loop before trusting a backoff constant.) The pattern across the row set: **core-bound rows scale with the clock** (Ch. 7 §7.2.1 — the client column wins them all), **the L3 row got worse on every tiled Intel server part and is 2–3× better on any AMD CCD**, **DRAM got slower in nanoseconds everywhere except the client parts**, and **the cross-domain core-to-core row is the one Appendix E could not have — it exists only on chiplet silicon** (Ch. 18).

## R.5 On-die accelerators: beside the hot path, not on it

Sapphire Rapids and its successors (and, differently, Zen 4/5) added fixed-function and matrix hardware. None of it is on the tick-to-trade path, because each is reached either through a device-style submission/completion interface with a PCIe-class round trip (Ch. 78) or through a mode switch with a warm-up cost; several are useful *beside* it.

```
   accelerator   what it is                                         interface                    trading use (and why not the hot path)
   ───────────── ────────────────────────────────────────────────── ──────────────────────────── ─────────────────────────────────────────────────
   AMX           tile-matrix units: INT8/BF16 (FP16 on GNR) GEMM    instructions (TMUL), tile     ML inference beside the strategy (Ch. 79's GPU
                 in 8 1 KiB tile registers                          state is XSAVE-managed        alternative on the host); first use after idle
                                                                                                  pays a tile-config + licence-class power ramp
   DSA           Data Streaming Accelerator: memcpy/memmove/fill,   work queues via ENQCMD/       capture-writer and replay copies, journal
                 CRC, compare, dualcast, DMA between address        MOVDIR64B, completion by      flushes (Ch. 98) — bulk copies off the core; a
                 spaces                                             polling or interrupt           submission is ~µs round trip: never a per-message op
   IAA           In-Memory Analytics Accelerator: compression/      same work-queue model          compressing tick storage (Ch. 85 §85.4.7),
                 decompression (Deflate), filter/scan                                              never on the order path
   QAT           QuickAssist: crypto (TLS record processing,        work queues / kernel crypto    TLS to cloud/web venues (Ch. 77), VPN; a ~10 µs
                 asymmetric), compression                           API, OpenSSL engine             round trip, so only for throughput paths
   DLB           Dynamic Load Balancer: hardware queue manager       memory-mapped producer/        a hardware MPMC queue with ordering/atomic
                 (enqueue/dequeue, ordering, load balance across    consumer ports (DPDK eventdev)  scheduling across cores — Ch. 43's shard fan-out in
                 cores) — the one that looks like it belongs                                        silicon; ~400–800 ns enqueue→dequeue (m), i.e. slower
                                                                                                  than an in-L3 SPSC ring (Ch. 39) for a point-to-point hop
   AVX10         the converged vector ISA: AVX-512 features at       -march=/-mavx10.1 (GCC 14 /    lets one binary vectorise for P- and E-cores; a
                 256- or 512-bit width, including on E-cores         Clang 18+), AVX10.2 later      compile-target question (Appendix D.2), not a runtime one
   APX           Advanced Performance Extensions: 32 GPRs (R16–R31), -mapxf (GCC 14 / Clang 19;     fewer spills and shorter dependency chains on the
                 3-operand integer ops, conditional load/store/cmp   Diamond Rapids / Panther Lake  decode and book kernels (Ch. 12, 64); verify with Ch. 4's
                                                                     and later)                      asm reading when the hardware ships
   (AMD)         no AMX/DSA/IAA/QAT equivalents on the CPU; AMD's      —                            Pensando DPUs and Xilinx FPGAs (Ch. 82, 81) are the
                 acceleration story is off-socket                                                    accelerator line
```

The rule Ch. 7 and Ch. 78 share: **a round trip to anything that is not the core's own execution units is ≥ 1 µs**, which is the whole tick-to-trade budget on a good day; accelerators earn their place on capture, replay, storage, encryption and research, and DLB is the one to benchmark against Ch. 39's queues before believing its brochure. AVX10 and APX are compiler flags whose codegen effect is read with Ch. 4 and Appendix D, and whose *frequency* effect is checked with Ch. 54.

## R.6 References

- Intel, product specification pages (ARK) for the 3rd, 4th and 5th Gen Xeon Scalable and Xeon 6 families and for Core 13th/14th Gen and Core Ultra 200S; the *Intel® 64 and IA-32 Architectures Optimization Reference Manual* chapters on Ice Lake-SP, Sapphire Rapids, Granite Rapids, Alder/Raptor Lake and Lunar/Arrow Lake; *Intel® Advanced Matrix Extensions*, *Data Streaming Accelerator Architecture Specification*, *In-Memory Analytics Accelerator*, *QuickAssist*, *Dynamic Load Balancer* programming guides; *Intel® AVX10 Architecture Specification*; *Intel® APX Architecture Specification*.
- AMD, product specification pages for EPYC 7003/9004/9005 and Ryzen 5000/7000/9000; the *Software Optimization Guides* for Zen 3/4/5 (Appendix Q.10); *AMD EPYC 9004/9005 Architecture Overview* white papers.
- Linux kernel, `drivers/idle/intel_idle.c` (the C-state exit tables) and `tools/perf/pmu-events/arch/x86/*/cache.json` (cache geometry as `perf` sees it); `lscpu`, `lstopo` and `cpuid -1` for the geometry of the part in front of you.
- Chips and Cheese; Anandtech; *uops.info* (Abel & Reineke) — the (m) rows: independently measured cache, DRAM and core-to-core latencies, and instruction latencies per generation.

## R.7 Additional Reading

- Ch. 7 (*Choosing & Clocking the Hot-Path CPU*) — how to turn these tables into a purchase; Ch. 18 (*Chiplets, Tiles & NUMA Within the Socket*) — the topology column measured; Ch. 54 (*Power Management & Frequency Determinism*) — the C-state and licence columns measured; Appendix E — the reference-machine table this appendix fans out; Appendix Q — the AMD-specific method notes.
- Agner Fog, *The microarchitecture of Intel, AMD and VIA CPUs* — the per-generation core descriptions behind the ROB/dispatch/µop-cache rows, updated with each new part.
- Hennessy & Patterson, *Computer Architecture: A Quantitative Approach* (6th ed.), ch. 2 and Appendix B — why the L3 row moves the way it does as caches grow and fabrics lengthen.
