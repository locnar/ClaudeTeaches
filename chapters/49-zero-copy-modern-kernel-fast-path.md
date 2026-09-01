# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 49 — Zero-Copy & the Modern Kernel Fast Path

> **Prerequisites:** Ch. 47 (native I/O — the copying stack this improves on), Ch. 48 (io_uring — the interface most of this rides), Ch. 23–24 (buffer lifetime and pinning). Relates forward to the networking hardware developed in Parts IX–X: the NIC as the DMA far end (Ch. 55), where RX lands (DDIO — Ch. 56), the PCIe/DMA boundary (Ch. 66), and full kernel bypass (Ch. 62) — the alternative this is a middle ground to.
>
> **Leads into:** Ch. 59 (precise transmission — pairs with zero-copy TX), Ch. 57 (flow steering — gets the packet to the core that owns the zero-copy buffer). The current frontier of the *kernel* fast path, between the io_uring of Ch. 48 and the full bypass of Ch. 62: removing the copies without leaving the kernel entirely.

---

## 49.1 Why it matters: the copy is a cost you pay on every byte

Kernel bypass (Ch. 62) is the lowest-latency path, but it's not free — it means giving up the kernel's TCP stack, its sockets API, its security, and running a poll-mode driver that burns a core. Many systems want *most* of the kernel-bypass win without that cost, and the single biggest inefficiency in the *kernel* network path is the **copy**: the standard `read`/`write`/`recv`/`send` path copies every byte between the kernel's socket buffers and your userspace buffer (Ch. 47). On receive, the NIC DMAs the packet into a kernel buffer, then the kernel *copies* it into your buffer on `recv`. On send, the kernel *copies* your buffer into a socket buffer before the NIC DMAs it out. At market-data rates (Ch. 53) or on a busy order path, that copy is memory bandwidth (Ch. 16) and cache pollution (Ch. 7) and latency you pay on *every byte of every message* — a tax the fast path shouldn't pay.

