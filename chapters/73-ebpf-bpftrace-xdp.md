# Part X — Kernel Bypass, RDMA & Transport

# Chapter 73 — eBPF, bpftrace & XDP / AF_XDP

> **Prerequisites:** Ch. 2 (profiling / the PMU — eBPF is the programmable extension), Ch. 48 (syscalls — what you trace), Ch. 57–59 (the kernel I/O path XDP sits in front of), Ch. 67 (NIC RX — XDP is the earliest software hook), Ch. 74-preview (kernel bypass — AF_XDP is the middle ground before it), Ch. 91 (privilege reduction — who may load eBPF, §91.4.6).
>
> **Leads into:** Ch. 74 (full kernel bypass — AF_XDP is the step before DPDK/ef_vi), Ch. 108 (production profiling — bpftrace as the continuous-observability tool), Ch. 82 (SmartNICs — eBPF/XDP offload to the NIC). Two distinct topics: eBPF *observability* (the production tracing tool) and XDP/AF_XDP *fast packet paths*.

---

## 73.1 Why it matters: low-overhead production tracing and fast packet paths

eBPF (extended Berkeley Packet Filter) is the most important Linux infrastructure advance of the last decade, and it serves *two* distinct purposes for low-latency systems — both worth this chapter. The first is **observability**: eBPF lets you attach small, *verified-safe* programs to almost any point in the kernel or your application (a syscall, a function entry, a tracepoint, a user-space probe) to measure latency, count events, and capture data **with very low overhead, in production, without restarting or recompiling anything**. This is the tool for answering "*why* is the p99.9 spiking *right now, on the live system*" — the questions a sampling profiler (Ch. 2) in a test environment can't, because the problem only happens in production under real load. `bpftrace` (the high-level front-end) makes this a one-liner: trace every syscall's latency, histogram a function's duration, catch the rare slow path — live, safely, cheaply.

