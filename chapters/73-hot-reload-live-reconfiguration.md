# Part XII — Observability & Operations in Production

# Chapter 73 — Hot Reload & Live Reconfiguration

> **Prerequisites:** Ch. 30 (atomics / memory model — the release-store/acquire-load that makes a swap safe), Ch. 35 (seqlocks — single-writer publication of config snapshots), Ch. 36 (safe reclamation — RCU/hazard/epoch for freeing the old version), Ch. 26 (mmap/shared memory — config segments), Ch. 46 (keeping the hot path warm — warming a freshly loaded strategy), Ch. 72 (security — validate/sign config before swap).
>
> **Leads into:** Ch. 74 (process topology — config distribution across cooperating processes, single-writer-per-segment), Ch. 76 (case study — live reconfiguration in the end-to-end system). A Part XII operations chapter: changing a running system *without* dropping a tick or tearing the hot path.

---

## 73.1 Why it matters: change strategy/config without dropping a tick

A trading system runs continuously through the session, and during that session you *must* be able to change it: tune a strategy parameter, update a risk limit, load new reference/symbol data, enable or disable a symbol, even swap the strategy *code* — and you must do it **without restarting, without dropping a tick, and without adding a microsecond of jitter to the hot path** (Ch. 1). The naive way to change config — stop the process, edit, restart — is unacceptable: a restart means missing market data during the downtime, losing warm caches/TLB/predictor state (Ch. 46 — the first messages after restart are *slow*, exactly when you can least afford it), dropping the order session, and a window of being blind to the market. During a live session, a restart is a small outage, and outages in trading cost money and risk.