**Zero-copy** networking removes that copy while keeping the kernel stack: the NIC DMAs directly to (or from) *your* buffer, and the kernel just manages the metadata. This is the frontier the mainline Linux kernel has been building out — `MSG_ZEROCOPY` for send, `TCP_ZEROCOPY_RECEIVE` for receive, io_uring's zero-copy send and (very recently) zero-copy receive, and the bleeding-edge **devmem TCP** that DMAs payload directly into GPU/FPGA memory. Together they close much of the gap to full bypass (Ch. 62) *without* abandoning the sockets API, the kernel TCP stack (Ch. 52), or the kernel's security and multiplexing — a genuinely different point on the latency/complexity curve (Ch. 62's middle ground, made concrete).

The catch, and the reason zero-copy is a *technique* and not just a flag, is that removing the copy **changes the buffer ownership model**. A normal `send` is done with your buffer when it returns; a zero-copy send is *not* — the NIC reads your buffer asynchronously, so you must not touch or reuse it until the kernel *notifies* you the DMA completed. Zero-copy trades a synchronous copy for asynchronous buffer lifetime management (Ch. 23–24), and getting that lifetime wrong is a correctness bug (send garbage) or a use-after-free (Ch. 72). This chapter explains where the copies are and how each zero-copy mechanism removes them (§49.2), measures the copy cost and zero-copy's fixed overhead (§49.3), details `MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy, and devmem TCP (§49.4), and warns about buffer lifetime and the small-message trap (§49.5). It's the "keep the kernel, lose the copy" chapter.

## 49.2 Mental model: where the copies are, and how zero-copy removes them

Trace a byte through the standard kernel path and mark every copy:

```
   RX (standard):  NIC --DMA--> [kernel skb/socket buf] --COPY(recv)--> [your buffer] --> app
   RX (zero-copy): NIC --DMA--> [your buffer, mmap'd/registered] ................. --> app   (no copy)

   TX (standard):  app --> [your buffer] --COPY(send)--> [kernel socket buf] --DMA--> NIC
   TX (zero-copy): app --> [your buffer, pinned] .................... --DMA(from your buf)--> NIC
                                                                        ▲ async: buffer busy until completion notified
```

The two directions have different mechanisms and different gotchas:

- **Zero-copy TX (`MSG_ZEROCOPY`, io_uring `SEND_ZC`).** The kernel **pins** your userspace pages and hands their physical addresses to the NIC, which DMAs *directly from your buffer*. No copy — but the buffer is now **in use by the NIC asynchronously**, so `send` returns *before* the data is on the wire, and you must wait for a **completion notification** (on the socket error queue for `MSG_ZEROCOPY`, or the io_uring CQE) before reusing the buffer. The cost moved from a copy to page-pinning + async lifetime management.
- **Zero-copy RX (`TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy RX).** Harder, because on receive the NIC decides where data lands *before* you've asked for it. The mechanisms: **mmap the receive stream** into your address space so the kernel maps (page-flips) the received pages into userspace instead of copying (`TCP_ZEROCOPY_RECEIVE`), or **pre-register buffers** the NIC DMAs into and get told which one filled (io_uring zero-copy RX). Both need **page alignment** and work best for larger transfers (the page-flip/mapping has fixed cost — small messages don't amortize it, §49.5).
- **devmem TCP (payload to device memory).** The newest: **header–data split** at the NIC — headers go to the host (for the kernel stack to process), *payload* DMAs directly into **device memory** (a GPU or FPGA, via a `dmabuf`), never touching host DRAM or host cache. For a NIC→accelerator pipeline (Ch. 67/69) this is the ultimate zero-copy: the host CPU never sees the payload bytes at all.

The unifying idea: **zero-copy makes the NIC DMA to/from the *final* buffer, eliminating the intermediate kernel copy — at the price of managing buffer lifetime asynchronously and respecting alignment.** It's a middle ground between the copying kernel stack (Ch. 47 — simple, slower) and full bypass (Ch. 62 — fastest, most complex): you keep the kernel and sockets but pay attention to buffers.

## 49.3 Measure it: copy cost, the zero-copy threshold, and RX latency

Three measurements: what the copy *costs*, where zero-copy TX *pays off* (it has fixed overhead), and the zero-copy RX benefit.

- **The copy cost.** Measure send/receive latency and CPU for a copying path vs zero-copy at increasing message sizes, and watch memory bandwidth (Ch. 16) and cache misses (Ch. 7) from the copy. Representative (Linux, modern NIC; figures pending real runs): for large messages the copy is a real fraction of the send/receive time and a large fraction of the CPU and memory bandwidth; for small messages it's negligible.
- **The zero-copy TX threshold.** `MSG_ZEROCOPY` has **fixed per-operation overhead** (pinning pages, tracking completion) that only pays off above a size threshold — small sends are *slower* zero-copy than copied. Measure the crossover:

| Message size | Copying `send` | `MSG_ZEROCOPY` | Winner |
|---|---|---|---|
| ~100 bytes (an order) | fast (copy is cheap) | slower (pinning overhead > copy) | **copy** |
| ~1–10 KB | copy noticeable | pinning amortized | ~even |
| ~64 KB+ (bulk, capture — Ch. 75) | copy dominates | much faster, less CPU | **zero-copy** |

  The lesson for HFT: a **single small order is not a zero-copy TX candidate** — the order is tiny, the copy is cheap, and pinning overhead dominates. Zero-copy TX shines for *bulk* (capture/journal streaming — Ch. 75, market-data replay, snapshots — Ch. 54), not the single-order hot path. Know which you're optimizing.
- **Zero-copy RX latency and CPU.** Measure receive latency and CPU with `TCP_ZEROCOPY_RECEIVE` / io_uring zero-copy RX vs copying `recv`, for the message sizes you actually handle. Zero-copy RX helps most for **large, page-aligned** receives (bulk recovery — Ch. 54, capture); for small unaligned messages the mapping overhead can exceed the copy saved (§49.5).

The lessons: the copy is a per-byte cost that matters at *volume* (bulk paths) more than for a single small message; zero-copy TX has a *fixed overhead* that makes it a bulk technique, not a small-order one; and zero-copy RX wants large, aligned transfers. Measure the crossover on your sizes — zero-copy is not universally faster, and applying it to tiny messages is a pessimization (§49.5).

## 49.4 Techniques

### 49.4.1 Zero-copy send: `MSG_ZEROCOPY` / `SO_ZEROCOPY`

- **Enable and use it.** Set `SO_ZEROCOPY` on the socket, then pass `MSG_ZEROCOPY` to `send`/`sendmsg`. The kernel pins your buffer's pages and the NIC DMAs from them. Use it for **bulk** egress (capture/journal to a collector — Ch. 75, market-data or snapshot replay — Ch. 54), not the single-order path (§49.3).
- **Manage the completion notification.** `send` returns before the data is on the wire; the kernel posts a completion on the **socket error queue** (`recvmsg` with `MSG_ERRQUEUE`), carrying a range of sequence numbers that identify which sends completed. You **must not reuse a buffer until its completion arrives** — track in-flight buffers (a ring of buffers, Ch. 24) and reclaim on completion. This is the asynchronous-lifetime discipline that makes zero-copy a technique, not a flag.
- **Watch for the "copied anyway" fallback.** The completion notification tells you whether the kernel actually did zero-copy or fell back to copying (it can, e.g. for small or unsuitable sends). Check it — if you're always getting the copy fallback, zero-copy is buying you nothing but complexity (§49.5).

### 49.4.2 Zero-copy receive: `TCP_ZEROCOPY_RECEIVE` and mmap

- **mmap-based receive.** `TCP_ZEROCOPY_RECEIVE` (via `getsockopt`/`setsockopt` with a control structure) maps received, page-aligned payload directly into your address space — the kernel page-flips the pages instead of copying. You process the data in place, then release the pages. Best for **large, aligned** streams (bulk recovery, capture — Ch. 75/54).
- **Alignment is mandatory.** Zero-copy RX requires page alignment (the kernel maps whole pages); mis-aligned or small receives can't be mapped and fall back to copy. Design the receive path (buffer sizes, message framing) for page-aligned bulk where you want zero-copy RX; accept copying for small control messages.
- **Combine with DDIO awareness (Ch. 56).** Zero-copy RX lands data in *your* pages; whether those pages are L3-hot (DDIO, Ch. 56) still depends on NIC-local placement (Ch. 16) and cache residency. Zero-copy removes the *copy*; DDIO/CAT (Ch. 44, 56) govern whether the *first read* is a cache hit. Use them together.

### 49.4.3 io_uring zero-copy send and receive

- **io_uring `SEND_ZC` (zero-copy send).** io_uring (Ch. 48) unifies zero-copy send with its async model: submit a zero-copy send, get a CQE when the buffer is free to reuse (the completion notification, integrated into the ring rather than a separate error queue). Cleaner than raw `MSG_ZEROCOPY` for an io_uring-based app — the completion is just another CQE. Pair with **registered buffers** (Ch. 48) so the pages are pre-pinned (no per-send pinning cost — removing part of the §49.3 fixed overhead).
- **io_uring zero-copy receive (new).** The newest addition: pre-register a pool of buffers and a fill/completion ring; the NIC DMAs incoming data into your registered buffers and io_uring tells you which buffer filled — zero-copy RX with the ergonomics of io_uring's provided-buffer model. This is the direction the kernel fast path is converging on: **registered buffers + zero-copy + async completion**, approaching bypass throughput while staying in the kernel.
- **Multishot + provided buffers (Ch. 48).** Combine zero-copy with multishot receive (one submission, many completions) and ring-provided buffers so the kernel picks buffers from your pool — minimizing per-message overhead for high-rate receive. This is the modern high-rate UDP/multicast RX path without full bypass.

### 49.4.4 devmem TCP: payload straight to device memory

- **Header–data split to a `dmabuf`.** devmem TCP (bleeding edge, recent mainline) splits the packet: headers to the host (the kernel TCP stack processes them normally — Ch. 52), payload DMA'd directly into **device memory** exported as a `dmabuf` (a GPU or FPGA — Ch. 67/69). The host CPU never touches the payload bytes.
- **Where it fits: NIC→accelerator pipelines (Ch. 67/69).** For workloads that receive data and immediately hand it to a GPU (risk/pricing/ML inference — Ch. 67) or an FPGA, devmem TCP removes the host-memory bounce entirely — the data goes wire → NIC → accelerator memory, host stack only steering it. It's the receive-side analog of peer-to-peer DMA (Ch. 66, GPUDirect) done through the *TCP stack*.
- **Emerging, know the constraints.** It requires NIC support (header-data split, flow steering to the dmabuf — Ch. 57), a supporting driver, and recent kernels; it's not yet a commodity path. Track it as the frontier of "the payload never touches the host," and use it where a NIC-to-accelerator path justifies the complexity.

## 49.5 Pitfalls & anti-patterns: buffer lifetime and the small-message trap

- **Reusing a zero-copy buffer before completion (the cardinal correctness bug).** With `MSG_ZEROCOPY` / `SEND_ZC`, `send` returns before the NIC has read the buffer; touching or reusing it before the completion notification sends **garbage** (whatever you overwrote) or is a use-after-free (Ch. 72). Track in-flight buffers and reclaim only on completion (§49.4.1). This is the single most important zero-copy discipline.
- **Applying zero-copy TX to small messages (the pessimization).** Zero-copy send has fixed per-op overhead (pinning, completion tracking) that exceeds the cost of copying a small buffer (§49.3) — a single 100-byte order is *slower* zero-copy. Zero-copy TX is a *bulk* technique (capture, replay, snapshots — Ch. 75/54); don't apply it to the small-order hot path.
- **Ignoring the copy-fallback.** The kernel can silently fall back to copying (small/unsuitable sends) while you carry all the zero-copy complexity. Check the completion status; if you're always falling back, you've added lifetime-management burden for no benefit (§49.4.1).
- **Zero-copy RX without alignment.** `TCP_ZEROCOPY_RECEIVE` needs page-aligned data; unaligned/small receives fall back to copy or fail. Design framing/buffers for alignment where you want zero-copy RX; accept copying for small control traffic (§49.4.2).
- **Page-pinning cost and limits.** Pinning pages for zero-copy consumes a limited resource and has cost; pinning huge numbers of small buffers (or leaking pinned buffers by not reclaiming on completion) exhausts it. Use registered buffers (Ch. 48) to pin once and reuse (§49.4.3), and bound the in-flight set.
- **Cache effects — zero-copy isn't automatically cache-hot.** Removing the copy saves bandwidth/CPU but doesn't by itself make the first read an L3 hit — that's DDIO/NUMA (Ch. 16, 56). Zero-copy + DDIO + NIC-local placement together give both no-copy *and* cache-hot; zero-copy alone can still land data in cold DRAM (§49.4.2).
- **Assuming zero-copy replaces bypass.** Zero-copy in the kernel stack is a *middle ground* (Ch. 62), not a replacement for full bypass on the very lowest-latency order path. For the tick-to-trade order send, bypass (Ch. 62) or inline hardware (Ch. 69) is still the floor; zero-copy is for the paths where keeping the kernel is worth it and copies are the bottleneck (§49.1).
- **Complexity without measurement.** Zero-copy adds real complexity (async lifetime, alignment, completion handling); adopt it only where the copy is a *measured* bottleneck (§49.3) — for a low-rate small-message path it's pure downside.

## 49.6 Exercises & checklist

**Exercises:**

1. **Copy cost curve.** Measure send/receive latency, CPU, and memory bandwidth for copying vs zero-copy across message sizes (100 B → 64 KB); find the crossover where zero-copy TX starts winning (§49.3).
2. **Zero-copy TX lifetime.** Implement `MSG_ZEROCOPY` send with an in-flight buffer ring reclaimed on error-queue completions; deliberately reuse a buffer early and observe the corruption (§49.4.1, §49.5).
3. **io_uring `SEND_ZC` + registered buffers.** Build a bulk sender with io_uring zero-copy send over registered buffers; compare CPU and latency to copying `send` and to raw `MSG_ZEROCOPY` (§49.4.3).
4. **Zero-copy RX.** Set up `TCP_ZEROCOPY_RECEIVE` (or io_uring zero-copy RX) for a large aligned stream; measure the latency/CPU benefit vs copying `recv`, and show small/unaligned receives fall back (§49.4.2, §49.5).
5. **DDIO + zero-copy.** Combine zero-copy RX with NIC-local placement (Ch. 16) and DDIO/CAT (Ch. 44, 56); confirm the first read is both no-copy *and* L3-hot (§49.4.2).

**Checklist:**

- [ ] Zero-copy is applied where the **copy is a measured bottleneck** (bulk paths — capture/replay/snapshots, Ch. 75/54), **not** the small-order hot path (§49.3, §49.5).
- [ ] Zero-copy **TX buffers are reclaimed only on completion** (error queue / CQE); the in-flight set is tracked and bounded — no early reuse (§49.4.1, §49.5).
- [ ] The **copy-fallback status is checked**; you're actually getting zero-copy, not carrying complexity for nothing (§49.4.1).
- [ ] Zero-copy **RX data is page-aligned**; small/control traffic uses copying (§49.4.2).
- [ ] Pages are **pinned once via registered buffers** (io_uring, Ch. 48) and reused, not pinned per-op; pinning limits respected (§49.4.3).
- [ ] Zero-copy is combined with **NIC-local placement (Ch. 16) and DDIO/CAT (Ch. 44, 56)** so the first read is no-copy *and* cache-hot (§49.4.2).
- [ ] For the **lowest-latency order send**, full bypass (Ch. 62) / inline hardware (Ch. 69) is used; zero-copy is the middle ground for kernel-retaining paths (§49.5).
- [ ] **devmem TCP** is considered for NIC→accelerator (GPU/FPGA — Ch. 67/69) pipelines where the payload should never touch host memory (§49.4.4).

## 49.7 References

- The Linux kernel networking documentation on **`MSG_ZEROCOPY`** (`Documentation/networking/msg_zerocopy.rst`), **`TCP_ZEROCOPY_RECEIVE`**, and **devmem TCP** (`Documentation/networking/devmem.rst`) — the authoritative mechanism references (§49.4).
- The **io_uring** documentation and `liburing` examples for zero-copy send (`SEND_ZC`), zero-copy receive, registered/provided buffers, and multishot (Ch. 48 references; §49.4.3).
- Willem de Bruijn's and the netdev community's talks/papers on kernel zero-copy networking — the design and the threshold behavior (§49.3).
- Ch. 47 (native I/O — the copying baseline), Ch. 48 (io_uring), Ch. 55 (NIC/DMA), Ch. 62 (bypass — the alternative), Ch. 66 (PCIe/P2P), Ch. 56 (DDIO — where RX lands), Ch. 67/69 (the accelerators devmem targets).

## 49.8 Additional Reading

- Netdev conference talks on `MSG_ZEROCOPY`, io_uring zero-copy, and devmem TCP — the evolution of the kernel fast path (§49.1, §49.4).
- The GPUDirect / devmem TCP / dmabuf materials (NVIDIA and kernel) — payload-to-device-memory receive (§49.4.4; Ch. 66–67).
- Ch. 59 (*Precise Transmission*) — pairs with zero-copy TX for bulk egress; Ch. 57 (*Flow Steering*) — steering RX to the core/buffer that owns it; Ch. 62 (*Kernel Bypass*) — the fuller-bypass alternative; **Appendix E** — copy/bandwidth costs; **Appendix F** — zero-copy/pinning/dmabuf glossary.

---

*Next: Ch. 50 — Inter-Process Communication, stepping off the network and onto the host: shared-memory queues, lock-free IPC, pipes vs shm, and cross-process ring buffers — how the cooperating processes of a trading system (Ch. 74) exchange data without a syscall.*
