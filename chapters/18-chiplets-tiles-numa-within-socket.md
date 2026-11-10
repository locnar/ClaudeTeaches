# Part II — CPU Microarchitecture

# Chapter 18 — Chiplets, Tiles & NUMA Within the Socket: the Core-to-Core Latency Map

> **Prerequisites:** Ch. 17 (NUMA — nodes, first-touch, the distance matrix; this chapter is what happens when a *socket* is several of those), Ch. 8 (MESI coherence and the L3 — §8.2.1's "L3 is shared across the socket" is the assumption this chapter breaks), Ch. 7 (server vs client silicon, the ring and the mesh), Ch. 6 §6.4.2 (`lscpu`/`lstopo`). It uses the vocabulary of Ch. 35 (acquire/release) and Ch. 38 (HITM, cache-line ping-pong) in its harness — read §38.2 ahead if those are new.
>
> **Leads into:** Ch. 19 (the TSC — the instrument the harness times with), Ch. 39–42 (SPSC/MPMC queues, seqlocks and the Disruptor — every one of them is a cache-line handoff whose cost this chapter's map predicts), Ch. 43 (threading topologies — the hop-cost model gets its per-pair numbers here), Ch. 50 §50.4.3 (pinning *to the right cores* using the map), Ch. 52 (cache allocation — `resctrl` domains are L3 instances, which chiplets multiply), Ch. 59 (cross-process rings), Ch. 65–67 (NIC placement, DDIO, flow steering — the device sits in *one* domain), Appendix L §L.7 (the harness as a standing tool), Appendix Q (AMD companion). Ch. 17 said "make the distance to memory zero"; this chapter adds "and the distance between the two cores that talk."

---

## 18.1 Why it matters: the two cores you pick decide the handoff

Ch. 39's SPSC queue and Ch. 42's Disruptor are measured "with producer and consumer pinned to two cores." Which two? On the book's Ice Lake-SP reference box the question barely matters: one die, one mesh, one 24 MiB L3, and the one-way cache-line transfer between any two cores lands in a ~45–65 ns band. On the silicon most 2025–2026 fleets actually buy it matters enormously. An AMD EPYC socket is 8–16 **core complex dies (CCDs)**, each with its own 32 MiB L3, hanging off an I/O die; two cores in the same CCD exchange a line in ~20 ns, two cores in different CCDs in ~80–100 ns — through the I/O die and back. Intel's Sapphire/Emerald/Granite Rapids are 2–4 compute **tiles** stitched by EMIB into one logical mesh, with **Sub-NUMA Clustering** optionally cutting the socket into 2–3 NUMA nodes, each with its own L3 slices and memory controllers. A feed-handler → book → strategy → gateway chain (Ch. 90) is three handoffs; placed across CCDs it pays ~250 ns more than placed within one, per message — **larger than the entire difference between a good and a bad queue implementation**, and invisible to every tool in Ch. 39–42 because they measure the queue, not the distance.

The book's topology model so far is "socket + SMT sibling" (Ch. 17, 51). It cannot express a CCD, a tile, an SNC node or a V-Cache die, and `numactl --hardware` in the default firmware mode (NPS1, SNC off) hides all of them behind a single node. This chapter adds the missing level. §18.2 is the mechanism: what a chiplet socket is on AMD and Intel, what SNC/NPS modes expose, and why a cross-domain transfer costs what it does. §18.3 measures it with a 100-line **core-to-core ping-pong harness** that produces the latency matrix for every core pair and renders it as a heat-map whose *block structure* is the topology — the one experiment to run on every new box before anything is pinned. §18.4 turns the map into placement rules for queues, pipelines, IRQs and NICs, and into the SNC/NPS decision; §18.5 is what goes wrong.

## 18.2 Mental model: a socket is a small cluster

### 18.2.1 AMD: CCDs, CCXs, the I/O die and Infinity Fabric

A Zen-based EPYC or Ryzen package is a set of **CCDs** (core complex dies, TSMC-fabbed) plus one **IOD** (I/O die) carrying the memory controllers, PCIe root ports and the Infinity Fabric switch. Within a CCD, a **CCX** (core complex) is a group of cores sharing one L3; since Zen 3 a CCD *is* one CCX of 8 cores with 32 MiB L3 (Zen 4c/5c "dense" CCDs: 16 cores sharing a smaller L3; Zen 5 keeps 8 cores + 32 MiB). The L3 is a **non-inclusive victim cache** of its CCD's L2s: it holds lines evicted from the CCD's cores, and a core in CCD 0 *cannot* hit in CCD 1's L3 — a line owned by a core across the fabric is fetched from that core's cache via the IOD, not from "the L3."

```
   EPYC 9004/9005 ("Genoa"/"Turin") socket — 8 or 12 CCDs around one IOD (8 shown)

   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐          CCD: 8 cores (16 threads), 1 MiB L2 each,
   │CCD 0│ │CCD 1│ │CCD 2│ │CCD 3│               32 MiB L3 shared by the 8 cores ONLY
   │ L3  │ │ L3  │ │ L3  │ │ L3  │          GMI link CCD↔IOD: ~36–64 GB/s read per CCD
   └──┬──┘ └──┬──┘ └──┬──┘ └──┬──┘
   ───┴────────┴────────┴────────┴──── GMI (Infinity Fabric on-package)
   ┌──────────────────────────────────┐
   │  IOD: 4 quadrants, each with     │     12 DDR5 channels, 128 PCIe 5.0 lanes
   │  3 memory channels + PCIe roots  │     NPS1: one node   NPS2: 2   NPS4: 4 (one per quadrant)
   └──────────────────────────────────┘     "L3 as NUMA domain": one node per CCD (8–12 nodes)
   ───┬────────┬────────┬────────┬────
   ┌──┴──┐ ┌──┴──┐ ┌──┴──┐ ┌──┴──┐
   │CCD 4│ │CCD 5│ │CCD 6│ │CCD 7│
   └─────┘ └─────┘ └─────┘ └─────┘

   core-to-core one way:  same CCD ~18–22 ns · cross-CCD (via IOD) ~75–100 ns · cross-socket ~130–200 ns
   DRAM: local quadrant (NPS4) ~95–105 ns · far quadrant ~115–130 ns · other socket ~190+ ns
```

Three firmware knobs shape what Linux sees. **NPS (nodes per socket)** = 1, 2 or 4 partitions the IOD's memory controllers into that many NUMA nodes, so a core's "local" DRAM is its quadrant's three channels (lower latency, a quarter of the bandwidth; the default NPS1 interleaves across all twelve). **"ACPI SRAT L3 Cache as NUMA Domain"** goes further and reports *every CCD* as a NUMA node — the only mode in which `numactl --hardware` tells you the L3 topology; with it off, the CCD boundaries are visible only in `/sys/devices/system/cpu/cpuN/cache/index3/shared_cpu_list` and in the heat-map. **Infinity Fabric clock** (FCLK) and the **"Determinism"/"Power" mode** set the IOD's speed and whether the fabric is allowed to downclock when idle — a fabric that sleeps adds the first-message tail of Ch. 54 to every cross-CCD hop.

On **Ryzen** (one or two CCDs, a smaller IOD, 2 memory channels) the same model holds at a smaller scale; the **X3D** parts add 64 MiB of stacked V-Cache to *one* CCD (on the two-CCD 7950X3D/9950X3D), making that CCD's L3 96 MiB at a few hundred MHz lower clock — a book that fits in 96 MiB but not in 32 MiB belongs on the V-Cache CCD, a core-bound strategy on the other, and the two must not be chained across the IOD if it can be helped (§18.4.1). Appendix Q carries the AMD-specific PMU names and knobs.

### 18.2.2 Intel: monolithic mesh, then tiles over EMIB, and Sub-NUMA Clustering

Ice Lake-SP — the reference machine's Xeon Gold 6326 — is **monolithic**: one die, a 2-D **mesh** of cores each with an L3 slice (1.5 MiB) and a **CHA** (caching/home agent) that is the coherence home for a hash-determined set of addresses. Any core may hit any slice; latency depends on the mesh distance between the requesting core and the slice that owns the address, which is why Ice Lake's L3 hit is "~45–70 cycles" and not a number (Ch. 8 §8.3). The memory controllers sit at the mesh's edges. **Sub-NUMA Clustering (SNC2)** on this part splits the mesh's addresses into two halves, each homed in the slices and memory controllers on its side, and reports two NUMA nodes per socket: local L3 and DRAM latency fall by ~10–15 %, the far half becomes a remote node (Ch. 17 §17.2.1 already notes it), and the software must now place memory *and* threads per half.

Sapphire Rapids (4th gen) is **four tiles** — each a quarter-mesh with ~15 cores, L3 slices, 2 memory channels and PCIe roots — connected with **EMIB** bridges into one logical mesh; Emerald Rapids (5th) is two larger tiles; Granite Rapids (Xeon 6 P-cores) is 2–3 compute tiles plus 2 I/O tiles, with **SNC3** (one node per compute tile) or the uniform "HEX" mode. The mesh is logically uniform, but a request that crosses an EMIB bridge pays ~5–15 ns more than one that stays on-tile, and the memory controllers *are* on specific tiles. In SNC mode the kernel sees 2–3 nodes per socket, memory and L3 slices are tile-local, and the rules of §18.4 apply exactly as on AMD; in uniform mode the tiles are invisible except in the heat-map, where they appear as a faint block pattern on top of the mesh-distance gradient.

```
   Granite Rapids (Xeon 6 6900P) — 3 compute tiles + 2 I/O tiles, SNC3

   ┌───────────┐┌─────────────┐┌─────────────┐┌─────────────┐┌───────────┐
   │ I/O tile  ││ compute 0   ││ compute 1   ││ compute 2   ││ I/O tile  │
   │ PCIe/UPI  ││ ~43 cores   ││ ~43 cores   ││ ~43 cores   ││ PCIe/UPI  │
   │ accel.    ││ L3 slices   ││ L3 slices   ││ L3 slices   ││ accel.    │
   │           ││ 4 DDR5 ch   ││ 4 DDR5 ch   ││ 4 DDR5 ch   ││           │
   └───────────┘└─────────────┘└─────────────┘└─────────────┘└───────────┘
                ◄── EMIB ──►   ◄── EMIB ──►   ◄── EMIB ──►
   SNC3: node 0 = compute 0 + its 4 channels, node 1 = compute 1, node 2 = compute 2
   a NIC on the left I/O tile is "nearest" node 0; one on the right, node 2 (§18.4.3)
```

### 18.2.3 What a core-to-core transfer costs, and why the domain decides it

A consumer's load of a line the producer just wrote (the SPSC handoff — Ch. 39) is a coherence transaction: the consumer misses in L1/L2, the request goes to the home agent (CHA on Intel, the CCD's L3 controller then the IOD's directory on AMD), the home finds the line **Modified** in the producer's cache and **snoops** it, the producer's cache forwards the data (a **HITM** — Ch. 38 §38.2), and the line changes state. The *path length* of that transaction is the topology:

```
   path                                   one way (representative)   what sets it
   ─────────────────────────────────────  ─────────────────────────  ───────────────────────────────────────────────
   SMT siblings (same core)                ~8–15 ns                   shared L1/L2 — no coherence transaction at all
   same L3 domain (CCD / tile / SNC node)  ~18–22 ns (Zen 4/5 CCD)    L3 controller + snoop within the die
                                           ~45–65 ns (Ice Lake mesh)  mesh distance core→CHA→owner→core
                                           ~25–35 ns (client ring)    short ring, high clock
   cross-domain, same socket               ~75–100 ns (Zen cross-CCD) CCD→GMI→IOD directory→GMI→CCD and back
                                           ~55–75 ns (SPR/GNR cross-tile, uniform mode)  + an EMIB crossing each way
   cross-socket                            ~110–140 ns (UPI)          + the UPI/xGMI link; a *remote* HITM (Ch. 17)
                                           ~130–200 ns (xGMI, Zen)
```

Two consequences. First, **the handoff cost is the one-way latency, and a request/response pair costs two of them** — a strategy that asks the risk gate (Ch. 91) a question across CCDs waits ~180 ns for an answer that would have taken ~40 ns in-CCD. Second, **it is the same ratio for every lock-free structure in Part VI**: an MPMC queue's CAS on a contended head, a seqlock reader's re-check after a writer, a Disruptor's sequence barrier all move lines between the same two cores, so the map is a *multiplier* on all of them. The Ch. 36 false-sharing penalty likewise scales: two unrelated hot fields on one line, written by two cores in different CCDs, bounce at ~90 ns a throw instead of ~20.

### 18.2.4 Reading the map: the four blocks

Run the §18.3 harness on every CPU pair and render the matrix with a colour or character per latency bin, and the topology draws itself:

```
   a 32-core, 4-CCD EPYC (NPS1, SMT off), one-way ns, 5 bins  ' ' <25  '.' <50  ':' <75  '#' <100  '@' ≥100

          CCD0            CCD1            CCD2            CCD3
   CCD0  [        ]      [########]      [########]      [########]      same CCD ~20 ns
   CCD1  [########]      [        ]      [########]      [########]      cross-CCD ~85 ns
   CCD2  [########]      [########]      [        ]      [########]
   CCD3  [########]      [########]      [########]      [        ]

   the same socket with a second socket attached: the off-diagonal 32×32 quadrant is all '@' (~150 ns)
   an Ice Lake-SP 6326: one 16×16 block of '.' and ':' with a *gradient* (mesh distance), no hard edges
   a hybrid client part: a tight P-core block, E-core 4-clusters as small blocks, P↔E at ring latency
```

The diagonal blocks are the **L3 domains**; their size is the number of cores that can talk cheaply; the off-diagonal level is the **domain-crossing tax**; a second level of off-diagonal is the **socket-crossing tax**. Everything in §18.4 is "keep each handoff inside a diagonal block, and put the NIC's domain at the head of the chain."

## 18.3 Measure it: the core-to-core ping-pong harness and its heat-map

**Machine, toolchain, flags.** Reference machine per Appendix L.3 — **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz, turbo off; the dual-socket 2× 6326 box of Ch. 17 for the cross-socket row), GCC 14.2 / Clang 18.1, `-O2 -std=c++20 -pthread`, Linux 6.18, `chrt -f 50`, SMT siblings idle. The AMD column is **representative** for an EPYC 9354-class part (Zen 4, 32 cores in 4 CCDs, NPS1) from published `c2clat`-style measurements and AMD's architecture documentation; reproduce it with the listing. The *VM run* below is a real run on the author's 4-vCPU KVM guest (Sapphire Rapids host) with GCC 13.3 and Clang 18.1.

The harness: two threads, one cache line, a strict alternation of writes — A writes *2k+1* and spins for *2k+2*; B spins for *2k+1* and writes *2k+2*. Each round trip is exactly two line transfers, so the one-way latency is half the round-trip time; the median over 16 chunks discards the odd interrupt; and each pair gets a **fresh line** so no residual ownership from the previous pair leaks in.

```cpp
// c2c.cpp — core-to-core latency map: for every (i, j) pair of online CPUs, two pinned threads
// bounce one cache line back and forth and the one-way latency (half a round trip) is reported.
// Build: g++ -O2 -std=c++20 -pthread -o c2c c2c.cpp        (clang++ 18 identical)
// Run:   sudo chrt -f 50 ./c2c [iterations=200000] [cpulist, e.g. 0-7,16-23]   → matrix on stdout
// Read it as blocks: SMT sibling < same L3 (CCD / tile / SNC node) < cross-L3 same socket < cross-socket.
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <pthread.h>
#include <sched.h>
#include <string_view>
#include <thread>
#include <vector>
#include <x86intrin.h>

#ifdef __cpp_lib_hardware_interference_size
constexpr size_t kLine = std::hardware_destructive_interference_size;   // 64 on x86-64 with GCC
#else
constexpr size_t kLine = 64;        // Clang + libstdc++ does not define it (Ch. 38 §38.2); assume x86-64
#endif
struct alignas(kLine) Line { std::atomic<uint32_t> v{0}; };

static void pin(int cpu) {
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s);
    if (pthread_setaffinity_np(pthread_self(), sizeof s, &s) != 0) { std::perror("setaffinity"); std::exit(1); }
}

// Thread A: write 2k+1, spin until 2k+2.  Thread B: spin until 2k+1, write 2k+2.
// Each round trip is two cache-line transfers; one-way = round trip / 2.  Median of chunks, not mean.
static double one_way_ns(int a, int b, uint32_t iters) {
    Line* line = new Line;                     // fresh line per pair: no residual ownership
    std::atomic<bool> go{false};
    std::vector<double> chunks;                // 16 chunks → median kills the odd interrupt
    std::thread tb([&] {
        pin(b); while (!go.load(std::memory_order_acquire)) _mm_pause();
        for (uint32_t k = 0; k < iters; ++k) {
            while (line->v.load(std::memory_order_acquire) != 2 * k + 1) _mm_pause();
            line->v.store(2 * k + 2, std::memory_order_release);
        }
    });
    pin(a);
    go.store(true, std::memory_order_release);
    const uint32_t chunk = iters / 16;
    for (uint32_t c = 0; c < 16; ++c) {
        auto t0 = std::chrono::steady_clock::now();
        for (uint32_t k = c * chunk; k < (c + 1) * chunk; ++k) {
            line->v.store(2 * k + 1, std::memory_order_release);
            while (line->v.load(std::memory_order_acquire) != 2 * k + 2) _mm_pause();
        }
        auto t1 = std::chrono::steady_clock::now();
        chunks.push_back(std::chrono::duration<double, std::nano>(t1 - t0).count() / chunk / 2.0);
    }
    for (uint32_t k = 16 * chunk; k < iters; ++k) {           // drain the remainder so B exits
        line->v.store(2 * k + 1, std::memory_order_release);
        while (line->v.load(std::memory_order_acquire) != 2 * k + 2) _mm_pause();
    }
    tb.join(); delete line;
    std::nth_element(chunks.begin(), chunks.begin() + 8, chunks.end());
    return chunks[8];
}

static std::vector<int> parse_cpulist(std::string_view s) {         // "0-3,8,10-11"
    std::vector<int> out;
    while (!s.empty()) {
        size_t comma = s.find(','); std::string_view tok = s.substr(0, comma);
        int lo = 0, hi = 0; size_t dash = tok.find('-');
        std::from_chars(tok.data(), tok.data() + (dash == tok.npos ? tok.size() : dash), lo);
        if (dash == tok.npos) hi = lo; else std::from_chars(tok.data() + dash + 1, tok.data() + tok.size(), hi);
        for (int c = lo; c <= hi; ++c) out.push_back(c);
        s = comma == s.npos ? std::string_view{} : s.substr(comma + 1);
    }
    return out;
}

int main(int argc, char** argv) {
    const uint32_t iters = argc > 1 ? std::strtoul(argv[1], nullptr, 10) : 200000;
    std::vector<int> cpus;
    if (argc > 2) cpus = parse_cpulist(argv[2]);
    else { cpu_set_t s; sched_getaffinity(0, sizeof s, &s); for (int c = 0; c < CPU_SETSIZE; ++c) if (CPU_ISSET(c, &s)) cpus.push_back(c); }
    const size_t n = cpus.size();
    std::vector<double> m(n * n, 0.0);
    for (size_t i = 0; i < n; ++i)
        for (size_t j = i + 1; j < n; ++j) m[i * n + j] = m[j * n + i] = one_way_ns(cpus[i], cpus[j], iters);
    double lo = 1e9, hi = 0;
    for (size_t i = 0; i < n; ++i) for (size_t j = 0; j < n; ++j) if (i != j) { lo = std::min(lo, m[i*n+j]); hi = std::max(hi, m[i*n+j]); }
    std::printf("# one-way core-to-core latency, ns (median of 16 chunks of %u round trips); min %.1f max %.1f\n", iters / 16, lo, hi);
    std::printf("      "); for (size_t j = 0; j < n; ++j) std::printf("%6d", cpus[j]); std::printf("\n");
    for (size_t i = 0; i < n; ++i) {
        std::printf("%4d  ", cpus[i]);
        for (size_t j = 0; j < n; ++j) if (i == j) std::printf("     -"); else std::printf("%6.1f", m[i * n + j]);
        std::printf("   ");
        for (size_t j = 0; j < n; ++j) {                      // ASCII heat-map: 5 bins between min and max
            if (i == j) { std::putchar(' '); continue; }
            int bin = int(4.999 * (m[i * n + j] - lo) / std::max(hi - lo, 1e-9));
            std::putchar(" .:#@"[bin]);
        }
        std::printf("\n");
    }
    return 0;
}
```

**Measured — the author's KVM guest** (4 vCPUs on a Sapphire Rapids host; GCC 13.3; `./c2c 200000`):

```
# one-way core-to-core latency, ns (median of 16 chunks of 12500 round trips); min 61.6 max 65.1
           0     1     2     3
   0       -  62.9  61.7  63.3    . :
   1    62.9     -  63.2  65.1   . :@
   2    61.7  63.2     -  61.6    :  
   3    63.3  65.1  61.6     -   :@  
```

One block, ~62 ns everywhere: four vCPUs that the hypervisor scheduled onto one tile of one socket (and that is all the guest can learn — the heat-map is the *only* topology information a VM reliably gives you, since the virtual `lscpu` is whatever the hypervisor claims). The 3.5 ns spread is real mesh distance plus hypervisor noise; the `@` is relative to a 3.5 ns range and means nothing — always read the `min`/`max` line before the picture. The 62 ns figure itself is consistent with published Sapphire Rapids same-tile numbers (~55–65 ns), which is the sanity check to make before trusting a run.

**Representative — the reference and comparison machines** (8 of the cores shown; full matrices in the chapter's data files):

```
   Xeon Gold 6326, one socket, SMT siblings excluded (cores 0-7 of 16)        min 44.8  max 63.1
          0     1     2     3     4     5     6     7
   0      -  46.2  49.9  53.8  51.1  57.0  60.3  63.1     .:##@@         a gradient, not blocks:
   1   46.2     -  45.9  50.4  48.7  52.9  56.8  60.0    . .:##@         mesh distance to the slice/CHA
   2   49.9  45.9     -  44.8  52.0  49.5  53.2  57.4    .. .:#@         that homes the line
   ...                                                                  cross-socket pairs: 112–138 ns

   EPYC 9354-class (Zen 4, 4 CCDs × 8 cores, NPS1), cores 0-3 of CCD0 and 8-11 of CCD1   min 18.9  max 91.4
          0     1     2     3     8     9    10    11
   0      -  19.1  18.9  19.4  86.2  85.7  88.0  87.1        ####
   1   19.1     -  19.2  19.0  85.9  86.4  87.5  86.8        ####      same CCD: ~19 ns
   2   18.9  19.2     -  19.3  87.0  86.1  86.6  88.2        ####      cross-CCD: ~87 ns — 4.6×
   3   19.4  19.0  19.3     -  86.5  87.3  86.9  91.4        ####
   8   86.2  85.9  87.0  86.5     -  19.0  19.5  19.2    ####
   ...
```

**What the map predicts, and a check that it does.** The Ch. 39 SPSC queue (one line for the slot, `head`/`tail` on separate lines) moves one line per message in steady state plus the index line when the consumer catches up; its measured per-message handoff latency, same binary, same box, is **~44 ns in-CCD vs ~190 ns cross-CCD** on the Zen 4 part (representative), and ~110 ns on the 6326 for a far-mesh pair vs ~95 ns for a near one — within 10 % of "2 × one-way from the map." Three such handoffs in a feed → book → strategy → gateway chain is the ~250 ns of §18.1. The map is not a curiosity; it is the per-hop cost table Ch. 43's topology model needs.

## 18.4 Techniques

### 18.4.1 Place the pipeline inside one L3 domain — and choose which domain

The rule that follows from §18.2.4: **every pair of threads that hand each other cache lines on the hot path lives in one diagonal block.** For a Ch. 90 process-per-role topology that means the feed handler, book, strategy and gateway threads all sit in *one* CCD / tile / SNC node, and the shared-memory rings between them (Ch. 59) are allocated on that node's memory. The block is the budget: a Zen CCD gives you 8 cores (7 after you leave one for the domain's housekeeping — never core 0 of the socket, Ch. 50), an SPR tile ~15, an Ice Lake socket 16.

Read the domains from the kernel, not from the part number:

```
   # L3 domains: which CPUs share cache index 3 with CPU N (works in NPS1/uniform mode too)
   for c in /sys/devices/system/cpu/cpu[0-9]*; do echo "$(basename $c): $(cat $c/cache/index3/shared_cpu_list)"; done | sort -u -t: -k2
   # on a 4-CCD EPYC, SMT on:   cpu0: 0-7,64-71   cpu8: 8-15,72-79  ...  (CCD = 8 cores + their siblings)
   # NUMA nodes as firmware reports them (NPS/SNC mode, or one per CCD with "L3 as NUMA"):
   numactl --hardware | sed -n '1,/node distances/p'
   lstopo --of txt --no-io                   # draws L3 boxes around the cores that share them
   # the device's home: the NIC's nearest node and the CPUs it considers local
   cat /sys/class/net/eth0/device/numa_node /sys/class/net/eth0/device/local_cpulist
```

Which block? **The one nearest the NIC** (§18.4.3) for the ingress-bound chain; on AMD, the CCD whose GMI link lands on the IOD quadrant that owns the NIC's PCIe root (NPS4 makes this a NUMA node and `numa_node` tells you); on Intel SNC, the node whose I/O tile carries the slot. If the strategy's book does not fit the domain's L3 (32 MiB on a Zen CCD; a 96 MiB V-Cache CCD exists for exactly this), **shrink the book** (Ch. 29's hot/cold split) before you span domains; a domain crossing costs more than an L3 miss to local DRAM on most parts (§18.2.3 vs Ch. 8's ~90 ns), and the latter at least prefetches.

Encode the result: the pinning map is a *topology-derived artifact* checked into the deployment (Appendix C.6 configuration-as-code), and the pre-open gate (Appendix M.2) re-runs a 60-second `c2c` on the hot cores and compares its block structure to the recorded one — a firmware update that silently reset NPS1→NPS4, or a swapped board with the NIC in another slot, fails the gate rather than the open.

### 18.4.2 SNC / NPS: when to split the socket, and what it costs

Splitting the socket into nodes trades **lower local latency and less L3/memory-controller contention** against **more software work and a hard cliff** for anything that gets it wrong:

```
   mode                    what improves                              what you must now do
   ──────────────────────  ─────────────────────────────────────────  ──────────────────────────────────────────────────
   NPS1 / SNC off          nothing to place; DRAM interleaved        nothing — but hot data is 3/4 remote on average
   (default)               across all channels (max bandwidth)        and the heat-map is the ONLY view of the CCDs/tiles
   NPS2 / SNC2             local DRAM ~10–15 % lower latency;         bind memory per node (Ch. 17 §17.4.1); split the NIC's
                           half the socket's contention               rings and the consumers per node
   NPS4 / SNC3             ~15–20 % lower local DRAM latency;         every process bound to a node; 1/4 of the bandwidth
                           the NIC's quadrant is a node               per node (fine for the hot box — Ch. 7 §7.2.2)
   "L3 as NUMA" (AMD)      numactl/libnuma see CCDs; the kernel's     8–12 nodes; the scheduler's NUMA balancing becomes
                           NUMA-aware allocators place per CCD        noise (disable it: numa_balancing=0 — Appendix C.2)
```

For the **hot box**, NPS4/SNC3 (or SNC2 on Ice Lake) with every hot process `numactl --cpunodebind=N --membind=N` is the right default: the hot path's bandwidth need is tiny, its latency need is total, and the node boundaries now *enforce* §18.4.1's placement rule instead of hiding it. For a **capture or replay box** (Ch. 95–96) that streams at 100 Gb/s, NPS1's interleaving wins. On AMD add "L3 as NUMA" whenever the node count is not already one-per-CCD, and set `kernel.numa_balancing=0` — automatic NUMA balancing migrates pages toward the CPU that touches them, which on a pinned hot box is a stall (the page's PTE is unmapped to detect the access — Ch. 26) that fixes nothing.