The second purpose is **fast packet paths**: **XDP** (eXpress Data Path) runs an eBPF program in the NIC driver at the *earliest possible software hook* — *before* the kernel allocates a socket buffer, before the network stack (Ch. 57) — so you can **drop, redirect, or filter packets at the very point they arrive** at line rate, with minimal CPU. And **AF_XDP** (Address Family XDP) is a socket type that uses XDP to deliver packets **zero-copy** into user space, bypassing most of the kernel stack — a **middle ground between the kernel stack (Ch. 57) and full kernel bypass (Ch. 74)**: faster than `epoll`/io_uring, not quite as fast as ef_vi/DPDK, but *standard Linux* (no proprietary driver, works with the regular networking tooling). For a trading system, XDP is useful for *filtering* (drop everything that isn't your market-data multicast before it costs the stack anything) and AF_XDP is a viable fast receive path that's more portable than vendor bypass.

The framing that matters: **eBPF/XDP belongs *near* the hot path, not *on* it.** The observability use is *off* the hot path (you trace the system to understand it, you don't run eBPF *in* the tick-to-trade loop — even low-overhead tracing adds *some* cost, and you don't want it on the critical path — §73.5). The XDP/AF_XDP packet path is a *receive* mechanism that sits in front of (or instead of) the kernel stack — useful, but for the absolute lowest latency, full kernel bypass (Ch. 74, ef_vi/DPDK) still wins. So eBPF/XDP is the **programmable, safe, standard-Linux** option: superb for production observability (the bpftrace one-liner that finds your jitter source), good for packet filtering and a portable fast path (XDP/AF_XDP), and explicitly *not* a replacement for bypass on the ultra-hot path. This chapter explains the eBPF VM/verifier mental model (§73.2), measures probe overhead (§73.3), details the tracing tools and XDP/AF_XDP (§73.4), and warns about the verifier's limits and tracing too close to the hot path (§73.5).

## 73.2 Mental model: the eBPF VM and verifier

**eBPF — safe, verified programs running in kernel context.** eBPF lets you load a small program into the *kernel* that runs when a hook fires (a syscall, a function, a packet arrival) — but, crucially, **safely**: the program can't crash the kernel, loop forever, or access arbitrary memory, because it's *verified* before loading and runs in a restricted VM:

```
   your eBPF program (C / bpftrace) → compiled to eBPF bytecode → loaded via bpf() syscall
        │
        ▼  the VERIFIER checks: no unbounded loops, no out-of-bounds memory, bounded stack,
        │  no uninitialized reads, terminates — REJECTS anything unsafe (no kernel crash possible)
        ▼
   JIT-compiled to native code → attached to a HOOK → runs when the hook fires (low overhead)
        │
        ▼  communicates results to user space via MAPS (shared key-value/array/ring-buffer structures)
```

- **The verifier — safety by static analysis.** Before loading, the verifier proves the program is safe: it **terminates** (no unbounded loops — historically *no* loops, now bounded loops), accesses only **valid memory** (bounded pointers, checked map accesses), has a **bounded stack** (512 bytes), and reads no uninitialized data. If it can't prove safety, it **rejects** the program. This is why eBPF can run in the kernel in production without risk — but it also *constrains* what you can write (§73.5).
- **Hooks — where eBPF attaches.** kprobes/kretprobes (any kernel function entry/return), uprobes/uretprobes (any *user-space* function — your trading app!), tracepoints (stable kernel instrumentation points), USDT (user statically-defined tracepoints — markers you put in your app), perf events (PMU — Ch. 2), and **XDP** (NIC driver, §73.4.2) / tc (traffic control). You can measure latency around almost anything.
- **Maps — the communication channel.** eBPF programs are stateless per-invocation but share **maps** (hash maps, arrays, per-CPU maps, ring buffers) with each other and user space — to accumulate histograms, pass events up, hold config. A latency histogram is a map the eBPF program updates and `bpftrace`/user-space reads.
- **`bpftrace` — the high-level front-end.** Instead of writing eBPF C + loader, `bpftrace` is an awk-like language for one-liners: `bpftrace -e 'tracepoint:syscalls:sys_enter_read { @[comm] = count(); }'`. It compiles to eBPF, handles maps/output, and makes ad-hoc production tracing trivial. **BCC** is the Python/C library for more complex tools (the `bcc-tools` suite: `biolatency`, `runqlat`, `funclatency`, etc.).

**XDP and AF_XDP — eBPF on the packet path:**

- **XDP** runs an eBPF program **in the NIC driver**, on the raw packet, *before* the kernel allocates an `sk_buff` or runs the stack (Ch. 57) — the earliest software hook. It returns an action: `XDP_DROP` (discard — at line rate, minimal CPU), `XDP_PASS` (continue to the stack), `XDP_TX` (send back out), `XDP_REDIRECT` (to another NIC or an AF_XDP socket). Used for line-rate *filtering*/DDoS-drop/load-balancing.
- **AF_XDP** is a socket that uses XDP to deliver packets **zero-copy** into a user-space ring (UMEM) — bypassing most of the stack. A middle ground (§73.1): faster than the kernel stack, standard Linux, but not quite ef_vi/DPDK (Ch. 74).

The model: **eBPF runs verified-safe, JIT'd programs at kernel/app hooks (kprobes/uprobes/tracepoints/USDT/XDP), communicating via maps — enabling low-overhead production observability (`bpftrace`/BCC) and, via XDP, line-rate in-driver packet filtering/redirect and (AF_XDP) zero-copy user-space receive. Safe and standard, but constrained by the verifier — a tool to use *near* the hot path (observe it, filter for it), not *on* it.**

## 73.3 Measure it: probe overhead

The key question for production use: **how much does a probe cost?** Measure the overhead a kprobe/uprobe adds to the traced function — because that determines whether you can run it on (near) a latency path. And measure what bpftrace *reveals* (the latency histogram it produces).

```
   # bpftrace one-liners (the observability payoff):
   $ bpftrace -e 'kprobe:vfs_read { @s[tid] = nsecs; } kretprobe:vfs_read /@s[tid]/ { @ = hist(nsecs - @s[tid]); delete(@s[tid]); }'   # read latency histogram (entry→return delta)
   $ bpftrace -e 'tracepoint:syscalls:sys_enter_* { @[probe] = count(); }'  # syscall counts (Ch.48!)
   $ bpftrace -e 'uprobe:/path/app:process_message { @start[tid] = nsecs; }
                  uretprobe:/path/app:process_message /@start[tid]/
                    { @lat = hist(nsecs - @start[tid]); }'                # YOUR function's latency dist

   # measure the probe's OWN overhead: time the function with and without the probe attached.
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), Linux 6.18 with BTF, bpftrace 0.2x; the uprobe/USDT figures are the pre-6.17 `int3` path (the 6.17+ `nop5`/uprobe-syscall optimization roughly halves them), illustrative:

```
   probe type                        per-event overhead     note
   tracepoint                        ~tens of ns            cheapest; stable; preferred
   kprobe (kernel function)          ~tens-hundreds of ns   dynamic; slightly more than tracepoint
   uprobe (user function)            ~hundreds of ns - µs    a TRAP into the kernel per hit — EXPENSIVE
   USDT (user static tracepoint)     0 untraced / uprobe when traced   a nop until attached; then it IS a uprobe (int3)

   bpftrace REVEALS (e.g. read latency histogram):
     @[ns]:  [1K, 2K)  ████████  ...  [256K, 512K)  ▏  ← the rare slow tail you couldn't see otherwise

   the lesson: tracepoints are cheap (use freely); UPROBES — and USDT markers once traced — trap into the kernel per hit —
   do NOT put a uprobe on a hot-path function called millions of times/sec (§73.5).
```

Read it: eBPF observability's value is that it gives you **production latency distributions** (the histogram — the p99.9 tail you can't see from averages, Ch. 1) for *any* function, *live*, without recompiling — `bpftrace` one-liners answer "what's slow right now" in seconds. But the **overhead varies by probe type by orders of magnitude**: **tracepoints** (~tens of ns, stable kernel instrumentation) are *cheap* — fine to run in production broadly. **kprobes** are dynamic and slightly more. **uprobes** (probing a user-space function) are *expensive* — each hit is a **trap into the kernel** (~hundreds of ns to µs; Linux 6.17's uprobe-syscall optimization roughly halves it), and an **enabled USDT marker *is* a uprobe** (the marker's `nop` is patched to `int3` when you attach): USDT's advantages are zero cost while *untraced*, stable typed probe points, and that *you* choose where it sits — at a boundary hit thousands, not millions, of times per second — not a cheaper trap. So a uprobe (or traced USDT) on a function called *millions of times per second on the hot path* adds enormous overhead and *changes the latency you're trying to measure* (the observer effect — §73.5). The discipline: **use cheap tracepoints freely for production observability; place USDT markers only at boundaries whose hit rate you have bounded; use uprobes sparingly and never on the hottest functions; and never run tracing *in* the tick-to-trade loop** — trace *near* the hot path to understand it, off the critical path. The right model: instrument with **USDT markers** at key boundaries (you control where; free until traced), use **tracepoints/kprobes** for kernel-side latency (syscalls — Ch. 48, scheduling, block I/O), and reach for a uprobe only for ad-hoc debugging of a *non*-hottest function.

## 73.4 Techniques

### 73.4.1 kprobes/uprobes/tracepoints/USDT; `bpftrace` one-liners

Production observability — the eBPF tracing toolkit:

- **`bpftrace` one-liners for ad-hoc investigation.** The go-to for "what's happening right now": histogram a latency (`hist(nsecs - @start[tid])` — a delta against a stored entry time; `hist(nsecs)` alone histograms *timestamps*), count events by process/function, catch a rare condition (`/lat > 100000/`), trace a syscall (Ch. 48). A few examples that matter for HFT debugging:
  - **Syscall audit** (Ch. 48): `tracepoint:raw_syscalls:sys_enter { @[comm,...] = count(); }` — find the hidden syscalls on a "syscall-free" hot path (Ch. 48's hunt).
  - **Scheduling latency** (Ch. 48, 53): `runqlat` (BCC) — how long threads wait to run (preemption/contention).
  - **Block I/O latency** (Ch. 98): `biolatency` — disk write latency for the journaler.
  - **Your function's latency**: uprobe/uretprobe (or USDT) on `process_message` → histogram.
- **Tracepoints — the preferred kernel hook.** Stable (don't change across kernel versions), cheap (§73.3), well-documented (`/sys/kernel/debug/tracing/events`). Prefer them over kprobes when a tracepoint exists for what you want (syscalls, scheduling, networking, block I/O).
- **USDT — instrument your *own* app at chosen boundaries.** Place **static tracepoints** (USDT markers — `<sys/sdt.h>` `DTRACE_PROBEn`) at key points in your trading app (message received, decision made, order sent) — a single `nop`, zero cost while untraced, and *you* control where they are (at boundaries, not in the hottest inner loop). Then trace them with bpftrace/BCC in production, remembering that an attached USDT marker *is* a uprobe (§73.3): the win over an ad-hoc uprobe is stable, typed probe points at sites whose hit rate you have bounded, not a cheaper trap. The whole mechanism in one file:

```cpp
// usdt.cpp — a USDT marker at a boundary of the trading app (NOT in the inner loop).
// Build: g++ -O2 -g -std=c++20 usdt.cpp -o app          (needs systemtap-sdt-dev for <sys/sdt.h>)
#include <sys/sdt.h>
#include <cstdint>

void on_message(std::uint64_t seq, std::uint64_t rx_ns) {
    // ... decode (Ch. 64), update the book (Ch. 30) ...
    DTRACE_PROBE2(hft, msg_decoded, seq, rx_ns);   // one `nop` until traced; once attached it IS a uprobe (§73.3)
}
// $ bpftrace -l 'usdt:./app:*'                                        # list the markers the binary carries
// $ bpftrace -e 'usdt:./app:hft:msg_decoded { @rx_to_decoded = hist(nsecs - arg1); }'   # wire-RX -> decoded latency
```

- **kprobes/uprobes — dynamic, for anything without a tracepoint.** kprobes attach to *any* kernel function (powerful, slightly more overhead); uprobes to *any* user function (expensive — §73.3, §73.5). Use for ad-hoc debugging where no tracepoint/USDT exists; mind the cost.
- **BCC tools and continuous profiling (Ch. 108).** The `bcc-tools`/`bpftrace` tool suite (`funclatency`, `offcputime`, `runqlat`, `biolatency`, `tcplife`, ...) are ready-made; and eBPF underlies **continuous profilers** (Parca, Pyroscope) that profile production with low overhead (Ch. 108). Build eBPF observability into production monitoring.

### 73.4.2 XDP for in-driver drop/redirect/filter

Line-rate packet processing at the earliest hook:

- **The earliest software hook (Ch. 57, 67).** XDP runs in the NIC driver on the raw packet, *before* the kernel allocates an `sk_buff` or runs the stack — so processing a packet here is *far* cheaper than letting it traverse the stack. Actions: `XDP_DROP` (discard at line rate), `XDP_PASS`, `XDP_TX`, `XDP_REDIRECT`.
- **Filtering — drop unwanted packets before they cost anything.** For a market-data box, drop everything that *isn't* your subscribed multicast (or is malformed) at XDP — so the stack never sees it, saving CPU and reducing jitter on the hot core. DDoS mitigation and ACLs are the classic XDP uses; for HFT it's "only let the feed I want reach the stack/decoder."
- **Redirect to an AF_XDP socket (§73.4.3) or another NIC.** `XDP_REDIRECT` steers selected packets into an AF_XDP user-space ring (the zero-copy fast path) or out another interface (load-balancing/forwarding) — at the driver level.
- **Offload to the NIC (Ch. 82).** Some SmartNICs (Ch. 82) can run the XDP program *on the NIC hardware* (offloaded XDP) — processing packets without the host CPU at all. The most extreme XDP placement (ties Ch. 82).
- **Where XDP fits (vs bypass — Ch. 74).** XDP is *in the kernel driver* (standard Linux, works with normal tooling) — great for filtering/redirect at line rate. For the *decoder's* receive path needing lowest latency, AF_XDP (§73.4.3) or full bypass (Ch. 74) delivers the packets to user space; XDP is the filter/steer in front.

### 73.4.3 AF_XDP zero-copy sockets

The standard-Linux fast receive path — the middle ground before bypass:

- **Zero-copy to user space via XDP.** An AF_XDP socket sets up a shared memory region (**UMEM**) of packet buffers and rings (fill/completion/RX/TX); an XDP program `XDP_REDIRECT`s packets into the socket's RX ring, and the application reads them **zero-copy** (the packet DMA's into the UMEM buffer the app reads — no kernel copy). Bypasses most of the stack while staying standard Linux.
- **The latency tier (§73.1, Ch. 74).** AF_XDP is **faster than the kernel stack** (epoll/io_uring — Ch. 57–59, no stack traversal, zero-copy) and **slower than full bypass** (ef_vi/DPDK — Ch. 74, which takes the flow — or with VFIO-bound DPDK the whole NIC — away from the kernel) — but it's **standard Linux** (no proprietary driver, the NIC stays usable by the normal stack for other traffic, normal tooling works). A pragmatic fast path when you don't want full bypass's lock-in/complexity.
- **Driver support and modes.** Three tiers, not two: **generic (SKB-mode) XDP** works on any driver and is the slowest; **native (driver-mode) XDP** runs the program in the driver's poll loop but by itself gives AF_XDP only *copy* mode (`XDP_COPY`); **AF_XDP zero-copy** needs the driver to implement the ZC hooks (`XDP_ZEROCOPY` — i40e/ice/ixgbe/mlx5 do; check `NETDEV_XDP_ACT_XSK_ZEROCOPY` in `ip link show`/`bpftool net`, and bind with `XDP_ZEROCOPY`, falling back to `XDP_COPY`). Busy-poll the AF_XDP rings (Ch. 48) on a dedicated core for lowest latency.
- **Use it as the decoder's receive path.** The feed handler (Ch. 64) can receive via AF_XDP: XDP filters to the subscribed multicast and redirects into the AF_XDP UMEM; the decoder overlays its structs (Ch. 64) on the zero-copy buffers and busy-polls the RX ring. A portable alternative to vendor bypass (Ch. 74) for the receive path.
- **vs DPDK/ef_vi (Ch. 74).** AF_XDP: standard Linux, NIC shared with the kernel, good latency. DPDK with UIO/VFIO-bound PMDs (Intel-class NICs): the device leaves the kernel entirely. ef_vi/Onload, libexanic and bifurcated DPDK (mlx5): the kernel driver stays loaded and hardware filters steer only the chosen flows to user space — lowest latency either way, vendor/driver-specific, more setup. The choice is latency vs portability/standardness (Ch. 74's decision).

## 73.5 Pitfalls & anti-patterns: verifier limits; tracing overhead near the hot path

- **Tracing *on* the hot path (the observer effect).** Putting a probe — especially a **uprobe** (a kernel trap per hit — §73.3) — on a function called millions of times/sec *in the tick-to-trade loop* adds large overhead and **changes the latency you're measuring** (and steals cycles from the hot core — Ch. 48). Trace *near* the hot path (USDT at boundaries, kernel-side tracepoints), **never in** the critical inner loop. eBPF observability is for understanding the system, not running in it.
- **uprobe overhead underestimated.** uprobes are *expensive* (trap into kernel — §73.3) vs cheap kernel tracepoints — and a traced USDT marker pays the same trap. A uprobe (or USDT) on a hot function can dominate. Prefer tracepoints, or **USDT** markers at low-rate boundaries; reserve uprobes for ad-hoc, non-hottest debugging.
- **Verifier rejection / fighting the verifier.** The verifier *rejects* programs it can't prove safe (unbounded loops, unbounded memory access, too-complex, too-large) — so eBPF programs are *constrained* (bounded loops, 512-byte stack, limited complexity). Complex logic may not pass; you restructure or move it to user space. Know the limits before writing a big eBPF program (§73.2).
- **eBPF security surface (Ch. 91).** eBPF is powerful (kernel code execution) and has been a **CVE source** (verifier bugs, privilege escalation); loading eBPF needs privilege (`CAP_BPF`/root), and on a security-sensitive box you control who can load programs. The verifier makes *correct* programs safe, but eBPF itself is an attack surface to manage (Ch. 91). Lock down eBPF loading.
- **XDP not a full-bypass replacement (Ch. 74).** XDP is in-kernel-driver (great for filtering/redirect); AF_XDP is a good fast path but not the lowest latency. For the ultra-hot tick-to-trade receive, **full bypass** (ef_vi/DPDK — Ch. 74) still wins. Use XDP/AF_XDP for filtering and a portable fast path; bypass for the floor.
- **AF_XDP without zero-copy driver support.** AF_XDP in *copy* mode is much slower — defeating the point — and native-XDP support alone does *not* imply it: the driver must implement the AF_XDP zero-copy hooks (`XDP_ZEROCOPY`, §73.4.3). Verify with `bpftool net`/the `xsk` bind flags before relying on AF_XDP latency.
- **Kernel-version dependence.** eBPF/XDP/AF_XDP features evolve fast and require recent kernels (and the feature/helper availability varies). Check kernel version and `bpftool feature`; an eBPF program/feature on an old kernel may not load. (HFT boxes sometimes run older "stable" kernels — verify support.)
- **Trusting a histogram without enough samples / context.** A bpftrace histogram with few samples, or that perturbs the system (observer effect), can mislead. Gather enough samples, account for the probe's own cost, and correlate with other signals (Ch. 2, 108). 
- **Leaving heavy tracing running in production.** Even cheap tracing has a cost; leaving broad/heavy eBPF tracing on continuously taxes the system. Use targeted, time-boxed tracing for investigation; for *continuous* observability use the low-overhead tools/sampling deliberately (Ch. 108).

## 73.6 Exercises & checklist

**Exercises**

1. **Find the hidden syscall (ties Ch. 48).** On a "syscall-free" hot path, run `bpftrace -e 'tracepoint:raw_syscalls:sys_enter /comm=="myapp"/ { @[args.id] = count(); }'` (bpftrace ≥ 0.21; `args->id` on older versions; translate ids with `ausyscall --dump`). Find any syscalls on the steady-state loop; eliminate them (Ch. 48). 
2. **Probe overhead.** Attach a uprobe vs a USDT marker vs a tracepoint to (near) a function; measure the per-hit overhead by timing the function with/without the probe. Confirm uprobe ≈ traced USDT ≫ tracepoint, and that an *untraced* USDT marker costs nothing (§73.3). Why is a uprobe expensive?
3. **Latency histogram.** Use `bpftrace` (or `funclatency`) to histogram your `process_message` latency in a running system; identify the tail (Ch. 1). Compare to what an in-process histogram (Ch. 3) shows — does the eBPF observer perturb it?
4. **XDP filter.** Write an XDP program (or use `xdp-tools`) that `XDP_DROP`s all packets except your multicast group; load it and verify (with traffic) that non-matching packets never reach the socket and the CPU cost drops (§73.4.2).
5. **AF_XDP receive.** Set up an AF_XDP socket (zero-copy if your driver supports it) receiving the feed; busy-poll the RX ring and decode (Ch. 64) from the UMEM buffers. Compare latency to a normal UDP socket (Ch. 57) and (if available) to ef_vi/DPDK (Ch. 74) (§73.4.3).

**Checklist — eBPF, bpftrace & XDP/AF_XDP**

- [ ] eBPF observability is used **near, not on, the hot path** — **USDT markers at boundaries** + **tracepoints/kprobes** for kernel-side latency (Ch. 48); **uprobes used sparingly** (expensive — §73.3) and **never in the critical inner loop** (observer effect — §73.5).
- [ ] `bpftrace`/BCC one-liners are part of the **production-debugging toolkit** (syscall audit, latency histograms, scheduling/block-I/O latency) — answering "what's slow live" (§73.4.1, Ch. 108).
- [ ] **XDP** filters unwanted packets at the driver (`XDP_DROP` non-feed/malformed before the stack — §73.4.2), reducing stack cost and hot-core jitter.
- [ ] **AF_XDP** (zero-copy, driver-supported) is used as a **portable fast receive path** where appropriate — faster than the kernel stack (Ch. 57), standard Linux — with the floor reserved for **full bypass** (ef_vi/DPDK — Ch. 74).
- [ ] eBPF program **verifier constraints** (bounded loops, 512B stack, complexity) are understood; complex logic moves to user space (§73.2, §73.5).
- [ ] eBPF's **security surface** (privileged loading, CVE history) is managed — loading locked down on sensitive boxes (Ch. 91).
- [ ] **Kernel-version / driver support** for eBPF/XDP/AF_XDP (esp. zero-copy) is verified (`bpftool feature`); copy-mode AF_XDP isn't mistaken for the fast path (§73.5).
- [ ] Tracing is **targeted and time-boxed** for investigation; continuous observability uses **low-overhead sampling** deliberately (Ch. 108) — heavy tracing isn't left running.

## 73.7 References

- B. Gregg, *BPF Performance Tools* and *Systems Performance* (2e) — the definitive treatment of eBPF/bpftrace/BCC observability (the basis of §73.3-§73.4.1).
- The `bpftrace`, BCC, and `bpftool` documentation, and the kernel `Documentation/bpf/` — the eBPF VM, verifier, maps, and program types (§73.2).
- The XDP and AF_XDP documentation (kernel `Documentation/networking/af_xdp.rst`, the `xdp-tools`/libxdp/libbpf projects) and the XDP paper (Høiland-Jørgensen et al., *"The eXpress Data Path"*) — in-driver processing and zero-copy sockets (§73.4.2-3).
- The Cilium / eBPF Foundation documentation — production eBPF networking and observability at scale.
- The eBPF security and verifier-CVE discussions — the attack-surface caveat of §73.5 (ties Ch. 91).

## 73.8 Additional Reading

- B. Gregg's blog (brendangregg.com) — bpftrace one-liners, flame graphs, and production tracing case studies.
- The continuous-profiling projects (Parca, Pyroscope, `perf`+eBPF) — low-overhead production profiling (ties Ch. 108).
- Ch. 2 (*Profiling/PMU*) — the measurement foundation eBPF extends; Ch. 48 (*Context Switching*) — the syscalls/scheduling you trace; Ch. 57–59 (*epoll/io_uring*) — the stack XDP fronts; Ch. 67 (*NIC*) — the RX path; Ch. 74 (*Kernel Bypass*) — the lower-latency floor beyond AF_XDP; Ch. 82 (*SmartNICs*) — offloaded XDP/eBPF; Ch. 108 (*Production Profiling*) — continuous observability.
- **Appendix I §I.5** — the bpftrace/eBPF one-liners as a cookbook; **Appendix E** — kernel-stack vs bypass vs FPGA RX numbers (AF_XDP sits between the first two — §74.3).

---

*Next: Ch. 74 — Kernel Bypass & Userspace Networking, the lowest-latency software path and the culmination of the kernel-networking progression: DPDK, poll-mode drivers, hugepage mempools, userspace TCP stacks, and the proprietary vendor APIs (Solarflare ef_vi, ExaNIC libexanic) — removing the kernel from the tick-to-trade fast path entirely.*
