# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 53 — Zero-Copy & the Modern Kernel Fast Path

> **Prerequisites:** Ch. 51 (native I/O — the copying stack this improves on), Ch. 52 (io_uring — the interface most of this rides), Ch. 23–24 (buffer lifetime and pinning). Relates forward to the networking hardware developed in Parts IX–XI: the NIC as the DMA far end (Ch. 60), where RX lands (DDIO — Ch. 61), the PCIe/DMA boundary (Ch. 71), and full kernel bypass (Ch. 67) — the alternative this is a middle ground to.
>
> **Leads into:** Ch. 64 (precise transmission — pairs with zero-copy TX), Ch. 62 (flow steering — gets the packet to the core that owns the zero-copy buffer). The current frontier of the *kernel* fast path, between the io_uring of Ch. 52 and the full bypass of Ch. 67: removing the copies without leaving the kernel entirely.

---

## 53.1 Why it matters: the copy is a cost you pay on every byte

Kernel bypass (Ch. 67) is the lowest-latency path, but it's not free — it means giving up the kernel's TCP stack, its sockets API, its security, and running a poll-mode driver that burns a core. Many systems want *most* of the kernel-bypass win without that cost, and the single biggest inefficiency in the *kernel* network path is the **copy**: the standard `read`/`write`/`recv`/`send` path copies every byte between the kernel's socket buffers and your userspace buffer (Ch. 51). On receive, the NIC DMAs the packet into a kernel buffer, then the kernel *copies* it into your buffer on `recv`. On send, the kernel *copies* your buffer into a socket buffer before the NIC DMAs it out. At market-data rates (Ch. 57) or on a busy order path, that copy is memory bandwidth (Ch. 16) and cache pollution (Ch. 7) and latency you pay on *every byte of every message* — a tax the fast path shouldn't pay.

