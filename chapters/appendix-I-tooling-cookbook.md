# Appendix I — Tooling Command Cookbook

> **Consolidates:** the commands used throughout the book, scattered across chapters — `perf` and the PMU (Ch. 2), micro-benchmarking (Ch. 3), reading asm / codegen (Ch. 4), debugging optimized builds (Ch. 5), `bpftrace`/eBPF (Ch. 69), plus the topology/pinning tools introduced operationally in Appendix C (`numactl`, `taskset`, `chrt`, `ethtool`, `cyclictest`) and `pahole` from Ch. 9. The *concepts* live in those chapters; this appendix is the **second-monitor cheat sheet** — the exact invocations, in one place.

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
   perf record -F 999 -g -- ./app && perf script | stackcollapse-perf.pl | flamegraph.pl > flame.svg   # on-CPU flame graph (Ch. 2 §2.4.2); add -e LLC-load-misses for a miss flame graph
   perf annotate <symbol>                        # per-instruction hotness over the asm (Ch. 4)
   perf list                                    # all events this CPU/kernel exposes
```

**Interpreting:** IPC (instructions/cycle) is the first-glance health metric (Ch. 2) — low IPC on a compute loop means stalls (find them with I.2). Build with `-fno-omit-frame-pointer -g` (Appendix D.4) so call graphs and `annotate` line up with source.

## I.2 PMU counters & top-down microarchitecture analysis

Attributing stalls to a microarchitectural cause (Ch. 2, 11, 12).

```
   perf stat -e cycles,instructions,cache-references,cache-misses,branches,branch-misses ./app   # the classic six (Ch. 2, 13)
   perf stat -e L1-dcache-load-misses,LLC-load-misses,dTLB-load-misses,iTLB-load-misses ./app   # cache/TLB breakdown (Ch. 7, 15)
   perf stat --topdown ./app                    # top-down: retiring / bad-spec / front-end / back-end
   perf stat --topdown --td-level 2 ./app       # level-2 breakdown — Sapphire Rapids+ only (Ice Lake-SP: level 1); --td-level does nothing without --topdown; pre-Ice Lake needs -a
   toplev.py -l3 --no-desc ./app                # pmu-tools top-down, level 3 (Andi Kleen's toplev)
   # --- beyond the core PMU (Ch. 2 §2.4.4): socket-scoped, never touch an isolated core ---
   perf list uncore | head -40                                                     # the uncore boxes this CPU exposes (uncore_imc/cha/upi/iio/m2m…)
   perf stat -a -I 1000 -e 'uncore_imc/cas_count_read/,uncore_imc/cas_count_write/'   # DRAM bandwidth: CAS × 64 B per channel (Ch. 7, 16)
   perf stat -a -I 1000 -e 'uncore_upi/txl_flits.all_data/'                         # cross-socket traffic (Ch. 16)
   perf stat -a -I 1000 -e 'uncore_iio/data_req_of_cpu.mem_write.part0/'           # PCIe inbound writes per IIO stack = NIC DMA (Ch. 64, 74)
   perf stat -a -I 1000 -e power/energy-pkg/,power/energy-ram/                       # RAPL joules → watts: is the socket power-limited? (Ch. 6, 99)
   perf stat -C 5 -e core_power.lvl0_turbo_license,core_power.lvl1_turbo_license,core_power.lvl2_turbo_license -- sleep 10   # AVX licence time (Ch. 32) — ONE attach IPI; not -I
   perf stat -a -I 1000 -e msr/smi/                                                  # SMI count per second — must be 0 (Ch. 6 §6.4.4, 46, 82)
   perf record -e ibs_op//  -c 100000 ./app; perf report                            # AMD IBS: precise op sampling (what perf mem/c2c use on Zen)
   perf record -e ibs_fetch// ./app                                                  # AMD IBS: front-end sampling (Ch. 12)
   perf stat -M PipelineL1 ./app                                                     # Zen 4 pipeline-utilization top-down (names: perf list metricgroup)
   # --- what it costs the hot core, and the syscall-free alternative (Ch. 101 §101.4.4) ---
   perf record -C 5 -e cycles:u -c 2000000 --call-graph fp --overwrite --switch-output=signal -o perf.data -- sleep 36000 &   # bounded, fp, frozen on SIGUSR2
   cat /sys/bus/event_source/devices/cpu/rdpmc                                      # 1 = rdpmc allowed for mmap'd events (default), 2 = always, 0 = never
   ./rdpmc                                                                           # Ch. 101 §101.4.4's listing: cycles + LLC misses per stage from the thread itself
