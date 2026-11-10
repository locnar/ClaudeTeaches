# Part XIII — Observability & Operations in Production

# Chapter 102 — Failover Without Split-Brain: Leases, Fencing & Session Takeover on the Order Path

> **Prerequisites:** Ch. 101 (the sequencer, replicas with identical state, hot/warm/cold standby and the *single-emitter rule* this chapter enforces across hosts; Appendix F already defines *fencing token*), Ch. 97 (the session layer the standby takes over — persisted sequence numbers, the resend store, cancel-on-disconnect as a venue setting, `TCP_USER_TIMEOUT`), Ch. 94 §94.3 and §94.4.3 (single-host restart time; the in-host heartbeat slot that is one of this chapter's two detection paths), Ch. 96 §96.4.4–§96.4.5 (recovery and reconciliation after a takeover), Ch. 95 §95.4.4 (the kill: cancel-all before anything else), Ch. 19 (monotonic clocks — a lease is a deadline on one), Ch. 50 (deadlines in the hot loop), Ch. 78 §78.4.1 (Aeron Cluster/Raft as the off-the-shelf alternative), Appendix K.4 and K.6 (venue session exclusivity, cancel-on-disconnect, redundant gateways).
>
> **Leads into:** Ch. 103 (a failover is a latency spike with a cause — the forensic timeline), Ch. 104–105 (the detector's two paths are host and network telemetry signals), Ch. 108 §108.4.3 (planned cutover reuses this chapter's token flip), Ch. 109 (the capstone's architecture gains a standby gateway and a lease), Appendix M (failover drill as a readiness item). Ch. 101 stood a replica up beside a dead primary *on one host, behind one sequencer*; this chapter is the cross-host case on the one path where being wrong is a position: two gateways that both believe they own the venue session. It defines the invariant, builds the detector, the lease and the fencing token, measures detect → promote on a VM, and shows the venue's own safety nets catching what the protocol misses.

---

## 102.1 Why it matters: two emitters under one session is worse than the outage

Ch. 94 split the system into processes so a crash is contained; Ch. 101 replicated the state so a standby can take over in microseconds. Neither says *when the standby may start sending*. Get that wrong in the slow direction and the firm is flat for the seconds it takes an operator to notice — expensive, survivable. Get it wrong in the fast direction and **two gateways send under one venue session** — a primary that was merely paused (a 50 ms GC-like stall, a stuck `SIGSTOP`, a partition that healed) wakes up believing it is still primary while the standby has already logged on with the same `SenderCompID`. Both now act on the same strategy decisions: doubled quotes, doubled position, sequence numbers the venue rejects at random, a `ResendRequest` storm, and a regulatory event (RTS 6 Art. 12 and 15c3-5 both require the firm to know what it has working). This is **split-brain**, and a distributed-systems textbook calls it the problem; in trading it is the *only* failure mode of failover that cannot be undone by cancelling.

The chapter's thesis is that split-brain is prevented by **three independent mechanisms**, not one: a **lease** that only one gateway can hold at a time, renewed on a monotonic clock and *expiring* so a dead holder's lease frees itself; a **fencing token** — the lease's epoch — carried on every message and checked by the party that receives it, so a zombie's late message is dropped rather than applied; and **the venue's own exclusivity and safety nets** — one session per `SenderCompID`, cancel-on-disconnect, mass cancel — which catch what the firm's protocol cannot see. §102.3 measures the whole path on a VM: detect in 1 ms (10 missed heartbeats on both paths), promote when the lease expires (~1.8 ms after the kill with a 2 ms lease), first accepted order ~7 µs later, and a resumed zombie's four stale-token messages **fenced** by the venue double with none accepted. The numbers are small; the structure that makes them *safe* is the chapter.

## 102.2 Mental model

### 102.2.1 The invariant and where it is enforced

```
   INVARIANT: at every instant, at most ONE gateway process may emit on venue session S.

   enforced by …              mechanism                                                        catches
   ─────────────────────────  ──────────────────────────────────────────────────────────────   ─────────────────────────────────────────────────────
   the lease                  CAS on a lease word {holder, epoch, expiry}; renew at half-life;  a standby promoting while the primary is merely slow (lease
                              step down BEFORE sending if expired (§102.4.1)                     still valid → acquire fails); a primary that pauses past
                                                                                                its lease (steps down when it wakes)
   the fencing token          epoch carried on every order/heartbeat; receiver drops tokens     a zombie whose "I am primary" check raced the lease expiry —
                              below the highest it has admitted (§102.4.1, §102.4.5)              its message arrives AFTER the standby's logon and is dropped
   the venue                  one session per CompID (a second Logon is rejected or bumps the   a firm whose lease store itself failed; cancel-on-disconnect
                              first); cancel-on-disconnect; OrderMassCancel; drop-copy           flattens the dead gateway's book even if nobody takes over
```

The three are *layered*, not alternative: the lease prevents most races, the token makes the remaining race harmless, the venue bounds the damage when both fail. A design that relies on one of them — "we only fail over when the primary is really dead" — is relying on failure detection being perfect, and §102.2.2 shows why it cannot be.

### 102.2.2 Failure detection over two paths

A failure detector in an asynchronous system cannot distinguish *dead* from *slow* (Chandra & Toueg 1996); it can only trade **detection time** against **false positives**. Two choices make the trade tolerable on a trading host:

- **Two independent paths.** The primary heartbeats into a shared-memory slot (in-host, Ch. 94 §94.4.3 — survives network failure, dies with the host) *and* over a side-channel UDP socket to the standby's host (survives the primary host's shm — i.e. the standby being elsewhere — dies with the network). A standby declares the primary dead only when **both** are silent: a lost UDP heartbeat, a stalled NIC or a partition alone is not death, and neither is one process stalling while its host is fine. §102.3's detector saw 4–5 false declarations in 20–40 trials on an un-isolated VM when *either* path alone would have fired — and zero takeovers from them, because the lease was still valid.
- **Fixed timeout vs φ-accrual.** A fixed "10 missed heartbeats" (1 ms at 100 µs) detects in ~1 ms with a false-positive rate set by the worst pause the primary ever takes; the **φ-accrual** detector (Hayashibara et al. 2004) models the heartbeat inter-arrival distribution and reports a suspicion level φ = −log₁₀ P(a heartbeat this late is still coming) — §102.3's φ > 8 declared death in 127–375 µs but produced 35 false suspicions in 20 trials on the VM's jittery heartbeats. On an isolated core (Ch. 54) with a tight inter-arrival distribution φ-accrual is the better detector; on anything noisy the fixed timeout at ~10× the heartbeat is the honest one. Either way, **detection only *starts* a takeover; the lease decides whether it proceeds.**

### 102.2.3 Leases, epochs and fencing tokens

A **lease** (Gray & Cheriton 1989) is a time-bounded grant: the holder may act as primary until `expiry`; it renews before then; if it cannot (it is dead, paused or partitioned from the lease store), the grant lapses and another process may acquire it. Two consequences are easy to miss. The lease must be measured on a **monotonic clock** (Ch. 19 — a wall-clock step would end or extend it arbitrarily), and the *holder* must check its own lease **before every emission**: "I hold the lease" is a fact about the past, and a process that was descheduled for longer than the lease between the check and the `send()` is exactly the zombie. Hence the **fencing token** (Kleppmann 2016; Lamport's ballot numbers; Raft's term): the lease store hands out a strictly increasing **epoch** with each acquisition, the holder stamps it on every message, and every *receiver* — the venue double here, the sequencer's emitter-control event in Ch. 101, the gateway's own session layer, a lease-aware journal — rejects a token below the highest it has seen. The zombie's late `send()` carries epoch *n* into a world that has admitted epoch *n+1*; it is dropped, and the zombie learns from the silence (or the reject) to step down. Real venues do not check *your* token — §102.4.5 is about where it is checked when they cannot.

### 102.2.4 The venue's safety nets

- **Session exclusivity.** A venue admits one session per `SenderCompID`/session ID. A second Logon while the first is alive is either rejected (the standby must wait for the venue to notice the old one is gone — `TCP_USER_TIMEOUT` on *their* side, their heartbeat timeout, typically 2–30 s) or *bumps* the first (the venue drops the old TCP connection). Which one is a per-venue fact the gateway records (Ch. 97 §97.4.4) and the takeover sequence depends on it.
- **Cancel-on-disconnect (COD).** On detecting the session dead the venue cancels its working orders — optionally after a grace delay, optionally only some order types. It is the net under everything: if no standby ever logs on, the dead gateway's book is flat within the venue's timeout. §102.3's venue double applied COD at 3 ms of silence; with a 10 ms lease it fired *before* the standby's cancel-all, with a 2 ms lease the standby's cancel-all came first every time.
- **Mass cancel and drop-copy.** `OrderMassCancelRequest` (Ch. 95 §95.4.4) is the standby's first message after Logon — cancel first, then trade, because its view of the in-flight table may lag the venue's by the orders in flight at the kill; drop-copy (Ch. 96 §96.4.5) is how it reconciles afterwards.

### 102.2.5 The takeover timeline

```
   t=0 primary dies ──► both paths silent ──► detector fires ──► lease expires ──► CAS: new epoch ──► Logon(token) ──► cancel-all ──► trade
                        ≤ 10 × HB (1 ms)      (VM: 0.96 ms)      (2 ms lease, renewed   +1 µs             +17–25 µs       +7 µs
                                                                  at half-life: 1.6–1.9 ms after the kill — the floor)
   RTO = max(detect, lease remaining) + logon + resync (Ch. 97 §97.3: 60–75 µs for 7 messages) + cancel-all + warm-up (Ch. 57)
   RPO = 0 for a hot replica (Ch. 101 §101.2.3); the orders in flight at the kill are the "unknown state" residue Ch. 96 §96.2.3 resolves
```

The floor is the **lease length**, not the detector: a standby cannot promote before the primary's lease has lapsed, however sure it is. Shortening the lease shortens the RTO (§102.3: 0.5 ms lease → promote at ~0.95 ms) and raises the chance a healthy primary loses it to an ordinary pause (a 1 ms hiccup — Ch. 49/56 — is a step-down with a 0.5 ms lease). The lease is sized to the **longest pause a healthy primary may take**, which is a measured number from Ch. 56's noise hunt, with margin.

## 102.3 Measure it: detect → lease → logon → first accepted order, and a zombie fenced

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 44 §44.3 (TSC 2.70 GHz, Linux 6.18, no isolation). Both "hosts" are processes on one VM and the venue is a thread, so the cross-host network terms (UDP over a real link, the venue's real Logon latency) are absent and the scheduling jitter is the VM's; the protocol timings and the lease arithmetic transfer, the absolute false-positive counts do not. Labelled VM-measured.*

**The harness** (`failover.cpp`): a **primary** gateway process holds the lease, heartbeats every 100 µs into a shm slot (path A) and over UDP (path B), and sends an order every 50 µs with its token; a **standby** process runs the two-path detector (fixed 10-heartbeat timeout, or φ-accrual with φ > 8 on both paths) and, on declaring death, tries to acquire the lease, logs on to the **venue double** with the new epoch, sends cancel-all, and trades. The venue admits one session, checks the token on every message, and applies cancel-on-disconnect after 3 ms of silence. Per trial the primary is `SIGKILL`ed at a random time; in a final trial it is `SIGSTOP`ped for 6 ms and resumed.

```
fixed-timeout detector (10 missed heartbeats on both paths), 2 ms lease renewed at half-life — 40 trials, µs after the kill:
  both paths declared dead        p50    959    p90  1 003    max  1 009
  lease acquired (new epoch)      p50  1 795    p90  1 932    max  1 968      ← waits for the primary's lease to lapse: the floor
  venue Logon with the new token  p50  1 817    p90  1 946    max  1 983
  first order accepted            p50  1 824    p90  1 952    max  1 989      (40/40 trials)
  venue cancel-on-disconnect      never fired: the standby's cancel-all (at ~1.8 ms) preceded the venue's 3 ms silence timer every time
  false declarations of death while the primary was alive: 4 in 40 trials — none led to a takeover (the lease was valid; acquire() failed)

lease length sweep (fixed detector, 20 trials each):   lease 0.5 ms → first order accepted p50 0.96 ms (detector-bound);  2 ms → 1.82 ms;  10 ms → 6.8 ms p50 / 9.7 ms p90,
  and with the 10 ms lease the VENUE's cancel-on-disconnect fired first, at 2.98 ms p50 — the venue flattened the book before the standby could

φ-accrual detector (φ > 8 on both paths), 2 ms lease — 20 trials:
  both paths declared dead        p50    127    p90    285    max    375      ← 7× faster than the fixed timeout …
  first order accepted            p50  1 803    p90  1 909    max  1 925      … and no faster to promote (the lease is the floor);  35 false suspicions in 20 trials on the VM

zombie: primary SIGSTOPped for 6 ms (> 2 ms lease) → standby promoted at +1.5–2.1 ms with epoch n+1 → primary resumed and sent with epoch n:
  4 messages FENCED by the venue, 0 accepted; the primary saw no acks and stepped down.   With a 10 ms lease the same 6 ms pause caused NO takeover (0 fenced): a pause shorter than the lease is survived.
```

Reading it:

- **The lease is the RTO floor and the safety margin at once.** Detection fired at ~1 ms every time, but promotion waited for the lease: ~1.8 ms with a 2 ms lease renewed at half-life (the standby acquires the instant `expiry` passes), ~0.95 ms with 0.5 ms (now detector-bound), 6.8–9.7 ms with 10 ms. Everything after the CAS — Logon 20 µs, cancel-all, first ack 7 µs — is noise against it. Choose the lease from the longest healthy pause (Ch. 56), not from the RTO you would like.
- **False positives are free when the lease holds.** Four (fixed) and thirty-five (φ) declarations of death against a live primary produced zero takeovers: `acquire()` failed because the lease was valid. The detector may be aggressive *because* it is not the thing that decides — which is the whole reason to separate detection from promotion.
- **The venue's net is real and its timing matters.** With a 10 ms lease the venue cancelled the dead gateway's orders at 3 ms, before the standby existed as far as the venue knew; the standby then logged on to an empty book and had to *re-place* the strategy's orders (Ch. 96 §96.4.4's path). With a 2 ms lease the standby's cancel-all came first and the book was flattened by the firm, deliberately. Both are correct; which one happens must be known in advance, because the reconciliation differs.
- **Fencing works when nothing else did.** The paused primary's own lease check ran *before* the pause, so when it woke it believed it was primary and sent four orders with epoch *n*; the venue double, having admitted epoch *n+1*, dropped them all. A venue that did not check tokens would have accepted them — §102.4.5 is about putting the check where the venue cannot.

## 102.4 Techniques

### 102.4.1 The lease word and the fencing token

```cpp
// lease.hpp — one cache line in a store both hosts can reach (a shm segment on one host, a replicated register / etcd / ZooKeeper / Chubby-style
// lock service across hosts, or the sequencer's stream — Ch. 101). Epoch = fencing token. All times CLOCK_MONOTONIC on the lease store's clock.
struct Lease { std::atomic<std::uint64_t> holder; std::atomic<std::uint64_t> epoch; std::atomic<std::uint64_t> expiry_ns; };
bool acquire(Lease& L, std::uint64_t me, std::uint64_t now, std::uint64_t len, std::uint64_t& token) {
    std::uint64_t exp = L.expiry_ns.load(); std::uint64_t h = L.holder.load();
    if (h != me && now < exp) return false;                                          // someone else holds a live lease: NO, however dead they look
    if (!L.expiry_ns.compare_exchange_strong(exp, now + len)) return false;           // lost the race to another standby
    L.holder = me; token = L.epoch.fetch_add(1) + 1; return true;                     // the new epoch fences every message of every earlier holder
}
// primary, in the hot loop (Ch. 44): renew at half-life, and CHECK BEFORE EVERY SEND
if (now > L.expiry_ns - len / 2 && L.holder == me) L.expiry_ns = now + len;
if (now >= L.expiry_ns || L.holder != me) { step_down(); return; }                  // expired or taken: do not send — not even the message already encoded
msg.token = token; send(msg);
```

- **Across hosts the lease store is the hard part**: it must be reachable from both gateways and must itself not split. The classic answer is a small **quorum** service (ZooKeeper/etcd/Chubby — Burrows 2006) off the hot path, whose only hot-path role is handing out epochs; the lighter answer, when a sequencer exists (Ch. 101), is to make *lease acquisition an event in the sequenced stream*, so every replica sees the same holder at the same `seq` and the sequencer's single-writer stamp is the quorum.
- **Clocks**: the lease is a deadline on the *store's* monotonic clock; holders compare with their own after a one-time offset estimate (Ch. 19, 71) and subtract the worst-case skew from `len` when checking. A lease that is safe only if two hosts' clocks agree to the microsecond is not safe.
- **The token travels on the wire** — a tag in the order message (a custom FIX tag, a field in the binary header), in the heartbeat, in every shm record the gateway publishes — so any receiver can fence.

### 102.4.2 The two-path detector

```cpp
// detector.hpp — path A: the primary's heartbeat slot in shm (Ch. 94 §94.4.3); path B: UDP heartbeats over an independent link. Dead = BOTH silent.
struct Phi { double mean = 0, m2 = 0; std::uint64_t n = 0, last = 0;                      // φ-accrual (Hayashibara 2004) over inter-arrival times
    void arrive(std::uint64_t t) { if (last) { double d = double(t - last); ++n; double dl = d - mean; mean += dl / double(n); m2 += dl * (d - mean); } last = t; }
    double phi(std::uint64_t now) const { if (n < 8) return 0; double sd = std::sqrt(m2 / double(n - 1)) + 1.0; double z = (double(now - last) - mean) / sd; return -std::log10(std::max(0.5 * std::erfc(z / std::sqrt(2.0)), 1e-300)); } };
bool dead(std::uint64_t now) const {
    bool a = fixed ? now - lastA > 10 * HB : phiA.phi(now) > 8;                            // choose per deployment: fixed on noisy hosts, φ on isolated cores
    bool b = fixed ? now - lastB > 10 * HB : phiB.phi(now) > 8;
    return a && b && lastA && lastB;                                                        // one silent path is a link or a stall, not a death
}
```

- **Heartbeats are hot-loop deadlines** (Ch. 50), stamped with the primary's monotonic clock *and* its token; the standby's detector runs in its own hot loop since it is a replica anyway (Ch. 101 §101.2.3).
- **Add a third signal where available**: the venue's view (did *it* disconnect the primary? drop-copy stopped?), the supervisor's `SIGCHLD` (Ch. 94 §94.4.3) on the primary's host, the switch's port state (Ch. 105). More independent evidence lets the timeout shrink without raising false positives.
- **Record every declaration**, true or false, with both paths' last-seen times (Ch. 91): the false-positive rate is the number that sizes the lease.

### 102.4.3 The takeover sequence: cancel first, then trade

```
   on dead() && acquire():
     1. Logon(token = new epoch) with the shared SeqJournal's next_out/next_in (Ch. 97 §97.4.1) — the venue may reject if it still sees the old session: retry at its timeout
     2. answer its ResendRequest from the shared store (Ch. 97 §97.4.3) — the primary's last sends are now provably delivered or provably not
     3. OrderMassCancelRequest (or per-order cancels from the in-flight table) — flatten BEFORE acting on any strategy decision (Ch. 95 §95.4.4)
     4. publish "emitter = me, epoch = n+1" into the sequenced stream (Ch. 101) so every replica agrees at which seq the switch happened
     5. reconcile against drop-copy / OrderStatus (Ch. 96 §96.4.5); resolve the unknown-state residue (orders in flight at the kill)
     6. warm up (Ch. 57) and resume emitting; the readiness state machine (Appendix M) moves to armed
```

Steps 1–3 took ~30 µs on the VM (§102.3); step 5 is bounded by the venue's drop-copy latency (milliseconds); step 6 by Ch. 57's warm-up. The order is fixed: a standby that trades before it has cancelled is acting on a book it does not fully know.

### 102.4.4 Zombies: bounded pauses and stepping down

A zombie is a primary that stopped running for longer than its lease — a page-fault storm (Ch. 29), a `SIGSTOP` (a debugger, Ch. 5), a hypervisor steal, a long GC in a managed component (Ch. 87), a partition from the lease store — and then resumed. The defences, in order: **check the lease before every send** (§102.4.1 — on waking, the first iteration sees `now ≥ expiry` and steps down before emitting); **bound the pause** so the check is rarely needed (Ch. 56's residual-noise hunt is literally the measurement of the longest pause); **the token** for the race the check cannot close (a wake-up *during* the standby's acquire, between its own check and `send()`); and **unacked sends under a stale token are a step-down signal** (§102.3: four fenced sends, no acks, the primary stepped down). A stepped-down primary becomes a standby: it re-reads the lease, re-syncs its replica from the stream, and waits.

### 102.4.5 Where the token is checked when the venue cannot

Real venues do not know your epoch; the check must live somewhere a zombie's message *must* pass through:

- **The gateway's own session layer** (Ch. 97): the process that owns the TCP socket to the venue checks the token on every order it is asked to send. If the gateway *is* the thing that failed over, the check moves to whatever hands it orders — the sequencer's emitter-control event (Ch. 101 §101.4.5): a gateway replica emits only orders whose `seq` is after the control event that named *it*.
- **The venue's session exclusivity** is a fence in itself: if the standby's Logon bumped the old connection, the zombie's `send()` fails with `ECONNRESET`/`EPIPE` — provided the venue bumps rather than rejects (§102.2.4). If it rejects, the standby's Logon fails until the venue times the old session out — which is *also* safe, just slow.
- **Network-level fencing**: the old host's NIC port disabled on the switch, or its order-entry flow blocked (STONITH — "shoot the other node in the head"), from the standby's host via an out-of-band path, *before* Logon. Crude, effective, and the only fence that works against a zombie that bypasses the gateway entirely (a bypass stack — Ch. 75 — that owns the NIC).
- **The journal**: lease-aware journaling (Ch. 99) stamps the token on every record, so the post-mortem (Ch. 103) can prove which gateway sent what in which epoch.

### 102.4.6 Off-the-shelf: consensus beside the path

Raft (Ongaro & Ousterhout 2014) and Paxos (Lamport 1998) give leader election with terms/ballots that are fencing tokens by construction; Aeron Cluster (Ch. 78 §78.4.1) packages Raft with a sequenced log for exactly the replicated-state-machine shape of Ch. 101. They cost a quorum round trip per decision — tens of µs within a rack, more across sites — which is why they sit **beside** the order path (electing the emitter, owning the lease store, agreeing the journal) and never *on* it: the hot loop reads one word (the token it holds, the epoch it last saw) and the consensus happens on the control plane. A firm that already runs one of these should take its leases from it rather than building §102.4.1's CAS word across hosts by hand; a firm with a sequencer can make the sequencer the quorum of one and keep the consensus for the sequencer's own failover (Ch. 101 §101.4.5).

## 102.5 Pitfalls & anti-patterns

- **Failover on detection.** "The primary missed ten heartbeats, promote" with no lease is the split-brain recipe: §102.3's detectors were wrong 4–35 times in 20–40 trials. Detection starts the process; the lease permits it.
- **A lease without a fence.** The holder's check is in the past by the time `send()` runs; a pause between them is the zombie. Carry the epoch on every message and check it at the receiver.
- **One detection path.** A lost UDP heartbeat is not a death; a stalled shm writer on a healthy host is not a death. Two independent paths, both silent.
- **Leases on the wall clock.** NTP steps and leap smearing (Ch. 19, 71) change the lease's length under you. Monotonic clocks, with the skew subtracted.
- **Lease sized to the RTO wish.** A 500 µs lease promotes in under a millisecond and steps a healthy primary down on its first 1 ms hiccup. Size it to the measured longest pause (Ch. 56) plus margin; accept the RTO that implies.
- **Trading before cancelling.** The standby's in-flight table lags the venue by the orders in flight at the kill; its first message is cancel-all (or the venue's COD must be known to have fired), then reconciliation, then orders.
- **Assuming the venue's behaviour.** Reject-vs-bump on a second Logon, COD delay and scope, mass-cancel availability are per-venue facts (Appendix K.4); the takeover sequence branches on them and the readiness gate (Appendix M) must have verified them in conformance (Ch. 108 §108.4.6).
- **Consensus on the hot path.** A quorum round trip per order is tens of µs; elect beside the path, read one word on it.