**Zero-copy** networking removes that copy while keeping the kernel stack: the NIC DMAs directly to (or from) *your* buffer, and the kernel just manages the metadata. This is the frontier the mainline Linux kernel has been building out — `MSG_ZEROCOPY` for send, `TCP_ZEROCOPY_RECEIVE` for receive, io_uring's zero-copy send and (very recently) zero-copy receive, and the bleeding-edge **devmem TCP** that DMAs payload directly into GPU/FPGA memory. Together they close much of the gap to full bypass (Ch. 67) *without* abandoning the sockets API, the kernel TCP stack (Ch. 56), or the kernel's security and multiplexing — a genuinely different point on the latency/complexity curve (Ch. 67's middle ground, made concrete).

The catch, and the reason zero-copy is a *technique* and not just a flag, is that removing the copy **changes the buffer ownership model**. A normal `send` is done with your buffer when it returns; a zero-copy send is *not* — the NIC reads your buffer asynchronously, so you must not touch or reuse it until the kernel *notifies* you the DMA completed. Zero-copy trades a synchronous copy for asynchronous buffer lifetime management (Ch. 23–24), and getting that lifetime wrong is a correctness bug (send garbage) or a use-after-free (Ch. 84). This chapter explains where the copies are and how each zero-copy mechanism removes them (§53.2), measures the copy cost and zero-copy's fixed overhead (§53.3), details `MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy, and devmem TCP (§53.4), and warns about buffer lifetime and the small-message trap (§53.5). It's the "keep the kernel, lose the copy" chapter.

## 53.2 Mental model: where the copies are, and how zero-copy removes them

Trace a byte through the standard kernel path and mark every copy:

```
   RX (standard):  NIC --DMA--> [kernel skb/socket buf] --COPY(recv)--> [your buffer] --> app
   RX (zero-copy): NIC --DMA--> [your buffer, mmap'd/registered] ................. --> app   (no copy)

   TX (standard):  app --> [your buffer] --COPY(send)--> [kernel socket buf] --DMA--> NIC
   TX (zero-copy): app --> [your buffer, pinned] .................... --DMA(from your buf)--> NIC
                                                                        ▲ async: buffer busy until completion notified
```

The two directions have different mechanisms and different gotchas:

- **Zero-copy TX (`MSG_ZEROCOPY`, io_uring `SEND_ZC`).** The kernel **pins** your userspace pages and hands their physical addresses to the NIC, which DMAs *directly from your buffer*. No copy — but the buffer is now **in use by the NIC asynchronously**, so `send` returns *before the kernel is done reading your buffer* (a copying `send` returns once the bytes are in the socket buffer — it, too, returns before the wire), and you must wait for a **completion notification** (on the socket error queue for `MSG_ZEROCOPY`, or the io_uring CQE) before reusing the buffer. The cost moved from a copy to page-pinning + async lifetime management.
- **Zero-copy RX (`TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy RX).** Harder, because on receive the NIC decides where data lands *before* you've asked for it. The mechanisms: **mmap the receive stream** into your address space so the kernel maps (page-flips) the received pages into userspace instead of copying (`TCP_ZEROCOPY_RECEIVE`), or **pre-register buffers** the NIC DMAs into and get told which one filled (io_uring zero-copy RX). `TCP_ZEROCOPY_RECEIVE` page-flips *whole pages*, so it needs page-aligned, page-sized payload; io_uring zero-copy RX instead registers a page-aligned *area* plus a refill ring and delivers fragments with byte offsets in the CQE — its hard requirements are NIC header/data split (`ethtool -G … tcp-data-split on`) and a dedicated RX queue (flow steering — Ch. 62), not per-message alignment. Both work best for larger transfers (the mapping/refill has fixed cost — small messages don't amortize it, §53.5). All three zero-copy RX mechanisms are **TCP-only** as of Linux 6.15.
- **devmem TCP (payload to device memory).** The newest: **header–data split** at the NIC — headers go to the host (for the kernel stack to process), *payload* DMAs directly into **device memory** (a GPU or FPGA, via a `dmabuf`), never touching host DRAM or host cache. For a NIC→accelerator pipeline (Ch. 72/74) this is the ultimate zero-copy: the host CPU never sees the payload bytes at all.

The unifying idea: **zero-copy makes the NIC DMA to/from the *final* buffer, eliminating the intermediate kernel copy — at the price of managing buffer lifetime asynchronously and respecting alignment.** It's a middle ground between the copying kernel stack (Ch. 51 — simple, slower) and full bypass (Ch. 67 — fastest, most complex): you keep the kernel and sockets but pay attention to buffers.

**Where AF_XDP (Ch. 66) sits.** There is a second "zero-copy without full bypass" path, and it is the one most market-data readers reach for: **AF_XDP** gives a userspace process the NIC's RX/TX rings in zero-copy mode for *any* protocol — including the **UDP multicast** feeds that none of this chapter's TCP-only receive mechanisms can touch — while the kernel keeps the device and the rest of the stack. The trade is that you parse UDP/IP yourself (Ch. 57) and lose the socket API on that queue; this chapter's mechanisms keep sockets and TCP and are the right tool for TCP order-entry, recovery and bulk streams. The older in-kernel zero-copy primitives — `sendfile`/`splice`/`vmsplice` (moving pages between fds without a userspace bounce) and `PF_PACKET`'s `TPACKET_V3` mmap'd rings (the zero-copy *capture* path behind `tcpdump`/`libpcap`) — belong to the bulk capture/journal use cases of Ch. 90, not the trading path (§53.4).

## 53.3 Measure it: copy cost, the zero-copy threshold, and RX latency

*Toolchain and machine: Appendix L.3.*

Three measurements: what the copy *costs*, where zero-copy TX *pays off* (it has fixed overhead), and the zero-copy RX benefit.

- **The copy cost.** Measure send/receive latency and CPU for a copying path vs zero-copy at increasing message sizes, and watch memory bandwidth (Ch. 16) and cache misses (Ch. 7) from the copy. Representative (Linux 6.x, a scatter-gather-capable NIC between *two hosts* — over loopback or veth every `MSG_ZEROCOPY` send is silently copied, §53.4.1; harness and figures pending real runs): for large messages the copy is a real fraction of the send/receive time and a large fraction of the CPU and memory bandwidth; for small messages it's negligible.
- **The zero-copy TX threshold.** `MSG_ZEROCOPY` has **fixed per-operation overhead** (pinning pages, tracking completion) that only pays off above a size threshold — small sends are *slower* zero-copy than copied. Measure the crossover:

| Message size | Copying `send` | `MSG_ZEROCOPY` | Winner |
|---|---|---|---|
| ~100 bytes (an order) | fast (copy is cheap) | slower (pinning overhead > copy) | **copy** |
| ~1–10 KB | copy noticeable | pinning amortized | ~even |
| ~64 KB+ (bulk, capture — Ch. 90) | copy dominates | much faster, less CPU | **zero-copy** |

  The lesson for HFT: a **single small order is not a zero-copy TX candidate** — the order is tiny, the copy is cheap, and pinning overhead dominates. Zero-copy TX shines for *bulk* (capture/journal streaming — Ch. 90, market-data replay, snapshots — Ch. 58), not the single-order hot path. Know which you're optimizing.
- **Zero-copy RX latency and CPU.** Measure receive latency and CPU with `TCP_ZEROCOPY_RECEIVE` / io_uring zero-copy RX vs copying `recv`, for the message sizes you actually handle. Zero-copy RX helps most for **large, page-aligned** receives (bulk recovery — Ch. 58, capture); for small unaligned messages the mapping overhead can exceed the copy saved (§53.5).

The lessons: the copy is a per-byte cost that matters at *volume* (bulk paths) more than for a single small message; zero-copy TX has a *fixed overhead* that makes it a bulk technique, not a small-order one; and zero-copy RX wants large, aligned transfers. Measure the crossover on your sizes — zero-copy is not universally faster, and applying it to tiny messages is a pessimization (§53.5).

## 53.4 Techniques

### 53.4.1 Zero-copy send: `MSG_ZEROCOPY` / `SO_ZEROCOPY`

- **Enable and use it.** Set `SO_ZEROCOPY` on the socket, then pass `MSG_ZEROCOPY` to `send`/`sendmsg` (Linux 4.14+ for TCP, 5.0+ for UDP; io_uring `SEND_ZC` is 6.0+). The kernel pins your buffer's pages and the NIC DMAs from them. Use it for **bulk** egress (capture/journal to a collector — Ch. 90, market-data or snapshot replay — Ch. 58), not the single-order path (§53.3).
- **Manage the completion notification.** `send` returns before the kernel is done reading your buffer; the kernel posts a completion on the **socket error queue** (`recvmsg` with `MSG_ERRQUEUE`), carrying a range of sequence numbers (`ee_info`..`ee_data`) that identify which sends completed. You **must not reuse a buffer until its completion arrives** — track in-flight buffers (a ring of buffers from a fixed pool, Ch. 24) and reclaim on completion. For **TCP** the completion fires only when the kernel drops its last reference to your pages — i.e. once the peer has **ACKed** the data, because the skb sits in the retransmit queue until then — so buffer-reuse latency is **at least one network RTT**, and the in-flight ring must hold at least *send rate × RTT* bytes (a 1 Gbit/s stream over a 1 ms RTT needs ≥ 125 KB outstanding; a venue 100 µs away, far less). Too many outstanding zero-copy sends fail with `ENOBUFS` when the per-socket optmem limit (`net.core.optmem_max`) is hit — size the ring, don't retry blindly. This is the asynchronous-lifetime discipline that makes zero-copy a technique, not a flag.
- **Loopback always copies.** `Documentation/networking/msg_zerocopy.rst`: packets looped to a local socket — `lo`, veth, or any socket with `tcpdump`/a packet socket attached — incur a *deferred copy*, and every completion reports `SO_EE_CODE_ZEROCOPY_COPIED`. Measure between two hosts over a NIC with scatter-gather on (`ethtool -k eth0 | grep scatter-gather`), or you will measure zero benefit and conclude the feature is broken (Exercises 1–3).
- **Watch for the "copied anyway" fallback.** The completion notification tells you whether the kernel actually did zero-copy or fell back to copying (it can, e.g. for small or unsuitable sends). Check it — if you're always getting the copy fallback, zero-copy is buying you nothing but complexity (§53.5).

### 53.4.2 Zero-copy receive: `TCP_ZEROCOPY_RECEIVE` and mmap

- **mmap-based receive.** `TCP_ZEROCOPY_RECEIVE` (Linux 4.18+) is a **`getsockopt`-only** option that presupposes the socket fd has been `mmap`'d first: `mmap(NULL, len, PROT_READ, MAP_SHARED, sockfd, 0)` once, then per receive `getsockopt(fd, IPPROTO_TCP, TCP_ZEROCOPY_RECEIVE, &zc, &zclen)` with `struct tcp_zerocopy_receive { .address = <the mapping>, .length = len, .copybuf_address/.copybuf_len = <a small buffer for the unaligned head/tail, 5.11+> }` — the kernel page-flips the received, page-aligned payload into that mapping instead of copying (any other VMA is rejected with `EINVAL`), returns how much it mapped (`zc.length`) and how much it had to copy (`zc.copybuf_len`), and you process the data in place, then `madvise(MADV_DONTNEED)`/re-call to release the pages. Best for **large, aligned** streams (bulk recovery, capture — Ch. 90/58).
- **Alignment is mandatory.** Zero-copy RX requires page alignment (the kernel maps whole pages); mis-aligned or small receives can't be mapped and fall back to copy. Design the receive path (buffer sizes, message framing) for page-aligned bulk where you want zero-copy RX; accept copying for small control messages.
- **Combine with DDIO awareness (Ch. 61).** Zero-copy RX lands data in *your* pages; whether those pages are L3-hot (DDIO, Ch. 61) still depends on NIC-local placement (Ch. 16) and cache residency. Zero-copy removes the *copy*; DDIO/CAT (Ch. 47, 61) govern whether the *first read* is a cache hit. Use them together.

### 53.4.3 io_uring zero-copy send and receive

- **io_uring `SEND_ZC` (zero-copy send).** io_uring (Ch. 52) unifies zero-copy send with its async model: submit a zero-copy send and get **two** CQEs for the same `user_data`: first the send result with `IORING_CQE_F_MORE` set (the kernel still owns the buffer), then — once the NIC and any retransmits are done with the pages — a notification CQE flagged `IORING_CQE_F_NOTIF` (`res` 0), which is the only signal that the buffer may be reused (the completion notification, integrated into the ring rather than a separate error queue). Cleaner than raw `MSG_ZEROCOPY` for an io_uring-based app — the notification is just another CQE; reap both and reuse only after the `F_NOTIF` one. Pair with **registered buffers** (Ch. 52) so the pages are pre-pinned (no per-send pinning cost — removing part of the §53.3 fixed overhead).
- **io_uring zero-copy receive (Linux 6.15+).** The newest addition: pre-register a pool of buffers and a fill/completion ring; the NIC DMAs incoming data into your registered buffers and io_uring tells you which buffer filled — zero-copy RX with the ergonomics of io_uring's provided-buffer model. This is the direction the kernel fast path is converging on: **registered buffers + zero-copy + async completion**, approaching bypass throughput while staying in the kernel.
- **Multishot + provided buffers (Ch. 52).** Combine zero-copy with multishot receive (one submission, many completions) and ring-provided buffers so the kernel picks buffers from your pool — minimizing per-message overhead for high-rate receive. Note the protocol boundary: **multishot recv + provided-buffer rings** is the high-rate UDP/multicast receive path *with one copy*; every *zero-copy* receive mechanism here — io_uring zcrx, `TCP_ZEROCOPY_RECEIVE`, devmem TCP — is **TCP-only** as of 6.15 (`io_zcrx_recv()` returns `EPROTONOSUPPORT` for anything but `tcp_recvmsg`). For zero-copy UDP multicast market data, the kernel-retaining answer is **AF_XDP** (Ch. 66), and beyond it full bypass (Ch. 67).

### 53.4.4 devmem TCP: payload straight to device memory

- **Header–data split to a `dmabuf`.** devmem TCP (Linux 6.12+) splits the packet: headers to the host (the kernel TCP stack processes them normally — Ch. 56), payload DMA'd directly into **device memory** exported as a `dmabuf` (a GPU or FPGA — Ch. 72/74). The host CPU never touches the payload bytes.
- **Where it fits: NIC→accelerator pipelines (Ch. 72/74).** For workloads that receive data and immediately hand it to a GPU (risk/pricing/ML inference — Ch. 72) or an FPGA, devmem TCP removes the host-memory bounce entirely — the data goes wire → NIC → accelerator memory, host stack only steering it. It's the receive-side analog of peer-to-peer DMA (Ch. 71, GPUDirect) done through the *TCP stack*.
- **Emerging, know the constraints.** It requires NIC support (header-data split, flow steering to the dmabuf — Ch. 62), a supporting driver, and recent kernels; it's not yet a commodity path. Track it as the frontier of "the payload never touches the host," and use it where a NIC-to-accelerator path justifies the complexity.

## 53.5 Pitfalls & anti-patterns: buffer lifetime and the small-message trap

- **Reusing a zero-copy buffer before completion (the cardinal correctness bug).** With `MSG_ZEROCOPY` / `SEND_ZC`, `send` returns before the NIC has read the buffer; touching or reusing it before the completion notification sends **garbage** (whatever you overwrote) or is a use-after-free (Ch. 84). Track in-flight buffers and reclaim only on completion (§53.4.1). This is the single most important zero-copy discipline.
- **Applying zero-copy TX to small messages (the pessimization).** Zero-copy send has fixed per-op overhead (pinning, completion tracking) that exceeds the cost of copying a small buffer (§53.3) — a single 100-byte order is *slower* zero-copy. Zero-copy TX is a *bulk* technique (capture, replay, snapshots — Ch. 90/58); don't apply it to the small-order hot path.
- **Ignoring the copy-fallback.** The kernel can silently fall back to copying (small/unsuitable sends) while you carry all the zero-copy complexity. Check the completion status; if you're always falling back, you've added lifetime-management burden for no benefit (§53.4.1).
- **Zero-copy RX without alignment.** `TCP_ZEROCOPY_RECEIVE` needs page-aligned data; unaligned/small receives fall back to copy or fail. Design framing/buffers for alignment where you want zero-copy RX; accept copying for small control traffic (§53.4.2).
- **Page-pinning cost and limits.** Pinning pages for zero-copy consumes a limited resource and has cost; pinning huge numbers of small buffers (or leaking pinned buffers by not reclaiming on completion) exhausts it. Use registered buffers (Ch. 52) to pin once and reuse (§53.4.3), and bound the in-flight set.
- **Cache effects — zero-copy isn't automatically cache-hot.** Removing the copy saves bandwidth/CPU but doesn't by itself make the first read an L3 hit — that's DDIO/NUMA (Ch. 16, 61). Zero-copy + DDIO + NIC-local placement together give both no-copy *and* cache-hot; zero-copy alone can still land data in cold DRAM (§53.4.2).
- **Assuming zero-copy replaces bypass.** Zero-copy in the kernel stack is a *middle ground* (Ch. 67), not a replacement for full bypass on the very lowest-latency order path. For the tick-to-trade order send, bypass (Ch. 67) or inline hardware (Ch. 74) is still the floor; zero-copy is for the paths where keeping the kernel is worth it and copies are the bottleneck (§53.1).
- **Complexity without measurement.** Zero-copy adds real complexity (async lifetime, alignment, completion handling); adopt it only where the copy is a *measured* bottleneck (§53.3) — for a low-rate small-message path it's pure downside.

## 53.6 Exercises & checklist

**Exercises** (run 1–3 between **two hosts** over a scatter-gather-capable NIC — over loopback/veth the kernel falls back to a deferred copy and every completion reports `SO_EE_CODE_ZEROCOPY_COPIED`, §53.4.1)

1. **Copy cost curve.** Measure send/receive latency, CPU, and memory bandwidth for copying vs zero-copy across message sizes (100 B → 64 KB); find the crossover where zero-copy TX starts winning (§53.3). Vary the RTT (a second host on the same switch vs across a WAN link) and watch the required in-flight buffer count scale with it.
2. **Zero-copy TX lifetime.** Implement `MSG_ZEROCOPY` send with an in-flight buffer ring reclaimed on error-queue completions; deliberately reuse a buffer early and observe the corruption (§53.4.1, §53.5).
3. **io_uring `SEND_ZC` + registered buffers.** Build a bulk sender with io_uring zero-copy send over registered buffers; compare CPU and latency to copying `send` and to raw `MSG_ZEROCOPY` (§53.4.3).
4. **Zero-copy RX.** Set up `TCP_ZEROCOPY_RECEIVE` (or io_uring zero-copy RX) for a large aligned stream; measure the latency/CPU benefit vs copying `recv`, and show small/unaligned receives fall back (§53.4.2, §53.5).
5. **DDIO + zero-copy.** Combine zero-copy RX with NIC-local placement (Ch. 16) and DDIO/CAT (Ch. 47, 61); confirm the first read is both no-copy *and* L3-hot (§53.4.2).

**Checklist — zero-copy**

- [ ] Zero-copy is applied where the **copy is a measured bottleneck** (bulk paths — capture/replay/snapshots, Ch. 90/58), **not** the small-order hot path (§53.3, §53.5).
- [ ] Zero-copy **TX buffers are reclaimed only on completion** (error queue / CQE); the in-flight set is tracked and bounded — no early reuse (§53.4.1, §53.5).
- [ ] The **copy-fallback status is checked**; you're actually getting zero-copy, not carrying complexity for nothing (§53.4.1).
- [ ] Zero-copy **RX data is page-aligned**; small/control traffic uses copying (§53.4.2).
- [ ] Pages are **pinned once via registered buffers** (io_uring, Ch. 52) and reused, not pinned per-op; pinning limits respected (§53.4.3).
- [ ] Zero-copy is combined with **NIC-local placement (Ch. 16) and DDIO/CAT (Ch. 47, 61)** so the first read is no-copy *and* cache-hot (§53.4.2).
- [ ] For the **lowest-latency order send**, full bypass (Ch. 67) / inline hardware (Ch. 74) is used; zero-copy is the middle ground for kernel-retaining paths (§53.5).
- [ ] **devmem TCP** is considered for NIC→accelerator (GPU/FPGA — Ch. 72/74) pipelines where the payload should never touch host memory (§53.4.4).

## 53.7 References

- The Linux kernel networking documentation on **`MSG_ZEROCOPY`** (`Documentation/networking/msg_zerocopy.rst`), **`TCP_ZEROCOPY_RECEIVE`**, and **devmem TCP** (`Documentation/networking/devmem.rst`) — the authoritative mechanism references (§53.4).
- The **io_uring** documentation and `liburing` examples for zero-copy send (`SEND_ZC`), zero-copy receive, registered/provided buffers, and multishot (Ch. 52 references; §53.4.3).
- Willem de Bruijn's and the netdev community's talks/papers on kernel zero-copy networking — the design and the threshold behavior (§53.3).
- Ch. 51 (native I/O — the copying baseline), Ch. 52 (io_uring), Ch. 60 (NIC/DMA), Ch. 67 (bypass — the alternative), Ch. 71 (PCIe/P2P), Ch. 61 (DDIO — where RX lands), Ch. 72/74 (the accelerators devmem targets).

## 53.8 Additional Reading

- Netdev conference talks on `MSG_ZEROCOPY`, io_uring zero-copy, and devmem TCP — the evolution of the kernel fast path (§53.1, §53.4).
- The GPUDirect / devmem TCP / dmabuf materials (NVIDIA and kernel) — payload-to-device-memory receive (§53.4.4; Ch. 71–72).
- Ch. 64 (*Precise Transmission*) — pairs with zero-copy TX for bulk egress; Ch. 62 (*Flow Steering*) — steering RX to the core/buffer that owns it; Ch. 67 (*Kernel Bypass*) — the fuller-bypass alternative; Ch. 7/16 — the cache and memory-bandwidth costs the copy spends; **Appendix F** — the zero-copy / `MSG_ZEROCOPY` / devmem TCP glossary entries.

---

*Next: Ch. 54 — Inter-Process Communication, stepping off the network and onto the host: shared-memory queues, lock-free IPC, pipes vs shm, and cross-process ring buffers — how the cooperating processes of a trading system (Ch. 86) exchange data without a syscall.*
