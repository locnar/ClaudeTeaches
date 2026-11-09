# Part X — Kernel Bypass, RDMA & Transport

# Chapter 67 — InfiniBand Verbs & RDMA

> **Prerequisites:** Ch. 66 (kernel bypass — RDMA is bypass taken to remote *memory*), Ch. 26 (mmap / shared memory — RDMA is cross-host shared memory), Ch. 23 (pinned/registered memory), Ch. 34–35 (lock-free / single-writer publication — what you build over RDMA), Ch. 59, 62 (NIC / timestamps), Ch. 7 (the memory the remote side reads/writes).
>
> **Leads into:** Ch. 68 (the lossless fabric RoCEv2 needs), Ch. 72 (MPI — often runs over RDMA/InfiniBand), Ch. 70 (PCIe — the host-device boundary RDMA crosses), Ch. 74 (SmartNICs — on-NIC RDMA). The intra-datacenter extreme of low-latency messaging, complementing the kernel bypass of Ch. 66.

---

## 67.1 Why it matters: one-sided remote memory access

Kernel bypass (Ch. 66) removed the kernel from the *local* network path. **RDMA (Remote Direct Memory Access)** goes one step further: it lets one host **read or write another host's memory directly** — across the network, without involving the *remote* CPU at all. A "one-sided" RDMA write places data straight into a remote machine's memory; a one-sided RDMA read fetches it — and in both cases the **remote CPU is not interrupted, runs no code, and isn't even aware** the access happened (the remote NIC does the DMA — Ch. 70). This is a fundamentally different communication model from send/receive (where both sides participate): it's **cross-host shared memory** (Ch. 26, extended across the network), with single-digit-microsecond (and sub-microsecond on the best fabrics) latency. For intra-datacenter messaging — between your colocated trading hosts, to a risk cluster, to a market-data distribution tier — RDMA is the lowest-latency way to move data between machines.

The "one-sided" property is the key advantage and the conceptual leap. With a normal send/recv (even over kernel bypass — Ch. 66), the *receiver* must be running, polling, and processing — its CPU is involved in every message. With a one-sided RDMA **write**, the sender pushes data into the receiver's memory and the receiver's CPU does *nothing* until it chooses to look — so the receiver isn't on the critical path of the transfer, and the sender's latency doesn't depend on the receiver's responsiveness. This maps beautifully onto the publication patterns of Part VI: a single writer RDMA-writing a market-data snapshot (seqlock-style — Ch. 35) into many remote readers' memory, or a host writing an order directly into a gateway's buffer. It's the cross-machine version of the shared-memory IPC of Ch. 53 — same "write to shared memory, the reader sees it" model, but the "shared memory" spans hosts.

The cost — and why RDMA is a specialized, advanced tool — is the **verbs API** (low-level, explicit) and the **memory-registration** discipline. RDMA requires memory to be **registered** (pinned and made known to the NIC — Ch. 23) before the NIC can DMA to/from it, and registration is *expensive* (a syscall, pinning pages) — so it must happen at setup, not on the hot path (the same pre-allocation discipline as Part IV). The verbs API (queue pairs, completion queues, work requests) is more complex than sockets, and the fabric choice (native InfiniBand vs RoCE over Ethernet) has real implications. And RDMA, like kernel bypass (Ch. 66), is for *intra-datacenter* — it doesn't help your latency *to the exchange* (that's the wire and the exchange — Appendix K, Ch. 64, measured with Ch. 62's timestamps); it helps the latency *between your own machines*. This chapter explains the verbs/queue-pair mental model (§67.2), measures RDMA write vs send/recv latency (§67.3), details read/write vs send/recv, RoCE vs InfiniBand, and completion polling (§67.4), and warns about the registration/pinning costs (§67.5). Kernel bypass (Ch. 66) is for the wire; RDMA is for cross-host memory.

## 67.2 Mental model: queue pairs, completion queues, memory regions/registration, work requests

