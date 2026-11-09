# Part X — Kernel Bypass, RDMA & Transport

# Chapter 66 — Kernel Bypass & Userspace Networking

> **Prerequisites:** Ch. 50 (the kernel stack this bypasses), Ch. 59 (NIC features / vendor stacks — ef_vi/Onload), Ch. 43, 44, 45, 47 (the dedicated/isolated/busy-polling cores bypass demands), Ch. 15/26 (huge pages / mmap — DPDK mempools), Ch. 56 (the decoder fed by the bypass RX path), Ch. 65 (AF_XDP — the standard-Linux middle ground).
>
> **Leads into:** Ch. 67 (RDMA — one-sided remote memory, the intra-DC extreme), Ch. 73 (FPGA — hardware bypass beyond software), Ch. 91 (the tick-to-trade path this completes). The lowest-latency *software* network path — the culmination of the kernel-networking progression (Ch. 50–65); the floor under everything in Part XI is the PCIe round-trip (Ch. 70).

---

## 66.1 Why it matters: the lowest-latency software path

Every step of the software networking path has built toward this one. Ch. 50 showed the kernel stack is microseconds even busy-polled; Ch. 51 (io_uring) and Ch. 65 (AF_XDP) shaved syscalls and copies; this chapter removes the kernel from the network path **entirely**. **Kernel bypass** means the application maps the NIC's hardware rings *directly* into user space and sends/receives packets with **no kernel, no syscalls, no copies, no interrupts** — the NIC DMA's a packet into a user-space buffer, the application polls a ring and reads it in place (Ch. 56), and an outbound packet goes from a user-space buffer to the wire without ever entering the kernel. This is the **lowest-latency software path** to the network — sub-microsecond RX, and the difference between a competitive tick-to-trade system and an also-ran. For the *ultra-hot* path (the market-data feed in, the order out — the path the whole book optimizes), kernel bypass is not optional; it's the baseline of a serious HFT system.

The reason the kernel stack can't compete is structural, not a tuning problem: a packet through the kernel traverses the driver, the network stack (IP/UDP/TCP processing), socket buffers, and a syscall + copy to user space — *microseconds* of irreducible per-packet work (Ch. 50), plus interrupt latency and the context switch if you block (Ch. 43). Kernel bypass deletes all of it: the application **owns the NIC** (or a queue of it), **polls** the rings (Ch. 43 — no interrupts, no syscalls), and parses **in place** in the DMA buffer (Ch. 56 — no copy). What's left is the hardware: the wire, the NIC, the PCIe transfer (Ch. 70), and your code. That's the ~1 µs (and below) RX latency (Ch. 59.3, 66.3) that bypass delivers — *the* enabling technology for the latency tier HFT operates in.

