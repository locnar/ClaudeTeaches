# Part X — Kernel Bypass, RDMA & Transport

# Chapter 69 — Transport Beyond TCP: Aeron, eRPC, Homa & Custom Reliable UDP

> **Prerequisites:** Ch. 54 (sockets, multicast), Ch. 56 (SBE and message encoding — the payload these transports carry), Ch. 67 (RDMA — a transport some of these ride), Ch. 57 (reliable multicast — the recovery ideas generalize here), Ch. 55 (TCP internals — the baseline these improve on), Ch. 37 (the Disruptor / mechanical sympathy — Aeron's lineage).
>
> **Leads into:** Ch. 70 (PCIe — the boundary under every bypass/RDMA substrate these transports ride), Ch. 72 (MPI — collective messaging over the same fabrics), Ch. 85 (process topology — Aeron IPC as the data plane), Ch. 91 (the end-to-end path). Ties back to Ch. 61 (flow steering — getting these transports' packets to the right core) and Ch. 68 (the lossless fabric RDMA-based transports need). The transport deep-dive: purpose-built low-latency messaging that beats general-purpose TCP for the messages a trading system actually sends.

---

## 69.1 Why it matters: TCP is a general-purpose stream, and you're sending messages

TCP is the default for reliable communication, and Ch. 55 showed how to tune it — but tuning only goes so far, because TCP's *design* is a mismatch for low-latency messaging. TCP gives you a **reliable, ordered byte stream** with congestion control tuned for the public internet — and a trading system almost never wants a byte stream. It wants **messages** (an order, a quote, a fill — discrete, framed, often small, encoded with SBE — Ch. 56); it wants the *latest* message to arrive fast even if an earlier one was lost; it wants flow control that doesn't collapse under the many-to-one fan-in of a matching engine; and it wants microsecond delivery, not the millisecond tail of a mistuned stack (Ch. 55). Several of TCP's core design choices actively hurt this:

- **Head-of-line (HOL) blocking** — because TCP delivers a strict *ordered stream*, a single lost segment blocks *every* later message behind it until the retransmit arrives (a round-trip, Ch. 55), even though those later messages are complete and independent. For a stream of independent orders/quotes, one loss stalls all of them — the single worst TCP property for messaging.
- **Connection-oriented, one-to-one** — TCP is a point-to-point connection; a matching engine talking to thousands of participants, or a service handling many clients, pays per-connection state and can't easily express fan-out (which is why market data is multicast — Ch. 54/57, not TCP).
- **Sender-driven congestion control** (the receiver window is only a buffer bound) tuned for fairness and bulk throughput, which under datacenter many-to-one *incast* (Ch. 68) can collapse, and which ramps and backs off in ways a controlled fabric doesn't need (Ch. 55).

So the state of the art is **purpose-built transports** that make different choices: message semantics instead of a stream, no HOL blocking, receiver-driven or credit-based flow control, connectionless or lightweight sessions, and designs tuned for datacenter fabrics and short messages. This chapter surveys the landmark ones — **Aeron** (the reference low-latency messaging transport, from the Disruptor lineage — Ch. 37), **eRPC** (microsecond RPC on commodity hardware), **Homa** (receiver-driven, connectionless, HOL-free), and the **custom reliable UDP** that many HFT shops build — and the design space they occupy (§69.2), measures them against TCP (§69.3), details each (§69.4), and warns about the trap of reinventing TCP badly (§69.5). It's the "TCP is the wrong shape; here's what fits" chapter.

## 69.2 Mental model: the transport design space

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
- **Receiver-driven flow control.** Instead of the sender guessing the rate (TCP), the *receiver* grants credits / schedules when senders may transmit — which handles many-to-one incast (Ch. 68) gracefully because the receiver controls its own inflow. Homa's core idea.
- **Connectionless or lightweight sessions.** No per-connection handshake/state to set up and tear down (Homa is connectionless; Aeron uses lightweight publications/subscriptions); lower setup latency and better fan-out.
- **Substrate-matched.** The same transport *idea* can run over kernel UDP (portable, slower), kernel-bypass UDP (Aeron over Onload/DPDK — Ch. 66), or DPDK/RDMA (eRPC — Ch. 67). The transport is the *protocol*; the substrate is *how fast the bytes move*. Fast transport + fast substrate = the state of the art.

The mental model: **a transport is a set of choices about reliability, ordering, HOL blocking, flow control, and connection model — and low-latency messaging wants message semantics, no HOL blocking, fan-in-aware flow control, and a bypass/RDMA substrate, none of which is TCP's default.** These transports pick the corner that fits trading; TCP picks the corner that fits web browsers.

## 69.3 Measure it: message rate, latency, and HOL blocking vs TCP

*Toolchain and machine: Appendix L.3.*

Compare a purpose-built transport to tuned TCP (Ch. 55) on the metrics that matter for messaging:

- **Short-message latency and the tail.** Send small messages (order-sized, Ch. 56) request-response and one-way; measure the latency *distribution* (Ch. 3). Harness: Aeron's `aeron-samples` Ping/Pong (UDP) and EmbeddedPingPong (IPC), `eRPC`'s `apps/latency`, and `sockperf ping-pong --tcp --tcp-nodelay` for the TCP baseline, all with thread pinning (Ch. 44) and HdrHistogram output (Ch. 3). Representative shape (figures pending real runs): a well-implemented transport over a bypass/RDMA substrate delivers **single-digit-microsecond** RPCs (eRPC's headline result) with a *tight tail*, versus tuned kernel TCP's ~tens of µs with a fatter tail (Ch. 55) — and *much* better than mistuned TCP's millisecond tail (the delayed-ACK stall, Ch. 55).
- **HOL blocking under loss — the decisive test.** Inject a single packet loss (`tc qdisc add dev <if> root netem loss 0.1%`, or an XDP program that drops every N-th packet — Ch. 65) into a stream of independent messages and measure the latency of the messages *after* the lost one. TCP: they all stall until the retransmit (a round-trip — the HOL blocking, §69.1). A message-oriented transport: the later messages arrive on time; only the lost one is delayed by its retransmit. This is the single clearest demonstration of why TCP is the wrong shape — reproduce it and the case makes itself.
- **Incast / many-to-one (Ch. 68).** Have many senders transmit to one receiver simultaneously (the matching-engine fan-in, or a scatter-gather — Ch. 72; `iperf3 -P` or N `sockperf` clients for TCP, N Aeron publications to one subscription). TCP's sender-driven control can collapse (incast throughput collapse); a receiver-driven transport (Homa) or credit-based (Aeron) holds up because the receiver paces its own inflow. Measure the goodput and tail under increasing fan-in.
- **Message rate (throughput of small messages).** Measure messages/second for tiny messages — where TCP's per-message overhead and the syscall path (Ch. 43) cap you, and batched/bypass transports (recvmmsg — Ch. 61, or bypass — Ch. 66) sustain far higher rates.

The lessons: purpose-built transports over fast substrates hit microsecond RPCs with tight tails; the HOL-blocking test is the decisive one (TCP stalls a whole stream on one loss, message transports don't); receiver-driven/credit flow control survives incast where TCP collapses; and small-message rate is far higher without TCP's stream/syscall overhead. The numbers matter, but the *shape* mismatch (HOL blocking, incast collapse) is the real argument — measure those, not just the median.

## 69.4 Techniques

### 69.4.1 Aeron: reliable UDP messaging, unicast and multicast

**Aeron** (Real Logic — Martin Thompson, of the Disruptor, Ch. 37) is the reference low-latency messaging transport, and the most likely one to adopt off-the-shelf:

- **Reliable UDP, message-oriented, no HOL blocking across streams.** Aeron provides reliable, ordered delivery *within a stream* over UDP (unicast **or multicast**) and over shared-memory IPC — with a design tuned end-to-end for low latency and mechanical sympathy (Ch. 37). It reliably delivers messages with NAK-based retransmission (like Ch. 57's recovery, generalized), flow control, and congestion control, without TCP's stream-wide HOL blocking.
- **Log/term-buffer architecture (Disruptor lineage).** Aeron publishes into a **log** made of term buffers — a lock-free, memory-mapped ring (Ch. 26, 34, 37) — that subscribers read. The same mechanical-sympathy design as the Disruptor (Ch. 37): pre-allocated, lock-free, cache-friendly, zero-allocation on the hot path (Ch. 23–24). Publication and subscription are the API; the media driver moves bytes over UDP/IPC.
- **Unified IPC and network.** The *same* API and log mechanism carries messages between processes on one host (shared memory — Ch. 53/85) and across the network (UDP) — so the process-topology data plane (Ch. 85) and the network transport are one system. This is a major operational simplification: one messaging layer for intra-host and inter-host.
- **Pairs with SBE (Ch. 56) and Aeron Cluster.** Aeron is typically used with **SBE** (Simple Binary Encoding — Ch. 56) for the message payloads (zero-copy, fixed-layout), and **Aeron Cluster** provides Raft-based fault-tolerant replicated state machines (Ch. 85's deterministic state machine, replicated) for consensus/failover. Together they're a full low-latency messaging + clustering stack used in production trading.

### 69.4.2 eRPC: microsecond RPC on commodity hardware

**eRPC** (Kalia, Kaminsky, Andersen) is a landmark showing that microsecond RPC doesn't require exotic hardware:

- **μs RPC over DPDK/RDMA on commodity NICs.** eRPC achieves single-digit-microsecond RPCs and millions of RPCs/sec/core over DPDK (Ch. 66) or RDMA (Ch. 67) UD (unreliable datagram) transport — on *commodity* NICs, not custom silicon. The result challenged the assumption that microsecond RPC needs lossless fabrics, one-sided RDMA or specialized hardware — it uses commodity NICs and plain unreliable datagrams over lossy Ethernet without PFC (still kernel-bypassed, via DPDK or verbs UD).
- **How it gets there (the design lessons).** Zero-copy, no per-message allocation (Ch. 23–24 — everything pre-allocated), a session-based design that keeps state small and cache-resident (Ch. 7), messages that fit in a cache line / packet where possible, and careful handling of congestion and the common (single-packet) case fast-pathed. It's a masterclass in applying this book's techniques (zero-alloc, cache-aware, batching) to a transport.
- **Where it fits.** eRPC-style transports suit intra-datacenter request-response (risk checks, service RPCs, scatter-gather — Ch. 72) where you want μs latency without building your own transport. Study its design even if you don't adopt it — it codifies how to build a fast transport.

### 69.4.3 Homa: receiver-driven, connectionless, HOL-free

**Homa** (John Ousterhout et al.) is the research-frontier answer to "TCP is wrong for datacenter RPC":

- **Message-oriented, connectionless, no HOL blocking.** Homa transports *messages* (not streams), is **connectionless** (no per-connection state or handshake — lower setup cost, better fan-out), and has **no HOL blocking** — independent messages don't block each other. It's designed from scratch for short-message datacenter RPC.
- **Receiver-driven flow control + priority queues.** The **receiver** controls transmission — it grants senders permission to transmit (rather than senders guessing, as TCP does), which handles **incast** (many-to-one, Ch. 68) gracefully because the receiver paces its own inflow. It uses network **priority queues** to give short messages precedence over long ones (short-message latency doesn't suffer behind bulk). These two ideas — receiver-driven + priorities — are Homa's core contributions.
- **Where it fits / status.** Homa has a Linux kernel implementation and is used/studied for datacenter RPC; for trading it's most relevant as the design that *fixes TCP's incast and HOL problems*, and as a candidate for internal service communication. Even where you don't deploy it, its ideas (receiver-driven flow, priorities for short messages) inform how you'd build or tune a fabric (Ch. 68) for many-to-one.

### 69.4.4 Rolling your own reliable UDP

Many HFT shops build a **custom reliable-UDP** transport, because even Aeron/eRPC may not fit an exact need and the requirements are narrow enough to build:

- **Why build.** You control the exact reliability/ordering/flow semantics, tune it for your fabric (Ch. 64) and message sizes (Ch. 56), run it over your chosen substrate (bypass — Ch. 66, or RDMA — Ch. 67), and avoid TCP's stream/HOL/ramp entirely. For a narrow, well-understood path, a purpose-built reliable UDP can beat a general library.
- **The core design (generalizing Ch. 57).** Sequence numbers on every message; **NAK-based selective retransmission** (receiver requests only what it missed — Ch. 57) rather than TCP's cumulative-ACK stream; a bounded **retransmit ring** of recent messages the sender holds for possible resend; **no HOL blocking** (deliver messages as they arrive, recover the missing one out-of-band — Ch. 57); and flow/congestion control appropriate to your fabric (credit-based, or paced — Ch. 63). It's Ch. 57's reliable-multicast recovery, generalized to a unicast reliable transport.
- **The discipline (don't reinvent TCP badly, §69.5).** Reliable transport is *hard* — you must handle flow control, congestion, retransmission, reordering, duplicates, connection liveness, and edge cases TCP spent decades on. Build only what you need, bound every buffer (Ch. 23), test adversarially (loss, reorder, incast — Ch. 42/68), and be honest about the operational cost vs adopting Aeron. Roll your own when the fit and the control genuinely justify the maintenance burden — not by default.

## 69.5 Pitfalls & anti-patterns: reinventing TCP badly

- **Reinventing TCP badly (the cardinal error).** Building a custom reliable transport that ignores flow control, congestion, or edge cases TCP handles — it works in the lab and collapses in production (congestion collapse, unbounded retransmit, incast — Ch. 68). If you build (§69.4.4), build *deliberately*, handle flow/congestion, bound buffers (Ch. 23), and test adversarially; otherwise adopt Aeron/eRPC. The graveyard of HFT is full of home-grown transports that skipped congestion control.
- **Using TCP where its shape hurts and not noticing.** Running a stream of independent messages over TCP and eating HOL blocking (§69.3) — a single loss stalling the whole stream — without realizing the transport is the problem. Measure the HOL-blocking behavior (§69.3); if independent messages block each other, TCP's shape is the issue, and a message transport fixes it.
- **Ignoring flow/congestion control.** A transport (custom or misconfigured) with no flow control floods a slow receiver or a congested fabric (Ch. 68), causing loss and collapse — especially under incast (many-to-one). Receiver-driven (Homa) or credit-based (Aeron) flow control exists precisely for this; don't omit it (§69.4.3–4).
- **Unbounded retransmit / reorder buffers.** A reliable transport that holds unbounded history for retransmission, or unbounded reorder buffers, allocates without limit (Ch. 23) and can be driven to OOM by a slow/lossy peer. Bound every buffer; drop/escalate (Ch. 57) when limits are hit (§69.4.4).
- **Assuming ordering you didn't guarantee.** A message transport may deliver out of order (that's often the point — no HOL blocking); code that assumes total order breaks. Design the application for message semantics (sequence numbers, idempotence — Ch. 56/57), not stream order (§69.2).
- **Substrate/transport confusion.** Expecting a fast *transport* to be fast over a slow *substrate* (kernel UDP with per-message syscalls — Ch. 43) — the transport protocol and the byte-moving substrate are separate (§69.2). Aeron over kernel UDP is slower than Aeron over bypass (Ch. 66); match the substrate to the latency tier.
- **Building when adopting would do (or vice versa).** Rolling your own when Aeron fits (needless maintenance burden and risk), or adopting a heavy framework when a narrow custom path is genuinely better — decide by the actual fit and the honest operational cost (§69.4.4), not by NIH or by defaulting to a library.
- **Ignoring operational maturity.** A custom transport lacks the years of production hardening, observability, and edge-case fixes that Aeron/TCP have. Weigh the operational cost — monitoring, debugging (Ch. 5), on-call — not just the latency number (§69.4.4).

## 69.6 Exercises & checklist

**Exercises:**

1. **HOL-blocking demonstration.** Send a stream of independent messages over TCP and over a message-oriented transport (Aeron, or a simple reliable UDP); inject one loss and measure the latency of *subsequent* messages — reproduce TCP's stall vs the message transport's independence (§69.3).
2. **Aeron pub/sub.** Build a publisher/subscriber over Aeron (UDP and IPC), pair with SBE (Ch. 56); measure latency and message rate vs a TCP baseline, and confirm zero-allocation on the hot path (Ch. 23–24; §69.4.1).
3. **Incast.** Drive many senders to one receiver over TCP and over a receiver-driven/credit transport; measure goodput and tail collapse vs graceful degradation (§69.3, §69.4.3).
4. **Custom reliable UDP.** Build a minimal NAK-based reliable UDP with a bounded retransmit ring and no HOL blocking (§69.4.4); test it adversarially (loss, reorder, duplicates, slow receiver — Ch. 42) and observe where a naive design breaks (§69.5).
5. **Substrate swap.** Run the same transport over kernel UDP vs kernel bypass (Ch. 66); quantify how much of the latency is the transport vs the substrate (§69.2, §69.5).

**Checklist — transport beyond TCP**

- [ ] The transport choice matches the **message shape**: framed messages, **no HOL blocking**, fan-in-appropriate **flow control** — not a byte stream where messages are independent (§69.1, §69.2).
- [ ] **HOL-blocking behavior is measured** (loss → do later messages stall?); if independent messages block each other, TCP's shape is the problem (§69.3).
- [ ] Flow/congestion control is **present and fan-in-aware** (receiver-driven / credit-based) — survives **incast** (Ch. 68), doesn't collapse (§69.4.3, §69.5).
- [ ] Any **custom transport** is built deliberately (flow, congestion, retransmit, reorder, liveness handled), with **bounded buffers** (Ch. 23) and **adversarial testing** (Ch. 42) — or a proven one (**Aeron**/eRPC) is adopted instead (§69.4.4, §69.5).
- [ ] The **substrate matches the latency tier** (bypass/RDMA — Ch. 66/67 — for the hot path; kernel UDP only off it) — transport and substrate not confused (§69.2, §69.5).
- [ ] Payloads use a **zero-copy encoding** (SBE — Ch. 56); the transport is **zero-allocation on the hot path** (Ch. 23–24) (§69.4.1).
- [ ] The build-vs-adopt decision weighs **operational maturity** (observability, debugging — Ch. 5, hardening), not just the latency number (§69.5).
- [ ] The application is designed for **message semantics** (sequence/idempotence — Ch. 56/57), not assumed stream ordering (§69.5).

## 69.7 References

- **Aeron** (Real Logic) documentation, design notes, and Martin Thompson's talks — the log/term-buffer architecture, reliable UDP/multicast, IPC unification, and Aeron Cluster (§69.4.1; Ch. 37).
- **eRPC** — Kalia, Kaminsky, Andersen, "Datacenter RPCs can be General and Fast" (NSDI 2019) — the μs-RPC design on commodity hardware (§69.4.2).
- **Homa** — Montazeri, Li, Alizadeh & Ousterhout, "Homa: A Receiver-Driven Low-Latency Transport Protocol Using Network Priorities" (SIGCOMM 2018); Ousterhout, "A Linux Kernel Implementation of the Homa Transport Protocol" (USENIX ATC 2021) — receiver-driven, connectionless, priority-based transport (§69.4.3).
- The reliable-multicast/transport literature (PGM/NORM — Ch. 57) and *TCP/IP Illustrated* (Ch. 55) — the baseline and the recovery ideas custom UDP generalizes (§69.4.4).
- Ch. 37 (Disruptor — Aeron's lineage), Ch. 56 (SBE payloads), Ch. 66/67 (bypass/RDMA substrates), Ch. 57 (reliable multicast recovery), Ch. 55 (TCP baseline), Ch. 68 (incast/lossless fabric).

## 69.8 Additional Reading

- Talks on Aeron in production trading systems, and the Aeron Cluster / Raft consensus material (external — the Aeron docs; Ch. 85 covers the single-process deterministic state machine that Aeron Cluster replicates) — the full messaging + clustering stack (§69.4.1).
- The datacenter-transport research line (Homa, NDP, pFabric, DCTCP — Ch. 68) — receiver-driven flow, priorities, and incast (§69.4.3).
- **Ch. 78 §78.4.1** (*Managed Runtimes*) — Chronicle/Aeron (off-heap, garbage-free Java messaging) and **Ch. 77 §77.4.4** — cross-language shared-memory transports; Ch. 61 (*Flow Steering*) — getting these transports' packets to the right core; Ch. 68 (*Lossless Ethernet*) — the fabric under RDMA-based transports; **Appendix F** — HOL-blocking / receiver-driven / reliable-UDP glossary; **Appendix G** — the eRPC/Homa/Aeron bibliography.

---

*Next: Ch. 70 — PCIe & the Host–Device Boundary, opening Part XI (Heterogeneous Computing & Hardware Acceleration): every accelerator and modern NIC sits behind PCIe, and the round-trip across that boundary is the irreducible floor under every offload decision in the rest of the book.*