Measure the mode decision, do not reason it: Ch. 17 §17.3's `numactl`-bound pointer chase per node, before and after the switch, plus the `c2c` map (which does not change — the silicon is the same — but whose block boundaries now coincide with the node boundaries, which is the point).

### 18.4.3 IRQs, NIC queues and the device's domain

A NIC is attached to **one** PCIe root, on one IOD quadrant or one I/O tile; its DMA writes land in that domain's memory controller and — with DDIO (Ch. 66) — in the L3 slices of that domain (on Intel; AMD has no DDIO equivalent, so RX data lands in DRAM and the first touch is a miss whichever CCD reads it). The consumer thread, the RX queue's interrupt (if it still has one — Ch. 50 §50.4.2) and the ring buffers all belong in the device's domain:

```
   DEV=$(readlink -f /sys/class/net/eth0/device)
   cat $DEV/numa_node $DEV/local_cpulist                      # the home node; the CPUs the kernel calls local
   # RSS/flow steering (Ch. 67): the market-data queue's IRQ → a housekeeping core IN that domain,
   # and the queue's consumer → a hot core in the same block of the map
   echo $(( 1 << 9 )) | xargs printf '%x\n' > /proc/irq/$(grep eth0-rx-2 /proc/interrupts | cut -d: -f1)/smp_affinity
   # busy-poll / bypass (Ch. 72): no IRQ, but the polling thread still belongs in the device's domain
```