## 102.6 Exercises & checklist

1. **Size the lease from data.** Run Ch. 56's noise hunt on your primary's core for a day; take the longest pause, multiply by a margin, and rerun `failover.cpp` with that lease; report the RTO it implies.
2. **Break it on purpose.** Remove the pre-send lease check from the primary and rerun the zombie trial; count accepted stale-token messages at the venue double with and without the token check. Then remove the token check too.
3. **Partition instead of kill.** Drop only the UDP path with an `iptables` rule (Ch. 97 §97.3's method); confirm no takeover; then drop both.
4. **Make the sequencer the lease store.** Replace the shm lease word with an acquire event in a Ch. 101 stream and show every replica agrees on the epoch at the same `seq`.
5. **Cross-host for real.** Put the standby on a second host with the lease in etcd/ZooKeeper; measure the acquire round trip and where it lands in the §102.2.5 timeline.

Checklist:

- [ ] The single-emitter invariant is enforced by **lease + fencing token + venue nets**, each tested to catch a failure the others miss (§102.2.1).
- [ ] Failure detection uses **two independent paths**; declarations are logged with both paths' last-seen times; the false-positive rate is known (§102.4.2).
- [ ] The lease is on a **monotonic clock**, renewed at half-life, **checked before every send**, and sized from the measured longest healthy pause (§102.4.1, §102.4.4).
- [ ] Every order, heartbeat and journal record carries the **epoch**; every receiver that can fence does (§102.4.5).
- [ ] The takeover sequence is **Logon → resync → cancel-all → publish emitter → reconcile → warm → trade**, and it has been rehearsed with the venue's conformance environment (§102.4.3, Ch. 108).
- [ ] The venue's reject-vs-bump, cancel-on-disconnect scope/delay and mass-cancel support are **recorded per session** and the sequence branches on them (§102.2.4).
- [ ] Detect → promote → first accepted order is a **measured distribution** (§102.3) in the readiness evidence (Appendix M), with the lease length and detector thresholds that produced it.

## 102.7 References

- Cary G. Gray and David R. Cheriton, "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency," SOSP 1989 — the lease as a time-bounded grant.
- Leslie Lamport, "The Part-Time Parliament," ACM TOCS 1998, and "Paxos Made Simple" (2001) — ballot numbers as the original fencing tokens; Diego Ongaro and John Ousterhout, "In Search of an Understandable Consensus Algorithm (Raft)," USENIX ATC 2014 — terms and leader election.
- Tushar D. Chandra and Sam Toueg, "Unreliable Failure Detectors for Reliable Distributed Systems," JACM 1996 — why detect/slow cannot be distinguished and what the detector classes buy.
- Naohiro Hayashibara, Xavier Défago, Rami Yared and Takuya Katayama, "The φ Accrual Failure Detector," SRDS 2004.
- Mike Burrows, "The Chubby Lock Service for Loosely-Coupled Distributed Systems," OSDI 2006 — leases and sequencers (fencing tokens) in a production lock service.
- Martin Kleppmann, "How to do distributed locking" (2016) and *Designing Data-Intensive Applications*, ch. 8–9 — fencing tokens, process pauses and the zombie problem.
- Venue documentation on cancel-on-disconnect, session exclusivity and mass cancel (CME iLink 3, Nasdaq OUCH/SoupBinTCP, Cboe BOE, FIX `OrderMassCancelRequest`) — Appendix K.4; ESMA RTS 6 Art. 12 and SEC Rule 15c3-5.
- Ch. 97 (the session the standby takes over), Ch. 101 (replicas and the single-emitter rule), Ch. 94 §94.4.3 (in-host liveness), Ch. 78 §78.4.1 (Aeron Cluster).

## 102.8 Additional Reading

- Ch. 108 §108.4.3 — the *planned* version of this chapter's token flip: blue/green cutover between process versions with instant rollback.
- Jepsen analyses (Kyle Kingsbury) of lock services and leader election — the catalogue of real split-brain bugs in systems that thought they had leases.
- The Aeron Cluster documentation and Martin Thompson's talks on replicated state machines in trading — the production shape of Ch. 101 + this chapter with Raft underneath.

---

*Next: Ch. 103 — Latency-Spike Forensics & Anomaly-Triggered Diagnostics: when a message blows its budget, freezing the flight recorder, the kernel's ring and the sampled profile at the moment of the spike — and, when a replica's digest disagrees or a failover fired, using the sequence number and the epoch as the pointer into the capture.*
