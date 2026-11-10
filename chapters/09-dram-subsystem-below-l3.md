# Part II — CPU Microarchitecture

# Chapter 9 — The DRAM Subsystem Below L3

> **Prerequisites:** Ch. 8 (the hierarchy above DRAM — this chapter opens the "~80 ns" box at the bottom of §8.2.1's diagram), Ch. 2 (offcore/uncore counters), Ch. 3 and Appendix L (the measurement discipline; the loaded-latency sweep is a Ch. 3-style experiment), Ch. 1 (you read distributions — DRAM latency is one).
>
> **Leads into:** Ch. 12 (software prefetch — memory-level parallelism is how you hide what this chapter cannot remove), Ch. 17 (the TLB — the other half of a cold access), Ch. 18–19 (NUMA and the in-socket topology — *which* memory controller you hit; CXL expanders are CPU-less NUMA nodes, Ch. 18 §18.2.4), Ch. 58 (MBA — the knob that caps a bandwidth-hungry neighbour), Ch. 107–108 (the capture writer that is that neighbour), Appendix E (the DRAM rows). The Ch. 8 cliff is not one number; this chapter is why.

---

## 9.1 Why it matters: "~80 ns" is a distribution, and your neighbours move it

Ch. 8 stops at the L3 miss: DRAM is "~80 ns local, ~130 ns remote", one box at the bottom of the diagram. For most of the book that is enough — the lesson is *don't miss*. But the order book that does not fit in L2 (Ch. 33 §33.3 measured a deep book at L3 sizes), the per-symbol state of a strategy that covers a thousand names, the reference-data lookup on a rarely-traded instrument: these miss, on the hot path, on purpose, because the alternative is a smaller universe. And for those misses the "~80 ns" is wrong in two ways that land in the tail.

First, it is **bimodal**. A DRAM access that finds its row already open in the bank's row buffer costs roughly the column access (tCL ≈ 14 ns at DDR4-3200) plus the fabric; one that finds a *different* row open pays a precharge and an activate first (tRP + tRCD ≈ 28 ns more). The cache-miss counters do not distinguish the two — `mem_load_retired.l3_miss` counts both — but the latency does, and a hot access pattern that happens to alternate rows in one bank pays the slow mode every time.

Second, it is **loaded**. Latency is quoted for an idle memory controller. The capture writer streaming 2 GB/s of packets to a journal (Ch. 107), the risk process scanning positions, the replay job on the "spare" cores: each adds queueing at the controller, and queueing latency is the convex curve of Ch. 1 §1.2.1's Little's law — flat until the channel is ~60–70 % busy, then a knee, then a wall. The strategy's cache miss on the hot core, which touched nothing shared, goes from 85 ns to 150+ ns during the open **with no counter on the hot core changing** — the misses are the same, each one just takes longer. That is the hidden neighbour this chapter names: not a thread on your core (Ch. 57), not a line evicted from your L3 (Ch. 58), but *traffic on your memory channel*.

This chapter opens the box: the topology below the L3 (§9.2.1), the row buffer and the bimodal miss (§9.2.2), loaded latency and where the knee is (§9.2.3), refresh and ECC as periodic tail sources (§9.2.4), and the memory you may be offered instead of DDR — HBM and CXL (§9.2.5). §9.3 measures the loaded curve and the per-access distribution; §9.4 is what you can do about it: isolate the channel from the streamers, lay hot data out for row locality, configure the DIMMs for latency, and keep CPU-less memory off the hot path.

## 9.2 Mental model

### 9.2.1 Below the L3: channels, DIMMs, ranks, bank groups, banks, rows

A load that misses the L3 leaves the core's world. On the reference Ice Lake-SP it goes: core → L3 slice (CHA, the caching/home agent that tracks the line — Ch. 18 §18.2.2) → the **integrated memory controller** (iMC) owning the address → one of the socket's **8 DDR4-3200 channels** → the DIMM(s) on that channel → a **rank** (one side's set of chips that answer together) → a **bank group** and **bank** inside each chip → a **row** (DRAM page, typically 8 KiB across the rank) → a **column** (the 64-byte burst). The address bits select each level, and the controller's **interleave** spreads consecutive lines across channels (commonly at 256 B or 4 KiB granularity) so that a sequential stream uses all eight channels at once.

```
   core ─ L1 ─ L2 ─ L3 slice/CHA ─ iMC ─┬─ ch0 ─ DIMM ─ rank 0/1 ─ 4 bank groups × 4 banks ─ rows (8 KiB) ─ columns (64 B bursts)
                                       ├─ ch1 …                        │
                                       ├─ …                      row buffer: ONE open row per bank
                                       └─ ch7 …                  (the "page" the bank can serve without an activate)
   Ice Lake-SP (Xeon Gold 6326): 8 channels/socket, DDR4-3200, 25.6 GB/s per channel, 204.8 GB/s per socket (peak, never reached)
   DDR5 (Sapphire Rapids+): 8 channels × 2 sub-channels of 32 bits, 8 bank groups, on-die ECC, same-bank refresh
```

Three structural facts matter for latency (the figures above are the reference machine's — Xeon Gold 6326, Appendix L.3):

- **Parallelism is per bank, per channel.** Two outstanding misses to different banks (or channels) proceed concurrently; two to the *same* bank serialize. The core's ~12 line-fill buffers (Ch. 12) are only useful if the addresses spread across banks — which the interleave does for sequential and random streams alike, but a stride that happens to map to one bank defeats (the DRAM analogue of Ch. 8 §8.5's set-conflict miss).
- **The row buffer is a one-entry cache per bank.** Each bank can hold one open row; serving a column from the open row is a *row hit*; serving a different row requires *precharge* (close) and *activate* (open) first. This is §9.2.2.
- **Everything shares the channel.** The hot core's miss, the capture writer's streaming stores, the kernel's page zeroing and the DMA engine of the NIC (Ch. 76: DDIO writes that miss the L3 allocation go to DRAM) queue at the same controller. There is no per-core memory channel; there is only per-*node* (Ch. 18) — which is the lever §9.4.1 pulls.

### 9.2.2 The row buffer: hit, miss, conflict — why the miss latency is bimodal

The timing parameters on the DIMM label (DDR4-3200 **CL22-22-22**) are the three costs: **tCL** (column access, ~13.75 ns), **tRCD** (row-to-column delay — the activate, ~13.75 ns) and **tRP** (row precharge, ~13.75 ns). A request to a bank sees one of three states:

| Bank state on arrival | DRAM-side cost | Total with the fabric (~55 ns on Ice Lake-SP) |
|---|---|---|
| **Row hit** — the row is already open | tCL ≈ 14 ns | **~70 ns** |
| **Row miss** — the bank is precharged (closed) | tRCD + tCL ≈ 28 ns | **~85 ns** |
| **Row conflict** — a *different* row is open | tRP + tRCD + tCL ≈ 41 ns | **~100 ns** |
| **+ refresh in progress on that bank** (§9.2.4) | + up to tRFC ≈ 350–550 ns | **~450–600 ns** |

The controller's **page policy** decides which of the first three you see most. *Open-page* keeps a row open after an access, betting on locality (a second access to the same 8 KiB row within a few hundred ns is a hit); *closed-page* (or adaptive) precharges immediately, betting on randomness (every access is a miss, never a conflict). Server firmware defaults to adaptive; what matters to you is that **random access across a large structure is dominated by misses and conflicts, sequential or row-local access by hits**, and the difference is ~30 % of the whole miss — the two modes §9.3's histogram shows. The cache counters cannot see this: a row conflict is just an L3 miss that took 30 ns longer. The only way to know is the distribution of miss latencies (§9.3) or the uncore's per-channel `ACT`/`PRE`/`CAS` counters (`perf stat -e uncore_imc/cas_count_read/`, `uncore_imc/act_count/`: a high ACT:CAS ratio means row misses are the rule).

### 9.2.3 Loaded latency: Little's law at the memory controller

An idle controller answers a miss in the row-hit/row-miss time above. A busy one makes the miss *wait*. The controller is a queue; its service rate is the channel bandwidth (25.6 GB/s per channel; ~75–85 % of that achievable); its arrival rate is everything the socket throws at it. Little's law (Ch. 1 §1.2.1) says the queue's mean residence time grows as 1/(1 − utilisation): nothing happens until ~60 % utilisation, latency doubles by ~85 %, and at saturation the controller's reorder queues fill and **every** miss pays hundreds of ns.

Intel's *Memory Latency Checker* (MLC) measures exactly this curve — the `--loaded_latency` mode runs a dependent-load chaser on one core while the other cores inject a controlled bandwidth, and prints latency vs achieved bandwidth. The representative shape for a single-socket 8-channel DDR4-3200 Ice Lake-SP (§9.3) is: **~85 ns at idle, ~90 ns at 50 GB/s, ~110 ns at 100 GB/s, the knee at ~130–140 GB/s, 200+ ns past 160 GB/s.** Two consequences for a trading box:

- **A streaming neighbour on your socket is a latency tax on your misses**, not just on its own throughput. A journal writer doing 4 GB/s is harmless; a replay or a `memcpy`-heavy risk job doing 60 GB/s per core-pair is halfway to the knee; a GPU staging buffer being filled at 100 GB/s is over it. The misses on the hot core do not change in number — their *latency* does. (The measured version, on the test VM, is §9.3.)
- **The hot core's own MLP works against it under load.** Ch. 12's prefetch distance was tuned to keep ~10 misses in flight; each of those is an entry in the controller's queue. Under load the "plateau" of Ch. 12 §12.3 rises with everything else.

### 9.2.4 Refresh, ECC and patrol scrub: the periodic tail sources

DRAM cells leak; every row must be **refreshed** within tREFI (7.8 µs at normal temperature, 3.9 µs above 85 °C). The controller issues a refresh command to a rank (DDR4: all banks; DDR5: per-bank or same-bank refresh is available) and that rank is unavailable for **tRFC** — ~350 ns for 8 Gb DDR4 chips, ~550 ns for 16 Gb, ~295 ns (all-bank) / ~130 ns (same-bank) for 16 Gb DDR5. A miss that lands on a refreshing rank waits the remainder. Averaged, refresh steals ~5–7 % of the channel's time and adds ~1–2 ns to the mean; **in the tail it adds a ~0.5 µs mode at a 1-in-20-to-1-in-50 rate per rank**, which is exactly the shape a p99.9 picks up and a mean hides. DDR5's fine-granularity refresh (FGR, tREFI/2 or /4 with shorter tRFC) trades more frequent, shorter stalls for the same total — a knob worth checking in the BIOS for a latency box.

**ECC** adds no latency on the hit path (the check is in the controller's pipeline) but a *correctable error* costs a few hundred ns of correction and — the real hazard — a **machine-check / CMCI interrupt** storm if a DIMM is failing (Ch. 112 counts them). **Patrol scrub** walks all of memory in the background reading and re-writing every line (Intel: once per 24 h by default, configurable; AMD: similar) — a low, constant bandwidth consumer that also opens rows under you; disable it during the session or set its interval so the sweep runs overnight (Appendix C.1). **DDR5 on-die ECC** is a different thing: it corrects single-bit errors *inside* the chip, invisibly, and does not replace sideband (controller) ECC; its correction cycles are one more source of a few ns of variance you cannot turn off.

### 9.2.5 HBM, CXL and memory you did not buy for latency

Two kinds of non-DDR memory now appear in server catalogues, and both are *bandwidth* or *capacity* products, not latency ones:

- **HBM** (Xeon Max, GPUs): stacked DRAM on the package with ~1 TB/s of bandwidth — and *higher* idle latency than DDR (the stacks sit behind a wide, slow-clocked interface; ~100–130 ns on Xeon Max in HBM-only mode). It is for the batch tier (Ch. 88–89), never for the order book.
- **CXL memory expanders** (CXL 2.0 Type 3 devices): DRAM behind a PCIe-shaped link, presented by the kernel as a **NUMA node with no CPUs** at ~170–250 ns from the nearest socket — roughly a NUMA-remote hop and a half. Linux's **memory tiering** (`/sys/devices/virtual/memory_tiering/`, kernel 6.1+) will *demote* cold pages to it and *promote* hot ones back under `numa_balancing=2`, which is a page-migration noise source that can undo Ch. 62's warm-up for the page it picked. Ch. 18 §18.2.4 is the NUMA-side treatment: how to see such a node (`numactl -H` distances, `node/*/access0/initiators`), and the rule — `--membind` the hot process to the socket's own nodes, `numa_balancing=0`, and never let a hot page be *eligible* for demotion.

The model: **below the L3 is a queue in front of a bank-parallel, row-buffered memory whose access time is bimodal (row hit vs miss/conflict), whose tail carries refresh stalls, and whose latency rises with everyone's bandwidth, not just yours. The hot core cannot see any of this in its own counters — it sees "L3 miss" — so you measure the distribution and the loaded curve, isolate the channel from the streamers, and lay hot data out for row hits.**

## 9.3 Measure it: the per-miss distribution and the loaded-latency curve

*Toolchain and machine: Appendix L.3; this section's live run is on the 4-vCPU KVM guest the batch-B and -C chapters also used (a 2.7 GHz Xeon host, nested paging, kernel 6.18, no uncore access), which makes the absolute numbers a guest's — the shapes are the lesson, and the reference-machine table below is representative pending the bare-metal run.*

The instrument is Ch. 8 §8.3's pointer chase, used twice: once per hop with `rdtscp` around each dependent load (the *distribution*), and once as a mean while `k` other cores stream through memory (the *loaded* curve). The chain is a random cyclic permutation of a 512 MiB region, THP-backed so the TLB component is mostly absent (Ch. 17); the streamers write 256 MiB each with plain stores in a loop — the capture-writer shape (Ch. 107), no locality, maximal channel use:

```cpp
// dram_probe.cpp — what sits below L3: unloaded vs loaded dependent-load latency, and the per-access distribution.
// Build: g++ -O2 -std=c++20 -march=native -pthread dram_probe.cpp -o dram_probe
// Run:   ./dram_probe <chaser-cpu> <neighbour-cpus comma-separated, may be empty>   e.g. ./dram_probe 3 0,1,2
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <random>
#include <sched.h>
#include <sys/mman.h>
#include <thread>
#include <vector>
#include <x86intrin.h>

static void pin(int cpu) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s); sched_setaffinity(0, sizeof s, &s); }
static void* big(std::size_t bytes) {               // THP-backed so the TLB component is mostly absent (Ch. 17)
    void* p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    madvise(p, bytes, MADV_HUGEPAGE); std::memset(p, 0, bytes); return p;
}
static std::uint64_t* make_chain(std::size_t n, std::uint64_t seed) {   // random cyclic permutation over n lines (Ch. 8 §8.3)
    auto* lines = static_cast<std::uint64_t*>(big(n * 64));
    std::vector<std::uint32_t> order(n); std::iota(order.begin(), order.end(), 0u);
    std::shuffle(order.begin(), order.end(), std::mt19937(seed));
    for (std::size_t i = 0; i < n; ++i) lines[std::size_t(order[i]) * 8] = std::uint64_t(order[(i + 1) % n]) * 8;
    return lines;
}
static std::atomic<bool> go{false}, stop{false};
static std::atomic<std::uint64_t> neighbour_bytes{0};
static std::chrono::steady_clock::time_point t_go;
static void streamer(int cpu) {                      // "the capture writer": streams its own 256 MiB — pure bandwidth, no locality
    pin(cpu); constexpr std::size_t N = 256u << 20; auto* buf = static_cast<std::uint8_t*>(big(N));
    while (!go.load(std::memory_order_acquire)) _mm_pause();
    std::uint64_t total = 0;
    while (!stop.load(std::memory_order_relaxed)) { for (std::size_t i = 0; i < N; i += 64) *reinterpret_cast<volatile std::uint64_t*>(buf + i) = i; total += N; }
    neighbour_bytes.fetch_add(total);
}
int main(int argc, char** argv) {
    int chaser = argc > 1 ? std::atoi(argv[1]) : 0; std::vector<int> nb;
    if (argc > 2 && argv[2][0]) for (char* s = std::strtok(argv[2], ","); s; s = std::strtok(nullptr, ",")) nb.push_back(std::atoi(s));
    pin(chaser);
    constexpr std::size_t LINES = (512u << 20) / 64;     // 512 MiB chain: >> L3
    std::uint64_t* chain = make_chain(LINES, 7);
    std::vector<std::thread> th; for (int c : nb) th.emplace_back(streamer, c);
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    go.store(true, std::memory_order_release); t_go = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    std::uint64_t idx = 0; const std::size_t HOPS = 20'000'000;              // (1) the mean under load
    auto t0 = std::chrono::steady_clock::now();
    for (std::size_t i = 0; i < HOPS; ++i) idx = chain[idx];
    double ns = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - t0).count() / HOPS;
    std::vector<std::uint32_t> samples(1'000'000); unsigned aux;                // (2) the per-hop distribution
    for (auto& s : samples) { std::uint64_t a = __rdtscp(&aux); idx = chain[idx]; asm volatile("" : "+r"(idx)); s = std::uint32_t(__rdtscp(&aux) - a); }
    stop.store(true); double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_go).count();
    for (auto& t : th) t.join();
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double p) { return samples[std::size_t(p * (samples.size() - 1))]; };
    std::printf("chaser cpu %d, %zu neighbour(s): mean dependent load %.1f ns; neighbour traffic %.1f GB/s\n", chaser, nb.size(), ns, neighbour_bytes.load() / 1e9 / secs);
    std::printf("   per-hop TSC ticks (incl. ~%u-tick rdtscp pair): p1 %u  p50 %u  p90 %u  p99 %u  p99.9 %u\n", pct(0.0), pct(0.01), pct(0.5), pct(0.9), pct(0.99), pct(0.999));
    std::vector<std::uint32_t> hist(60, 0); for (auto s : samples) hist[std::min<std::size_t>(s / 20, 59)]++;
    std::printf("   histogram (bin = 20 ticks):"); for (std::size_t b = 0; b < hist.size(); ++b) if (hist[b] * 1000 >= samples.size()) std::printf(" [%zu:%u]", b * 20, hist[b]); std::printf("\n");
}
```

Measured on the guest (`g++ 13.3 -O2 -march=native`; the same binary under `clang++ 18.1` is within 2 % on every row; chaser on vCPU 3, streamers on vCPUs 0–2; ticks are 2.7 GHz TSC ticks, 0.37 ns each):

```
   neighbours   mean dep. load   neighbour traffic   p1     p50    p90    p99    p99.9      (ticks per hop, ~48 of which are the rdtscp pair)
   0            211.5 ns           0 GB/s           394    550    706   1062   1358
   1            216.2 ns          ~11 GB/s          406    570    760   1116   1402
   2            229.3 ns          ~22 GB/s          410    608    846   1208   1650
   3            247.8 ns          ~34 GB/s          436    638    906   1298   1750
   histogram, 0 neighbours (bin = 20 ticks, hops per bin):   [400:47810] [420:69941] [440:67946] [460:56219] [480:62058] [500:74947] [520:72489]
       [540:67501] [560:74323] [580:87591] [600:83905] [620:59501] [640:32232] [660:16472] … [1180+: 2885]
```

Three things to read off it, in Ch. 1 order:

- **The distribution is bimodal even with the machine to yourself.** The 0-neighbour histogram has a mode at ~420–460 ticks (≈155–170 ns) and a second at ~580–620 ticks (≈215–230 ns), ~60 ns apart — the row-hit/row-miss and row-conflict modes of §9.2.2 (on a guest with nested paging a residual second-level page walk is the other candidate for the slow mode; on bare metal with 1 GiB pages, the uncore `act_count`/`cas_count` ratio settles it). **No cache counter distinguishes the two modes**; the p50 and p90 do.
- **The neighbours moved the tail more than the mean.** Three streamers at ~34 GB/s aggregate — far below any knee a server controller would show, and a quarter of what three *bare-metal* cores would push — raised the mean 17 % (211 → 248 ns) but the p99 22 % and the p99.9 29 %. The chaser touched nothing the streamers touched; the only shared thing is the channel.
- **The guest cannot reach the knee.** Four vCPUs behind one host controller saturate nothing, so the curve here is the flat-ish left of the knee. On the reference machine the standard instrument is MLC, run from a housekeeping core while the hot core keeps its own measurement running:

```
   # Intel MLC: latency vs injected bandwidth, 8 channels DDR4-3200, single socket (representative — reference machine, Appendix L.3)
   $ mlc --loaded_latency -t60 -d0            # -d0: injectors run with no delay = max bandwidth, then increasing delays
   Inject   Latency(ns)   Bandwidth(MB/s)
   00000      205.3        158,900          <- saturated: every miss queues; p99.9 of the hot core's misses here is >400 ns
   00100      142.7        141,200          <- the knee: ~70 % of peak, latency 1.6x idle
   00300      101.2        103,500
   01000       88.6         52,100          <- a 50 GB/s neighbour costs the hot core ~4 ns per miss
   99999       85.1          4,300          <- idle: the Appendix E number
```

Run it with the capture writer, the risk scanner and the replay job *actually running* (not synthetic injectors) and you have the box's real position on the curve; the uncore's `uncore_imc/cas_count_read/,cas_count_write/` per channel (`perf stat -a -e ... -I 1000`) tells you who is generating the bandwidth and whether the channels are evenly loaded (an interleave or population mistake shows as one channel at 90 % and seven at 10 %).

## 9.4 Techniques

### 9.4.1 Own the channel: isolate the streamers from the hot socket's memory

The controller is shared by node; the fix is placement and throttling, in that order:

- **Put bandwidth consumers on another node — or another box.** The capture writer (Ch. 107–108), the replay/backtest job, the risk scanner's position sweep, the logger's file writer (Ch. 97) belong on housekeeping cores *whose memory is on a different node* (`numactl --membind` — Ch. 18 §18.4.1) on a two-socket box, or on a separate machine fed by the sequencer's stream (Ch. 109). On a single-socket box there is no second channel set, which is one of the arguments for a second socket *even if the hot path never uses it* (Ch. 7 §7.4.1), and for SNC/NPS modes that give the hot node its own channels (Ch. 19 §19.4.2) — with the capture writer bound to the *other* half.
- **Cap what you cannot move.** Intel RDT's **MBA** (Ch. 58 §58.4.2) throttles a class of service's memory bandwidth at the core's request queue; put every non-hot process in a CLOS with a 20–40 % MBA limit and measure the hot core's miss p99.9 before and after — MBA is coarse (steps of 10 %) and indirect (it delays requests, it does not reserve bandwidth), but it caps the neighbour below the knee. AMD's equivalent is the QoS bandwidth enforcement in `resctrl` on Zen 3+ (Appendix Q).
- **Make streamers stream politely.** Non-temporal stores (Ch. 12 §12.4.2) keep the writer out of the L3, *not* out of the channel — a 10 GB/s journal writer is 10 GB/s either way. Rate-limit the writer's drain to what the retention budget needs (Ch. 107 §107.4.3's batching makes it burstier, which is worse for the hot core's p99.9 — flatten it), and schedule bulk scans (reference-data reloads, replays) outside the session.
- **Watch the DMA writers too.** A NIC at line rate on a non-DDIO path, an NVMe writing the journal at 3 GB/s, a GPU staging buffer (Ch. 88): all are channel consumers the CPU counters do not show; the uncore `cas_count_write` does.

### 9.4.2 Row locality: lay hot data out so misses hit open rows

When the hot structure must live in DRAM, make its *misses* cheap:

- **Cluster what you touch together into the same DRAM pages.** A row is ~8 KiB of physically contiguous memory per rank (the exact span depends on the interleave); a book level's price, quantity, order-count and the first few resting orders laid out within 8 KiB (Ch. 33 §33.4.1's dense ladder does this by construction) turn the second and third misses of an update into row hits. The ordering is the Ch. 10 hot/cold split applied one level down: hot fields adjacent, cold fields elsewhere.
- **Walk in DRAM-page order when you must walk.** A sweep over a large table (end-of-day, a risk recomputation that runs intraday) should go in address order so the open-page policy serves hits, not row-major-then-column-major through a transposed layout that conflicts in every bank.
- **Huge pages help the TLB, not the row buffer** — but they make *physical* contiguity match *virtual* contiguity, which is what "lay it out within 8 KiB" needs; with 4 KiB pages two adjacent virtual pages can be anywhere in DRAM (Ch. 17 §17.4.1).
- **Exploit bank parallelism with MLP.** Ch. 12's prefetch distance hides latency by keeping ~10 misses in flight; those misses finish in parallel only if they hit different banks/channels. A random gather already spreads; a stride equal to the channel-interleave period does not (the DRAM-level conflict miss — test with the §9.3 chaser using a strided rather than random permutation).

### 9.4.3 Configure the memory for latency, not capacity

The DIMM population and the BIOS memory page decide the idle number and the knee, and are set once (Appendix C.1):

- **Populate every channel, one DIMM per channel (1DPC).** Eight channels at 1DPC is the full 204.8 GB/s and the highest supported speed; 2DPC often forces a lower data rate (DDR4-2933 or lower) and adds rank-switching turnarounds. Fewer populated channels means a lower knee *and* a worse interleave (a 6-channel population interleaves unevenly).
- **Match the ranks; prefer dual-rank DIMMs** at 1DPC: more banks per channel = more parallelism and fewer conflicts; single-rank DIMMs halve the bank count. Rank count and speed are on the DIMM label; `dmidecode -t 17` prints what the box actually has.
- **Interleave: channel interleaving on, node interleaving off** (Appendix C.1): spread each node's lines across its own channels; never interleave across sockets (that is the Ch. 18 §18.5 "50 % remote by design" trap at the memory-controller level).
- **Refresh and scrub**: enable DDR5 fine-granularity refresh / DDR4 "refresh rate ×1" at normal temperature (×2 above 85 °C — keep the DIMMs cool; the airflow profile is a latency setting), set patrol scrub to run outside the session (or off, if the box is rebooted daily and the risk is accepted), keep demand scrub on. Record the resulting `dmidecode`/`decode-dimms` output in the host fingerprint (Appendix M.2).
- **Power-down modes off**: DIMM self-refresh / CKE power-down on idle ranks adds an exit latency to the first access after an idle gap — the memory-side analogue of a C-state (Ch. 60). "Memory Power Savings = Disabled".

### 9.4.4 CPU-less memory: keep CXL and tiering off the hot path

If the box has a CXL expander (or a Xeon Max in flat HBM+DDR mode, or any node `numactl -H` lists with `cpus:` empty):

- **`--membind` the hot process to the socket's DDR nodes** (Ch. 18 §18.4.1) so no hot allocation can land on the far node; `mbind`/`set_mempolicy` with `MPOL_BIND` and the DDR node mask from inside the process for the segments it maps itself (Ch. 34).
- **`numa_balancing=0`** (Appendix C.3), which also disables the tiering promotion/demotion hooks (`numa_balancing=2` is the tiering mode) — a demotion is a page migration during the session, and a promotion is a hinting fault first (Ch. 18 §18.5).
- **Verify after warm-up**: `numastat -p <pid>` and `/proc/<pid>/numa_maps` must show zero pages on the CPU-less node; the Ch. 112 sampler watches `node/*/numastat` for `numa_foreign`/demotion counters moving during the session.
- **Use it for what it is**: the replay cache, the historical tick store (Ch. 107 §107.4.7), the risk job's working set — capacity-bound, latency-tolerant work that would otherwise compete for the hot node's DDR.

## 9.5 Pitfalls & anti-patterns: the miss that got slower with no counter changing

- **"The cache-miss rate is unchanged, so memory isn't the problem."** The headline trap. Under a streaming neighbour (or during a refresh storm from a hot DIMM) the *number* of misses on the hot core is identical and each takes 1.5–2× longer; `mem_load_retired.l3_miss` is flat and the p99.9 is up 50 %. The evidence is the per-miss latency distribution (§9.3's chaser, or `perf mem`/`perf stat -e cycle_activity.stalls_l3_miss` *per miss*) and the uncore channel counters, not the core's miss counts.
- **A streaming job "on the spare cores" of the hot socket.** Spare cores are not spare channels. The capture writer, the replay, the end-of-day sweep run intraday on cores 10–15 of the hot socket push the controller toward the knee for the hot core on core 2. Place by node, cap with MBA, or schedule out of session (§9.4.1).
- **Testing latency on an idle box.** Appendix L's quiet-box discipline is right for microbenchmarks and wrong for this chapter's question: the loaded curve must be measured *with the production neighbours running*, because that is where the hot core will live at 09:30. Keep both numbers — idle (the floor) and loaded (the truth).
- **Row-conflict-prone strides.** A stride that maps to one bank group (a power-of-two multiple of the interleave period) serializes misses at the DRAM, the way a power-of-two stride serializes them at a cache set (Ch. 8 §8.5). Symptom: a random access pattern is *faster* than a strided one over the same data. Pad the stride or randomize the walk order.
- **Node-interleaved memory, or an unbalanced population.** "Memory interleaving = all" in firmware, or a 6-of-8-channel population, makes half the lines remote or a quarter of the channels do half the work — a constant latency tax that no software fixes. Check `dmidecode -t 17`, `numactl -H` and the per-channel `cas_count` balance before anything else.
- **Pages that drifted to the CXL node.** A reference-data table allocated by a setup thread with the default policy, demoted overnight by tiering, promoted back on first touch during the open — a hinting fault plus a migration on the hot path. `--membind` + `numa_balancing=0` + `numastat -p` at the gate (§9.4.4).
- **Assuming the quoted "DDR4-3200" number is the latency.** 3200 MT/s is the *transfer rate*; the latency is tCL/tRCD/tRP at that clock (22 cycles × 0.625 ns ≈ 13.75 ns each), the fabric adds 50–60 ns, and the queue adds whatever the neighbours cost. Faster memory lowers the first term only; a DDR5-4800 CL40 DIMM has a *higher* tCL in ns (16.7 ns) than DDR4-3200 CL22 — bandwidth went up, latency did not go down.
- **Thermal refresh doubling.** A DIMM above 85 °C refreshes twice as often (tREFI 3.9 µs): refresh overhead doubles, the ~0.5 µs tail mode doubles in frequency, and nothing in the OS says so. The BMC's DIMM temperature sensors belong in the Ch. 112 telemetry.

## 9.6 Exercises & checklist

**Exercises**

1. **See the modes.** Build and run `dram_probe` with no neighbours, pinned, on your box with THP (or explicit 1 GiB pages — Ch. 17) backing the chain. Plot the histogram. How many modes, how far apart? Compare the gap to your DIMM's tRP + tRCD (from the label or `decode-dimms`). Then collect `uncore_imc/act_count/` and `cas_count_read/` for the run: what is the ACT:CAS ratio?
2. **Find the knee.** Run `mlc --loaded_latency` (or `dram_probe` with 1…N streamers on bare metal) and plot latency vs bandwidth. Where is the knee as a fraction of theoretical peak (channels × 25.6 GB/s)? Now run it with the production capture writer and risk process instead of synthetic streamers: where on the curve does the box sit at the open?
3. **Move the neighbour.** On a two-socket box, run the chaser on socket 0 and the streamers on socket 1 with `--membind=1`. Confirm the hot core's distribution returns to the idle one (§9.4.1). Then put the streamers back on socket 0 under an MBA class limited to 30 % (Ch. 58): how much of the tail did MBA recover?
4. **Row-local vs scattered layout.** Build a book-level structure whose hot fields fit in 8 KiB vs one whose fields are spread across three structures 64 KiB apart; run a random-update loop over 100k levels in DRAM and compare p50/p99 per update (§9.4.2). Which structure's misses hit open rows?
5. **Refresh in the tail.** From the §9.3 histogram, count hops beyond 2× the p50. Compare their rate to 1/(tREFI/tRFC) for your DIMMs (§9.2.4). Warm the DIMMs (a sustained bandwidth load on another core for 20 minutes) and repeat: did the rate double?

**Checklist — the DRAM subsystem**

- [ ] I measured the hot core's **per-miss latency distribution** (modes and p99.9), not only its miss *count*, both idle and **with the production neighbours running** (§9.3).
- [ ] The box's position on the **loaded-latency curve** at the open is known (MLC or the chaser + the real streamers) and is left of the knee (§9.2.3).
- [ ] **Bandwidth consumers** (capture, replay, risk sweeps, loggers, DMA writers) are placed on another **node or box**, capped with **MBA**, or scheduled out of session (§9.4.1); their channel use is visible in `cas_count_*` telemetry (Ch. 112).
- [ ] Hot DRAM-resident structures are laid out for **row hits** (hot fields within one DRAM page; walks in address order; no interleave-period strides) (§9.4.2).
- [ ] Memory is populated **1DPC, every channel, matched dual-rank**, channel-interleaved, node-interleave off, power-down off, refresh/scrub configured for the session, recorded in the host fingerprint (§9.4.3, Appendix C.1, M.2).
- [ ] **CPU-less nodes** (CXL/HBM-flat) are excluded from the hot process by `--membind`, `numa_balancing=0`, and verified with `numastat -p` at the gate (§9.4.4).
- [ ] I know that **DRAM latency did not improve with the DDR generation** — only bandwidth did — and I size the hot working set for L2/L3 first (Ch. 8, 33) and treat DRAM as the measured, loaded, bimodal thing it is.

## 9.7 References

- B. Jacob, S. Ng and D. Wang, *Memory Systems: Cache, DRAM, Disk* (Morgan Kaufmann, 2008) — the DRAM organisation, timing parameters, page policies and refresh of §9.2.1–§9.2.4 at textbook depth.
- JEDEC, *DDR4 SDRAM Standard* (JESD79-4) and *DDR5 SDRAM Standard* (JESD79-5) — tCL/tRCD/tRP/tRFC/tREFI definitions, bank-group architecture, fine-granularity and same-bank refresh, on-die ECC (§9.2.2, §9.2.4).
- Intel, *Memory Latency Checker (MLC)* user guide — `--loaded_latency`, `--idle_latency`, the injector model and how the loaded curve is produced (§9.3).
- Intel, *3rd Gen Xeon Scalable (Ice Lake-SP) uncore performance monitoring reference* — the iMC `CAS_COUNT`/`ACT_COUNT`/`PRE_COUNT` events used to see row-buffer behaviour and per-channel load (§9.2.2, §9.3).
- U. Drepper, *What Every Programmer Should Know About Memory* (2007), §2 — the DRAM primer the §9.2 picture descends from.
- Linux kernel documentation: `Documentation/admin-guide/mm/numa_memory_policy.rst`, `Documentation/mm/memory-tiering.rst` (kernel 6.1+) and the CXL subsystem docs (`Documentation/driver-api/cxl/`) — CPU-less nodes, tiering and the `numa_balancing=2` promotion/demotion mode (§9.2.5, §9.4.4).
- Y. Kim et al., *A Case for Exploiting Subarray-Level Parallelism (SALP) in DRAM* (ISCA 2012) and O. Mutlu's DRAM lecture notes — the row-buffer locality, bank parallelism and refresh-overhead literature behind §9.2.

## 9.8 Additional Reading

- Chips and Cheese memory-latency and bandwidth deep dives (Ice Lake-SP, Sapphire Rapids with DDR5 and HBM, Genoa) — measured loaded-latency curves and the DDR4→DDR5 latency comparison (§9.5's last-but-one pitfall).
- The CXL Consortium's CXL 2.0/3.0 specifications and Intel/AMD platform notes on memory expansion — what a Type 3 device looks like to the OS and why it is a capacity product.
- Ch. 8 (*caches*) — the hierarchy this chapter sits under; Ch. 12 (*prefetching*) — hiding what remains; Ch. 17–19 (*TLB, NUMA, chiplets*) — the other components of a cold access and the node topology the channels belong to; Ch. 58 (*RDT*) — MBA; Ch. 107–108 (*capture*) — the writer this chapter keeps off the channel.
- **Appendix E** — the DRAM rows (row hit, row conflict, loaded) restated with the method; **Appendix C.1** — the memory population/interleave/refresh/scrub knobs; **Appendix Q/R** — the AMD and per-generation memory subsystems (Zen's per-CCD view of the same controllers; DDR5 and MCR DIMMs on the newer Intel parts).

---

*Next: Ch. 10 — Cache-Aware & Data-Oriented Design, where we go back up the hierarchy and turn Ch. 8's mental model into layout decisions: Array-of-Structs vs Struct-of-Arrays, hot/cold field splitting, and an order-book update loop that touches only the lines it needs — so that the misses this chapter priced are the ones you chose to pay.*