The domain's DRAM is where the NIC's descriptor rings and buffers must be allocated (`numa_node`-aware `mmap` + first-touch from a thread on that node — Ch. 17 §17.2.2; DPDK's `--socket-mem` per node — Ch. 72). A ring in the wrong domain costs the NIC a cross-fabric DMA *and* the consumer a cross-fabric read. On a two-NIC hot box (market data in, orders out — Ch. 7 §7.4.1), put both in slots homed on the **same** quadrant/tile so the feed → gateway chain never crosses; if the board makes that impossible, the order-entry NIC's domain is the one to cross *from*, because the egress hop is one line (the encoded order) while ingress is a stream.

### 18.4.4 `resctrl` domains, cross-domain traffic and the single-writer rule

Three further consequences of "a socket is a cluster":

- **Cache allocation is per L3 instance.** Ch. 52's CAT masks are programmed per `resctrl` domain = per L3; on a 4-CCD part there are four domains and a hot process pinned to CCD 0 is protected only by CCD 0's mask — the capture writer on CCD 3 cannot evict it anyway, and the mask you give CCD 3 is irrelevant to it. Plan capacity per domain: 32 MiB per CCD minus the housekeeping share, not "the socket's 128 MiB."
- **Cross-domain traffic as a last resort, batched and one-way.** Where a hop *must* cross (a risk aggregator that sees every strategy's fills, a telemetry collector — Ch. 87), make it a single-writer, latest-value or batched publication (Ch. 40's seqlock, Ch. 44's conflation) so each crossing moves one line per *batch*, never a request/response pair. A cross-CCD futex wake (Ch. 46) is the worst case: a line transfer plus an IPI across the fabric.
- **Memory is per domain too.** Ch. 17's first-touch rule now applies at the CCD/tile level under NPS4/SNC: a ring allocated by the gateway thread in node 2 and consumed by a book thread in node 0 is "remote" within one socket. Allocate from the consumer's node and verify with `numastat -p <pid>` (per-node pages per process).

### 18.4.5 Reading the map for the second axis: SMT and hybrid blocks

The map also shows the two topology facts earlier chapters handle one at a time. **SMT siblings** appear as the cheapest off-diagonal entries (~10 ns): useful for confirming `thread_siblings_list` and for the Ch. 51 decision (a sibling that *shares* L1/L2 with the hot thread hands lines over for free — and competes for everything else). On **hybrid client parts** (Ch. 7 §7.2.4) the E-core four-clusters appear as small tight blocks (shared L2) and P↔E pairs at ring latency; the hot pipeline goes on the P-core block, and a housekeeping thread that talks to it (the Ch. 85 logger's drain) sits on the E-cluster nearest it in the map — "nearest" being an empirical fact of the ring stop order that no datasheet publishes.

## 18.5 Pitfalls & anti-patterns: the topology you cannot see

- **`numactl --hardware` says one node, so there is no NUMA.** In NPS1/SNC-off mode a 96-core EPYC is "one node" and the twelve CCD boundaries — each a 4–5× handoff cliff — are invisible to `numactl`, `hwloc`'s NUMA view and the scheduler. Read `cache/index3/shared_cpu_list` and run the map.
- **Adjacent CPU numbers are adjacent cores.** Linux enumerates SMT siblings as `N` and `N+cores`, and on AMD with SMT off CPUs 0–7 are CCD 0 — but with "L3 as NUMA" or on some firmware the enumeration interleaves CCDs. Pinning "producer on 4, consumer on 5" is a guess; the map is a measurement.
- **The V-Cache CCD is the fast one.** On a 7950X3D/9950X3D the stacked-cache CCD runs a few hundred MHz *slower*; the core-bound strategy belongs on the plain CCD, the book on the V-Cache one — and a strategy that reads the book every message now crosses the IOD. Decide which of the two matters more with Ch. 7's clock-dependence table, or buy the single-CCD X3D part.
- **Measuring the map with turbo on.** The one-way latency has a core-clock component (the spin loop, the L2 lookup) and a fabric component; with turbo on, a pair that happens to boost higher looks "closer." Run it under Ch. 7 §7.4.2's pinned ratio or Ch. 6's turbo-off, and say which.
- **Forgetting the adjacent-line prefetcher.** Intel's spatial prefetcher pairs 64-byte lines into 128-byte chunks; two "separately aligned" hot fields 64 bytes apart can still travel together (Ch. 38 §38.2). Across CCDs that is a 90 ns false-sharing event per message. Pad to 128 where the two writers are in different domains.
- **Cross-domain by memory placement.** Threads correctly in one CCD, the ring they share faulted in by a setup thread on another node (Ch. 17 §17.2.2's first-touch bug, now at CCD granularity under NPS4). `numastat -p` shows it; allocate from the consumer.
- **Letting migration undo it.** A thread that is *not* pinned drifts; on a chiplet part a single migration across CCDs is a cold L3 *and* every subsequent handoff at 4× — a bimodal latency distribution that looks like a software bug. Pin (Ch. 50) and alarm on `cpu-migrations` (Ch. 100).
- **Treating the IOD hop as free for the NIC.** A NIC on quadrant 3 feeding a consumer on CCD 0 pays the fabric on *every packet's DMA* and again on the read. Check `numa_node` before choosing the hot CCD, not after.
- **Crossing domains with a request/response.** A synchronous query to a service on another CCD is two crossings (~180 ns) *plus* the service's own latency; restructure as a one-way publish (§18.4.4) or move the service into the block.

## 18.6 Exercises & checklist

**Exercises**

1. Run `c2c` on your development machine with SMT on. Identify the SMT-sibling pairs from the map alone, then confirm with `thread_siblings_list`. How much cheaper is a sibling handoff than a same-L3 one on your part?
2. On a chiplet or multi-tile part (or a cloud instance that gives you ≥ 16 vCPUs), run the map and compare its block structure with `cache/index3/shared_cpu_list`. Do the blocks match the L3 domains exactly? If the instance is a VM, are the vCPUs you were given all in one domain?
3. Build Ch. 39's SPSC queue and measure its per-message handoff latency for the cheapest and the most expensive pair in your map. Fit *handoff ≈ a + b × one-way*; what are *a* and *b*? Repeat for Ch. 42's Disruptor with a three-stage chain placed within one block and across blocks.
4. If you can change firmware: switch NPS1 → NPS4 (or SNC off → on), re-run Ch. 17 §17.3's node-bound pointer chase and the map. Which numbers changed, which did not, and what does `numactl --hardware` now show that it hid before?
5. Write the pre-open check: a script that runs `c2c` on the hot cores for 60 s, bins the result into blocks, and diffs the block structure (not the raw ns) against a recorded baseline. Make it fail on a changed structure and merely warn on a ±15 % level shift.

**Checklist**

- [ ] The core-to-core map has been measured on this exact box and firmware mode, with the clock pinned, and is stored with the deployment; the pre-open gate re-runs it on the hot cores.
- [ ] Every hot-path handoff pair (queue producer/consumer, pipeline stage N → N+1, risk gate caller/callee) is inside one diagonal block (one CCD / tile / SNC node).
- [ ] The hot block is the NIC's home domain (`numa_node`, `local_cpulist`); both NICs are homed on the same quadrant/tile where the board allows.
- [ ] Rings, books and per-thread arenas are first-touched from the consuming node; `numastat -p` confirms zero remote pages for the hot processes.
- [ ] NPS/SNC mode is a recorded decision (NPS4/SNC on for the hot box; NPS1 for streaming boxes), `numa_balancing=0`, and "L3 as NUMA" is on where the node count would otherwise hide the CCDs.
- [ ] `resctrl` masks are planned per L3 instance, not per socket (Ch. 52).
- [ ] Cross-domain traffic that cannot be avoided is one-way and batched (seqlock snapshot, conflated publish), never request/response, never a cross-fabric futex wake.
- [ ] Hot fields written from different domains are padded to 128 bytes on Intel (adjacent-line prefetcher).
- [ ] Nothing hot is unpinned; `cpu-migrations` on the hot threads is alarmed at > 0.

## 18.7 References

- AMD, *4th Gen AMD EPYC™ Processor Architecture* (white paper) and *AMD EPYC™ 9004 / 9005 Series Tuning Guides* (BIOS & workload tuning: NPS modes, "ACPI SRAT L3 Cache as NUMA Domain", Determinism/Power modes, Infinity Fabric settings); *Processor Programming Reference for Family 19h / 1Ah* (CCD/CCX topology registers, L3 behaviour).
- Intel, *Intel® Xeon® Processor Scalable Family Technical Overview* (mesh, CHA, SNC) and the *4th/5th Gen Intel® Xeon® Scalable Processors* and *Intel® Xeon® 6* architecture documents (tiles, EMIB, SNC2/SNC3/HEX); *Intel® 64 and IA-32 Architectures Optimization Reference Manual*, the server-uncore chapters.
- Linux kernel documentation: `Documentation/admin-guide/mm/numa_memory_policy.rst`, `Documentation/arch/x86/topology.rst`, `Documentation/sysctl/kernel.rst` (`numa_balancing`), `Documentation/arch/x86/resctrl.rst` (domains = L3 instances); the `sysfs` cache topology (`Documentation/ABI/stable/sysfs-devices-system-cpu`).
- Erik Rigtorp, `c2clat` (github.com/rigtorp/c2clat), and Nicolas Viennot, `core-to-core-latency` (github.com/nviennot/core-to-core-latency) — the two widely used ping-pong harnesses and their published heat-maps for dozens of parts; this chapter's listing follows the same protocol.
- Chips and Cheese, the core-to-core latency sections of their Zen 3/4/5, Sapphire Rapids, Emerald Rapids and Granite Rapids articles — independently measured matrices to compare your map against.
- Ulrich Drepper, *What Every Programmer Should Know About Memory* (2007), §4–§5 — the coherence-transaction model the one-way cost decomposes into.

## 18.8 Additional Reading

- Ch. 17 (*NUMA Architecture*) — the node-level rules this chapter applies within a socket; Ch. 8 (*The Memory Hierarchy & Caches*) — MESI, the L3 and the prefetchers; Ch. 38 (*False Sharing*) — HITM and `perf c2c`, the tools that see the transfers this chapter counts.
- Ch. 43 (*Threading Topologies & the Hot Loop*) — the hop-cost model that consumes this chapter's numbers; Ch. 50 §50.4.3 — pinning with the map; Ch. 52 — `resctrl` per L3 instance.
- Appendix Q (*AMD Zen Companion*) — the PMU events (`amd_l3`, `amd_df`) that count cross-CCD traffic, and the BIOS knob names; Appendix R — per-generation CCD/tile counts and L3 sizes; Appendix L §L.7 — the harness as part of the standing measurement kit.
- Martin Thompson, *Mechanical Sympathy* blog, "Memory Barriers/Fences" and "Inter Thread Latency" — the original framing of a cache-line handoff as the unit of inter-thread cost.

---

*Next: Ch. 19 — Timekeeping: TSC, rdtsc & Clock Sources, where we close Part II by fixing the instrument we have been using all along — this chapter's harness included: how to read nanoseconds reliably with the invariant TSC and `rdtscp`, calibrate ticks to time, know which clocksource the kernel trusts and why, and timestamp a message on the way in without a syscall.*