```

**Interpreting (Ch. 2):** the top-down buckets tell you *where* to look before *how* to fix — **front-end-bound** → I-cache/DSB/decode (Ch. 12); **bad speculation** → branch mispredicts (Ch. 13); **back-end-bound** → data cache/memory or execution-port pressure (Ch. 7, 11); **retiring** → you're actually doing work (optimize the algorithm, not the microarchitecture).

## I.3 `perf c2c`, `perf mem` & false-sharing / HITM

Finding cache-line contention and load latency (Ch. 7, 36).

```
   perf c2c record ./app                        # capture cache-to-cache transfers
   perf c2c report -NN --stdio                  # HITM (modified-line steals) ranked by cache line + offset (Ch. 36)
   perf mem record ./app                        # sample load/store with data source + latency
   perf mem report --stdio                      # where loads are served from (L1/L2/L3/DRAM/remote)
```

**Interpreting:** on AMD both `perf mem` and `perf c2c` sample through IBS (`ibs_op`), with the same reports (Ch. 2 §2.4.4); the data-source and latency columns are IBS's, which have no skid. `perf c2c` is *the* false-sharing detector (Ch. 36) — it points at the exact cache line and the two offsets whose writers are ping-ponging it; the fix is padding/alignment (`alignas(std::hardware_destructive_interference_size)`, Ch. 36). `perf mem` reveals a memory-bound loop's data source — lots of remote-DRAM hits means a NUMA-placement problem (Ch. 16).

## I.4 VTune quick recipes

When you want the GUI top-down tree and memory-access modeling (Ch. 2).

```
   vtune -collect uarch-exploration -r r_ue ./app     # top-down microarchitecture (Ch. 2, 11-13)
   vtune -collect memory-access -r r_mem ./app        # loads/stores, NUMA, bandwidth (Ch. 7, 16)
   vtune -collect threading -r r_thr ./app            # lock contention, wait time (Ch. 34-36)
   vtune -collect hotspots -knob sampling-mode=hw ./app
   vtune-gui r_ue                                      # open a result in the GUI
```

**Interpreting:** VTune's Memory Access analysis surfaces the loads-are-remote / bandwidth-bound story more legibly than raw `perf mem`, and its microarchitecture tree is the same top-down model as I.2 with per-function attribution. Overkill for a quick check; worth it for a deep back-end-bound investigation.

## I.5 `bpftrace` / eBPF one-liners

Low-overhead, production-safe tracing (Ch. 69) — attach to a running process without restarting it.

```
   bpftrace -e 'tracepoint:syscalls:sys_enter_* { @[probe] = count(); }'   # which syscalls, by count
   bpftrace -e 'tracepoint:sched:sched_switch { @[args.next_comm] = count(); }'   # context switches by task (Ch. 46); kprobe:finish_task_switch* also works (GCC emits it as finish_task_switch.isra.0)
   # run-queue latency (scheduler wait) as a histogram, in ns. Stamp on wakeup AND on
   # preemption-while-runnable (prev_state == TASK_RUNNING) — on an isolated core the
   # preemptions by ksoftirqd/IRQ threads ARE the latency, and a wakeup-only probe misses them:
   bpftrace -e 'tracepoint:sched:sched_wakeup,tracepoint:sched:sched_wakeup_new { @qt[args.pid] = nsecs }
                tracepoint:sched:sched_switch {
                  if (args.prev_state == TASK_RUNNING) { @qt[args.prev_pid] = nsecs; }
                  $t = @qt[args.next_pid];
                  if ($t) { @runq_ns = hist(nsecs - $t); delete(@qt, args.next_pid); } }'
   #   (bpftrace ≥ 0.22: `args.field` and `delete(@map, key)`; older releases spell them `args->field` / `delete(@map[key])`)
   bpftrace -e 'software:page-faults:1 { @[comm] = count(); }'             # page faults by process (Ch. 26)
   # user-space latency of a function via uprobe, histogram in ns:
   bpftrace -e 'uprobe:./app:cpp:on_message { @s[tid] = nsecs }
                uretprobe:./app:cpp:on_message /@s[tid]/ { @ns = hist(nsecs - @s[tid]); delete(@s, tid); }'
   #   C++ symbols are mangled: use the `cpp` qualifier (or the mangled name, or `*on_message*`); the function must not be inlined
