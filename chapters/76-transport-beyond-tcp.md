# Part X — Kernel Bypass, RDMA & Transport

# Chapter 76 — Transport Beyond TCP: Aeron, eRPC, Homa & Custom Reliable UDP

> **Prerequisites:** Ch. 61 (sockets, multicast), Ch. 63 (SBE and message encoding — the payload these transports carry), Ch. 74 (RDMA — a transport some of these ride), Ch. 64 (reliable multicast — the recovery ideas generalize here), Ch. 62 (TCP internals — the baseline these improve on), Ch. 42 (the Disruptor / mechanical sympathy — Aeron's lineage).
>
> **Leads into:** Ch. 77 (PCIe — the boundary under every bypass/RDMA substrate these transports ride), Ch. 79 (MPI — collective messaging over the same fabrics), Ch. 92 (process topology — Aeron IPC as the data plane), Ch. 107 (the end-to-end path). Ties back to Ch. 68 (flow steering — getting these transports' packets to the right core) and Ch. 75 (the lossless fabric RDMA-based transports need). The transport deep-dive: purpose-built low-latency messaging that beats general-purpose TCP for the messages a trading system actually sends.

---

## 76.1 Why it matters: TCP is a general-purpose stream, and you're sending messages

TCP is the default for reliable communication, and Ch. 62 showed how to tune it — but tuning only goes so far, because TCP's *design* is a mismatch for low-latency messaging. TCP gives you a **reliable, ordered byte stream** with congestion control tuned for the public internet — and a trading system almost never wants a byte stream. It wants **messages** (an order, a quote, a fill — discrete, framed, often small, encoded with SBE — Ch. 63); it wants the *latest* message to arrive fast even if an earlier one was lost; it wants flow control that doesn't collapse under the many-to-one fan-in of a matching engine; and it wants microsecond delivery, not the millisecond tail of a mistuned stack (Ch. 62). Several of TCP's core design choices actively hurt this:

- **Head-of-line (HOL) blocking** — because TCP delivers a strict *ordered stream*, a single lost segment blocks *every* later message behind it until the retransmit arrives (a round-trip, Ch. 62), even though those later messages are complete and independent. For a stream of independent orders/quotes, one loss stalls all of them — the single worst TCP property for messaging.
- **Connection-oriented, one-to-one** — TCP is a point-to-point connection; a matching engine talking to thousands of participants, or a service handling many clients, pays per-connection state and can't easily express fan-out (which is why market data is multicast — Ch. 61/64, not TCP).
- **Sender-driven congestion control** (the receiver window is only a buffer bound) tuned for fairness and bulk throughput, which under datacenter many-to-one *incast* (Ch. 75) can collapse, and which ramps and backs off in ways a controlled fabric doesn't need (Ch. 62).

So the state of the art is **purpose-built transports** that make different choices: message semantics instead of a stream, no HOL blocking, receiver-driven or credit-based flow control, connectionless or lightweight sessions, and designs tuned for datacenter fabrics and short messages. This chapter surveys the landmark ones — **Aeron** (the reference low-latency messaging transport, from the Disruptor lineage — Ch. 42), **eRPC** (microsecond RPC on commodity hardware), **Homa** (receiver-driven, connectionless, HOL-free), and the **custom reliable UDP** that many HFT shops build — and the design space they occupy (§76.2), measures them against TCP (§76.3), details each (§76.4), and warns about the trap of reinventing TCP badly (§76.5). It's the "TCP is the wrong shape; here's what fits" chapter.

## 76.2 Mental model: the transport design space

Every transport makes choices along a few axes; TCP picks one corner, and the low-latency transports pick differently:

```
   reliability:      unreliable (UDP) ......... reliable (TCP, Aeron, eRPC, Homa, custom)
   ordering:         unordered ......... per-message ......... total stream order (TCP)
   HOL blocking:     none (message) ......................... full (TCP stream)
   congestion ctl:   none ... sender-driven (TCP) ... receiver-scheduled grants (Homa) ... receiver window + NAK (Aeron)
   connection:       connectionless (UDP, Homa) ... session (Aeron) ... connection (TCP, eRPC)
   fan-out:          multicast (Aeron, market data) ... unicast (TCP, eRPC)
   substrate:        kernel UDP ... kernel-bypass UDP (Aeron) ... DPDK (eRPC, Homa/RAMCloud) / verbs UD (eRPC) ... in-kernel IP (the Homa Linux module)
```

The key insight: **you rarely want a totally-ordered reliable byte stream (TCP's corner) — you want reliable, framed *messages* with no HOL blocking, flow control that suits your fan-in, and a substrate that matches your latency tier.** The transports:

- **Reliable but message-oriented, no HOL blocking.** Deliver discrete messages reliably; a lost message is retransmitted *without* blocking later independent messages (the opposite of TCP's stream). Homa and gap-tolerant custom reliable-UDP designs live here. Aeron is reliable and *ordered within a stream* — a gap stalls that stream until the immediately NAK'd retransmit lands (no TCP RTO or cwnd ramp) — but isolates streams/sessions from each other, so one stream's loss never blocks another's.
- **Receiver-driven flow control.** Instead of the sender guessing the rate (TCP), the *receiver* grants credits / schedules when senders may transmit — which handles many-to-one incast (Ch. 75) gracefully because the receiver controls its own inflow. Homa's core idea.
- **Connectionless or lightweight sessions.** No per-connection handshake/state to set up and tear down (Homa is connectionless; Aeron uses lightweight publications/subscriptions); lower setup latency and better fan-out.
- **Substrate-matched.** The same transport *idea* can run over kernel UDP (portable, slower), kernel-bypass UDP (Aeron over Onload/DPDK — Ch. 73), or DPDK/RDMA (eRPC — Ch. 74). The transport is the *protocol*; the substrate is *how fast the bytes move*. Fast transport + fast substrate = the state of the art.

The mental model: **a transport is a set of choices about reliability, ordering, HOL blocking, flow control, and connection model — and low-latency messaging wants message semantics, no HOL blocking, fan-in-aware flow control, and a bypass/RDMA substrate, none of which is TCP's default.** These transports pick the corner that fits trading; TCP picks the corner that fits web browsers.

## 76.3 Measure it: message rate, latency, and HOL blocking vs TCP

*Toolchain and machine: Appendix L.3.*

Compare a purpose-built transport to tuned TCP (Ch. 62) on the metrics that matter for messaging:

- **Short-message latency and the tail.** Send small messages (order-sized, Ch. 63) request-response and one-way; measure the latency *distribution* (Ch. 3). Harness: Aeron's `aeron-samples` Ping/Pong (UDP) and EmbeddedPingPong (IPC), `eRPC`'s `apps/latency`, and `sockperf ping-pong --tcp --tcp-nodelay` for the TCP baseline, all with thread pinning (Ch. 50) and HdrHistogram output (Ch. 3). Representative shape (figures pending real runs): a well-implemented transport over a bypass/RDMA substrate delivers **single-digit-microsecond** RPCs (eRPC's headline result) with a *tight tail*, versus tuned kernel TCP's ~tens of µs with a fatter tail (Ch. 62) — and *much* better than mistuned TCP's millisecond tail (the delayed-ACK stall, Ch. 62).
- **HOL blocking under loss — the decisive test.** Inject a single packet loss (`tc qdisc add dev <if> root netem loss 0.1%`, or an XDP program that drops every N-th packet — Ch. 72) into a stream of independent messages and measure the latency of the messages *after* the lost one. TCP: they all stall until the retransmit (a round-trip — the HOL blocking, §76.1). A message-oriented transport: the later messages arrive on time; only the lost one is delayed by its retransmit. This is the single clearest demonstration of why TCP is the wrong shape — reproduce it and the case makes itself.
- **Incast / many-to-one (Ch. 75).** Have many senders transmit to one receiver simultaneously (the matching-engine fan-in, or a scatter-gather — Ch. 79; `iperf3 -P` or N `sockperf` clients for TCP, N Aeron publications to one subscription). TCP's sender-driven control can collapse (incast throughput collapse); a receiver-driven transport (Homa) or credit-based (Aeron) holds up because the receiver paces its own inflow. Measure the goodput and tail under increasing fan-in.
- **Message rate (throughput of small messages).** Measure messages/second for tiny messages — where TCP's per-message overhead and the syscall path (Ch. 48) cap you, and batched/bypass transports (recvmmsg — Ch. 68, or bypass — Ch. 73) sustain far higher rates.

The lessons: purpose-built transports over fast substrates hit microsecond RPCs with tight tails; the HOL-blocking test is the decisive one (TCP stalls a whole stream on one loss, message transports don't); receiver-driven/credit flow control survives incast where TCP collapses; and small-message rate is far higher without TCP's stream/syscall overhead. The numbers matter, but the *shape* mismatch (HOL blocking, incast collapse) is the real argument — measure those, not just the median.

## 76.4 Techniques

### 76.4.1 Aeron: reliable UDP messaging, unicast and multicast

**Aeron** (Real Logic — Martin Thompson, of the Disruptor, Ch. 42) is the reference low-latency messaging transport, and the most likely one to adopt off-the-shelf:

- **Reliable UDP, message-oriented, no HOL blocking across streams.** Aeron provides reliable, ordered delivery *within a stream* over UDP (unicast **or multicast**) and over shared-memory IPC — with a design tuned end-to-end for low latency and mechanical sympathy (Ch. 42). It reliably delivers messages with NAK-based retransmission (like Ch. 64's recovery, generalized), flow control, and congestion control, without TCP's stream-wide HOL blocking.
- **Log/term-buffer architecture (Disruptor lineage).** Aeron publishes into a **log** made of term buffers — a lock-free, memory-mapped ring (Ch. 31, 39, 42) — that subscribers read. The same mechanical-sympathy design as the Disruptor (Ch. 42): pre-allocated, lock-free, cache-friendly, zero-allocation on the hot path (Ch. 28–29). Publication and subscription are the API; the media driver moves bytes over UDP/IPC.
- **Unified IPC and network.** The *same* API and log mechanism carries messages between processes on one host (shared memory — Ch. 60/92) and across the network (UDP) — so the process-topology data plane (Ch. 92) and the network transport are one system. This is a major operational simplification: one messaging layer for intra-host and inter-host.
- **Pairs with SBE (Ch. 63) and Aeron Cluster.** Aeron is typically used with **SBE** (Simple Binary Encoding — Ch. 63) for the message payloads (zero-copy, fixed-layout), and **Aeron Cluster** provides Raft-based fault-tolerant replicated state machines (Ch. 92's deterministic state machine, replicated) for consensus/failover. Together they're a full low-latency messaging + clustering stack used in production trading.

### 76.4.2 eRPC: microsecond RPC on commodity hardware

**eRPC** (Kalia, Kaminsky, Andersen) is a landmark showing that microsecond RPC doesn't require exotic hardware:

- **μs RPC over DPDK/RDMA on commodity NICs.** eRPC achieves single-digit-microsecond RPCs and millions of RPCs/sec/core over DPDK (Ch. 73) or RDMA (Ch. 74) UD (unreliable datagram) transport — on *commodity* NICs, not custom silicon. The result challenged the assumption that microsecond RPC needs lossless fabrics, one-sided RDMA or specialized hardware — it uses commodity NICs and plain unreliable datagrams over lossy Ethernet without PFC (still kernel-bypassed, via DPDK or verbs UD).
- **How it gets there (the design lessons).** Zero-copy, no per-message allocation (Ch. 28–29 — everything pre-allocated), a session-based design that keeps state small and cache-resident (Ch. 8), messages that fit in a cache line / packet where possible, and careful handling of congestion and the common (single-packet) case fast-pathed. It's a masterclass in applying this book's techniques (zero-alloc, cache-aware, batching) to a transport.
- **Where it fits.** eRPC-style transports suit intra-datacenter request-response (risk checks, service RPCs, scatter-gather — Ch. 79) where you want μs latency without building your own transport. Study its design even if you don't adopt it — it codifies how to build a fast transport.

### 76.4.3 Homa: receiver-driven, connectionless, HOL-free

**Homa** (John Ousterhout et al.) is the research-frontier answer to "TCP is wrong for datacenter RPC":

- **Message-oriented, connectionless, no HOL blocking.** Homa transports *messages* (not streams), is **connectionless** (no per-connection state or handshake — lower setup cost, better fan-out), and has **no HOL blocking** — independent messages don't block each other. It's designed from scratch for short-message datacenter RPC.
- **Receiver-driven flow control + priority queues.** The **receiver** controls transmission — it grants senders permission to transmit (rather than senders guessing, as TCP does), which handles **incast** (many-to-one, Ch. 75) gracefully because the receiver paces its own inflow. It uses network **priority queues** to give short messages precedence over long ones (short-message latency doesn't suffer behind bulk). These two ideas — receiver-driven + priorities — are Homa's core contributions.
- **Where it fits / status.** Homa has a Linux kernel implementation and is used/studied for datacenter RPC; for trading it's most relevant as the design that *fixes TCP's incast and HOL problems*, and as a candidate for internal service communication. Even where you don't deploy it, its ideas (receiver-driven flow, priorities for short messages) inform how you'd build or tune a fabric (Ch. 75) for many-to-one.

### 76.4.4 Rolling your own reliable UDP

Many HFT shops build a **custom reliable-UDP** transport, because even Aeron/eRPC may not fit an exact need and the requirements are narrow enough to build:

- **Why build.** You control the exact reliability/ordering/flow semantics, tune it for your fabric (Ch. 71) and message sizes (Ch. 63), run it over your chosen substrate (bypass — Ch. 73, or RDMA — Ch. 74), and avoid TCP's stream/HOL/ramp entirely. For a narrow, well-understood path, a purpose-built reliable UDP can beat a general library.
- **The core design (generalizing Ch. 64).** Sequence numbers on every message; **NAK-based selective retransmission** (receiver requests only what it missed — Ch. 64) rather than TCP's cumulative-ACK stream; a bounded **retransmit ring** of recent messages the sender holds for possible resend; **no HOL blocking** (deliver messages as they arrive, recover the missing one out-of-band — Ch. 64); and flow/congestion control appropriate to your fabric (credit-based, or paced — Ch. 70). It's Ch. 64's reliable-multicast recovery, generalized to a unicast reliable transport.
- **The discipline (don't reinvent TCP badly, §76.5).** Reliable transport is *hard* — you must handle flow control, congestion, retransmission, reordering, duplicates, connection liveness, and edge cases TCP spent decades on. Build only what you need, bound every buffer (Ch. 28), test adversarially (loss, reorder, incast — Ch. 47/75), and be honest about the operational cost vs adopting Aeron. Roll your own when the fit and the control genuinely justify the maintenance burden — not by default.

## 76.5 Pitfalls & anti-patterns: reinventing TCP badly

- **Reinventing TCP badly (the cardinal error).** Building a custom reliable transport that ignores flow control, congestion, or edge cases TCP handles — it works in the lab and collapses in production (congestion collapse, unbounded retransmit, incast — Ch. 75). If you build (§76.4.4), build *deliberately*, handle flow/congestion, bound buffers (Ch. 28), and test adversarially; otherwise adopt Aeron/eRPC. The graveyard of HFT is full of home-grown transports that skipped congestion control.
- **Using TCP where its shape hurts and not noticing.** Running a stream of independent messages over TCP and eating HOL blocking (§76.3) — a single loss stalling the whole stream — without realizing the transport is the problem. Measure the HOL-blocking behavior (§76.3); if independent messages block each other, TCP's shape is the issue, and a message transport fixes it.
- **Ignoring flow/congestion control.** A transport (custom or misconfigured) with no flow control floods a slow receiver or a congested fabric (Ch. 75), causing loss and collapse — especially under incast (many-to-one). Receiver-driven (Homa) or credit-based (Aeron) flow control exists precisely for this; don't omit it (§76.4.3–4).
- **Unbounded retransmit / reorder buffers.** A reliable transport that holds unbounded history for retransmission, or unbounded reorder buffers, allocates without limit (Ch. 28) and can be driven to OOM by a slow/lossy peer. Bound every buffer; drop/escalate (Ch. 64) when limits are hit (§76.4.4).
- **Assuming ordering you didn't guarantee.** A message transport may deliver out of order (that's often the point — no HOL blocking); code that assumes total order breaks. Design the application for message semantics (sequence numbers, idempotence — Ch. 63/64), not stream order (§76.2).
- **Substrate/transport confusion.** Expecting a fast *transport* to be fast over a slow *substrate* (kernel UDP with per-message syscalls — Ch. 48) — the transport protocol and the byte-moving substrate are separate (§76.2). Aeron over kernel UDP is slower than Aeron over bypass (Ch. 73); match the substrate to the latency tier.
- **Building when adopting would do (or vice versa).** Rolling your own when Aeron fits (needless maintenance burden and risk), or adopting a heavy framework when a narrow custom path is genuinely better — decide by the actual fit and the honest operational cost (§76.4.4), not by NIH or by defaulting to a library.
- **Ignoring operational maturity.** A custom transport lacks the years of production hardening, observability, and edge-case fixes that Aeron/TCP have. Weigh the operational cost — monitoring, debugging (Ch. 5), on-call — not just the latency number (§76.4.4).

## 76.6 Exercises & checklist

**Exercises:**

1. **HOL-blocking demonstration.** Send a stream of independent messages over TCP and over a message-oriented transport (Aeron, or a simple reliable UDP); inject one loss and measure the latency of *subsequent* messages — reproduce TCP's stall vs the message transport's independence (§76.3).
2. **Aeron pub/sub.** Build a publisher/subscriber over Aeron (UDP and IPC), pair with SBE (Ch. 63); measure latency and message rate vs a TCP baseline, and confirm zero-allocation on the hot path (Ch. 28–29; §76.4.1).
3. **Incast.** Drive many senders to one receiver over TCP and over a receiver-driven/credit transport; measure goodput and tail collapse vs graceful degradation (§76.3, §76.4.3).
4. **Custom reliable UDP.** Build a minimal NAK-based reliable UDP with a bounded retransmit ring and no HOL blocking (§76.4.4); test it adversarially (loss, reorder, duplicates, slow receiver — Ch. 47) and observe where a naive design breaks (§76.5).
5. **Substrate swap.** Run the same transport over kernel UDP vs kernel bypass (Ch. 73); quantify how much of the latency is the transport vs the substrate (§76.2, §76.5).

**Checklist — transport beyond TCP**

- [ ] The transport choice matches the **message shape**: framed messages, **no HOL blocking**, fan-in-appropriate **flow control** — not a byte stream where messages are independent (§76.1, §76.2).
- [ ] **HOL-blocking behavior is measured** (loss → do later messages stall?); if independent messages block each other, TCP's shape is the problem (§76.3).
- [ ] Flow/congestion control is **present and fan-in-aware** (receiver-driven / credit-based) — survives **incast** (Ch. 75), doesn't collapse (§76.4.3, §76.5).
- [ ] Any **custom transport** is built deliberately (flow, congestion, retransmit, reorder, liveness handled), with **bounded buffers** (Ch. 28) and **adversarial testing** (Ch. 47) — or a proven one (**Aeron**/eRPC) is adopted instead (§76.4.4, §76.5).
- [ ] The **substrate matches the latency tier** (bypass/RDMA — Ch. 73/74 — for the hot path; kernel UDP only off it) — transport and substrate not confused (§76.2, §76.5).
- [ ] Payloads use a **zero-copy encoding** (SBE — Ch. 63); the transport is **zero-allocation on the hot path** (Ch. 28–29) (§76.4.1).
- [ ] The build-vs-adopt decision weighs **operational maturity** (observability, debugging — Ch. 5, hardening), not just the latency number (§76.5).
- [ ] The application is designed for **message semantics** (sequence/idempotence — Ch. 63/64), not assumed stream ordering (§76.5).

## 76.7 References

- **Aeron** (Real Logic) documentation, design notes, and Martin Thompson's talks — the log/term-buffer architecture, reliable UDP/multicast, IPC unification, and Aeron Cluster (§76.4.1; Ch. 42).
- **eRPC** — Kalia, Kaminsky, Andersen, "Datacenter RPCs can be General and Fast" (NSDI 2019) — the μs-RPC design on commodity hardware (§76.4.2).
- **Homa** — Montazeri, Li, Alizadeh & Ousterhout, "Homa: A Receiver-Driven Low-Latency Transport Protocol Using Network Priorities" (SIGCOMM 2018); Ousterhout, "A Linux Kernel Implementation of the Homa Transport Protocol" (USENIX ATC 2021) — receiver-driven, connectionless, priority-based transport (§76.4.3).
- The reliable-multicast/transport literature (PGM/NORM — Ch. 64) and *TCP/IP Illustrated* (Ch. 62) — the baseline and the recovery ideas custom UDP generalizes (§76.4.4).
- Ch. 42 (Disruptor — Aeron's lineage), Ch. 63 (SBE payloads), Ch. 73/74 (bypass/RDMA substrates), Ch. 64 (reliable multicast recovery), Ch. 62 (TCP baseline), Ch. 75 (incast/lossless fabric).

## 76.8 Additional Reading

- Talks on Aeron in production trading systems, and the Aeron Cluster / Raft consensus material (external — the Aeron docs; Ch. 99 covers the sequenced, replicated state machine that Aeron Cluster packages, Ch. 100 §100.4.6 where consensus sits relative to the order path) — the full messaging + clustering stack (§76.4.1).
- The datacenter-transport research line (Homa, NDP, pFabric, DCTCP — Ch. 75) — receiver-driven flow, priorities, and incast (§76.4.3).
- **Ch. 85 §85.4.1** (*Managed Runtimes*) — Chronicle/Aeron (off-heap, garbage-free Java messaging) and **Ch. 84 §84.4.4** — cross-language shared-memory transports; Ch. 68 (*Flow Steering*) — getting these transports' packets to the right core; Ch. 75 (*Lossless Ethernet*) — the fabric under RDMA-based transports; **Appendix F** — HOL-blocking / receiver-driven / reliable-UDP glossary; **Appendix G** — the eRPC/Homa/Aeron bibliography.

---

*Next: Ch. 77 — PCIe & the Host–Device Boundary, opening Part XI (Heterogeneous Computing & Hardware Acceleration): every accelerator and modern NIC sits behind PCIe, and the round-trip across that boundary is the irreducible floor under every offload decision in the rest of the book.*