**RDMA — the NIC moves memory between hosts, bypassing both kernels and (one-sided) the remote CPU:**

```
   host A (initiator)                  network (InfiniBand / RoCE)        host B (target)
   ┌──────────────────┐                                                  ┌──────────────────┐
   │ registered memory│                                                  │ registered memory│
   │   (pinned, NIC-   │── RDMA WRITE: A's NIC DMAs data into B's mem ──►│   (the data just │
   │    known)         │     ... B's CPU is NOT involved (one-sided) ... │    APPEARS here) │
   │                  │◄─ RDMA READ:  A's NIC DMAs data FROM B's mem ───│                  │
   └──────────────────┘                                                  └──────────────────┘
     post a Work Request to a Queue Pair → NIC does the transfer → a Completion appears in the CQ (poll it)
```

The verbs objects (the API you program against — `libibverbs`):

- **Queue Pair (QP) — the connection endpoint.** A QP is a **send queue** + **receive queue**; you post **Work Requests** (WRs) to it (an RDMA write/read, a send, a recv) and the NIC processes them. A QP connects to a remote QP. QP types: **RC (Reliable Connected)** — reliable, in-order, supports RDMA read/write (the usual choice); **UD (Unreliable Datagram)** — connectionless, send/recv only; **UC** — unreliable connected. RC is the default for low-latency reliable messaging.
- **Completion Queue (CQ) — how you learn a transfer finished.** When a WR completes, the NIC posts a **Completion (WC)** to the CQ; you **poll** the CQ (or use events) to learn the transfer is done. Polling the CQ (busy-poll — Ch. 43) is the low-latency way (no interrupts — §67.4.3).
- **Memory Region (MR) — registered memory.** Before the NIC can DMA to/from a buffer, you **register** it (`ibv_reg_mr`) — which **pins** the pages (Ch. 23, so they can't be swapped/moved — the NIC DMAs to physical addresses) and returns local/remote **keys** (lkey/rkey). One-sided RDMA needs the remote's address + rkey (exchanged at connection setup). **Registration is expensive (pin + syscall) — do it at setup** (§67.5).
- **Work Requests (WRs) — what you ask the NIC to do.** Post a WR to the QP's send queue: an **RDMA WRITE** (DMA local data into remote memory at a given address+rkey), **RDMA READ** (DMA remote data into local memory), **SEND** (two-sided — the remote must have posted a RECV), or atomics (remote compare-and-swap/fetch-add — Ch. 30, across the network!). The NIC executes it without the CPU (after you post it).

**One-sided vs two-sided — the key distinction (§67.4.1):**

- **One-sided (RDMA WRITE / READ):** the *remote* CPU is **not involved** — A's NIC directly accesses B's registered memory. Lowest latency, no remote-CPU dependency, but A must know B's memory address+rkey, and B doesn't *know* when its memory changed (it must poll/be-signaled). The cross-host shared-memory model.
- **Two-sided (SEND / RECV):** *both* CPUs participate — B must post a RECV buffer before A's SEND arrives; B's CQ signals it got a message. Like a normal message, but kernel-bypassed. Simpler semantics, but the remote CPU is on the path.

The whole API in one minimal program — connection setup through `rdma_cm` (which drives the QP state machine RESET → INIT → RTR → RTS and exchanges the remote `{addr, rkey}` in the connection's private data), then the data path:

```cpp
// rdma_write.cpp — client side of a minimal RC one-sided RDMA WRITE: rdma_cm for connection setup, verbs for the data path.
// Build: g++ -O2 -std=c++20 rdma_write.cpp -o rdma_write -libverbs -lrdmacm        (rdma-core 4x)
// Server (not shown): rdma_listen/rdma_accept with the same PD/CQ/QP setup, registers its buffer with
// IBV_ACCESS_REMOTE_WRITE, returns {addr, rkey} in rdma_accept's private_data, then POLLS the buffer (§67.4.1).
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <netdb.h>
#include <infiniband/verbs.h>
#include <rdma/rdma_cma.h>

struct RemoteMr { std::uint64_t addr; std::uint32_t rkey; };

int main(int argc, char** argv) {
    if (argc < 2) return 1;
    rdma_event_channel* ec = rdma_create_event_channel();
    rdma_cm_id* id = nullptr;  rdma_create_id(ec, &id, nullptr, RDMA_PS_TCP);        // RDMA_PS_TCP = RC semantics
    addrinfo* ai = nullptr;    getaddrinfo(argv[1], "7471", nullptr, &ai);
    rdma_cm_event* ev = nullptr;
    rdma_resolve_addr(id, nullptr, ai->ai_addr, 2000); rdma_get_cm_event(ec, &ev); rdma_ack_cm_event(ev);   // ADDR_RESOLVED
    rdma_resolve_route(id, 2000);                       rdma_get_cm_event(ec, &ev); rdma_ack_cm_event(ev);   // ROUTE_RESOLVED

    // --- SETUP (all of this is setup cost, never per message — §67.5): PD, CQ, QP, registered memory
    ibv_pd* pd = ibv_alloc_pd(id->verbs);
    ibv_cq* cq = ibv_create_cq(id->verbs, 64, nullptr, nullptr, 0);
    ibv_qp_init_attr qa{}; qa.send_cq = cq; qa.recv_cq = cq; qa.qp_type = IBV_QPT_RC;
    qa.cap.max_send_wr = 64; qa.cap.max_recv_wr = 1; qa.cap.max_send_sge = 1; qa.cap.max_recv_sge = 1; qa.cap.max_inline_data = 64;
    rdma_create_qp(id, pd, &qa);                     // rdma_cm walks the QP through RESET -> INIT -> RTR -> RTS during connect
    alignas(64) static std::uint8_t buf[4096];
    ibv_mr* mr = ibv_reg_mr(pd, buf, sizeof buf, IBV_ACCESS_LOCAL_WRITE);   // pins the pages: microseconds to milliseconds
    rdma_conn_param cp{}; cp.initiator_depth = 1; cp.responder_resources = 1; cp.retry_count = 7;
    rdma_connect(id, &cp); rdma_get_cm_event(ec, &ev);                       // ESTABLISHED, carrying the server's {addr, rkey}
    RemoteMr rem{}; std::memcpy(&rem, ev->param.conn.private_data, sizeof rem); rdma_ack_cm_event(ev);

    // --- HOT PATH: one-sided WRITE of a 64-byte snapshot + sequence into the remote buffer, then reap the completion
    for (std::uint64_t seq = 1; seq <= 1000; ++seq) {
        std::memcpy(buf, &seq, sizeof seq);                                   // payload; "data, then flag" is Ch. 35's layout
        ibv_sge sge{}; sge.addr = reinterpret_cast<std::uint64_t>(buf); sge.length = 64; sge.lkey = mr->lkey;
        ibv_send_wr wr{}; ibv_send_wr* bad = nullptr;
        wr.opcode = IBV_WR_RDMA_WRITE; wr.sg_list = &sge; wr.num_sge = 1;
        wr.send_flags = IBV_SEND_SIGNALED | IBV_SEND_INLINE;                  // inline: 64 B ride in the WQE, no second DMA read
        wr.wr.rdma.remote_addr = rem.addr; wr.wr.rdma.rkey = rem.rkey;        // IBV_WR_RDMA_WRITE_WITH_IMM (+ wr.imm_data) would
        if (ibv_post_send(id->qp, &wr, &bad)) return 2;                       //   also consume a server RECV and raise a CQE there
        ibv_wc wc{};
        while (ibv_poll_cq(cq, 1, &wc) == 0) { }                              // busy-poll the CQ (Ch. 43); never ibv_get_cq_event here
        if (wc.status != IBV_WC_SUCCESS) { std::fprintf(stderr, "wc: %s\n", ibv_wc_status_str(wc.status)); return 3; }
    }
    rdma_disconnect(id); ibv_dereg_mr(mr); rdma_destroy_qp(id); ibv_destroy_cq(cq); ibv_dealloc_pd(pd); rdma_destroy_id(id);
    return 0;
}
```

The model: **RDMA lets a NIC read/write a remote host's *registered* (pinned) memory directly via Work Requests posted to Queue Pairs, with Completions reaped from a Completion Queue (polled). One-sided WRITE/READ don't involve the remote CPU at all (cross-host shared memory, lowest latency); two-sided SEND/RECV involve both. Memory must be registered (pinned) at setup — registration is expensive. It's kernel bypass extended to remote memory.**

## 67.3 Measure it: RDMA write vs send/recv latency

Measure the RDMA latency tiers — one-sided **WRITE** (lowest, no remote CPU), **READ** (a round-trip), **SEND/RECV** (two-sided) — vs the kernel-bypass send/recv (Ch. 66) and the kernel stack (Ch. 50), and the **registration cost** (which must be off the hot path).

```
   # Tools: perftest (ib_write_lat, ib_read_lat, ib_send_lat) — the standard RDMA latency benchmarks.
   $ ib_write_lat <server>     # one-sided RDMA write latency (half round-trip / ping-pong)
   $ ib_read_lat  <server>     # RDMA read (a full round-trip — fetch from remote)
   $ ib_send_lat  <server>     # two-sided send/recv latency
   # measure registration cost: time ibv_reg_mr for various buffer sizes (a SETUP cost — §67.5)
```

Representative results (toolchain and machine: Appendix L.3) — two reference hosts with ConnectX-6 HDR100 (native IB) or RoCEv2 on 100 GbE, one switch hop, rdma-core 4x / perftest 4.5, polled CQ on an isolated core (illustrative; the *tiers* are the point; a ConnectX-5 vs -7 generation moves these ~2×). Rows marked one-way are half the ping-pong RTT, as `perftest` reports them:

```
   operation                          one-way / round-trip latency      note
   RDMA WRITE (one-sided)             ~0.8-1.5 µs one-way                remote CPU NOT involved — lowest
   RDMA READ (one-sided)              ~1.5-2.5 µs (RTT — inherently a round trip)  a full round-trip to fetch
   SEND/RECV (two-sided)              ~1.2-2 µs one-way                  both CPUs involved
   (context) kernel-bypass UDP (Ch.66) ~1-2 µs (local)                  comparable for local send/recv
   (context) kernel stack TCP (Ch.50)  ~5-20 µs                         the baseline RDMA beats

   memory REGISTRATION (ibv_reg_mr):  ~µs to ms (pins pages + syscall)  — a SETUP cost, NEVER on hot path (§67.5)
```

Read it: RDMA delivers **single-digit-microsecond cross-host** latency (WRITE and SEND/RECV sub-2 µs one-way; READ ~2–2.5 µs as a full round trip) — far below the kernel stack (Ch. 50, ~5-20µs) and comparable to local kernel bypass (Ch. 66), but *across machines*. The **one-sided WRITE is lowest** (the remote CPU isn't involved — the NIC just DMAs the data in), **READ is a round-trip** (fetch from remote — higher), and **SEND/RECV is two-sided** (both CPUs). The tier choice maps to the access pattern (§67.4.1): WRITE to *push* data to a known remote location (publication — Ch. 35), READ to *pull* from remote, SEND/RECV for message-passing semantics. The crucial operational number is **registration**: `ibv_reg_mr` costs microseconds-to-milliseconds (it pins pages and syscalls — Ch. 23) — so it's a **setup** cost; registering memory on the hot path would inject that latency into every message (§67.5). Register all RDMA buffers at startup (the pre-allocation discipline of Part IV). The measurement discipline: **poll the CQ** (busy-poll — Ch. 43, no interrupts) on a dedicated core, **register at setup**, and use the standard `perftest` tools (`ib_*_lat`) plus hardware timestamps (Ch. 59, 62) for the true numbers. RDMA is the intra-DC messaging floor.

## 67.4 Techniques

### 67.4.1 RDMA read/write vs send/recv

Choosing the RDMA operation for the access pattern:

- **RDMA WRITE — push to a known remote location (the publication pattern).** When you know *where* in the remote's memory to put data (its address+rkey, exchanged at setup), WRITE is lowest-latency and doesn't involve the remote CPU. Maps onto **single-writer publication** (Ch. 35): a host RDMA-writes a market-data snapshot / BBO / state directly into many remote readers' memory; the readers poll their local copy (which the remote NIC updated). The cross-host seqlock. Also: writing an order into a gateway's buffer, updating a remote ring buffer (Ch. 34) over RDMA.
- **RDMA READ — pull from remote.** When you need to *fetch* remote data on demand (read a remote value/structure) — a round-trip (higher latency than WRITE — §67.3). Use when the consumer decides *when* to read (vs WRITE where the producer pushes). Less common on the hot path than WRITE-publication.
- **SEND/RECV — two-sided message passing.** When you want *message* semantics (the remote is notified it got a message, processes it) — the remote posts RECV buffers, you SEND. Both CPUs involved. Use for request/response or where the remote must *act* on each message (vs WRITE-publication where the remote passively reads). Simpler to reason about; the remote CPU is on the path.
- **RDMA atomics — remote compare-and-swap / fetch-add (Ch. 30).** The NIC can do **atomic** operations on remote memory (atomic CAS, fetch-add) — enabling lock-free coordination *across hosts* (a distributed counter, a remote lock). Powerful for distributed data structures, but slower than WRITE and with caveats (atomicity scope, alignment). Niche but unique to RDMA.
- **Choosing.** Push-publication (one writer, many readers, latest value) → **WRITE** (the common HFT pattern, ties Ch. 35). On-demand fetch → **READ**. Message/request-response → **SEND/RECV**. Cross-host atomic coordination → **atomics**. Match the verb to the data-flow pattern, preferring one-sided WRITE where it fits (lowest latency, no remote-CPU dependency).

### 67.4.2 RoCEv2 vs native InfiniBand

The fabric choice — the network RDMA runs over:

- **Native InfiniBand (IB) — purpose-built RDMA fabric.** A dedicated, lossless, low-latency interconnect (HCAs, IB switches, the IB protocol stack) — the lowest latency and the original RDMA fabric. Requires IB hardware (separate from Ethernet) — a parallel network. The choice where absolute lowest latency and a dedicated fabric are warranted (HPC, some HFT clusters).
- **RoCE (RDMA over Converged Ethernet) — RDMA on Ethernet.** Runs the RDMA verbs over **Ethernet** (RoCEv1 = layer 2; **RoCEv2** = routable, over UDP/IP — the common one) — so you use your Ethernet infrastructure (and routing) instead of a separate IB fabric. Slightly higher latency than native IB, but **converged** (one network) and uses standard Ethernet switches. The pragmatic choice for many datacenters (including cloud — Ch. 72's MPI often runs RoCE).
- **The lossless requirement (PFC/ECN).** RDMA assumes a (near-)**lossless** network (it doesn't have TCP's heavy retransmit; packet loss is costly). Native IB is lossless by design; **RoCE needs the Ethernet fabric configured lossless** — **PFC** (Priority Flow Control) and **ECN**/DCQCN congestion control — or it performs badly under loss/congestion. Configuring lossless RoCE (PFC/ECN tuning) is the main RoCE operational challenge; misconfiguration causes drops and latency spikes — the subject of Ch. 68.
- **iWARP — RDMA over TCP.** A third option: RDMA over a TCP/IP stack (offloaded to the NIC) — works over standard routed networks without lossless config, but higher latency than RoCE/IB. Less common for low-latency.
- **The decision.** Native IB: lowest latency, dedicated fabric, more cost/complexity (separate network). RoCEv2: converged Ethernet, slightly higher latency, needs lossless config (PFC/ECN). iWARP: most compatible, highest latency. For HFT intra-DC, native IB or well-tuned RoCEv2; the choice weighs latency vs infrastructure convergence.

### 67.4.3 Polling completions; registered-memory/zero-copy

The low-latency RDMA discipline (ties Ch. 23, 43):

- **Poll the CQ, don't wait for events (Ch. 43).** Reap completions by **busy-polling** the Completion Queue (`ibv_poll_cq` in a loop) on a dedicated core (Ch. 44, 47) — *not* by waiting for a completion event/interrupt (which adds latency + a context switch — Ch. 43). Polling is the low-latency model, the same as kernel-bypass polling (Ch. 66) and the busy-poll discipline throughout (Ch. 43). Burns a core; accepted for the hot path.
- **Register memory at setup, reuse it (§67.5, Ch. 23).** Register all RDMA buffers **once at startup** (`ibv_reg_mr` pins them — expensive, §67.3) and **reuse** them for every transfer — the pre-allocation/pool discipline of Part IV applied to RDMA. Never register on the hot path. Use registered buffers as a pool (Ch. 24). Pre-fault/`mlock` is implied (registration pins).
- **Zero-copy by construction.** RDMA is inherently zero-copy: the NIC DMAs directly between registered application buffers on both hosts — no copies, no kernel buffers. The data the remote NIC writes appears *in your application's memory* (Ch. 56's decode reads it in place). This is the "registered-memory/zero-copy" win — true end-to-end zero-copy across hosts.
- **Inline data for tiny messages.** For very small payloads, RDMA **inline** (`IBV_SEND_INLINE`) puts the data *in the work request* itself (no separate buffer DMA) — lower latency for small messages (avoids a DMA of the payload). A useful optimization for small orders/updates.
- **Doorbell batching and unsignaled completions.** Post multiple WRs and ring the doorbell once (batching — Ch. 37, 66); use **unsignaled** completions (don't generate a CQ entry for every WR, only periodically) to reduce CQ-polling overhead for high-rate sends. The RDMA analogs of the batching throughout the networking chapters.

## 67.5 Pitfalls & anti-patterns: registration cost, memory pinning

- **Registering memory on the hot path (the cardinal RDMA bug).** `ibv_reg_mr` pins pages and syscalls — microseconds-to-milliseconds (§67.3) — so registering a buffer per message injects that into every transfer's latency. **Register all RDMA buffers at setup** and reuse them (a registered pool — §67.4.3, Ch. 24). Hot-path registration is the RDMA equivalent of hot-path `malloc` (Ch. 23).
- **Pinning too much memory.** Registration **pins** pages (they can't be swapped/moved — Ch. 23) — registering huge regions pins lots of RAM (and there are limits, `RLIMIT_MEMLOCK`). Register only what you need; reuse a bounded registered pool. Over-registration wastes (pinned) memory.
- **Waiting for completion events instead of polling.** Using CQ *events* (interrupt-driven completion notification) instead of busy-**polling** adds latency + a context switch (Ch. 43) — defeating RDMA's latency. **Poll the CQ** on a dedicated core (§67.4.3).
- **RoCE without lossless config (PFC/ECN).** RoCE assumes a (near-)lossless fabric; running it over a standard (lossy) Ethernet without **PFC/ECN** tuning causes drops → poor performance / latency spikes (§67.4.2, Ch. 68). Configure the fabric lossless (Ch. 68), or use native IB. Misconfigured RoCE is a common operational failure.
- **One-sided WRITE without knowing when the remote sees it.** A one-sided WRITE updates remote memory but the **remote CPU isn't notified** — the remote must *poll* its memory (or you signal via a separate mechanism: a flag the remote polls, written after the data — the seqlock/publication ordering of Ch. 30, 35, now across the network). Forgetting that the remote needs a way to *notice* the write is a correctness gap. The verbs API has a first-class answer: **`IBV_WR_RDMA_WRITE_WITH_IMM`** places the data one-sided *and* consumes a posted RECV on the responder, yielding a CQ entry (with a 32-bit immediate) the remote polls — no memory scanning. Otherwise publish the flag/sequence as a *separate* WR on the same QP after the data (RC delivers WRs in order; within one WRITE the NIC writes bytes in no guaranteed order, so a flag inside the payload can land before the data) — Ch. 35.
- **Memory ordering across RDMA (Ch. 30).** RDMA writes complete in an order, and the remote reads them — the cross-host analog of the memory model (Ch. 30). Ensure the data is fully written before the flag/sequence that signals it (write ordering / RDMA completion semantics) — a torn/early read otherwise (Ch. 35's torn-read concern, across hosts). Understand the RDMA ordering/completion guarantees.
- **Assuming RDMA helps latency *to the exchange*.** RDMA is **intra-datacenter** (between *your* hosts) — it doesn't reduce latency to the exchange (that's the wire + exchange — Appendix K, Ch. 64; measured with Ch. 62's timestamps). It's for your internal host-to-host messaging (risk cluster, distribution tier, gateway). Don't expect it to help the external leg.
- **Verbs API complexity / correctness.** The verbs API (QPs, CQs, MRs, WRs, state transitions) is low-level and error-prone; QP setup/teardown, connection management (RDMA CM), and error handling are intricate. Use a higher-level library (`librdmacm`, UCX, or a messaging layer like the one MPI — Ch. 72 — uses) where it fits, and test thoroughly (Ch. 42). Hand-rolling raw verbs is for the ultra-hot path with the engineering to back it.
- **IOMMU / security (Ch. 70, 83).** RDMA gives a remote NIC DMA access to registered memory — a security consideration (a compromised/buggy peer could read/write your registered memory within the rkey scope). Scope rkeys tightly, configure the IOMMU (Ch. 70), and treat RDMA peers with appropriate trust (Ch. 83).

## 67.6 Exercises & checklist

**Exercises**

1. **RDMA latency tiers.** Use `perftest` (`ib_write_lat`/`ib_read_lat`/`ib_send_lat`) between two RDMA hosts; record the latencies. Confirm WRITE < SEND/RECV (one-way, sub-2 µs) < READ (a full round trip, ~2–2.5 µs), all far below the kernel stack (§67.3).
2. **Registration cost.** Time `ibv_reg_mr` for buffer sizes from 4KB to 1GB; plot the cost (it pins pages). Confirm it's a setup-scale cost (µs-ms) you must keep off the hot path (§67.5). Then build a registered pool and reuse it.
3. **One-sided publication (ties Ch. 35).** Build a one-sided RDMA WRITE publisher that writes a snapshot + a sequence/flag (as a second WR, in the right order — Ch. 30, 35) into a remote reader's memory; the reader polls its local memory. Then redo it with `IBV_WR_RDMA_WRITE_WITH_IMM` and a polled CQ on the reader; compare the notification latency. Confirm the reader sees consistent snapshots (the cross-host seqlock). Inject a torn write to show the ordering matters.
4. **Poll vs event.** Reap completions by busy-polling the CQ vs by completion events (interrupt); measure the latency difference (§67.4.3, Ch. 43). Confirm polling wins on a dedicated core.
5. **RoCE lossless.** (If on RoCE) Ch. 68's Exercise 1 is this experiment — run an RDMA workload without PFC/ECN under congestion, then lossless, and quantify the requirement (§67.4.2).

**Checklist — InfiniBand verbs & RDMA**

- [ ] RDMA memory is **registered at setup and reused** (a registered pool — Ch. 24) — **never `ibv_reg_mr` on the hot path** (§67.5); pinned memory is bounded (`RLIMIT_MEMLOCK`).
- [ ] Completions are reaped by **busy-polling the CQ** (Ch. 43) on a **dedicated/isolated core** (Ch. 44, 47) — not completion events/interrupts (§67.4.3).
- [ ] The **verb matches the data-flow pattern**: one-sided **WRITE** for push/publication (the cross-host seqlock — Ch. 35), **READ** for fetch, **SEND/RECV** for message passing, **atomics** for cross-host coordination (§67.4.1).
- [ ] One-sided WRITE publication uses **correct ordering** (data then flag/sequence — Ch. 30, 35) so the remote (which **polls** its memory) sees consistent, non-torn data (§67.5).
- [ ] The **fabric** is chosen and configured: native **IB** (lowest latency, dedicated) or **RoCEv2** with **lossless PFC/ECN** (not lossy Ethernet) (§67.4.2).
- [ ] RDMA is used for **intra-datacenter** host-to-host messaging (risk cluster, distribution, gateways) — not expected to help the external **exchange** leg (Appendix K; the switching fabric — Ch. 64); zero-copy registered buffers throughout (§67.4.3).
- [ ] **Inline** small messages, **batch doorbells / unsignaled completions** for high rate (§67.4.3); a higher-level library (`librdmacm`/UCX) is used where raw verbs complexity isn't justified.
- [ ] **Security/IOMMU**: rkeys scoped tightly, IOMMU configured (Ch. 70), RDMA peers treated with appropriate trust (Ch. 83).

## 67.7 References

- The RDMA/InfiniBand verbs documentation — `libibverbs`, `librdmacm`, and the `perftest` benchmark suite (`ib_*_lat`) — the API and measurement tools of this chapter.
- The InfiniBand Architecture Specification and the RoCE (v1/v2) / iWARP standards — the fabrics of §67.4.2.
- The RDMA Aware Networks Programming User Manual (NVIDIA/Mellanox) — the practical verbs programming guide (QPs, CQs, MRs, WRs — §67.2).
- FaRM, HERD, and the academic RDMA-systems papers (Dragojević et al., Kalia et al.) — RDMA design patterns (one-sided vs two-sided, when each wins) behind §67.4.1.
- The RoCE lossless / PFC / ECN / DCQCN configuration guides (NVIDIA, Cisco) — the §67.4.2 operational requirement.

## 67.8 Additional Reading

- The UCX (Unified Communication X) documentation — a higher-level RDMA/transport abstraction (used by MPI — Ch. 72).
- Talks on RDMA in trading / distribution tiers and the "one-sided vs two-sided" performance debate — practical patterns.
- Ch. 66 (*Kernel Bypass*) — local bypass, the prequel; Ch. 26 (*mmap*) / Ch. 53 (*IPC*) — shared memory, of which RDMA is the cross-host version; Ch. 35 (*Seqlocks*) — the publication pattern over one-sided WRITE; Ch. 72 (*MPI*) — runs over RDMA; Ch. 70 (*PCIe*) — the host-device boundary RDMA crosses; Ch. 74 (*SmartNICs*) — on-NIC RDMA; Ch. 30 (*Memory Model*) — ordering, now across hosts.
- Ch. 68 (*Lossless Ethernet & RDMA Congestion*) — what RoCEv2 needs underneath it: PFC/ECN/DCQCN to make Ethernet lossless, and the incast/deadlock hazards that come with it.
- **Appendix F** — RDMA/QP/CQ/MR/RoCE glossary.

---

*Next: Ch. 68 — Lossless Ethernet & RDMA Congestion, what RoCEv2 needs underneath the verbs of this chapter: PFC, ECN, and DCQCN make Ethernet lossless enough for RDMA — and introduce their own hazards (PFC deadlock, incast collapse) that a production RDMA fabric must get right.*