So the requirement is **live reconfiguration**: the running process picks up new parameters/data/code *in place*, atomically, while the hot path keeps reading and trading at full speed. This is harder than it sounds because of the **reader/writer concurrency** at its core: the hot path is *constantly reading* the config (every tick consults the strategy parameters, the risk limits, the symbol table), and a control thread wants to *replace* that config underneath it. Do it carelessly and you get the two failure modes this chapter exists to prevent: a **torn read** (the hot path sees a half-updated config — old price scale with new tick size, a symbol table mid-rewrite — and makes a *wrong* decision, a correctness/safety bug as bad as Ch. 72's), or a **stall** (the update takes a lock the hot path also needs, or triggers an allocation/page-fault, and the hot path blocks — a tail-latency event, Ch. 1).

The resolution is the publication machinery this book already built: **the hot path reads config lock-free; the writer publishes a new immutable version with a single atomic release-store; the old version is reclaimed safely once no reader can see it** (Ch. 30, 35, 36). The config is never *mutated in place* — it's *replaced wholesale*, so a reader always sees a complete, consistent version (old or new, never half). This chapter builds that: the atomic-swap/double-buffer mental model (§73.2), a test that proves no tear or stall (§73.3), the seqlock/RCU-published, versioned, validated-before-swap, drain-or-cutover techniques including dynamic strategy reload with warm-up (§73.4), and the ways a reload sneaks a tear or a stall onto the hot path (§73.5).

## 73.2 Mental model: atomic pointer swaps and double-buffering

The core idea: **config is immutable once published; updating means building a new version off-path and swapping a pointer to it atomically.** The hot path never sees a mutation — it dereferences a pointer that points at a complete, frozen config, and the only thing that ever changes is *which* complete config the pointer names:

```
   CONTROL THREAD (off hot path)                 HOT PATH (reads every tick)
   ─────────────────────────────                 ───────────────────────────
   1. build new Config v2 (alloc, parse,         cfg = config_ptr.load(acquire);   // one atomic load
      VALIDATE — §61.4.3, off-path)              use cfg->param, cfg->limit, ...    // read a FROZEN, consistent version
   2. config_ptr.store(&v2, release);  ◄──swap──►  (reader either sees v1 or v2, NEVER a mix)
   3. reclaim v1 once no reader holds it (Ch.35)
        ▲ the ONLY hot-path-visible change is the single atomic pointer store
```

The pieces:

- **Immutable, wholesale replacement.** A config version is built completely and *then* frozen; readers only ever see a fully-constructed version. You never edit the live config field-by-field (that's the torn-read bug, §73.5). To change one parameter you build a *new* version (copy + change) and swap to it. This is the RCU discipline (Ch. 36) applied to config.
- **The atomic pointer swap is the publish.** A single `std::atomic<Config*>` (or `shared_ptr` with care — §73.5). The writer does a **release** store (Ch. 30) after fully building+validating v2; the reader does an **acquire** load before reading fields. Release/acquire guarantees that if the reader sees the new pointer, it sees *all* of v2's fully-written contents (no reordering exposes a half-built version) — this is exactly the Ch. 30 publication pattern.
- **Double-buffering** is the bounded form: two (or a small ring of) config buffers, the writer fills the inactive one and flips an index/pointer. Bounds the memory and is the natural shape when versions are large (a full symbol table) and you don't want unbounded allocation.
- **Safe reclamation** (Ch. 36) frees the old version *only after* every reader that could be holding it has moved on — RCU grace period, hazard pointer, epoch, or (simplest) a `shared_ptr` whose refcount the readers bump. Free too early and a reader dereferences freed memory (UAF, Ch. 72); the whole point of Ch. 36 is doing this without blocking the readers.

The mental shift from "edit the config" to "**publish a new immutable config and atomically point at it**" is the entire chapter. Once config is immutable-and-swapped, the hot path's interaction is a single lock-free atomic load — no lock, no allocation, no tear — and reconfiguration is just "build off-path, validate, release-store, reclaim."

## 73.3 Measure it: reload-induced tear/stall test

The two failure modes — **tear** and **stall** — are exactly what you must measure, under load, while reloading repeatedly. A reload that "works" in a quiet test can tear or stall under the real hot path.

**Stall test** — does a reload add latency to the hot path? Run the hot path under representative load (Ch. 3, with HdrHistogram — Ch. 1) and fire reloads continuously from the control thread; compare the hot-path latency distribution *with* concurrent reloads vs *without*. Representative shape (Xeon Gold 6326; figures pending real runs):

| Reconfiguration approach | Hot-path cost to read config | Hot-path impact during a reload | Verdict |
|---|---|---|---|
| **Lock around shared config** (read + write under mutex) | lock/unlock per read | reader **blocks** while writer holds lock → ms stall under contention | ✗ stall — a Ch. 32 convoy on the hot path |
| **Mutate config in place** (no lock, "it's just a few fields") | a few loads | **torn read** — reader sees half-updated config | ✗ tear — wrong decisions |
| **Atomic pointer swap / RCU-published** (§73.2) | **one acquire load** | reader sees old-or-new, never blocks; reclamation off-path | ✓ no tear, no stall — flat distribution |
| **Seqlock-published** (Ch. 35) | a couple of loads + a version check (retry on rare concurrent write) | reader retries only if it races the rare write; no block | ✓ no tear; bounded retry (good for small/fixed config) |

**Tear test** — can the hot path ever observe an inconsistent config? Make the config have a cross-field invariant (e.g. `tick_size` and `price_scale` that must match, or a symbol table where an index must be valid). Have the writer reload versions that flip *both* fields together, and have the reader *assert the invariant* on every read while reloads hammer. With in-place mutation, the assertion fires (the reader caught a half-update); with atomic swap / seqlock, it never fires (every read is a complete version). Run it for millions of reloads under TSan (Ch. 40) to catch the race the assertion might miss.

The lessons:

- **A lock makes the writer's work the reader's stall** (Ch. 32) — unacceptable on the hot path. The reader must never block on the writer.
- **In-place mutation tears** — there is no "small enough" update that's safe to mutate live; even two fields can be observed half-updated. Immutable wholesale replacement is the only tear-free approach (§73.2).
- **Atomic swap / seqlock / RCU give a flat hot-path distribution *and* tear-freedom** — the reader pays one acquire load (swap/RCU) or a couple loads + version check (seqlock), and a reload adds *nothing* to the hot path's tail. The cost moved entirely off-path (build, validate, reclaim).

## 73.4 Techniques

### 73.4.1 Seqlock-/RCU-published config snapshots read lock-free

The two lock-free publication mechanisms (Ch. 35–36), chosen by config size and shape:

- **RCU / atomic-pointer-published (best for larger or pointer-reached config).** The config is behind an `std::atomic<const Config*>`; readers `load(acquire)` and use the pointed-to version (Ch. 36's read side — no lock, no retry, just a dependent load). The writer builds a new version, `store(release)`, and reclaims the old via grace period / hazard pointer / epoch (§73.4.2). Read cost: one acquire load. This is the default for strategy parameters, risk limits, and reference data reached through a pointer.
- **Seqlock-published (best for small, fixed-size, frequently-read config).** The config is a small fixed struct published with a sequence counter (Ch. 35): the writer bumps the sequence (odd), writes the fields, bumps again (even); the reader reads sequence, reads fields, re-reads sequence, and retries if it changed or was odd. No pointer chase, no reclamation, but readers *retry* on the rare concurrent write and the payload must be small/trivially copyable (Ch. 35's constraints). Ideal for a handful of hot parameters read every tick (a threshold, a flag, a limit).
- **Single-writer discipline (both).** One writer (the control thread) publishes; many readers (hot threads) consume. This single-writer-multiple-reader shape (Ch. 35) is what makes both mechanisms simple and correct — and it ties Ch. 74's single-writer-per-segment rule. Never two writers to the same config without their own serialization.

### 73.4.2 Versioned configuration and safe reclamation

Replacing a version raises the question Ch. 36 answers: **when is it safe to free the old one?** A reader may still be using v1 at the instant you publish v2.

- **Version the config** (a monotonically increasing version number in each snapshot) so you can tell which is live, log which version made a decision (ties Ch. 75 capture — record the config version with each order for replay), and detect a stuck reader.
- **Reclaim with an Ch. 36 mechanism, chosen by constraints:**
  - **`shared_ptr<const Config>`** — simplest: readers `atomic_load` the `shared_ptr` (or load a raw pointer and bump a ref), the old version frees when the last reader drops it. Correct and easy, but the refcount bump/drop is an atomic RMW on the read side (Ch. 25 — some hot-path cost and a shared cache line; measure). Fine when config reads aren't the very hottest thing.
  - **RCU grace period** — readers are unmarked-fast (just an acquire load, no RMW); the writer waits a grace period (until every reader has passed a quiescent point) before freeing. Lowest read cost; needs the RCU plumbing (Ch. 36).
  - **Hazard pointers / epoch** — bounded memory, lock-free reclamation (Ch. 36), middle ground.
- **Don't free on the hot path.** Reclamation (the actual `delete`/`free`, which can page-fault or take the allocator lock — Ch. 23) runs on the control/reclaimer thread, never the hot path. The hot path only ever *reads*.

### 73.4.3 Shared-memory config segments; validation-before-swap

For config that crosses process boundaries (Ch. 74's process-per-role topology) and for the validation gate:

- **Shared-memory config segments (Ch. 26).** Put the config in a shared-memory segment (`mmap`, Ch. 26) so a separate *control/admin process* writes it and the trading processes read it — the same publish/swap discipline (§73.2) across processes: an atomic version/pointer-offset in the segment, single-writer (the control process), many readers (the trading processes). This decouples the *act of reconfiguring* (an operator tool, an admin process) from the *trading processes* (Ch. 74 fault isolation), and survives a trading-process restart (the config is in shared memory, pre-faulted and `mlock`'d — Ch. 26 — so a restarted process picks it up warm). Use offsets, not pointers, in cross-process segments (the segment maps at different addresses).
- **Validate before swap — always (ties Ch. 72).** The new version is fully **validated off the hot path before it's published**: schema/range checks (a price scale that's positive, limits that are sane, a symbol table that's internally consistent — the §73.2 cross-field invariants), and — for config that arrives from outside — signature/integrity verification (Ch. 72.4.7). A bad config must be *rejected before the swap*, never published and then discovered live. The swap publishes only known-good versions; validation failure leaves the live config untouched. This is the config analog of Ch. 72's "validate untrusted input at the boundary."

### 73.4.4 Draining vs instantaneous cutover; dynamic-library/strategy reload with warm-up

Two cutover styles, and the hardest case (reloading *code*):

- **Instantaneous cutover (the atomic swap, §73.2).** The new version becomes live the instant the pointer is stored; in-flight reads finish on whichever version they loaded. Right for **stateless** config — parameters, limits, reference data — where old-or-new are both individually valid and there's no transition state. This is the common case and it's tear-free by construction.
- **Draining cutover.** When the change has *transition semantics* — you must stop sending *new* work to the old version, let in-flight work complete on it, *then* switch (e.g. retiring a strategy that has open orders, or a change that mustn't apply mid-sequence) — you **drain**: mark the old version closing, route new events to the new version, wait until the old has no in-flight work (its readers/orders have completed), then reclaim. Draining is RCU's grace period generalized to *application* in-flight state, not just memory readers.
- **Dynamic-library / strategy code reload (the hard case).** Swapping *code* (a `dlopen`'d strategy `.so`, or a strategy object behind a pointer) is the §73.2 swap applied to a *strategy instance*: build/load the new strategy off-path, **warm it up** (Ch. 46 — pre-touch its memory, prime its caches/branch predictors with shadow traffic so its *first real tick* isn't a cold-start stall — a reloaded-but-cold strategy is a tail event exactly when you switched to it), validate it, then atomically swap the active-strategy pointer (instantaneous) or drain the old one (if it holds open orders). Manage the old `.so`'s lifetime with Ch. 36 reclamation (don't `dlclose` while a hot thread is inside it). The combination — load + validate + **warm** + atomic swap + safe reclaim — is hot reload's most demanding form, and warming (Ch. 46) is the piece teams forget.

## 73.5 Pitfalls & anti-patterns: torn config, stalls during cutover

- **Mutating config in place (the cardinal tear).** Editing the live config's fields while the hot path reads it — even "just one field" — lets the reader observe a half-update and make a wrong decision (§73.3 tear test). There is no safe in-place mutation; replace the whole version immutably and swap (§73.2).
- **A lock the hot path must take (the cardinal stall).** Guarding config with a mutex the reader also acquires turns the writer's update into the reader's stall (Ch. 32 convoy, §73.3). The reader must be lock-free (atomic load / seqlock / RCU); the writer must never block it.
- **Freeing the old version too early (UAF).** Reclaiming v1 while a reader still holds it is a use-after-free (Ch. 72) — silent in an arena (Ch. 24). Use an Ch. 36 mechanism (grace period / hazard / epoch / `shared_ptr`) that frees only after no reader can see it; reclaim off the hot path (§73.4.2).
- **Publishing before validating (or never validating).** Swapping to a config that's malformed/out-of-range/unsigned (Ch. 72) means discovering the bad config *live*, mid-trade. Validate (and verify integrity) off-path *before* the swap; reject bad versions without touching the live one (§73.4.3).
- **Reloading code without warming it (cold-start stall).** `dlopen` + swap to a strategy whose caches/TLB/predictors are cold → the first real tick after the swap is slow (Ch. 46), precisely when you switched. Warm the new strategy with shadow traffic before going live (§73.4.4).
- **Naive `shared_ptr` without atomic access.** A plain (non-atomic) `shared_ptr` swapped concurrently is itself a data race (the control block / pointer aren't atomically updated together). Use `std::atomic<std::shared_ptr<>>` (C++20) or `atomic_load`/`atomic_store` on the `shared_ptr`, and remember the refcount RMW is a hot-path cost to measure (§73.4.2).
- **Missing release/acquire on the swap.** Storing the new pointer without a release (or loading without acquire) lets the reader see the new pointer but stale/half-written contents (Ch. 30) — a subtle tear. The publish is a *release* store after full construction; the read is an *acquire* load before use (§73.2).
- **Cross-process config with raw pointers.** A shared-memory config segment (Ch. 26) maps at different addresses in different processes — storing raw pointers in it is a bug. Use offsets within the segment (§73.4.3).
- **Two writers to one config.** Single-writer is what makes the publication simple and correct (Ch. 35, §73.4.1). Two uncoordinated writers race; if you truly need multiple config sources, serialize them through one publisher.
- **Instantaneous cutover where draining was needed.** Atomically swapping when the change has transition state (open orders on the old strategy, a mid-sequence change) can strand or double-handle in-flight work. Recognize transition semantics and *drain* (§73.4.4).

## 73.6 Exercises & checklist

**Exercises:**

1. **Tear test.** Build a config with a cross-field invariant (`tick_size`/`price_scale`) and a reader that asserts it every read. Reload versions that flip both fields (a) by in-place mutation and (b) by atomic pointer swap (§73.2), under load, for millions of iterations under TSan (Ch. 40). Show (a) fires the assertion / TSan race and (b) never does.
2. **Stall test.** Measure the hot-path latency distribution (HdrHistogram, Ch. 1) with continuous reloads vs none, for (a) lock-guarded config and (b) RCU/atomic-swap (§73.3). Show (a) injects stalls and (b) is flat.
3. **Seqlock vs RCU config.** Implement the same small parameter set published two ways — seqlock (Ch. 35) and atomic-pointer/RCU (Ch. 36) — and compare read cost and behavior under a high writer rate (seqlock retries; RCU doesn't). Decide which fits a small fixed payload vs a large pointer-reached one (§73.4.1).
4. **Safe reclamation.** Reload rapidly and use ASan (Ch. 40) to catch a use-after-free if you free the old version too early; then add `shared_ptr`/RCU reclamation (§73.4.2) and show it's clean. Confirm the `delete` runs off the hot path.
5. **Validate-before-swap.** Add a validation gate (§73.4.3) that rejects an out-of-range/unsigned config; feed it a bad version and confirm the live config is untouched and the bad one never publishes.
6. **Warm strategy reload.** `dlopen` a new strategy, warm it with shadow traffic (Ch. 46), then atomically swap. Measure the first-real-tick latency *with* and *without* the warm-up step and quantify the cold-start stall you avoided (§73.4.4).

**Checklist:**

- [ ] Config is **immutable once published** and replaced **wholesale** — never mutated in place (§73.2, §73.5).
- [ ] The hot path reads config **lock-free** — one acquire load (atomic-pointer/RCU) or seqlock read; it **never blocks** on the writer (§73.3, §73.4.1).
- [ ] The publish is a **release store after full construction+validation**; the read is an **acquire load** (Ch. 30); no torn read possible (§73.2, §73.5).
- [ ] The old version is **reclaimed off the hot path** via an Ch. 36 mechanism (`shared_ptr`/RCU/hazard/epoch) only after **no reader can see it** — no UAF (§73.4.2).
- [ ] New config is **validated (and integrity/signature-verified, Ch. 72) off-path before the swap**; bad versions are **rejected without touching the live config** (§73.4.3).
- [ ] **Single writer** per config; cross-process config in **shared memory (Ch. 26) using offsets** (not raw pointers), pre-faulted/`mlock`'d (§73.4.3).
- [ ] **Stateless** changes use **instantaneous cutover**; changes with **transition state** use **draining** (§73.4.4).
- [ ] **Code/strategy reloads** load + validate + **warm (Ch. 46)** + atomic-swap + safe-reclaim — the new strategy's **first real tick is not a cold-start stall** (§73.4.4).
- [ ] The config **version** is recorded with decisions/orders (ties Ch. 75 replay) (§73.4.2).

## 73.7 References

- McKenney on **RCU** (Ch. 36 references) — read-side-lock-free publication and grace-period reclamation, the foundation of pointer-swapped config (§73.4.1-2).
- The **seqlock** literature (Ch. 35 references) — single-writer versioned publication for small payloads (§73.4.1).
- The C++ memory model / `std::atomic` documentation (Ch. 30 references) — release/acquire publication and `std::atomic<std::shared_ptr>` (C++20) (§73.2, §73.5).
- `dlopen`/`dlsym`/`dlclose` and the ELF dynamic-linking documentation — dynamic-library strategy reload (§73.4.4).
- Ch. 26 (mmap/shared memory — config segments), Ch. 46 (warming a reloaded strategy), Ch. 72 (validate/sign before swap), Ch. 35–36 (the publication/reclamation machinery).

## 73.8 Additional Reading

- Talks and write-ups on live reconfiguration and zero-downtime parameter updates in trading and other always-on systems — double-buffering and atomic-swap patterns in practice (§73.2).
- The RCU and hazard-pointer usage guides (Ch. 36) applied to configuration and reference data, not just data structures (§73.4.2).
- Ch. 74 (*Process Topology*) — config distribution across cooperating processes, single-writer-per-segment, and the admin/control process; Ch. 75 (*Capture*) — recording the config version that made each decision for deterministic replay; Ch. 46 (*Keeping the Hot Path Warm*) — warming a reloaded strategy; Ch. 76 (*Case Study*) — live reconfiguration end-to-end.
- **Appendix F** — RCU/seqlock/release-acquire/double-buffer glossary; **Appendix E** — the cost of an atomic RMW (the `shared_ptr` refcount on the read side).

---

*Next: Ch. 74 — Process Topology & The Deterministic State Machine, structuring the trading system as multiple cooperating processes for fault containment: process-per-role (feed handler / strategy / gateway / risk), the deterministic side-effect-free state machine for exact replayability, shared-memory data planes (Ch. 26, 50), fault domains and blast radius, supervisor/watchdog/heartbeats, crash-only design and fast warm restart (Ch. 46), and the kill-switch / safe-state on failure.*