The cost — and the reason this is a deliberate, advanced choice — is that you give up everything the kernel provided: with UIO/VFIO-bound DPDK PMDs (Intel-class NICs) the **NIC leaves the kernel entirely** (it can't be a normal network interface for other traffic), while with ef_vi/Onload, libexanic and bifurcated DPDK (mlx5) the kernel driver stays loaded and only the flows you steer with hardware filters go to user space; either way you **burn a core** busy-polling it 100% (Ch. 43), you **lose the kernel's networking** (TCP stack, tooling like `tcpdump`/`netstat`, the firewall) and must provide your own (a userspace TCP stack — §66.4.2 — for the order path), and the APIs are low-level and often **vendor-specific** (Solarflare ef_vi, ExaNIC libexanic — §66.4.4) or complex (DPDK — §66.4.1). So bypass is for the **ultra-hot path specifically** — the market-data RX and order TX — while the rest of the system's networking (control, admin, less-critical feeds) stays on the tuned kernel stack (Ch. 50, 54) or AF_XDP (Ch. 65). This chapter explains the poll-mode-driver/userspace-stack mental model (§66.2), measures kernel-stack vs DPDK RX latency (§66.3), details DPDK/mempools, userspace TCP, the tick-to-trade fast path, and the proprietary vendor APIs (§66.4), and warns about the costs — burning cores and losing kernel tooling (§66.5). It's the culmination of the software networking path: the lowest-latency way to move packets in software.

## 66.2 Mental model: poll-mode drivers; userspace stacks

**The kernel stack vs bypass, structurally:**

```
   KERNEL STACK (Ch.50):  wire → NIC → IRQ → driver → IP/UDP/TCP stack → socket buf → syscall+copy → app
                          microseconds; interrupt + context switch + copy per packet

   KERNEL BYPASS:         wire → NIC → DMA into USER-SPACE buffer → app POLLS the ring → parse in place
                          ~1µs; NO kernel, NO syscall, NO interrupt, NO copy
                          the app OWNS the NIC (or a queue); the kernel isn't in the path at all
```

- **Poll-mode driver (PMD).** The defining mechanism: instead of the NIC **interrupting** the CPU when a packet arrives (interrupt latency + handler + possible context switch — Ch. 43), the application **polls** the NIC's RX descriptor ring in a tight loop — checking for new packets and processing them the instant they arrive (Ch. 43's busy-poll, taken to the hardware). No interrupts → no interrupt latency, no jitter, no context switch. The PMD runs the NIC entirely from user space, polling. (This is why bypass *requires* a dedicated, busy-polling core — Ch. 43, 44, 45, 47 — burning it at 100%.)
- **User-space NIC access (mapped rings).** The NIC's RX/TX descriptor rings and packet buffers are **mapped into the application's address space** (via the NIC's user-space driver — DPDK's UIO/VFIO, ef_vi, libexanic). The app posts RX buffers, polls for completions, reads packets in place, and posts TX packets — directly manipulating the hardware rings, no kernel. Packets DMA into huge-page-backed buffers (Ch. 15) the app reads (Ch. 56's zero-copy decode reads the DMA buffer in place).
- **You lose (and must replace) the kernel's stack.** The kernel provided IP/UDP/TCP, ARP, routing, the firewall, and tooling. With bypass you get **raw packets** (Ethernet/IP frames) — for *market data* (UDP multicast — Ch. 54, 56) that's fine (you parse UDP/IP yourself, trivial). For *order entry* (TCP — Ch. 54) you need a **userspace TCP stack** (§66.4.2) — DPDK's or a vendor's or a third-party one — to handle the TCP state machine, because there's no kernel TCP anymore.
- **The bypass options, by abstraction level:**
  - **DPDK (Data Plane Development Kit)** — the vendor-neutral, full-featured bypass framework (PMDs for most NICs, huge-page mempools, a userspace stack ecosystem). Powerful, portable across NICs, complex (§66.4.1).
  - **ef_vi (Solarflare/AMD)** — the raw, lowest-latency layer-2 API for Solarflare NICs (Ch. 59) — minimal, fastest, vendor-specific (§66.4.4).
  - **libexanic (ExaNIC/Cisco)** — the equivalent for ExaNIC, with extreme low latency and precise hardware timestamps, optional FPGA inline (Ch. 73) (§66.4.4).
  - **Onload (Solarflare/AMD)** — *transparent* bypass: accelerates existing **socket** apps via `LD_PRELOAD` (no code change), a userspace stack under the socket API (§66.4.4) — easier, slightly higher latency than raw ef_vi.
  - **AF_XDP (Ch. 65)** — standard-Linux zero-copy, the middle ground (faster than the stack, slower than full bypass, NIC stays shared).

The model: **kernel bypass maps the NIC's rings into user space and uses a poll-mode driver (busy-poll, no interrupts) to send/receive packets with no kernel, no syscalls, no copies — the lowest-latency software path (~1µs). You dedicate the NIC and a core, and replace the kernel's stack (raw packets for UDP market data; a userspace TCP stack for order entry). Options range from vendor-neutral DPDK to raw vendor APIs (ef_vi/libexanic, lowest latency) to transparent Onload — the choice is latency vs portability vs effort.**

## 66.3 Measure it: kernel-stack vs DPDK RX latency

Measure the RX latency floor across the tiers built through the networking chapters — kernel stack (Ch. 50), busy-poll (Ch. 59), AF_XDP (Ch. 65), and full bypass (DPDK/ef_vi) — ideally with hardware timestamps (Ch. 59, 62) for the true wire-to-app number.

```
   # DPDK: build a poll-mode RX loop (rte_eth_rx_burst) on a dedicated isolated core (Ch.44,47);
   #   measure NIC-hw-timestamp (Ch.59) → app-sees-packet time = the wire-to-app RX latency.
   # ef_vi: ef_vi_receive_poll on a dedicated core; lowest-latency raw L2 access (Ch.59.4.3).
   # Compare against the kernel stack (Ch.50) and AF_XDP (Ch.65) on the same NIC/traffic.
```

Representative results (toolchain and machine: Appendix L.3) — HFT-grade NIC (Solarflare/ExaNIC) + dedicated isolated core, hardware timestamps, turbo off (illustrative; the *tiers* are the point):

```
   RX path                              wire-to-app latency (p50 / p99)    CPU       notes
   kernel stack, blocking (Ch.50)       ~5 µs / ~20 µs                     low       syscalls + stack + switch (Ch.43)
   kernel stack, busy-poll (Ch.50,59)   ~3 µs / ~6 µs                      high      no block/wake switch
   AF_XDP zero-copy (Ch.65)             ~2 µs / ~4 µs                      high      standard Linux, near-bypass
   Onload (transparent, LD_PRELOAD)     ~1.2 µs / ~2.5 µs                  100%      socket API kept; EF_POLL_USEC spinning (§66.4.4)
   DPDK poll-mode driver                ~1 µs / ~2 µs                      100%      no kernel; owns the NIC (VFIO) or the flow (mlx5)
   ef_vi (raw vendor, Solarflare)       ~0.8 µs / ~1.3 µs                  100%      lowest software latency
   (context) FPGA inline (Ch.73)        ~tens-hundreds of NS               —         hardware, no software at all

   each tier removes a layer; bypass (DPDK/ef_vi) removes the KERNEL — the big step to sub-µs.
```

Read it: this is the software-networking story in one table — **each tier removes a layer of overhead, and full bypass (DPDK/ef_vi) removes the *kernel itself*, the big step to sub-microsecond.** The kernel stack (Ch. 50), even busy-polled, is ~3-5 µs — the stack traversal + syscalls are irreducible. AF_XDP (Ch. 65) gets close to bypass while staying standard Linux. **DPDK** (poll-mode, no kernel) reaches ~1 µs; **ef_vi** (raw vendor API, minimal abstraction) edges lower (~0.8 µs) — the lowest *software* latency. The cost column tells the trade: bypass pegs a core at 100% (busy-polling — Ch. 43) and dedicates the NIC. The context row points to Part XI: beyond software, **FPGA inline** processing (Ch. 73) does the work *in hardware* (tens-hundreds of *nanoseconds*) — no software at all — the next tier when even ef_vi isn't enough. The measurement discipline (Ch. 59, 62): use **hardware timestamps** for the true wire-to-app number (a software timestamp includes the latency you're measuring), on a **dedicated isolated core** (Ch. 44, 45, 47), and measure under **realistic burst load** (Ch. 56, 87). The conclusion: **for the ultra-hot path, full bypass (ef_vi/DPDK) is the floor of software networking — ~1 µs and below — and it's the HFT baseline.**

