# Part VII — OS, Scheduling & Isolation

# Chapter 44 — Cache Allocation Technology & Intel RDT

> **Prerequisites:** Ch. 7–8 (the memory hierarchy and why an L3 eviction costs a DRAM round-trip), Ch. 16 (NUMA), Ch. 42, 43, 45 (isolating the hot core — this chapter isolates its *cache*, the piece core-pinning alone can't), Ch. 33 (contention on shared cache lines), Appendix C (the tuning checklist this extends).
>
> **Leads into:** Ch. 56 (DDIO — which lands NIC data in the very L3 region this chapter learns to partition), Appendix C (add these knobs to the quiet-box checklist). The isolation step beyond core pinning (Ch. 42) and SMT control (Ch. 43): partitioning the shared last-level cache so a noisy neighbor can't evict the hot working set even when the core is dedicated.

---

## 44.1 Why it matters: pinning the core isn't enough if the cache is shared

Part VII went to great lengths to give the hot thread a *quiet core* — `isolcpus`, `nohz_full`, IRQ affinity, RT priority (Ch. 42, 43, 45). But a pinned core is not an *isolated* core, because the one resource it still shares with every other core on the socket is the **last-level cache (L3)**. On a modern Xeon the L3 is a single shared pool, inclusive-ish, carved across all cores. So the housekeeping thread on core 1, the logging consumer (Ch. 71) on core 0, a capture writer (Ch. 75), even a stray `cron` job (Ch. 45) — anything running anywhere on the socket — allocates into the *same* L3 your hot path depends on, and can **evict your hot working set** (the order book, the strategy state, the decode tables — Ch. 25). That eviction is invisible to `taskset`: the core is yours, but the cache underneath it is a commons, and a noisy neighbor turns an L3 hit (~15–20 ns, Ch. 7) into a DRAM miss (~80–100 ns) at exactly the wrong moment (Ch. 1).

This is the last, quietest source of tail latency on an otherwise-tuned box, and it is precisely the kind of jitter that *only* shows up under production load — when the housekeeping cores are actually busy — and never in a quiet lab (Ch. 76's test-vs-prod drift). The symptom is maddening: p50 is perfect, but p99.9 shows the hot path occasionally paying DRAM latency for data that *should* be resident, because something on another core reached into the shared L3 and knocked it out. Core isolation gave you the compute; it did nothing for the cache.

**Intel RDT (Resource Director Technology)** — and its AMD analog (Platform QoS / L3 QoS) — is the hardware feature that closes this gap. It lets you **partition the shared L3** (and memory bandwidth) between groups of cores, so the hot core gets a dedicated slice of cache that no housekeeping thread can evict, plus **monitoring** so you can *see* who is occupying the cache and consuming bandwidth. Combined with core pinning (Ch. 42) it completes the isolation: the hot path gets a private core *and* a private chunk of L3. This chapter explains the RDT family and the `resctrl` interface (§44.2), measures a noisy neighbor evicting the hot working set and the isolation that fixes it (§44.3), shows how to partition cache and bandwidth in practice (§44.4), and warns about the ways cache partitioning backfires (§44.5). It's the cache half of "give the hot path a quiet machine."

## 44.2 Mental model: RDT, CLOS, capacity bitmasks, and `resctrl`

**Intel RDT is a family of cache/bandwidth partitioning and monitoring features**, controlled through a single kernel interface. The pieces:

- **CAT (Cache Allocation Technology)** — partitions the **L3** (and on some parts, L2) by *ways*. The L3 is N-way associative; CAT lets you assign each *class of service* a **capacity bitmask (CBM)** — a bitmap of which cache ways that class may allocate into. Give the hot class a set of ways nobody else can touch, and its lines are never evicted by another class.
- **CDP (Code/Data Prioritization)** — an extension of CAT that splits the mask into *code* and *data*, so you can protect the hot path's instructions (Ch. 12 I-cache pressure) separately from its data.
- **MBA (Memory Bandwidth Allocation)** — throttles the **memory bandwidth** a class may consume (as a percentage / delay value), so a bandwidth-hungry neighbor (a capture writer streaming to disk, Ch. 75; a `memcpy`-heavy batch job) can't saturate the memory controller and starve the hot path (Ch. 16's bandwidth is shared too).
- **CMT (Cache Monitoring Technology)** and **MBM (Memory Bandwidth Monitoring)** — the *monitoring* half: per-class L3 **occupancy** (how many bytes of L3 a group currently holds) and memory **bandwidth** (local and total). You can't manage what you can't measure (Ch. 2); CMT/MBM show you exactly who is in the cache.

The organizing abstraction is the **CLOS (Class of Service)**: a numbered group. You assign each CLOS a CBM (which ways it may use) and/or an MBA throttle, then assign *cores* (or *tasks*) to a CLOS. Cores in CLOS 1 allocate only into CLOS 1's ways.

The Linux interface is the **`resctrl` pseudo-filesystem** (`/sys/fs/resctrl`), mounted like `cgroup`:

```
   /sys/fs/resctrl/
     schemata            # default CLOS0: L3:0=fffff;MB:0=100  (all ways, 100% BW)
     tasks / cpus        # who is in CLOS0
     info/L3/            # num_closids, cbm_mask, min_cbm_bits
     info/L3_MON/        # monitoring: max_rmid, mon_features
     mon_data/           # per-group occupancy & bandwidth counters
     hotpath/            # a group YOU create (a CLOS)
       schemata          #   L3:0=00003;MB:0=100   (only the low 2 ways)
       cpus              #   the hot core(s)
       mon_data/         #   this group's L3 occupancy / BW
```

The model to hold: **a CLOS is a lease on cache ways and bandwidth; you give the hot path an exclusive lease and everyone else a *different, non-overlapping* lease.** Two CLOSes whose bitmasks don't overlap cannot evict each other's lines — that's the isolation. Overlapping masks share ways (and defeat the purpose — §44.5). AMD's equivalent (L3 QoS / QoS Enforcement) exposes a very similar `resctrl` interface, so the mechanics transfer with different capacity granularity.

## 44.3 Measure it: a noisy neighbor vs a partitioned cache

The effect to measure is **hot-path tail latency with and without a cache-thrashing neighbor, before and after partitioning.** Set up: the hot thread on an isolated core (Ch. 42) doing repeated lookups over a working set sized to fit L3 (an order book, Ch. 25); a "noisy neighbor" on another core streaming a large buffer (bigger than L3) to evict everything. Measure the hot path's latency distribution (HdrHistogram, Ch. 3) and its L3 miss rate (`perf`, Ch. 2) in three conditions. Representative shape (single-socket Xeon Gold 6326, Ice Lake-SP, ~30 MB L3, 12-way; figures pending real runs):

| Condition | Hot-path L3 miss rate | p50 | p99.9 | Notes |
|---|---|---|---|---|
| **Hot path alone** (quiet box) | ~0.2% | ~0.9 µs | ~1.1 µs | the ideal — working set resident in L3 |
| **+ noisy neighbor, no CAT** | ~18% | ~1.0 µs | ~**4.8 µs** | neighbor evicts the working set; tail explodes with DRAM misses |
| **+ noisy neighbor, CAT partitioned** | ~0.3% | ~0.9 µs | ~1.2 µs | hot path's ways are private; neighbor can't evict — tail restored |

And confirm with **CMT occupancy**: watch `mon_data/.../llc_occupancy` for the hot group and the neighbor group. Without CAT, the neighbor's occupancy balloons and the hot group's shrinks (it's being evicted). With CAT, each group's occupancy stays bounded by its assigned ways — you can *see* the partition holding.

The lessons:

- **A noisy neighbor is a p99.9 weapon that pinning doesn't stop.** The median barely moves (the working set is usually resident), but the *tail* — the lookups that hit a just-evicted line — pays full DRAM latency (Ch. 7). This is the exact profile of a jitter source that passes a quiet-lab test and fails in production (Ch. 76).
- **CAT restores the tail by making eviction impossible, not merely unlikely.** With non-overlapping ways, the neighbor *cannot* touch the hot path's lines — it's a hard partition, not a heuristic. The tail returns to the quiet-box baseline.
- **Measure occupancy, not just latency (CMT).** Occupancy tells you *why* — it shows the neighbor eating the cache and the partition stopping it. It also tells you if you sized the hot partition right (§44.5): if the hot group's occupancy is pinned at its ceiling and its miss rate is still high, it needs more ways.
- **MBA matters when the neighbor is bandwidth-bound, not cache-bound.** If the neighbor streams (capture to disk, Ch. 75), it may not evict much but can saturate memory bandwidth (Ch. 16), stalling the hot path's *own* misses. MBA throttling the neighbor's bandwidth is the fix there — measure with MBM.

## 44.4 Techniques

### 44.4.1 Partitioning the L3 with CAT via `resctrl`

The core technique: carve the L3 so the hot path owns a private set of ways.

- **Mount resctrl and inspect capabilities.** `mount -t resctrl resctrl /sys/fs/resctrl`; read `info/L3/cbm_mask` (the full mask, e.g. `fffff` = 20 ways) and `info/L3/min_cbm_bits` (minimum contiguous ways per allocation) and `info/L3/num_closids` (how many CLOSes you have).
- **Create a hot group and give it exclusive ways.** `mkdir /sys/fs/resctrl/hotpath`; write a bitmask to its `schemata` that reserves the ways *only it* uses, e.g. `L3:0=00f00` (4 ways). Then **shrink the default group** (`CLOS0`, everything else) to the *complementary* ways: `L3:0=ff0ff` — critically, the default mask must **not overlap** the hot mask, or the isolation is void (§44.5). Bitmasks in CAT must be **contiguous** (a run of 1-bits) on most parts.
- **Assign the hot core(s).** Write the hot CPU(s) to `hotpath/cpus` (CPU-based assignment — everything on that core uses the hot CLOS), or write the hot thread's TID to `hotpath/tasks` (task-based). CPU-based is simplest for a dedicated hot core (Ch. 42).
- **Size the partition to the working set.** Give the hot group *just enough* ways to hold its working set (order book + strategy state + decode tables — Ch. 25), measured via CMT occupancy (§44.3). Too few ways and the hot path evicts *itself* (§44.5); too many and you starve everyone else, which can bounce back as bandwidth pressure. Right-size with occupancy monitoring, don't guess.
- **CDP for code/data split (optional).** If the hot path is also front-end-bound (Ch. 12), enable CDP and give its *code* ways protection from its own data streaming, keeping hot instructions resident.

### 44.4.2 Throttling neighbors with MBA; monitoring with CMT/MBM

Cache partitioning handles *capacity*; bandwidth and monitoring complete the picture.

- **MBA to cap a bandwidth-hungry neighbor.** Put the streaming housekeeping work (capture writer — Ch. 75, log flusher — Ch. 71, batch jobs) in a CLOS with an MBA throttle: `MB:0=20` (limit to ~20% of memory bandwidth). This stops a `memcpy`-heavy neighbor from saturating the memory controller (Ch. 16) and stalling the hot path's own DRAM accesses. MBA is a coarse throttle (delay-based, per-core); measure its effect with MBM, don't assume the percentage is exact.
- **Monitor continuously (CMT/MBM) in production.** Create monitoring groups and poll `mon_data/mon_L3_XX/llc_occupancy` and `.../mbm_total_bytes` — this is the production-safe observability (Ch. 76) for the cache: it shows, live, who occupies the L3 and who consumes bandwidth. Alert when the hot group's occupancy dips (something is stealing its ways — a misconfiguration or a new neighbor) or when a neighbor's bandwidth spikes.
- **Combine CAT + MBA + pinning as one policy.** The full isolation recipe: pin the hot thread to an isolated core (Ch. 42), give that core a CLOS with exclusive L3 ways (CAT), and put *all other* work in CLOSes with complementary ways and MBA throttles. Bake it into the box's startup (Appendix C) alongside `isolcpus`/IRQ affinity, and verify with CMT/MBM in the readiness check (Ch. 76).

### 44.4.3 Partitioning for DDIO and multi-tenant boxes

Two higher-order uses that connect forward:

- **Reserve ways for the NIC's DDIO region (ties Ch. 56).** DDIO lands received packets directly in a slice of L3 (Ch. 56). By default that DDIO region can *conflict* with your hot working set — the NIC's incoming data evicts your order book, or vice versa. CAT lets you *separate* the DDIO region from the hot data region so they don't fight — one of the most important combined uses, developed in Ch. 56. (You allocate ways for the NIC's write region and keep the hot path's ways disjoint.)
- **Multi-tenant / multi-strategy boxes.** When several strategies (or shards — Ch. 74) share a socket, CAT partitions the L3 *between* them so one strategy's working set can't evict another's — cache-level fault isolation to match the process-level isolation of Ch. 74. Each strategy gets a CLOS sized to its footprint.

## 44.5 Pitfalls & anti-patterns: over-partitioning and overlapping masks

- **Overlapping bitmasks (the cardinal error).** Giving the hot group ways `00f00` but leaving the default group at `fffff` (which *includes* those ways) means everyone still allocates into the hot ways — you've partitioned nothing. The default/other CLOSes must be shrunk to the **complement** of the hot mask so the masks are **disjoint**. Verify every CLOS's schemata; overlap silently defeats the isolation (and the CMT occupancy will show it — the neighbor still eats the hot ways).
- **Over-partitioning — starving the hot path of its own cache.** Give the hot group too few ways and it can't hold its working set, so it evicts *itself* on every pass — you've manufactured the miss you were trying to prevent. Size to the working set via CMT occupancy (§44.3), with headroom; a partition smaller than the footprint is worse than no partition.
- **Starving everyone else into bandwidth pressure.** Give the hot group almost *all* the ways and the housekeeping cores thrash their tiny slice, miss constantly, and pound memory bandwidth (Ch. 16) — which bounces back as bandwidth contention on the hot path's own misses. Isolation is a balance: enough for the hot path, enough left for the rest. Watch total bandwidth (MBM).
- **Forgetting bandwidth (CAT without MBA).** A neighbor that streams (capture, Ch. 75) may occupy few *ways* but saturate *bandwidth* — CAT won't stop it, only MBA will. If the hot path's misses get slower under a streaming neighbor despite CAT, it's bandwidth, not capacity (§44.4.2).
- **Non-contiguous / invalid masks.** Most CAT implementations require **contiguous** bitmasks (a single run of 1-bits) and a minimum width (`min_cbm_bits`). A non-contiguous or too-narrow mask is rejected or silently clamped. Read `info/L3/` before writing masks.
- **Hyperthread and shared-L2 subtleties.** CAT partitions L3; two hyperthreads on a core still share L1/L2 (Ch. 43) regardless of CLOS — cache isolation between siblings needs L2 CAT (if supported) or, more simply, leaving the sibling idle (Ch. 43). Assigning two CLOSes to two siblings doesn't isolate their L1/L2.
- **AMD/Intel and generational differences.** Way counts, granularity, L2 CAT availability, and MBA semantics vary by CPU (and AMD's QoS differs in detail). Probe `info/` at runtime; don't hard-code masks for one part (this is the Appendix A portability discipline applied to RDT).
- **Not monitoring — flying blind.** Setting masks once and never checking CMT occupancy means you won't notice when a config drift, a new neighbor, or a working-set change breaks the isolation. Monitor in production (Ch. 76); the partition is a claim to *verify*, not a set-and-forget.

## 44.6 Exercises & checklist

**Exercises:**

1. **Reproduce the noisy neighbor.** Run a hot lookup loop (working set ~half L3) on an isolated core and a buffer-streaming neighbor on another core. Measure the hot path's p99.9 and L3 miss rate (Ch. 2/3) with the neighbor on and off — reproduce the §44.3 tail explosion.
2. **Partition and restore.** Create a `resctrl` hot group with exclusive ways, shrink the default group to the complement, assign the hot core, and re-measure — show the tail returns to baseline and the miss rate drops. Verify the masks are disjoint.
3. **Right-size with CMT.** Poll `llc_occupancy` for the hot group while shrinking its ways one at a time; find the point where occupancy hits the ceiling and the miss rate climbs — that's the working-set size in ways. Set the partition just above it.
4. **Bandwidth vs capacity.** Replace the cache-thrashing neighbor with a bandwidth-streaming one (large sequential `memcpy`). Show CAT alone doesn't fully fix it, then add an MBA throttle and re-measure (MBM) — isolate the bandwidth effect from the capacity effect.
5. **DDIO preview.** Read Ch. 56, then reserve a CAT region for the NIC's DDIO writes disjoint from the hot data ways; measure whether separating them reduces hot-path tail under heavy RX (this is the §44.4.3 / Ch. 56 combined technique).

**Checklist:**

- [ ] The hot core has a **CLOS with exclusive L3 ways** (CAT), and **all other CLOSes use the disjoint complement** — masks verified non-overlapping (§44.4.1, §44.5).
- [ ] The hot partition is **sized to the working set** via **CMT occupancy** — enough ways to stay resident, with headroom, not starving itself (§44.4.1, §44.5).
- [ ] Enough cache is **left for housekeeping** that it doesn't thrash into bandwidth pressure; total bandwidth watched (MBM) (§44.5).
- [ ] **Bandwidth-hungry neighbors** (capture, logging, batch) are **MBA-throttled** so they can't saturate the memory controller (§44.4.2).
- [ ] **CMT/MBM monitoring runs in production** (Ch. 76) — occupancy and bandwidth per group, alerting on the hot group losing ways or a neighbor spiking (§44.4.2, §44.5).
- [ ] Masks respect the part's **contiguity/min-width** rules and are **probed at runtime** (`info/L3/`), not hard-coded across CPU generations (§44.5).
- [ ] The **CAT/MBA policy is baked into box startup** (Appendix C) alongside pinning/isolation, and verified in the readiness check; combined with the DDIO region separation of Ch. 56 where a NIC is on the hot path (§44.4.3).

## 44.7 References

- The **Intel RDT** documentation and the *Intel 64 and IA-32 Architectures Software Developer's Manual* (CAT/CDP/MBA/CMT/MBM chapters) — the authoritative feature reference (§44.2).
- The Linux kernel **`resctrl`** documentation (`Documentation/arch/x86/resctrl.rst`) — the interface, schemata syntax, and monitoring groups (§44.2, §44.4).
- The `intel-cmt-cat` toolkit (`pqos`) documentation — a userspace tool for CAT/MBA/monitoring, an alternative to raw `resctrl` (§44.4).
- The **AMD64 QoS Extensions** (Platform QoS / L3 QoS Enforcement) documentation — the AMD analog and its `resctrl` support (§44.2, §44.5).
- Ch. 7–8 (cache hierarchy — what CAT protects), Ch. 16 (memory bandwidth — what MBA governs), Ch. 42, 43, 45 (core isolation this completes), Ch. 56 (DDIO — the combined technique), Appendix C (tuning checklist).

## 44.8 Additional Reading

- Intel and vendor whitepapers on cache QoS / RDT for network-function and low-latency workloads — real deployments of CAT/MBA for tail-latency isolation (§44.3).
- Talks on cache partitioning in HFT and latency-sensitive systems — the noisy-neighbor problem and CAT as the fix (§44.1, §44.3).
- The `pqos`/`intel-cmt-cat` examples and the resctrl kernel test suite — practical CLOS/mask configuration patterns (§44.4).
- Ch. 56 (*DDIO*) — separating the NIC's cache region from the hot data region; Ch. 74 (*Process Topology*) — per-strategy cache isolation; **Appendix C** — the quiet-box checklist; **Appendix E** — the L3-vs-DRAM latencies that make this matter; **Appendix F** — RDT/CAT/CLOS glossary.

---

*Next: Ch. 45 — Real-Time Scheduling & Kernel Tuning, the kernel configuration behind the isolation this chapter assumed: `isolcpus`, `nohz_full`, RT priorities, cgroups, and `cyclictest` — turning a pinned, cache-partitioned core into a jitter-free one.*