```

**Interpreting (Ch. 69):** eBPF is the right tool for *production* latency questions the PMU can't answer directly — run-queue latency, off-CPU time, syscall counts under real load — at overhead low enough to leave running. Keep the probes off the hot core's critical section; measure the tracing overhead itself (Ch. 69) before trusting production numbers.

## I.6 Topology, pinning & scheduling

Discovering the machine and placing threads on it (Ch. 16, 48, 49; Appendix C).

```
   lscpu                                         # sockets, cores, threads, cache sizes, NUMA nodes
   lstopo --of txt                               # hwloc topology (cores, caches, NUMA, PCI) as ASCII
   numactl --hardware                            # NUMA nodes, free memory, node distances (Ch. 16)
   cat /sys/devices/system/cpu/cpu2/topology/thread_siblings_list   # SMT sibling of CPU2 (Ch. 49)
   taskset -c 2-7 ./app                          # pin to isolated cores (Ch. 48)
   chrt -f 80 ./app                              # SCHED_FIFO priority 80 (Ch. 51)
   numactl --cpunodebind=0 --membind=0 ./app     # bind cores AND memory to node 0 (Ch. 16)
   cpupower frequency-info                        # governor + current/available frequencies (Ch. 6)
   grep -E 'CPU|<if>' /proc/interrupts           # header + the NIC's IRQ rows: per-CPU counts show where IRQs land (Ch. 48)
   cyclictest -m -p 80 -a 2 -t 1 -i 200 -d 0 -D 1h   # wake-up latency on isolated core 2 — read the max, not the avg (Ch. 51, App C.5)
   # --- hunting residual noise (Ch. 52): attribute what still lands on the isolated core
   rtla osnoise top -c 2 -P f:80 -d 10m -T 1 -s 20 -t   # spin on cpu2, count noise >= 1 us, STOP + save trace on a 20 us gap; HW column = SMI/firmware
   rtla timerlat top -c 2 -P f:95 -p 100 -a 20 -u        # timer latency split into IRQ vs thread; stop + trace above 20 us; -u = user thread too
   rtla hwnoise -c 2 -d 2m -T 1                          # the firmware floor (hwlat); qualification only — it monopolises the core
   echo 4 > /sys/kernel/tracing/tracing_cpumask          # hex mask: trace ONLY cpu2 (0x4); then set_event irq_vectors:* tlb:tlb_flush sched:sched_switch ...
   echo x86-tsc > /sys/kernel/tracing/trace_clock        # TSC timestamps that line up with the app's rdtsc (Ch. 17)
   trace-cmd record -M 4 -C x86-tsc -e irq -e sched -e irq_vectors -e tlb -e timer:tick_stop   # to a file; `trace-cmd report` / KernelShark
   perf sched record -C 2 -- sleep 600 && perf sched timehist -C 2    # who ran on cpu2, when, for how long (thread intruders)
   awk 'NR==1 || /LOC|RES|CAL|TLB|IWI|NMI|PMI/' /proc/interrupts     # the IPI rows: diff two reads a minute apart on the hot cpu's column (Ch. 52 §52.4.1, Ch. 99)
   grep -l -E '^Cpus_allowed_list:.*\b2\b' /proc/[0-9]*/task/*/status  # every thread ALLOWED on cpu2 (want: yours + the per-cpu kthreads)
   cat /proc/irq/*/effective_affinity_list | sort | uniq -c            # where vectors REALLY live (managed ones ignore smp_affinity — Ch. 48)
   sudo rdmsr -p 0 0x34                                                # MSR_SMI_COUNT from a HOUSEKEEPING cpu (rdmsr -a IPIs every core — Ch. 99 §99.2.4)
   turbostat --quiet --show CPU,Busy%,Bzy_MHz,SMI,PkgTmp --interval 5 --cpu 0-1   # --cpu restricts the MSR reads to housekeeping cpus
   cat /proc/<pid>/task/*/timerslack_ns                                # 50000 = the default 50 µs timer slack on every sleeping thread (Ch. 47 §47.3); 1 after prctl(PR_SET_TIMERSLACK, 1)
   echo 1 | sudo tee /proc/<pid>/task/<tid>/timerslack_ns              # set it from outside for a component you cannot patch (Ch. 47 §47.4.4)
   cat /proc/sys/kernel/sched_rt_runtime_us                            # 950000 = RT throttle ON: a busy-polling SCHED_FIFO thread stalls 50 ms/s (Ch. 47 §47.3); -1 on isolated boxes (Ch. 51, Appendix C)
   perf trace -e 'timer:hrtimer_expire_entry' -C 2 -- sleep 10         # whose hrtimers fire on the isolated cpu (want: none but the hot thread's own — Ch. 47 §47.4.4)
   sudo cyclictest -p 80 -i 200 -h 400 -a 1 -t 1 -D 60s                # wake-up latency of a sleeping RT thread on cpu1 — the kernel-timer half of Ch. 47, after Ch. 51's tuning
```

**Interpreting:** `lstopo` is the map you tune against (Ch. 49) — it shows which logical CPUs are SMT siblings and which cores share an L2/L3, so you can leave siblings idle (Ch. 49) and keep a thread and its data on one NUMA node (Ch. 16). These are the runtime counterparts to the boot-time isolation in Appendix C.

## I.7 Layout & memory inspection

Seeing struct layout and memory behavior (Ch. 9, 15).

```
   pahole -C OrderBookLevel ./app               # struct layout: offsets, holes, padding, cache lines (Ch. 9)
   pahole --hex -C Order ./app | grep -i hole    # just the padding holes to reclaim
   g++ -Wpadded ...                              # warn where the compiler inserts padding (Ch. 9, App D.7)
   valgrind --tool=cachegrind --cache-sim=yes ./app   # simulated I1/D1/LL miss counts per line (default since Valgrind 3.21 is Ir only)
   cg_annotate cachegrind.out.<pid>             # annotate source with simulated misses
   grep -i huge /proc/<pid>/smaps               # per-mapping huge-page usage (Ch. 15)
   awk '/AnonHugePages/{s+=$2} END{print s" kB"}' /proc/<pid>/smaps   # total THP backing (Ch. 15)
```

**Interpreting:** `pahole` (from the `dwarves` package, reads DWARF — build with `-g`) is the definitive struct-layout tool (Ch. 9) — it shows exactly where padding wastes bytes and whether a hot struct straddles two cache lines, which is what you reorder members to fix. `cachegrind` is a deterministic (if idealized) cross-check when hardware counters are noisy or unavailable.

## I.8 Network & NIC

Inspecting and tuning the RX/TX path (Ch. 58, 60, 63, 64).

```
   ethtool -g eth0                              # current vs max RX/TX ring sizes (Ch. 60, 63)
   ethtool -G eth0 rx 4096                       # enlarge RX ring to absorb microbursts (Ch. 60)
   ethtool -c eth0                              # interrupt coalescing settings (Ch. 63)
   ethtool -C eth0 rx-usecs 0 adaptive-rx off    # disable coalescing for lowest latency (Ch. 63)
   ethtool -S eth0 | grep -iE 'drop|miss|err|fifo'   # hardware drops — RX overruns at the open (Ch. 60)
   ethtool -T eth0                              # hardware-timestamping capabilities (Ch. 66)
   ss -tim                                       # per-socket TCP info: rtt, cwnd, retrans (Ch. 58)
   tcpdump -i eth0 -nn -ttt 'udp port 12345'     # inter-packet deltas on a market-data feed
   ptp4l -i eth0 -m                             # PTP daemon, verbose (Ch. 66)
   phc2sys -a -r -m                             # sync system clock to the NIC PHC (Ch. 66)
   # --- multicast membership (Ch. 62): verify state, not traffic
   cat /proc/net/igmp                           # kernel's view: (device, group) joins, little-endian hex groups; Querier column = IGMP version in use
   ip maddr show dev eth0                       # the same, decoded
   timeout 260 tcpdump -i eth0 -nn -c 1 'igmp and igmp[0]==0x11'   # a General Query within one interval, or the feed goes dark ~260 s after the last join
   nstat -az IPReversePathFilter UdpRcvbufErrors UdpNoPorts        # rp_filter drops vs socket-buffer drops vs no-socket drops (Ch. 62, 100)
   sysctl net.ipv4.igmp_max_memberships net.ipv4.conf.eth0.rp_filter   # 20 by default (the 21st join fails ENOBUFS); strict rp_filter drops the feed
   # --- network health & drop forensics (Ch. 100): which LAYER dropped it
   ethtool -S eth0 | grep -iE 'missed|out_of_buffer|nodesc'          # host ring full (poll too slow)        — layer 2b
   ethtool -S eth0 | grep -iE 'fifo|over|discards_phy'               # NIC port FIFO / PCIe backpressure      — layer 2a (Ch. 73)
   ethtool -S eth0 | grep -iE 'crc|symbol|fec|pause'                 # link/PHY/FEC and pause frames         — layer 1 / fabric
   awk '{print NR-1, strtonum("0x"$1), strtonum("0x"$2), strtonum("0x"$3)}' /proc/net/softnet_stat   # cpu processed dropped time_squeeze (NAPI, layer 3)
   nstat -az TcpExt:TCPTimeouts Tcp:RetransSegs TcpExt:TCPSpuriousRTOs   # an RTO on the order session = a 200 ms+ stall (Ch. 59)
   ss -tim dst <venue>                                                # per-socket rtt/cwnd/retrans/notsent — or getsockopt(TCP_INFO) at 1 Hz from the gateway
   sudo dropwatch -l kas                                              # live histogram of kernel drops by function (then: start)
   sudo bpftrace -e 'tracepoint:skb:kfree_skb { @[args->reason, ksym(args->location)] = count(); }'   # drop REASON + location; arm BEFORE the session (text_poke IPIs)
   ethtool -m eth0 | grep -iE 'power|temperature'                     # optics DOM: rx/tx power vs the module's warning thresholds
   cat /sys/class/net/eth0/carrier_changes                            # link flaps since boot
```

**Interpreting:** the `ethtool -S ... drop` counters are the first thing to check after a market-open incident (Ch. 60) — non-zero RX drops mean the ring overflowed and you lost packets in hardware, which no software tuning downstream can recover — Ch. 100 §100.2 is the per-driver map of which counter means ring-full, port-FIFO (PCIe) or link error, and §100.4.2 the `kfree_skb` forensics for drops the counters don't explain. `ethtool -T`/`ptp4l`/`phc2sys` are the hardware-timestamp and clock-sync chain behind wire-to-wire measurement (Ch. 66, Appendix K.5).

## I.9 Codegen, binaries & sanitizers

Verifying what the compiler produced, and catching bugs before production (Ch. 4, 24, 45, 87).

```
   objdump -d -M intel --no-show-raw-insn ./app          # disassemble (Intel syntax; Ch. 4)
   objdump -drwC ./app                                    # demangled disassembly (-C demangles; no c++filt needed)
   g++ -O2 -S -masm=intel -o - x.cpp | c++filt            # asm straight from source (Godbolt-at-home; Ch. 4)
   nm -D --defined-only -C ./app | wc -l                  # exported (dynamic) symbol count (visibility hygiene; App D.4)
   g++ ... -fno-reorder-blocks-and-partition -Wl,--emit-relocs -o app   # BOLT needs relocations to reorder functions, and does its own hot/cold splitting (App D.5–D.6)
   perf record -e cycles:u -j any,u ./app                 # LBR data for BOLT
   perf2bolt -p perf.data -o app.fdata ./app              # convert perf profile for BOLT (Ch. 24)
   llvm-bolt ./app -o ./app.bolt -data=app.fdata \
     -reorder-blocks=ext-tsp -reorder-functions=hfsort \
     -split-functions -split-all-cold -split-eh -dyno-stats   # post-link layout + BOLT's hot/cold split (Ch. 24, App D.6)
   # sanitizers — TEST/CI builds only, never production (App D.7):
   g++ -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined x.cpp && ./a.out
   g++ -O1 -g -fsanitize=thread x.cpp && ./a.out          # data races (Ch. 33-36, 45)
   clang++ -fsanitize=fuzzer,address parse_fuzzer.cpp && ./a.out   # fuzz a decoder (Ch. 87)
```

**Interpreting:** `objdump`/`-S` is the offline Godbolt (Ch. 4) — use it to confirm the optimizer did what you expected (vectorized the loop, elided the branch, inlined the accessor) before believing a micro-benchmark. The sanitizers are a *correctness* gate (Ch. 45, 87), not a performance tool — run them in CI at `-O1`, fix every finding, and never ship them (ASan ≈2× CPU/≈3× memory, UBSan a few %–2×, TSan 5–15× CPU and 5–10× memory — Ch. 45, Appendix D.7).

## I.10 Crash, core-dump, live-introspection & incident one-liners

The operational face of Ch. 5 §5.4.4–§5.4.5, Ch. 93, Ch. 98 and Appendix N.

```
   # --- cores and crash records (Ch. 93) ---
   readelf -n ./app | grep 'Build ID'                     # the key that matches a core/crash record to its archived .debug (Ch. 5 §5.4.1)
   readelf -p .GCC.command.line ./app                     # the flags it was built with (needs -frecord-gcc-switches; App D.7)
   cat /proc/sys/kernel/core_pattern                      # "core", "|/usr/lib/systemd/systemd-coredump …" or your pipe handler (Ch. 93 §93.4.4)
   cat /proc/$PID/coredump_filter; echo 0x11 > /proc/$PID/coredump_filter   # dump private anon + ELF headers only (no shared/huge segments)
   grep -E 'VmRSS|VmHWM|HugetlbPages' /proc/$PID/status   # how big the core WOULD be before MADV_DONTDUMP (Ch. 93 §93.3)
   coredumpctl list; coredumpctl info $PID; coredumpctl gdb $PID            # systemd-stored cores (Ch. 93 §93.4.4)
   gdb -q ./app.debug -c core -batch -ex 'thread apply all bt' -ex 'info registers'   # every thread's stack, once
   eu-stack --core core -e ./app --debuginfo-path /var/debug   # the same without gdb's start-up (elfutils)
   addr2line -e ./app.debug -f -C -i -p 0x$(( FRAME - LOAD_BASE ))   # a raw crash-record frame → file:line (load base from maps)
   eu-unstrip -n --core core                               # which build IDs the core wants, and which you have
   # --- live introspection without stopping the hot thread (Ch. 5 §5.4.4) ---
   cat /proc/sys/kernel/yama/ptrace_scope                  # 0/1: same-uid attach; 2: CAP_SYS_PTRACE only; 3: never
   gdb -q -batch -ex 'set non-stop on' -ex 'set pagination off' -ex "attach $PID" -ex 'info threads' -ex detach
   gdb -q -batch -ex 'set non-stop on' -ex "attach $PID" -ex "thread $HK_TID" -ex 'interrupt' -ex 'bt' -ex detach   # one housekeeping thread only
   gdbserver --attach :2345 $PID                           # debug from another box; still a ptrace stop — housekeeping threads only
   for t in /proc/$PID/task/*; do printf '%s ' $t; awk '/^(voluntary|nonvoluntary)_ctxt/ {printf "%s ", $0}' $t/status; echo; done   # who stopped (Ch. 99)
   perf record -e mem:0x$ADDR:w -p $PID -- sleep 60        # hardware data breakpoint on a LIVE pid: the next writer's IP (Ch. 5 §5.4.5)
   perf script -F tid,ip,sym,srcline                       # … and who it was
   # --- latency-spike forensics (Ch. 98) ---
   echo x86-tsc > /sys/kernel/tracing/trace_clock          # make the kernel ring comparable to rdtsc stamps (BEFORE the incident)
   echo 0 > /sys/kernel/tracing/tracing_on; cat /sys/kernel/tracing/per_cpu/cpu5/trace | tail -200   # freeze and read the hot core's ring
   perf record -C 5 -e cycles:u -c 2000000 --call-graph fp -k CLOCK_MONOTONIC --overwrite --switch-output=signal -o perf.data -- sleep 36000 &
   kill -USR2 %1                                           # dump the last few seconds of the hot core's samples to perf.data.<ts> (Ch. 98 §98.4.3)
   perf record -e intel_pt/cyc/u -C 5 --snapshot=e -k CLOCK_MONOTONIC -o pt.data -- sleep 36000 &   # Intel PT snapshot mode
   kill -USR2 %2; perf script -i pt.data --itrace=i0ns -F time,ip,sym,srcline --time $M0,$M1   # decode the window ELSEWHERE (Ch. 2 §2.4.4)
   perf script -i perf.data --ns -F time,cpu,comm,ip,sym --time $M0,$M1   # the samples inside a CLOCK_MONOTONIC window
   journalctl -k -o short-precise --since=@$T --until=@$((T+1))   # the kernel log second that contains the window
   dmesg --time-format=iso | tail                           # µs-resolution BOOTTIME-based stamps (not -T, which rounds to the second)
   rdmsr -p 5 0x34                                         # SMI_COUNT on CPU 5 — from a housekeeping core's shell only (Ch. 99 §99.4.2)
   # --- the incident bundle (Appendix N) ---
   incident-collect.sh $CORR $T0 $T1 $PID perf.data.*     # bounded collector: provenance, clocks, rings, host/net windows, capture slice
   perf archive perf.data.2026…                            # the symbols a perf.data needs to be read on another box
   sha256sum -c sha256sums.txt                             # the bundle is intact
```

**Interpreting:** the crash record (Ch. 93 §93.4.1) usually names the bug before the core is opened — `si_code`, `si_addr` and the fault error code against the registers; `addr2line` on `frame0` is the second step, the core the third. For a live process the rule is *never an all-stop attach to a process with a hot thread*: `set non-stop on` before `attach`, and interrupt only the housekeeping thread you need (Ch. 5 §5.4.4 measured the all-stop attach at a quarter of a second on the hot thread); `process_vm_readv`/`/proc/PID/mem` of a seqlock-versioned snapshot costs the target nothing. For a spike, the order is Ch. 98 §98.4.4's: the ftrace ring in the window first (did the thread leave the CPU? an IRQ? a shootdown? an empty gap?), the host and network samples second, `perf`/PT decode last and never on the trading host.

## I.11 Other toolchains: asm, runtime logs and the C-ABI waist

The "verify the codegen" step and the runtime's own diagnostics for the languages of Part XII (Ch. 79–83), plus the one-liners for a C ABI boundary (Ch. 81).

```
   # --- inline / hand-written asm (Ch. 79)
   g++ -O2 -S -masm=intel x.cpp -o - | grep -n -A12 '#APP'    # GCC brackets every asm statement with #APP/#NO_APP: read what is live around it
   nasm -f elf64 f.asm -o f.o && objdump -d -M intel f.o        # assemble + check a hand-written function; readelf -S f.o | grep -i gnu-stack (no exec stack)
   timeout 5 ./prog; echo "exit=$?"                              # 124 = the loop never ended: an undeclared clobber (Ch. 79 §79.6)
   # --- Rust (Ch. 80)
   rustc --edition 2021 -C opt-level=3 -C target-cpu=native -C panic=abort --emit asm k.rs -o k.s   # whole-crate asm (AT&T); add --crate-type cdylib + #[no_mangle] for readable symbols
   cargo asm --rust crate::module::function                      # cargo-show-asm: one function, source interleaved (cargo install cargo-show-asm)
   grep -c 'panic_bounds_check\|call' k.s                       # bounds checks and calls left in a hot body (0 is the target — Ch. 80 §80.5)
   cargo rustc --release -- -C target-cpu=native -C force-frame-pointers=yes   # frame pointers for perf/the Ch. 24 unwinders
   readelf -SW target/release/app | grep -E 'eh_frame|gcc_except'   # unwind-table sizes: panic=abort alone barely moves them (Ch. 80 §80.3)
   # --- Zig (Ch. 81, 83)
   zig build-obj -O ReleaseFast -fomit-frame-pointer -femit-asm=k.s k.zig     # asm; zig translate-c api.h > api.zig (C header → Zig externs)
   zig cc -target aarch64-linux-gnu -O2 -c api.c                 # the C side of the waist, cross-compiled (Appendix A)
   # --- JVM (Ch. 82)
   java -Xms2g -Xmx2g -XX:+AlwaysPreTouch -Xlog:gc*,safepoint:file=gc.log App         # pauses AND time-to-safepoint; -Xlog:gc+phases for ZGC
   java -XX:+PrintCompilation App | grep -E 'Hot::method|made not entrant'                # tiers (3 = C1, 4 = C2), % = OSR, deoptimisations
   java -XX:+UnlockDiagnosticVMOptions -XX:+PrintAssembly -XX:CompileCommand=print,Hot::method App   # needs the hsdis plugin; or feed -XX:+LogCompilation to JITWatch
   java -XX:+UnlockExperimentalVMOptions -XX:+UseEpsilonGC -Xms4g -Xmx4g -XX:+AlwaysPreTouch App    # prove a session is garbage-free: OOM = a failed test
   # --- .NET (Ch. 82)
   DOTNET_gcServer=1 DOTNET_GCgen0size=0x4000000 dotnet app.dll          # server GC, 64 MiB gen0; DOTNET_GCHeapAffinitizeMask to keep GC threads off the hot core
   DOTNET_JitDisasm=Apply DOTNET_JitStdOutFile=jit.txt dotnet app.dll    # tier-0 and tier-1 asm of one method; DOTNET_TieredCompilation=0 for the final tier only
   dotnet publish -c Release -p:PublishReadyToRun=true      # precompiled starting tier;  -p:PublishAot=true for NativeAOT (no JIT, no warm-up curve)
   dotnet build-server shutdown                             # before benchmarking anything else on the box (Ch. 83 §83.2.3)
   # --- Go (Ch. 82)
   GODEBUG=gctrace=1 ./app                                  # one line per GC cycle: STW/concurrent phase times, heap before→after→live
   GOGC=off GOMEMLIMIT=4GiB ./app                           # collect only at the limit (then runtime.GC() at a quiet moment)
   go build -gcflags='-m -m' ./... 2>&1 | grep 'escapes'    # every heap escape in the hot loop;  -gcflags=-d=ssa/check_bce/debug=1 lists surviving bounds checks
   go tool objdump -s 'main\.\(\*Book\)\.apply' ./app      # disassemble one method of the shipped binary
   # --- OCaml (Ch. 82)
   ocamlopt -O3 -unboxed-types -S k.ml && grep -c 'caml_alloc\|caml_call_gc' k.s   # allocation sites in the hot function (-O3 needs a flambda switch)
   OCAMLRUNPARAM='s=4M,v=0x400' ./app                       # minor heap 4M words; v=0x400 prints GC statistics at exit
   # --- the C-ABI waist (Ch. 81)
   nm -D --defined-only libbook.so                          # exactly the exported waist, nothing else (-fvisibility=hidden + explicit default)
   abidiff libbook-1.0.so libbook-1.1.so                    # libabigail: did the ABI change? (CI gate)
   python3 -c 'import ctypes; L=ctypes.CDLL("./libbook.so"); print(L.book_new())'   # the cheapest smoke test of the waist from the research tier
```

**Interpreting:** every toolchain can show you its machine code; the questions are the same ones Ch. 4 asks of C++ — is the hot method one block of loads and arithmetic, with no call to an allocator, no barrier, no type check, no bounds-check branch to a panic — and for the managed runtimes, the runtime's own log is the second instrument beside the hot thread's clock (Ch. 82 §82.3): read time-to-safepoint and deoptimisations, not only pause time.

## I.12 Build, toolchain, attributes, statics & TLS

Profiling and policing the build (Ch. 25), checking that an attribute fired (Appendix P), and finding the hidden runtime in statics and `thread_local` (Ch. 21 §21.4.6).

```
   clang++ -ftime-trace -c hot.cpp -o hot.o                 # per-TU build profile → hot.json (headers, instantiations, passes; Ch. 25 §25.4.1)
   ClangBuildAnalyzer --all build/ out.bin && ClangBuildAnalyzer --analyze out.bin   # aggregate: most expensive headers/templates across a build
   g++ -ftime-report -c hot.cpp                              # GCC per-pass times (no per-header view)
   clang++ -std=c++23 --precompile book.cppm -o book.pcm && clang++ -std=c++23 -fmodule-file=book=book.pcm -c tu.cpp   # named module, Clang 18 (Ch. 25 §25.4.4)
   cmake -G Ninja -B build -DCMAKE_CXX_COMPILER=clang++-18 -DCMAKE_CXX_COMPILER_CLANG_SCAN_DEPS=$(which clang-scan-deps-18)   # CMake 3.28 module scanning
   SOURCE_DATE_EPOCH=1700000000 g++ -O2 -ffile-prefix-map=$PWD=/src -frandom-seed=x.cpp -Wl,--build-id=sha1 x.cpp -o x && cmp x x.rebuilt   # reproducible? (Ch. 25 §25.4.6)
   readelf -p .GCC.command.line hot.o                        # the flags THIS object was built with (-frecord-gcc-switches; Ch. 25 §25.4.5)
   llvm-dwarfdump --debug-info hot.o | grep -m1 DW_AT_producer   # same, Clang (DWARF producer string)
   readelf -n ./app | grep -A1 'Build ID'                    # content-derived build ID (Ch. 25, 93)
   # attributes & UB licences (Appendix P):
   clang++ -O2 -Rpass=inline -Rpass-missed=inline -Rpass-missed=loop-vectorize -c hot.cpp     # what inlined / vectorised, and why not
   g++ -O2 -fopt-info-vec-all -fopt-info-inline-optimized -c hot.cpp
   readelf -S hot.o | grep -E 'text\.(hot|unlikely)'          # hot/cold placement ([[gnu::hot]]/[[gnu::cold]], [[likely]]; Ch. 12)
   nm hot.o | grep -E '\.resolver|\.avx2|\.avx512'             # target_clones bodies + ifunc resolver
   objdump -d -M intel hot.o | grep -cE 'cmpxchg16b|__atomic_compare_exchange'   # inline DWCAS vs libatomic call (Ch. 33 §33.4.3)
   g++ -O2 -fwrapv -S -o wrap.s hot.cpp && g++ -O2 -S -o nowrap.s hot.cpp && diff wrap.s nowrap.s   # what the overflow licence bought (App. P.3)
   # statics & TLS (Ch. 21 §21.4.6):
   nm -C hot.o | grep 'guard variable'                       # function-local statics with a __cxa_guard check on every call
   objdump -d libstrategy.so | grep -c __tls_get_addr         # general-dynamic TLS accesses (should be 0 on a hot .so; -ftls-model=initial-exec)
   readelf -d libstrategy.so | grep -i tls; readelf -l libstrategy.so | grep TLS   # TLS segment size (static TLS budget for dlopen'd objects)
   objdump -s -j .init_array ./app                           # dynamic-initialiser order (init_priority; link order)
```

**Interpreting:** the `-ftime-trace` `Source` events are per-header parse cost *including* nested includes — the top one is usually a convenience include in a shared header (Ch. 25 §25.3 found `<chrono>` at 566 ms/TU); a difference in `.GCC.command.line` across objects in a meaning-class flag (`-fwrapv`, `-fno-strict-aliasing`, `-fno-exceptions`, sanitizers, an ISA-dependent `-march`) is an ODR hazard the linker will not report; a non-empty `guard variable` list or `__tls_get_addr` count on a hot object is a per-call cost the chapter measured (0.2 ns and a branch; 1.3–3 ns and a first-touch `malloc`).

## I.13 References

- The **`perf`** documentation — `perf-stat`, `perf-record`, `perf-report`, `perf-annotate`, `perf-c2c`, `perf-mem` man pages, and the kernel `tools/perf` docs (I.1–I.3).
- **Brendan Gregg's** `perf` examples and *BPF Performance Tools* / `bpftrace` reference (I.5), and the `bcc`/`bpftrace` repositories.
- **Intel VTune** documentation and the *perfmon* event reference (I.2, I.4); Andi Kleen's **pmu-tools/toplev** (I.2).
- The `hwloc`/`lstopo`, `numactl`, `ethtool`, `pahole` (dwarves), `chrt`, `taskset`, `objdump`, and `llvm-bolt`/`perf2bolt` man pages and docs (I.6–I.9).
- `core(5)`, `coredumpctl(1)`, `eu-stack(1)`, `eu-unstrip(1)`, `addr2line(1)`, the gdb manual's *Non-Stop Mode* and *Debugging Programs with Multiple Threads* chapters, `perf-record(1)` (`--overwrite`, `--switch-output`, `--snapshot`), `perf-intel-pt(1)`, and `Documentation/trace/ftrace.rst` (I.10).

## I.14 Additional Reading

- **Appendix C** (System Tuning Checklist) — where many of these commands are applied as *configuration* (IRQ pinning, ring sizing, governor) rather than one-off *observation*.
- **Appendix D** (Compiler Flag Reference) — the build flags behind I.9 (`-fno-omit-frame-pointer`, LTO/PGO/BOLT, sanitizers).
- **Appendix E** (Latency Numbers) — the reference costs these tools let you measure against; **Appendix L** (Benchmark Harness) — how to wrap a measurement so `perf stat` and the histogram agree.
- Gregg, *Systems Performance* and *BPF Performance Tools* (Ch. 2, 69) — the book-length treatment of everything here.

---

*Next: Appendix J — HFT Market-Structure & Protocol Primer, a domain onramp for the strong-C++-but-new-to-trading reader: matching engines and price-time priority, the order book, the order lifecycle, market-data feeds, and a field-level quick reference for ITCH/OUCH/FIX/SBE/FAST.*
