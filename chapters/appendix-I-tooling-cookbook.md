# Appendix I — Tooling Command Cookbook

> **Consolidates:** the commands used throughout the book, scattered across chapters — `perf` and the PMU (Ch. 2), micro-benchmarking (Ch. 3), reading asm / codegen (Ch. 4), debugging optimized builds (Ch. 5), `bpftrace`/eBPF (Ch. 61), plus the topology/pinning tools introduced operationally in Appendix C (`numactl`, `taskset`, `chrt`, `ethtool`, `cyclictest`) and `pahole` from Ch. 9. The *concepts* live in those chapters; this appendix is the **second-monitor cheat sheet** — the exact invocations, in one place.

**How to use this:** grouped by tool/purpose (I.1–I.9). Each entry is a command plus a one-line note on what it measures and which chapter derives the interpretation. Two rules from the methodology chapters govern all of it: **a number is only meaningful against a baseline** (Ch. 3) — always measure before/after, not once; and **profile, don't guess** (Ch. 2) — let the counters tell you where the time goes before you optimize. Commands assume a recent Linux `perf` (matching your kernel), GCC/Clang, and either root or the capabilities the PMU/eBPF need (`CAP_PERFMON`/`CAP_BPF`, or `sysctl kernel.perf_event_paranoid=-1` on a lab box — Ch. 2). Adapt core numbers (`2`, `2-7`) to your topology (I.6).

---

## I.1 `perf`: stat, record/report, annotate

The everyday profiling loop (Ch. 2).

```
   perf stat ./app                              # cycles, instructions, IPC, branches, task-clock
   perf stat -r 20 ./app                        # repeat 20× and report mean ± stddev (Ch. 3)
   perf stat -d -d -d ./app                     # add cache + TLB detail
   perf record -g ./app                         # sample with call graphs (default: frame pointers)
   perf record --call-graph dwarf ./app         # DWARF unwinding if no frame pointers (slower; Ch. 5, App D.4)
   perf record --call-graph lbr ./app           # LBR-based stacks (Intel; cheapest, shallow)
   perf report                                  # interactive TUI; --stdio for text
   perf report -n --sort=overhead,symbol        # ranked hot symbols with sample counts
   perf annotate <symbol>                        # per-instruction hotness over the asm (Ch. 4)
   perf list                                    # all events this CPU/kernel exposes
```

**Interpreting:** IPC (instructions/cycle) is the first-glance health metric (Ch. 2) — low IPC on a compute loop means stalls (find them with I.2). Build with `-fno-omit-frame-pointer -g` (Appendix D.4) so call graphs and `annotate` line up with source.

## I.2 PMU counters & top-down microarchitecture analysis

Attributing stalls to a microarchitectural cause (Ch. 2, 11, 12).

```
   perf stat -e cycles,instructions,\
   cache-references,cache-misses,\
   branches,branch-misses ./app                 # the classic six (Ch. 2, 12)
   perf stat -e L1-dcache-load-misses,LLC-load-misses,\
   dTLB-load-misses,iTLB-load-misses ./app      # cache/TLB breakdown (Ch. 7, 14)
   perf stat --topdown ./app                    # top-down: retiring / bad-spec / front-end / back-end
   perf stat --td-level 2 ./app                 # drill one level deeper (needs recent perf + CPU)
   toplev.py -l3 --no-desc ./app                # pmu-tools top-down, level 3 (Andi Kleen's toplev)
```

**Interpreting (Ch. 2):** the top-down buckets tell you *where* to look before *how* to fix — **front-end-bound** → I-cache/DSB/decode (Ch. 11); **bad speculation** → branch mispredicts (Ch. 12); **back-end-bound** → data cache/memory or execution-port pressure (Ch. 7, 10); **retiring** → you're actually doing work (optimize the algorithm, not the microarchitecture).

## I.3 `perf c2c`, `perf mem` & false-sharing / HITM

Finding cache-line contention and load latency (Ch. 7, 32).

```
   perf c2c record ./app                        # capture cache-to-cache transfers
   perf c2c report -NN --stdio                  # HITM (modified-line steals) ranked by cache line + offset (Ch. 32)
   perf mem record ./app                        # sample load/store with data source + latency
   perf mem report --stdio                      # where loads are served from (L1/L2/L3/DRAM/remote)
```

**Interpreting:** `perf c2c` is *the* false-sharing detector (Ch. 32) — it points at the exact cache line and the two offsets whose writers are ping-ponging it; the fix is padding/alignment (`alignas(std::hardware_destructive_interference_size)`, Ch. 32). `perf mem` reveals a memory-bound loop's data source — lots of remote-DRAM hits means a NUMA-placement problem (Ch. 16).

## I.4 VTune quick recipes

When you want the GUI top-down tree and memory-access modeling (Ch. 2).

