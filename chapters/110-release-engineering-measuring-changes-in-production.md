# Part XIII — Observability & Operations in Production

# Chapter 110 — Release Engineering & Measuring Changes in Production

> **Prerequisites:** Ch. 95 (hot reload — in-process config and strategy cutover; this chapter is the *binary* and *process* version), Ch. 96 (process-per-role, the supervisor, warm restart, sharding), Ch. 104 (the token flip — a planned cutover is a failover the operator chose), Ch. 103 §103.2.2 (replicas and the digest — a shadow run is a replica with outputs suppressed and its digest compared), Ch. 59 §59.4.1 (side-effect-suppressed shadow traffic), Ch. 64 §64.4.2 and Ch. 96 §96.4.8 (the segment header's layout version and fingerprint — the compatibility contract a new version attaches under), Ch. 27 §27.4.5 (reproducible, signed builds — what "the same binary" means), Ch. 26 §26.4.1 (`-march` and the fleet), Ch. 108–109 (the tests and gates a build passes *before* any of this), Ch. 99 (venue conformance exercises the session layer), Appendix M (the readiness gate the release re-runs), Appendix K.4 (certification and venue rules).
>
> **Leads into:** Ch. 111 (the capstone's monitoring tiers watch every release; its §111.5 "drift between test and production" is this chapter's reason to exist), Appendix M (the T-60/T-30/T-5 gates as the release's last checklist), Appendix N (a bad release is an incident with a bundle). Ch. 108 tested the logic and Ch. 109 gated the performance; this chapter ships the binary: when a trading system may change at all, how a new process version attaches to live rings beside the old one and takes over with one store (measured: one tick of gap, no loss, no duplicate, instant rollback), canary shards and shadow runs with decision parity, venue conformance, and the production measurement that the CI gate cannot replace — including the systematic bias an A/A run exposes in the paired design itself.

---

## 110.1 Why it matters: the deploy is a latency event you schedule

A trading system changes almost every day — a parameter, a strategy, a decoder for a venue's new message type, a kernel patch, a NIC firmware — and each change is a chance to drop a tick, double an order, or ship the regression Ch. 109's gate could not see because production has effects the lab does not: real microbursts, DDIO contention from neighbours (Ch. 71), the venue's actual gateway behaviour, a different NUMA placement. Ch. 95 covered the changes that happen *inside* a running process; everything else — a new binary, a new layout of a shared segment, a kernel, a BIOS setting — has had no procedure in this book beyond "verify with the regression gate, then deploy" (Ch. 5). This chapter is that procedure. Its four ideas: the **deploy calendar** is a latency decision (what may change intraday, what only pre-open, what only on a weekend, and the change freezes around expiries), **cutover is a token flip** between two running versions on the same data plane (§110.3: 400 000 ticks through blue and green with one `store` moving the accepted writer — zero lost, zero duplicated, one tick of gap, a 10 µs worst-case blip, and rollback by storing the old value), **trust is earned in stages** — shadow run with decision parity, canary shard under tighter limits, fleet — and **production is measured, not assumed**: a candidate on a layer-1-replicated feed with orders suppressed, compared tick-for-tick with the incumbent, with the A/A calibration of §102 applied to the paired design too, because §110.3 found a 50 ns systematic bias in "identical" shadows that no amount of pairing removes.

## 110.2 Mental model

### 110.2.1 The deploy calendar: what may change when

```
   change                                        when                              mechanism                                      why
   ───────────────────────────────────────────   ───────────────────────────────   ────────────────────────────────────────────   ───────────────────────────────────────────────
   strategy parameters, limits, symbol data      intraday                          Ch. 95 (seqlock/RCU-published snapshot)        no process restart, no warm-up loss
   strategy code (.so), decoder tables           intraday, quiet minutes           Ch. 95 §95.4.4 (dlopen + warm + swap)          in-process; the hot path never stops
   a process binary (gateway, strategy, risk)    pre-open or post-close; intraday  §110.4.3 blue/green: start vN+1 beside vN,     attaching to live rings needs the compatibility
                                                   only under the canary rules       warm, flip the accepted writer, keep vN        window (§110.2.2); the flip is one store
   shm layout version                            pre-open, all processes together  §110.4.2 compatibility window, or a full         a layout change is a contract change
                                                                                     restart in the pre-open window (Appendix M)
   kernel, drivers, NIC firmware, BIOS, microcode weekend / scheduled maintenance   full host rebuild + Appendix C verification +   changes the noise floor; needs cyclictest and a
                                                                                     Appendix M host fingerprint re-baselined          fresh A/A table (Ch. 109 §109.4.1)
   anything                                      NOT during change freezes:        —                                              the cost of being wrong is highest when volume is
                                                   expiries, index rebalances,                                                      highest and the venue is busiest
                                                   macro releases, venue upgrades
```

The calendar is written down and enforced by the deploy tooling, not remembered. Its logic is Ch. 1's: the tail of a deploy's cost is at the open and at expiry, so changes with any restart cost happen where that tail is cheapest (post-close, pre-open with time to re-run the readiness gate — Appendix M), and the intraday set is exactly what Ch. 95 made free.

### 110.2.2 Compatibility windows: attaching a new version to live state

Blue/green on a process-per-role system means **two versions of a process read the same shared-memory rings at once** (§110.4.3), so the new version must understand the old layouts. The segment header's `layout_version` and `layout_fingerprint` (Ch. 96 §96.4.8) are the contract: a version N+1 binary declares the set of layout versions it can attach to (its **compatibility window**), and attaching outside the window is a refusal, not a best effort. A layout change therefore ships in two steps — a binary that reads both layouts and writes the old one, then (after every reader has moved) a binary that writes the new one — or in one pre-open restart of everything that shares the segment. The same rule covers IPC message versions (an SBE `schemaId`/`version` header — Ch. 67), journal record formats (Ch. 101 §101.4.2) and the readiness telemetry segment (Appendix M). A process that "just reinterprets the bytes" of a layout it was not built for is the deploy-time version of Ch. 96 §96.4.8's torn write.

### 110.2.3 Trust in stages: shadow → canary → fleet

```
   stage         what runs                                           what is compared                                         who can be hurt
   ───────────   ─────────────────────────────────────────────────   ──────────────────────────────────────────────────────   ─────────────────────────────
   CI gate       the candidate on replayed captures (Ch. 108–109)     digest == golden; p50/p99 vs baseline with A/A tolerance  nobody
   shadow run    the candidate as a REPLICA on the live stream        per-seq decision digest vs the incumbent (parity rate),   nobody — outputs are
                 (Ch. 103 §103.2.2) with outputs suppressed               per-tick latency paired with the incumbent (§110.4.5)    suppressed at the last gate
   canary shard  the candidate EMITTING on one symbol/venue shard     the shard's SLIs vs its own history and vs sibling         one shard, under tighter
                 (Ch. 96 §96.4.5) under tighter risk limits (Ch. 97)   shards: fill rate, reject rate, markout (Ch. 111 §111.4.5) limits, for a bounded time
   fleet         everywhere, via the §110.4.3 flip per process        the trend (Ch. 109 §109.4.6) and the telemetry (Ch. 93)   everyone; rollback is one store per host
```

A shadow run answers "does it decide the same things, as fast?" without risk; a canary answers "does the *market* respond the same?" — fills, rejects and adverse selection are only visible when orders are real — on the smallest surface that has them; the fleet gets what survived both. Each stage has an **auto-stop**: parity below a budget, a canary SLI outside its band, a kill trigger (Ch. 97 §97.4.3) — and each stage is reversible by the mechanism of the previous one.

### 110.2.4 Why the CI gate is necessary and not sufficient

The nightly replay gate (Ch. 109 §109.4.4) runs the candidate on the lab's twin of production with the real open's cadence; it still cannot see: DDIO and LLC contention from the *other* processes on the production host (Ch. 71), the venue's actual gateway latency and throttling (Appendix K.4), the feed's live microbursts (which differ from last week's capture), real PTP offsets (Ch. 73), and the NIC firmware the lab host does not have. A **paired comparison in production** — incumbent and candidate consuming the same ticks at the same instant from a layer-1-replicated feed (Ch. 75 §75.4.3), the candidate's orders suppressed — sees all of these, and pairs each tick's two latencies so the host's noise cancels in the difference. §110.3 shows what that buys and what it does not: a median per-tick difference with a 1 ns confidence interval, and a **50 ns systematic offset between two identical shadows** caused by which of them observed the ring cursor first — a bias that an A/A run reveals and that pairing cannot remove, only calibrate. The lesson generalises: every measurement design has its own noise floor, and production comparison needs its A/A exactly as the CI gate does.

## 110.3 Measure it: a blue/green cutover tick by tick, and paired comparison with its own A/A

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 46 §46.3 (TSC 2.70 GHz, Linux 6.18, processes pinned to vCPUs 1–3, no isolation — hypervisor steal produces the sub-millisecond tails below). Labelled VM-measured; the protocol and the zero-loss result transfer, the absolute tails do not.*

**The harness** (`cutover.cpp`): a producer publishes one tick per microsecond into a shared-memory ring with a sequence number and a timestamp (the sequencer's stream — Ch. 103); consumer **blue** (v1, a process) folds every tick and, if the gateway's `accepted_epoch` word equals its epoch, emits a decision into the gateway ring; at tick 100 000 **green** (v2) is started beside it, attaches to the live ring at the current cursor, folds 50 000 ticks to warm, runs as a **shadow** (same inputs, outputs suppressed, its digest advancing in lockstep with blue's), and at tick ~200 000 the operator **flips** `accepted_epoch` from 1 to 2 — one release store. Blue keeps running, suppressed. 100 000 ticks later the flip is reversed (**rollback**). The gateway ring is then audited: every tick exactly once, latency per tick, and what happened around the flips. Then the paired-comparison protocol: two shadows (A and B) read the same ring; B carries 0, 3 or 30 extra iterations of work per tick; per-tick latency pairs are compared paired and unpaired, with cores swapped as a control.

```
cutover (400 000 ticks at 1 µs; green started at tick 100 000, warmed 50 000, shadowed; flip at tick 201 341; rollback at 301 341)
  decisions in the gateway ring: 400 000 = 300 000 blue + 100 000 green;  duplicates: 0;  missing: 0
  at the flip: last blue decision seq 201 340, first green decision seq 201 341 → gap 1 tick: nothing lost, nothing doubled; the flip is one store
  tick → decision latency      p50          p99          p99.9 / max
    20 000 ticks before the flip (blue)       293 ns      397 µs*      569 µs*          * VM steal: the pinned vCPU descheduled; the same tails appear with no cutover at all (Ch. 46 §46.3)
    first 1 000 ticks after the flip (green)  303 ns      6.0 µs       max 10.5 µs      ← the cutover's own cost: green's first emitted decisions, cache-cold on the gateway ring
    steady green                              298 ns      1.0 ms*      1.9 ms*
    after rollback (blue, suppressed → emitting)  278 ns  1.0 µs
  shadow phase: blue's and green's digests advanced together in 48 520 / 48 520 checks (production checks per-seq equality — Ch. 103 §103.2.2)

paired comparison: two shadows on the same ring, 399 000 ticks each, per-tick latency (t_decision − t_publish)
                                              unpaired Δp50 (B − A)   95 % CI        paired median of per-tick (B − A)   95 % CI
  A/A: identical code, A on cpu 2, B on cpu 3    +35.0 ns            [35, 35]         +49.0 ns                           [49, 49]   ← identical code, 50 ns apart: B observes the cursor after A
  A/A, cores swapped                              +33.0 ns            [32, 35]         +50.0 ns                           [50, 50]   ← the bias follows the ROLE (started second), not the core
  B = +3 iterations (~1 ns of work)              −2.0 / +3.0 ns       [−2, −1] / [3, 4]  −43.0 / +49.0 ns                 [−43, −43] / [48, 49]   (two runs: sign flips with placement)
  B = +30 iterations (~12 ns of work)             +2.0 ns             [2, 2]           +52.0 ns                           [52, 53]
  B = +30, cores swapped                          +2.0 ns             [2, 3]           −11.0 ns                           [−33, +10]
  tails: unpaired Δp99.9 and the paired p99.9 of (B − A) are both the VM's steal on one side or the other (0.2–1.6 ms) — pairing attributes a tail to a side; it does not cancel it
```

Reading it:

- **The flip is free and reversible.** One `store(2, release)` moved emission from blue to green between two consecutive ticks with nothing lost or doubled, because both versions were applying the same stream and the gateway's accept check is one load per decision (Ch. 104's token, chosen by the operator instead of a lease). The worst tick in the thousand after the flip cost 10.5 µs — green's first real emissions touching the gateway ring cold — against a 300 ns median; blue, still running suppressed, took the stream back on rollback with a 278 ns median. This is what Ch. 95 §95.4.4's in-process cutover looks like at process granularity, and it is why the old version is *kept running* until the new one has earned the fleet.
- **The shadow phase is the parity test.** 48 520 digest checks in lockstep; production compares digests at equal `seq` (Ch. 103 §103.2.2) and stops the rollout when parity falls below a budget (a candidate that decides differently is a *behaviour change*, to be expected only if the release says so).
- **Paired comparison has its own noise floor, and it is not small.** Two shadows running identical code differed by 49–50 ns per tick with a confidence interval one nanosecond wide — a systematic offset, not noise: the shadow started second observes the producer's cursor store later (coherence order on the shared line), every tick, in whichever core it runs. The 1 ns and 12 ns candidates then read as −43, +49, +52 and −11 ns: the design was measuring the pairing, not the change. The unpaired medians, by contrast, were within 2–8 ns of the truth in five of six runs — because they do not inherit the per-tick ordering. **Run A/A on the production comparison exactly as on the CI gate (Ch. 109 §109.4.1)**, counterbalance (swap which process is "candidate" between windows and alternate ticks' roles where the design allows), and subtract the A/A offset; without that, a paired production study reports its own structure. For effects under ~10 ns, production comparison is the wrong instrument — the same-core replay gate (Ch. 109 §109.4.4) is.
- **Pairing attributes tails; it does not cancel them.** The p99.9 of per-tick differences was the hypervisor's steal landing on one side — informative (which side stalled, on which tick) but not a statement about the candidate. Report tails per side with the Ch. 105 forensics attached, and compare medians and bulk shifts paired.

## 110.4 Techniques

### 110.4.1 The release manifest: what "a release" is

A release is a signed manifest (Ch. 27 §27.4.5, Ch. 94 §94.4.7) naming: the binaries by build ID and the exact flags (`-march` for the target hosts — Ch. 26 §26.5's SIGILL is a release bug), the shm layout versions each binary reads and writes (§110.2.2), the PGO/BOLT profile ids and their training captures (Ch. 109 §109.4.5), the configuration version (Ch. 95), the kernel/firmware fingerprint it was gated on (Appendix M's host fingerprint), the CI evidence (Ch. 108's suite, Ch. 109's gate JSON, the nightly replay result), and the rollout plan (§110.2.3's stages with their budgets and auto-stop conditions). The supervisor (Ch. 96 §96.4.3) refuses to start a binary whose manifest does not match the host's fingerprint or whose layout window excludes a live segment. Everything Ch. 105's post-mortem and Appendix N's bundle need to attribute an incident to a release is in the manifest, by construction.

### 110.4.2 Compatibility windows in code

```cpp
// layout_window.hpp — a binary declares the layout versions it can attach to; attach() refuses anything else (Ch. 96 §96.4.8's header check, made a release contract)
struct LayoutWindow { std::uint32_t min_read, max_read, writes; };                           // reads [min_read, max_read], writes exactly `writes`
inline constexpr LayoutWindow kFeedRing{3, 4, 3};                                             // step 1 of a layout change: read 3–4, still write 3
// inline constexpr LayoutWindow kFeedRing{4, 4, 4};                                          // step 2, after every reader has moved: write 4
Segment* attach(const char* name, const LayoutWindow& w) {
    Segment* s = map(name); if (s->h.magic != MAGIC) refuse("not a feed ring");
    if (s->h.layout_version < w.min_read || s->h.layout_version > w.max_read) refuse("layout %u outside [%u, %u]: this binary cannot attach", …);
    if (s->h.layout_fingerprint != fingerprint_for(s->h.layout_version)) refuse("layout %u fingerprint mismatch: rebuilt struct under the same version");
    return s;
}
```

- **Two-step layout changes** (read-both, then write-new) are the only intraday-safe kind; a one-step change is a pre-open restart of every process on the segment.
- **Message versions** ride the same rule: an SBE header's `schemaId`/`version`/`blockLength` (Ch. 67) is read before the body and a version outside the window is a refusal with a counter (Ch. 93), not a decode.
- **The readiness gate (Appendix M.4) checks the windows**: every process's declared window vs every live segment's version, before arming.

### 110.4.3 Blue/green cutover for process-per-role

```
   1. start vN+1 on its reserved core(s) (Ch. 53 — the deploy plan reserves a core per role for exactly this), attached to the live rings under its window (§110.4.2),
      as a REPLICA (Ch. 103 §103.2.3): apply from the current cursor (or from a snapshot + catch-up — Ch. 96 §96.4.7), emit nothing
   2. warm it (Ch. 59): N ticks applied, pages touched, connections pre-established where the role has them (the gateway's warm standby — Ch. 104)
   3. shadow: compare vN+1's per-seq digest with vN's (Ch. 103 §103.2.2); parity below budget → stop, keep vN, collect the diverging seq (Ch. 96 §96.4.6)
   4. flip: store accepted_epoch = N+1 (release) in the consumer-side gateway word (§110.3: one store, one tick of gap); for the gateway role itself the flip is
      Ch. 104 §104.4.3's takeover sequence with the operator as the lease store (Logon with the new token, resync, cancel-all only if the venue session changes)
   5. keep vN running SUPPRESSED for the rollback window (minutes to a session): rollback = store accepted_epoch = N (§110.3: 278 ns median after rollback, no gap)
   6. retire vN after the window; its core returns to the pool
```

- **The accept check is already there**: every emitter in Ch. 104's design checks a token before sending; the planned cutover reuses the same word with the operator writing it. There is no separate "deploy mode".
- **The gateway role is special** because the venue sees it: a new gateway version either takes over the *session* (Ch. 104's sequence, with the venue's reject-vs-bump rule deciding timing) or inherits the socket (`SCM_RIGHTS` fd passing to the new process — Ch. 64 — keeps the TCP session up while the process changes; the sequence page and resend store are in shm already — Ch. 99 §99.4.1).
- **Measure the cutover every time** as §110.3 does: decisions per version, duplicates, missing, gap at the flip, latency of the first thousand ticks after it; the numbers go in the release's evidence.

### 110.4.4 Shadow runs and canary shards

**Shadow runs** are step 3 of §110.4.3 run for longer and on a *separate host* fed by layer-1 replication of the production feed (Ch. 75 §75.4.3): the candidate decides on the real ticks, its orders are suppressed at the last gate (Ch. 59 §59.4.1's mechanism — the gateway has no venue credentials on that host), and three things are compared with the incumbent: **decision parity** (per-seq digest; a budgeted divergence rate for intentional changes), **timing** (§110.4.5), and **divergence content** (the first differing decision per divergence class, journaled for review). Auto-stop on parity below budget or a kill trigger. **Canary shards** are the first *emitting* stage: the candidate takes one symbol/venue shard (Ch. 96 §96.4.5) with tighter per-shard limits in the risk gate (Ch. 97 — lower notional, lower rate), for a bounded time, and its SLIs — fill rate, reject rate, cancel ratio, markout (Ch. 111 §111.4.5), tick-to-trade p50/p99 — are compared with the shard's own history and with the sibling shards still on the incumbent, over the same minutes. A canary that is better on latency and worse on markout is a decision for a human, with the data in front of them; a canary that trips a limit is rolled back by the flip.

### 110.4.5 Paired production comparison, with its A/A

```
   design: incumbent (A) and candidate (B) consume the SAME ticks at the SAME instant (layer-1 replica or the same shm ring); B's orders suppressed;
           per tick: l_A = t_decision_A − t_publish, l_B = t_decision_B − t_publish  (hardware timestamps where the path crosses a NIC — Ch. 70, 73)
   report:  median of (l_B − l_A) with a bootstrap CI  — the bulk shift, with the host's per-tick noise cancelled
            Δp50 unpaired with a CI                     — the sanity check that does not inherit the pairing structure
            p99.9 of l_A and of l_B separately, with Ch. 105 forensics on the worst ticks — tails are attributed, not differenced
   A/A FIRST: run the design with B = A (same binary) for a window; the median of (l_B − l_A) is the design's OFFSET (§110.3: 49–50 ns, from cursor-observation order);
              counterbalance by swapping roles between windows; subtract the offset; a candidate effect smaller than the offset's uncertainty is not measurable this way
   where the tick enters through a NIC rather than a shm ring, the two hosts' PTP offsets (Ch. 73) are part of the A/A offset too
```

- **Decision parity before timing**: a candidate that decides differently is not comparable tick-for-tick; the timing comparison is over the parity-equal ticks.
- **Report per stage** (Ch. 92's in-band tracer gives l_A and l_B per §111.3 stage): a +20 ns decode and a −20 ns strategy cancel in the end-to-end and are both findings.
- **Promotion criteria are written before the run**: bulk shift within X ns of zero after offset correction, no stage worse than Y, parity above Z, tails with no new mechanism — the same discipline as a pre-registered study, because the person who ran the candidate wants it to pass.

### 110.4.6 Venue conformance and the weekend test

Every venue requires a **conformance / certification** session before a new order-entry build may connect (Appendix K.4): a scripted sequence — logon with sequence gaps, resend across a reconnect, heartbeat miss, reject handling, mass cancel, cancel-on-disconnect behaviour — run against the venue's test environment, usually in a weekend window. Treat it as a release step with evidence: the script's results, the gateway's journal (Ch. 99) and the venue's log, attached to the manifest; and treat the venue's test environment as a *fault-injecting venue double with ground truth* (Ch. 108 §108.4.3) — the DST seeds that model the venue's documented behaviours are validated against it. The same weekend window hosts the **DR/failover drill** (Ch. 104 §104.6, Appendix M) and the tap-validated replay of the last open through the new build (Ch. 102 §102.4.4) on the production hardware itself.

## 110.5 Pitfalls & anti-patterns

- **Deploying into the open.** The calendar exists because the cost tail of any change is at the open and at expiry; "it is a small change" is how the big outage starts.
- **Restart-in-place as the cutover.** Stopping vN and starting vN+1 is a cold start (Ch. 59) with no rollback and a warm-up tail on the first real tick; start beside, warm, flip, keep.
- **Attaching to a layout you were not built for.** A new binary that reinterprets the old ring's bytes is a torn read at deploy time; declare the window, refuse outside it (§110.4.2).
- **Shadow traffic that can send.** A shadow host with venue credentials is one bug away from doubling the book (Ch. 59 §59.5); suppression is structural (no credentials, no route), not a flag.
- **A canary without tighter limits.** The canary's job is to be hurt a little; under full limits it can be hurt fully.
- **Paired comparison without an A/A.** §110.3's 50 ns offset would have been reported as the candidate's regression — or its improvement, depending on which process started first. Calibrate and counterbalance.
- **Trusting the lab's number over production's.** The CI gate cannot see DDIO contention, the venue, live bursts or firmware; the shadow and canary stages exist because Ch. 111 §111.5's drift is real.
- **Rollback by rebuild.** If undoing a release takes a build, it is not a rollback; keep vN running and its manifest ready; the flip back is one store (§110.4.3).

## 110.6 Exercises & checklist

1. **Write the calendar.** For your system, classify every change class into §110.2.1's rows with the mechanism and the window; add the freeze dates for the next quarter.
2. **Cut over for real.** Run `cutover.cpp`'s protocol with two versions of a Ch. 96 role on a shm ring; report decisions per version, duplicates, missing, gap at the flip, and the first-1000-ticks latency; then roll back.
3. **Two-step layout change.** Add a field to a ring record behind a layout version bump; ship the read-both binary, then the write-new one; show the readiness gate refusing a binary outside its window.
4. **A/A your production comparison.** Run two identical shadows for an hour on the layer-1 replica and report the per-tick offset and its stability across role swaps; decide the smallest effect the design can measure.
5. **Conformance as DST.** Encode your venue's certification script as `SimVenue` behaviours (Ch. 108 §108.4.3) and run it as a build-time test; compare with the weekend session's log.

Checklist:

- [ ] A **deploy calendar** maps every change class to a window and a mechanism; freezes are enforced by tooling (§110.2.1).
- [ ] Every binary declares a **layout compatibility window**; attach refuses outside it; layout changes are two-step or pre-open (§110.4.2).
- [ ] Releases are **signed manifests** naming build IDs, flags, layouts, profiles, config and gate evidence; the supervisor checks the manifest against the host (§110.4.1).
- [ ] Cutover is **start beside → warm → shadow with parity → one-store flip → keep the old version for rollback**, measured each time (§110.4.3).
- [ ] Shadow hosts **cannot send** structurally; canaries run on one shard under **tighter limits** with SLIs compared to siblings (§110.4.4).
- [ ] Production comparison is **paired, A/A-calibrated and counterbalanced**, reported per stage, with promotion criteria written in advance (§110.4.5).
- [ ] Venue **conformance**, the failover drill and the tap-validated replay run in the weekend window and attach to the manifest (§110.4.6).

## 110.7 References

- Jez Humble and David Farley, *Continuous Delivery* (2010) — deployment pipelines, blue/green and canary releases; Martin Fowler, "BlueGreenDeployment" and "CanaryRelease" (martinfowler.com) — the vocabulary this chapter specialises for a trading system.
- Nicole Forsgren, Jez Humble and Gene Kim, *Accelerate* (2018) — change failure rate and time-to-restore as the release metrics that matter.
- Ya Xu et al., "From Infrastructure to Culture: A/B Testing Challenges in Large Scale Social Networks" (KDD 2015) and Kohavi, Tang and Xu, *Trustworthy Online Controlled Experiments* (2020) — A/A tests and paired designs; the source of §110.4.5's "A/A first".
- The venue conformance/certification guides (CME, Nasdaq, Cboe, Eurex) — Appendix K.4; FIA/ESMA guidance on change management for algorithmic trading (RTS 6 Art. 5–7: testing, conformance and deployment).
- `SCM_RIGHTS` / `unix(7)` — passing a live socket to a new process; Ch. 64.
- Ch. 95 (in-process cutover), Ch. 96 §96.4.8 (layout headers), Ch. 103 §103.2.2 (digest parity), Ch. 104 (the token flip and takeover), Ch. 109 (the gate and its A/A), Appendix M (readiness).

## 110.8 Additional Reading

- Charity Majors et al., *Observability Engineering* (2022) — "testing in production" done deliberately; the shadow and canary stages as observability problems.
- Ch. 111 §111.4.5 — markout and race outcome as the canary's business SLIs, computed from captures and venue timestamps.
- Dan Luu, "Files are hard" and the Jepsen reports — what "the old version is still running" protects you from when the new one is subtly wrong.

---

*Next: Ch. 111 — Production Profiling & End-to-End Case Study, the capstone: with the application's histograms (Ch. 93), the host's invariants (Ch. 106) and the wire's counters (Ch. 107) on one timeline, a full tick-to-trade walkthrough that decomposes the budget stage by stage, deterministic replay as the regression fixture, the CI gate in principle (its mechanics now live in Ch. 109), and — new — post-trade latency analytics: joining the capture, the order lifecycle and the venue's timestamps to learn what a nanosecond is worth.*