## 66.4 Techniques

### 66.4.1 DPDK and hugepage mempools

The vendor-neutral bypass framework:

- **Poll-mode drivers (PMDs).** DPDK provides user-space PMDs for most NICs (Intel, Mellanox/NVIDIA, etc.) — the app calls `rte_eth_rx_burst`/`rte_eth_tx_burst` to receive/send *bursts* of packets by polling, no kernel, no interrupts. The PMD owns the NIC (bound via VFIO/UIO, unbound from the kernel driver). Portable across NICs (the DPDK abstraction) — its key advantage over vendor APIs.
- **Hugepage mempools (Ch. 15, 23, 26).** DPDK pre-allocates packet buffers in **huge-page-backed mempools** (Ch. 15 — fewer TLB misses, no faults — Ch. 23) at startup; RX packets DMA into mempool buffers, the app processes them, and returns them to the pool (no per-packet allocation — Ch. 23–24, the pool pattern). Mempools are per-NUMA-node (Ch. 16) and lockless/per-core where possible (Ch. 31, 34). The pre-allocated, huge-page, NUMA-local, pool discipline of Part IV, applied to packet buffers.
- **Burst processing (Ch. 37).** `rx_burst` returns *many* packets per poll — process them as a batch (Ch. 37's batching) through decode (Ch. 56) → book → strategy. Batching amortizes the poll and per-packet overhead, and self-corrects under burst (Ch. 37.4.2, 56). The DPDK RX loop is a batched poll.
- **The DPDK ecosystem.** Beyond raw packet I/O: userspace TCP stacks (§66.4.2), flow classification, crypto, and the run-to-completion / pipeline models. Rich but complex — DPDK is a *framework*, not just a driver. For pure low-latency RX/TX, a minimal DPDK app (or a raw vendor API — §66.4.4) suffices; the full framework is more than HFT often needs.
- **Lcores and the run-to-completion model.** DPDK pins worker threads (*lcores*) to cores (Ch. 44), each running a poll-process loop — the shared-nothing, pinned, busy-polling model (Ch. 31, 43) the whole book has built toward, here as DPDK's execution model.

### 66.4.2 Userspace TCP stacks

Replacing the kernel TCP you gave up (for the order path):

- **Why you need one.** Bypass gives *raw packets* — fine for UDP market data (parse UDP/IP yourself — Ch. 56), but **order entry is TCP** (Ch. 54), and there's no kernel TCP anymore. A **userspace TCP stack** implements the TCP state machine (handshake, sequencing, retransmit, congestion control — Ch. 54) on top of the bypass packet I/O.
- **The options.** DPDK ecosystem stacks (F-Stack, mTCP, Seastar's stack, lwIP-based, VPP), vendor stacks (Onload *is* a userspace TCP stack — §66.4.4), or a purpose-built minimal TCP for the specific venue protocols. For HFT order entry, a *minimal, latency-tuned* TCP (just what the venue needs, `TCP_NODELAY`-equivalent behavior — Ch. 54, pre-established warm connections — Ch. 49) often beats a general stack.
- **Onload as the easy path (§66.4.4).** Onload transparently provides a userspace TCP stack *under the socket API* — so an existing sockets-based order gateway gets bypass TCP with **no code change** (`LD_PRELOAD`). The pragmatic choice when you don't want to integrate a raw bypass stack into the order path.
- **Complexity and correctness.** A userspace TCP stack is real complexity (TCP is subtle — retransmit, windows, edge cases) and a correctness risk on the order path (a TCP bug = a lost/duplicated order). Use a *mature* stack (Onload, a vetted DPDK stack) rather than hand-rolling TCP; reserve custom stacks for where the latency justifies the engineering and testing (Ch. 42).
- **UDP needs no stack.** Market-data multicast (Ch. 54, 56) is UDP — you just parse the UDP/IP headers off the raw packet (a few fields — Ch. 56) and decode. No userspace stack needed for the *receive* feed path; the stack is only for TCP order entry.
- **What UDP *does* still need without a kernel (the control plane).** Group membership — periodic IGMPv2/v3 membership reports that the switch's snooping timers expect (Ch. 64); nobody sends them once a VFIO-bound NIC has left the kernel, so your app, a sidecar on a kernel-side interface, or static groups on the switch must. The NIC's multicast MAC/VLAN filter (`rte_eth_dev_set_mc_addr_list`, or `allmulticast`/promiscuous). ARP/neighbour resolution for any TX destination (static entries, or answer ARP yourself). And IP/UDP checksum verification on receive if the NIC offload is not enabled. Every bypass deployment hits all four in its first week.

### 66.4.3 The tick-to-trade fast path

Assembling the whole low-latency path (the book's culmination, ties Ch. 91):

- **The bypass fast path end to end.** Market-data multicast → NIC → DMA into huge-page buffer → **ef_vi/DPDK poll** (no kernel) → **zero-copy decode** (Ch. 56, parse in the DMA buffer) → **order book** (Ch. 25) → **strategy** (the deterministic state machine — Ch. 85) → order built → **bypass TX** (ef_vi/DPDK send, or a userspace TCP stack for TCP venues) → wire. **No kernel, no syscalls, no copies, no allocation (Ch. 23), no locks (Ch. 34–37)** — the entire tick-to-trade path is user-space, busy-polling, on dedicated isolated cores (Ch. 44, 45, 47), warm (Ch. 49).
- **Single-threaded, run-to-completion on a pinned core.** The hottest path is often a *single* thread on a *single* isolated core (Ch. 31, 43) running the whole poll → decode → strategy → send loop with no handoffs — the lowest latency (no inter-thread/inter-process hops on the critical path). Heavier/parallel work (risk, journaling) is off this core (Ch. 31, Ch. 37 pipeline, Ch. 85 process topology).
- **Hardware timestamps throughout (Ch. 59, 62).** Capture NIC hardware RX/TX timestamps (the bypass APIs expose them) for true wire-to-wire measurement (Ch. 62, 91) — the tick-to-trade latency, measured honestly.
- **Warm and pre-faulted (Ch. 23, 49).** Buffers pre-faulted/`mlock`'d (Ch. 23, 26), connections pre-established and warm (Ch. 49), the path kept warm with shadow traffic (Ch. 49) so the first real message isn't cold. Bypass removes the kernel; warming removes the cold-start (Ch. 49) — together they make the *rare critical message* fast.
- **The floor is now hardware (Ch. 70–73).** With software bypass, the remaining latency is the wire, the NIC, the **PCIe round-trip** (Ch. 70 — the floor under every host-device transfer), and your (cache-warm — Ch. 7, branch-predicted — Ch. 13) code. To go lower, you move work *into hardware* — **FPGA inline** on the NIC (Ch. 73), the next tier. Software bypass is the software floor; Part XI is beyond it.

### 66.4.4 Proprietary vendor APIs: Solarflare `ef_vi` and ExaNIC `libexanic` for zero-copy NIC ring access; transparent vs explicit bypass latency floors

The vendor-specific raw APIs — the lowest software latency (ties Ch. 59):

- **ef_vi (Solarflare/AMD) — raw layer-2, lowest latency.** ef_vi gives the application *direct* access to the NIC's RX/TX rings: post RX buffers (`ef_vi_receive_init` per buffer, then `ef_vi_receive_push` to hand them to the NIC — or `ef_vi_receive_post` for both), poll the event queue (`ef_eventq_poll`, or `ef_receive_poll` on adapters with RX-only polling) for `EF_EVENT_TYPE_RX` events, read the packet in place, send (`ef_vi_transmit`). Minimal abstraction → **lowest software latency** (§66.3) — and per-packet **hardware timestamps** (Ch. 59, 62). The choice for the ultra-hot Solarflare path. More work than Onload (you handle the raw rings/packets), but faster.
- **libexanic (ExaNIC/Cisco) — raw, extreme low latency + FPGA.** The ExaNIC equivalent: direct ring access (`exanic_receive_frame`), exceptional low latency and **precise hardware timestamps**, and some ExaNICs offer **FPGA-based inline** processing (Ch. 73 — do work on the NIC's FPGA before it even reaches software). The raw API for ExaNIC cards.
- **Transparent (Onload) vs explicit (ef_vi/libexanic/DPDK) bypass — the latency-floor trade.** **Transparent** bypass (Onload — §66.4.2) intercepts the **socket API** (`LD_PRELOAD`) and services it from a userspace stack — **no code change**, works with existing sockets apps, but the socket-API compatibility layer adds a little latency (a higher floor — ~0.2–0.4 µs over DPDK and ~0.4–1 µs over raw ef_vi at p50/p99 in §66.3's ladder). **Explicit** bypass (ef_vi/libexanic/DPDK) requires writing to the *raw* bypass API — **more work**, no socket compatibility, but the **lowest floor** (no compatibility overhead, direct ring access). The decision: Onload for *easy, good-enough* bypass of an existing app (control/order gateways, less-ultra-hot); ef_vi/libexanic for the *ultra-hot* feed/order path where every nanosecond counts and you'll write to the raw API.
- **Vendor lock-in and the DPDK alternative.** ef_vi/libexanic/Onload tie you to Solarflare/ExaNIC hardware; **DPDK** (§66.4.1) is vendor-neutral (PMDs for many NICs) at a slightly higher floor than the best raw vendor APIs and more framework complexity. The choice: vendor raw API (lowest latency, lock-in) vs DPDK (portable, slightly higher) vs Onload (transparent, easy, higher) vs AF_XDP (standard Linux, Ch. 65). HFT typically uses the **raw vendor API on the ultra-hot path**, accepting the lock-in for the latency.

## 66.5 Pitfalls & anti-patterns: burning cores; losing kernel tooling

- **Bypass without a dedicated/isolated core (Ch. 43, 44, 45, 47).** A poll-mode driver busy-polls at 100% — it *must* run on a dedicated, isolated, pinned core (Ch. 44, 47), or it starves other work and gets preempted (defeating the point — Ch. 43). Bypass *requires* the OS-isolation work of Part VII; it's not standalone.
- **Burning cores you didn't budget.** Each bypass RX/TX queue polled is a core at 100% (Ch. 43) — and the NIC is dedicated. On a box with limited cores, bypass for *everything* is wasteful. Use bypass for the **ultra-hot path** (market-data RX, order TX); keep other networking on the kernel stack (Ch. 50, 54) / AF_XDP (Ch. 65). Budget the cores and NICs.
- **Losing kernel tooling (no `tcpdump`/`netstat`/firewall).** With the NIC bypassed, the kernel's tools don't see the traffic — `tcpdump`, `netstat`, `ss`, the firewall, and standard monitoring are blind. You must provide your own capture (Ch. 87 — capture in the bypass path), monitoring, and (if needed) filtering. Plan for observability *in* the bypass path (the vendor APIs / DPDK have capture hooks; the drop counters that replace the kernel's — `imissed`, `rx_nombuf`, `onload_stackdump` — are Ch. 90 §90.4.5).
- **Reimplementing TCP badly (§66.4.2).** A hand-rolled userspace TCP stack for order entry is a major correctness risk (TCP is subtle — a bug = a lost/duplicated/corrupted order). Use a **mature** stack (Onload, a vetted DPDK stack) and test it ruthlessly (Ch. 42); don't hand-roll TCP unless the latency truly justifies the engineering+testing burden.
- **Bypass on the path but the rest un-tuned.** Bypass removes the kernel, but if the core isn't isolated (Ch. 47), the buffers aren't pre-faulted/huge-paged (Ch. 15, 23), the path isn't warm (Ch. 49), or the decode is slow (Ch. 56), you don't get the latency. Bypass is *part* of a fully-tuned system (Parts IV–X together), not a standalone fix.
- **Vendor lock-in not accounted for (§66.4.4).** Committing to ef_vi/libexanic ties you to that NIC vendor (hardware, support, roadmap). A deliberate trade (latency vs portability) — DPDK or AF_XDP if portability matters more. Don't stumble into lock-in; choose it knowingly.
- **Ignoring the security surface (Ch. 83).** Bypass gives user-space direct hardware (DMA) access — a powerful capability with security implications (a bug can DMA anywhere via the IOMMU config — Ch. 70), and you've removed the kernel firewall. Configure the IOMMU (Ch. 70), validate all wire input (Ch. 56, 83 — the decoder is now the *only* line of defense), and manage the privilege bypass requires.
- **Assuming software bypass is the floor.** Bypass is the *software* floor (~1 µs — §66.3); for lower, the work moves *into hardware* — **FPGA inline** (Ch. 73), SmartNICs (Ch. 74). If you're at the ef_vi floor and need lower, that's Part XI, not more software tuning.
- **Not measuring with hardware timestamps (Ch. 59, 62).** Measuring bypass latency with *software* timestamps includes the latency you're measuring (Ch. 59) — use NIC hardware timestamps on PTP-synced clocks (Ch. 62) for the true number.

## 66.6 Exercises & checklist

**Exercises**

1. **The tier ladder.** Measure UDP RX latency (hardware timestamps — Ch. 59, 62, dedicated isolated core — Ch. 44, 45, 47) across: kernel stack busy-poll (Ch. 50), AF_XDP (Ch. 65), DPDK PMD, and (if available) ef_vi. Reproduce the §66.3 ladder; confirm bypass is the big sub-µs step.
2. **DPDK RX loop.** Build the skeleton below (`rte_eth_rx_burst` on an lcore pinned to an isolated core, huge-page mempool); receive and decode (Ch. 56) the feed. Measure latency and confirm the NIC is bound to DPDK (not the kernel) and the core is at 100%.

```cpp
// dpdk_rx.cpp — minimal DPDK poll-mode RX loop: one port, one queue, a huge-page mempool on the NIC's NUMA node.
// Build:  g++ -O2 -std=c++20 -march=native dpdk_rx.cpp -o dpdk_rx $(pkg-config --cflags --libs libdpdk)   (DPDK 23.11)
// Setup:  echo 1024 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages; mount -t hugetlbfs none /dev/hugepages
//         dpdk-devbind.py --bind=vfio-pci 0000:17:00.0   # Intel-class NIC leaves the kernel; mlx5 is bifurcated (no rebind)
// Run:    ./dpdk_rx -l 2 -n 4 -a 0000:17:00.0            # -l 2: the isolated lcore (Ch. 44, 47)
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_mbuf.h>

static inline void decode(const std::uint8_t* frame, std::uint32_t len) {   // Ch. 56's zero-copy decoder, in place in the mbuf
    (void)frame; (void)len;
}

int main(int argc, char** argv) {
    if (rte_eal_init(argc, argv) < 0) rte_exit(EXIT_FAILURE, "EAL init failed\n");   // parses -l/-n/-a, maps huge pages, probes the PMD
    const std::uint16_t port = 0;
    const int node = rte_eth_dev_socket_id(port);                       // the NIC's NUMA node (Ch. 16)
    rte_mempool* pool = rte_pktmbuf_pool_create("rx_pool", 8191, 256, 0, RTE_MBUF_DEFAULT_BUF_SIZE, node);
    if (!pool) rte_exit(EXIT_FAILURE, "mempool failed\n");

    rte_eth_conf conf{};                                                // defaults: no RSS, no offloads — one queue, raw frames
    if (rte_eth_dev_configure(port, 1, 1, &conf) < 0)                 rte_exit(EXIT_FAILURE, "configure failed\n");
    if (rte_eth_rx_queue_setup(port, 0, 512, node, nullptr, pool) < 0) rte_exit(EXIT_FAILURE, "rxq failed\n");
    if (rte_eth_tx_queue_setup(port, 0, 512, node, nullptr) < 0)       rte_exit(EXIT_FAILURE, "txq failed\n");
    if (rte_eth_dev_start(port) < 0)                                   rte_exit(EXIT_FAILURE, "start failed\n");
    rte_eth_allmulticast_enable(port);     // or rte_eth_dev_set_mc_addr_list(): the kernel no longer programs the group filter

    rte_mbuf* burst[32];
    for (;;) {                                                          // 100% of one isolated core; no syscalls, no interrupts
        const std::uint16_t got = rte_eth_rx_burst(port, 0, burst, 32);
        for (std::uint16_t i = 0; i < got; ++i) {
            decode(rte_pktmbuf_mtod(burst[i], const std::uint8_t*), rte_pktmbuf_pkt_len(burst[i]));   // zero-copy: parse in the DMA buffer
            rte_pktmbuf_free(burst[i]);                                 // back to the pool — no allocation on the hot path (Ch. 23)
        }
    }
}
```

3. **Transparent vs explicit.** Run an existing UDP/TCP sockets app under Onload (`onload ./app` — transparent bypass, no code change) vs a raw ef_vi/DPDK version; compare latency and effort. Quantify the transparent-vs-explicit floor difference (§66.4.4).
4. **Lose the tooling.** With the NIC bypassed, try `tcpdump`/`netstat` — observe they don't see the traffic (§66.5). Add capture *in* the bypass path (Ch. 87). 
5. **End-to-end fast path (ties Ch. 91).** Assemble bypass RX → zero-copy decode (Ch. 56) → book (Ch. 25) → strategy → bypass TX, single-threaded on an isolated warm core; measure the wire-to-wire tick-to-trade latency (hardware timestamps — Ch. 62). This is the book's culminating measurement (§66.4.3).

**Checklist — kernel bypass & userspace networking**

- [ ] The **ultra-hot path** (market-data RX, order TX) uses **kernel bypass** (ef_vi/libexanic raw, or DPDK) — the rest of networking stays on the **tuned kernel stack** (Ch. 50, 54) / **AF_XDP** (Ch. 65); cores and NICs are **budgeted** (§66.5).
- [ ] Bypass runs on **dedicated, isolated, pinned cores** (Ch. 44, 47) **busy-polling** (Ch. 43) — never on a shared/non-isolated core; the NIC is dedicated.
- [ ] Packet buffers are **huge-page-backed mempools**, **pre-faulted/`mlock`'d**, **NUMA-local**, **per-core/lockless** (Ch. 15, 16, 23, 26, 34) — no per-packet allocation; RX is **burst/batched** (Ch. 37, 56).
- [ ] **UDP market data** is parsed raw (Ch. 56 — no stack needed); **TCP order entry** uses a **mature userspace TCP stack** (Onload / vetted DPDK stack), **not hand-rolled** unless justified + tested (Ch. 42, §66.4.2).
- [ ] The **bypass-API choice** is deliberate: **raw vendor (ef_vi/libexanic)** for lowest latency on the ultra-hot path (accepting lock-in), **DPDK** for portability, **Onload** for transparent ease, **AF_XDP** for standard Linux (§66.4.4).
- [ ] **Hardware timestamps** (Ch. 59) on **PTP-synced clocks** (Ch. 62) measure the true wire-to-wire latency; the path is **warm + pre-faulted** (Ch. 23, 49).
- [ ] **Lost kernel tooling/firewall** is replaced: capture in the bypass path (Ch. 87), own monitoring, **all wire input validated** (the decoder is the only defense now — Ch. 56, 83), **IOMMU** configured (Ch. 70, 83).
- [ ] I recognize bypass is the **software floor** (~1 µs); lower requires **hardware** (FPGA inline — Ch. 73, SmartNICs — Ch. 74), Part XI — not more software tuning.

## 66.7 References

- The **DPDK** documentation (Programmer's Guide, PMDs, mempools, `rte_eth`) — the vendor-neutral bypass framework (§66.4.1).
- The Solarflare/AMD **ef_vi** and **Onload** documentation, and the **ExaNIC `libexanic`** documentation — the raw vendor APIs and transparent bypass (§66.4.4, ties Ch. 59).
- The userspace-TCP-stack projects — **F-Stack**, **mTCP**, **Seastar**, **VPP**, lwIP — for §66.4.2.
- L. Rizzo, *"netmap: a novel framework for fast packet I/O"* and the kernel-bypass literature — the poll-mode-driver / user-space-NIC-access model (§66.2).
- Carl Cook / HFT latency talks and the "Mechanical Sympathy" networking writeups — bypass on the tick-to-trade path (§66.4.3).

## 66.8 Additional Reading

- The DPDK and Onload performance-tuning guides — production bypass configuration (cores, mempools, NUMA).
- Talks on building HFT tick-to-trade fast paths with ef_vi/ExaNIC — real-world sub-µs software paths.
- Ch. 50 (*Linux Native I/O*) — the stack bypassed; Ch. 59 (*NIC*) — the NIC/vendor stacks; Ch. 65 (*AF_XDP*) — the standard-Linux middle ground; Ch. 43–49 (*Scheduling/Warming*) — the isolated, warm cores bypass needs; Ch. 56 (*Decoding*) — the zero-copy decode; Ch. 62 (*PTP*) — hardware timestamps; Ch. 67 (*RDMA*) — one-sided remote memory; Ch. 70 (*PCIe*) — the round-trip floor under bypass; Ch. 73 (*FPGA*) — the hardware tier below software bypass; Ch. 91 (*End-to-End*) — the tick-to-trade path.
- Ch. 52 (*Zero-Copy & the Modern Kernel Fast Path*) — the kernel-retaining middle ground below full bypass; Ch. 61 (*Flow Steering*) — `rte_flow` steering feeds to cores on the bypass path.
- **Appendix E** — kernel-stack vs bypass vs FPGA RX rows (the AF_XDP/Onload/DPDK/ef_vi ladder itself is §66.3); **Appendix C** — bypass/core/NIC tuning.

---

*Next: Ch. 67 — InfiniBand Verbs & RDMA, the one-sided remote-memory extreme of low-latency messaging: queue pairs, completion queues, registered memory, and RDMA read/write that lets one host read or write another's memory directly — and where it fits intra-datacenter vs kernel bypass.*