```
   vtune -collect uarch-exploration -r r_ue ./app     # top-down microarchitecture (Ch. 2, 10-12)
   vtune -collect memory-access -r r_mem ./app        # loads/stores, NUMA, bandwidth (Ch. 7, 16)
   vtune -collect threading -r r_thr ./app            # lock contention, wait time (Ch. 30-32)
   vtune -collect hotspots -knob sampling-mode=hw ./app
   vtune-gui r_ue                                      # open a result in the GUI
```

**Interpreting:** VTune's Memory Access analysis surfaces the loads-are-remote / bandwidth-bound story more legibly than raw `perf mem`, and its microarchitecture tree is the same top-down model as I.2 with per-function attribution. Overkill for a quick check; worth it for a deep back-end-bound investigation.

## I.5 `bpftrace` / eBPF one-liners

Low-overhead, production-safe tracing (Ch. 61) — attach to a running process without restarting it.

```
   bpftrace -e 'tracepoint:syscalls:sys_enter_* { @[probe] = count(); }'   # which syscalls, by count
   bpftrace -e 'kprobe:finish_task_switch { @[comm] = count(); }'          # context switches by task (Ch. 41)
   # off-CPU / run-queue latency (scheduler wait) as a histogram, in ns:
   bpftrace -e 'tracepoint:sched:sched_wakeup { @qt[args->pid] = nsecs }
                tracepoint:sched:sched_switch /@qt[args->next_pid]/ {
                  @runq_ns = hist(nsecs - @qt[args->next_pid]); delete(@qt[args->next_pid]); }'
   bpftrace -e 'software:page-faults:1 { @[comm] = count(); }'             # page faults by process (Ch. 23)
   # user-space latency of a function via uprobe, histogram in ns:
   bpftrace -e 'uprobe:./app:on_message { @s[tid] = nsecs }
                uretprobe:./app:on_message /@s[tid]/ { @ns = hist(nsecs - @s[tid]); delete(@s[tid]); }'
```

**Interpreting (Ch. 61):** eBPF is the right tool for *production* latency questions the PMU can't answer directly — run-queue latency, off-CPU time, syscall counts under real load — at overhead low enough to leave running. Keep the probes off the hot core's critical section; measure the tracing overhead itself (Ch. 61) before trusting production numbers.

## I.6 Topology, pinning & scheduling

Discovering the machine and placing threads on it (Ch. 16, 42, 43; Appendix C).

```
   lscpu                                         # sockets, cores, threads, cache sizes, NUMA nodes
   lstopo --of txt                               # hwloc topology (cores, caches, NUMA, PCI) as ASCII
   numactl --hardware                            # NUMA nodes, free memory, node distances (Ch. 16)
   cat /sys/devices/system/cpu/cpu2/topology/thread_siblings_list   # SMT sibling of CPU2 (Ch. 43)
   taskset -c 2-7 ./app                          # pin to isolated cores (Ch. 42)
   chrt -f 80 ./app                              # SCHED_FIFO priority 80 (Ch. 45)
   numactl --cpunodebind=0 --membind=0 ./app     # bind cores AND memory to node 0 (Ch. 16)
   cpupower frequency-info                        # governor + current/available frequencies (Ch. 6)
   grep -E 'cpu[0-9]' /proc/interrupts           # where IRQs are landing (Ch. 42)
```

**Interpreting:** `lstopo` is the map you tune against (Ch. 43) — it shows which logical CPUs are SMT siblings and which cores share an L2/L3, so you can leave siblings idle (Ch. 43) and keep a thread and its data on one NUMA node (Ch. 16). These are the runtime counterparts to the boot-time isolation in Appendix C.

## I.7 Layout & memory inspection

Seeing struct layout and memory behavior (Ch. 9, 15).

```
   pahole -C OrderBookLevel ./app               # struct layout: offsets, holes, padding, cache lines (Ch. 9)
   pahole --hex -C Order ./app | grep -i hole    # just the padding holes to reclaim
   g++ -Wpadded ...                              # warn where the compiler inserts padding (Ch. 9, App D.7)
   valgrind --tool=cachegrind ./app             # simulated cache-miss counts per line (no hardware needed)
   cg_annotate cachegrind.out.<pid>             # annotate source with simulated misses
   grep -i huge /proc/<pid>/smaps               # per-mapping huge-page usage (Ch. 15)
   awk '/AnonHugePages/{s+=$2} END{print s" kB"}' /proc/<pid>/smaps   # total THP backing (Ch. 15)
```

**Interpreting:** `pahole` (from the `dwarves` package, reads DWARF — build with `-g`) is the definitive struct-layout tool (Ch. 9) — it shows exactly where padding wastes bytes and whether a hot struct straddles two cache lines, which is what you reorder members to fix. `cachegrind` is a deterministic (if idealized) cross-check when hardware counters are noisy or unavailable.

## I.8 Network & NIC

Inspecting and tuning the RX/TX path (Ch. 51, 53, 55, 56).

```
   ethtool -g eth0                              # current vs max RX/TX ring sizes (Ch. 53, 55)
   ethtool -G eth0 rx 4096                       # enlarge RX ring to absorb microbursts (Ch. 53)
   ethtool -c eth0                              # interrupt coalescing settings (Ch. 55)
   ethtool -C eth0 rx-usecs 0 adaptive-rx off    # disable coalescing for lowest latency (Ch. 55)
   ethtool -S eth0 | grep -iE 'drop|miss|err|fifo'   # hardware drops — RX overruns at the open (Ch. 53)
   ethtool -T eth0                              # hardware-timestamping capabilities (Ch. 56)
   ss -tim                                       # per-socket TCP info: rtt, cwnd, retrans (Ch. 51)
   tcpdump -i eth0 -nn -ttt 'udp port 12345'     # inter-packet deltas on a market-data feed
   ptp4l -i eth0 -m                             # PTP daemon, verbose (Ch. 56)
   phc2sys -a -r -m                             # sync system clock to the NIC PHC (Ch. 56)
```

**Interpreting:** the `ethtool -S ... drop` counters are the first thing to check after a market-open incident (Ch. 53) — non-zero RX drops mean the ring overflowed and you lost packets in hardware, which no software tuning downstream can recover. `ethtool -T`/`ptp4l`/`phc2sys` are the hardware-timestamp and clock-sync chain behind wire-to-wire measurement (Ch. 56, Appendix K.5).

## I.9 Codegen, binaries & sanitizers

Verifying what the compiler produced, and catching bugs before production (Ch. 4, 22, 38, 72).

```
   objdump -d -M intel --no-show-raw-insn ./app          # disassemble (Intel syntax; Ch. 4)
   objdump -drwС ./app | c++filt                          # demangled disassembly
   g++ -O2 -S -masm=intel -o - x.cpp | c++filt            # asm straight from source (Godbolt-at-home; Ch. 4)
   nm --defined-only -C ./app | wc -l                     # exported symbol count (visibility hygiene; App D.4)
   perf record -e cycles:u -j any,u ./app                 # LBR data for BOLT
   perf2bolt -p perf.data -o app.fdata ./app              # convert perf profile for BOLT (Ch. 22)
   llvm-bolt ./app -o ./app.bolt -data=app.fdata \
     -reorder-blocks=ext-tsp -reorder-functions=hfsort    # post-link layout (Ch. 22, App D.6)
   # sanitizers — TEST/CI builds only, never production (App D.7):
   g++ -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined x.cpp && ./a.out
   g++ -O1 -g -fsanitize=thread x.cpp && ./a.out          # data races (Ch. 30-33, 40)
   clang++ -fsanitize=fuzzer,address parse_fuzzer.cpp && ./a.out   # fuzz a decoder (Ch. 72)
```

**Interpreting:** `objdump`/`-S` is the offline Godbolt (Ch. 4) — use it to confirm the optimizer did what you expected (vectorized the loop, elided the branch, inlined the accessor) before believing a micro-benchmark. The sanitizers are a *correctness* gate (Ch. 38, 72), not a performance tool — run them in CI at `-O1`, fix every finding, and never ship them (they cost 2–3×, Appendix D.7).

## I.10 References

- The **`perf`** documentation — `perf-stat`, `perf-record`, `perf-report`, `perf-annotate`, `perf-c2c`, `perf-mem` man pages, and the kernel `tools/perf` docs (I.1–I.3).
- **Brendan Gregg's** `perf` examples and *BPF Performance Tools* / `bpftrace` reference (I.5), and the `bcc`/`bpftrace` repositories.
- **Intel VTune** documentation and the *perfmon* event reference (I.2, I.4); Andi Kleen's **pmu-tools/toplev** (I.2).
- The `hwloc`/`lstopo`, `numactl`, `ethtool`, `pahole` (dwarves), `chrt`, `taskset`, `objdump`, and `llvm-bolt`/`perf2bolt` man pages and docs (I.6–I.9).

## I.11 Additional Reading

- **Appendix C** (System Tuning Checklist) — where many of these commands are applied as *configuration* (IRQ pinning, ring sizing, governor) rather than one-off *observation*.
- **Appendix D** (Compiler Flag Reference) — the build flags behind I.9 (`-fno-omit-frame-pointer`, LTO/PGO/BOLT, sanitizers).
- **Appendix E** (Latency Numbers) — the reference costs these tools let you measure against; **Appendix L** (Benchmark Harness) — how to wrap a measurement so `perf stat` and the histogram agree.
- Gregg, *Systems Performance* and *BPF Performance Tools* (Ch. 2, 61) — the book-length treatment of everything here.

---

*Next: Appendix J — HFT Market-Structure & Protocol Primer, a domain onramp for the strong-C++-but-new-to-trading reader: matching engines and price-time priority, the order book, the order lifecycle, market-data feeds, and a field-level quick reference for ITCH/OUCH/FIX/SBE/FAST.*
