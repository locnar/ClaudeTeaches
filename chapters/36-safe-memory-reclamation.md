# Part VI — Concurrency

# Chapter 36 — Safe Memory Reclamation

> **Prerequisites:** Ch. 34 (lock-free structures — reclamation is the hard problem they deferred), Ch. 35 (seqlocks/pointer-swap — RCU is the large-state version of single-writer publication), Ch. 30 (memory ordering — reclamation protocols rest on it), Ch. 23–24 (allocation/pools — what you're freeing, and the no-free escape), Ch. 7 (coherence — readers must not write).
>
> **Leads into:** Ch. 37 (Disruptor — avoids reclamation via a fixed ring), Ch. 73 (hot reload — publish-new-version-reclaim-old is exactly RCU/pointer-swap), Ch. 74 (process topology). RCU's read-side is the large-state complement to Ch. 35's seqlock; the "avoid it" escape ties back to Ch. 24.

---

## 36.1 Why it matters: freeing memory under lock-free readers

Chapters 34 and 35 each *dodged* the single hardest problem in lock-free programming. The SPSC ring (Ch. 34) avoided it by never freeing anything (fixed array, integer indices). The seqlock (Ch. 35) avoided it by copying small data instead of sharing it by pointer. But the moment you have a **genuinely dynamic, pointer-linked, shared structure** — a lock-free list/tree/hash-map where nodes are allocated and removed, read concurrently by many threads without locks — you face the question they both ducked: **when a node is removed, when is it safe to `free` it?** The answer is *not* "immediately after unlinking it," because another thread may have loaded the pointer to that node *before* you unlinked it and is **still reading it**. Freeing it now is a **use-after-free** (Ch. 23, 72) — the reader dereferences freed (possibly reallocated) memory. This is the **safe-memory-reclamation (SMR)** problem, and it's the reason lock-free programming has a reputation for being brutally hard.

The difficulty is fundamental: in a *locked* structure, a removed node is trivially safe to free (no one can be inside the structure without the lock). In a *lock-free* structure there's no lock, so there's no moment you can point to and say "no reader is here." A reader can be holding a raw pointer to a node with nothing telling the remover about it. So you need a protocol that lets readers stay lock-free (no writing, no contention — the whole point) while *also* letting the writer know when every reader that *could* have seen a removed node has finished with it — at which point reclamation is safe. The three classic solutions — **RCU**, **hazard pointers**, and **epoch-based reclamation** — are different answers to "how does the writer know it's safe to free?", trading off read-side cost, reclamation latency, and memory overhead.

For HFT the relevance is real but bounded, and the most important lesson is *strategic*: **the fastest reclamation is the one you don't do.** Much of the low-latency data plane is deliberately built to *sidestep* reclamation — fixed-size rings (Ch. 34, 37), object pools with integer indices (Ch. 24), seqlock copies (Ch. 35), single-writer double-buffering — precisely *because* SMR is hard, has overhead, and is a rich source of subtle bugs. But where you genuinely need a dynamic shared structure read at low latency by many threads — a **read-mostly** reference structure (a symbol table, a routing map, configuration, a large book snapshot) updated rarely and read constantly — **RCU** (or atomic pointer-swap with deferred reclamation) is the right and beautiful tool: readers pay *almost nothing* (no atomics on the read side in classic RCU), and the rare writer publishes a new version and reclaims the old once all readers have moved on. This is the large-state generalization of Ch. 35's single-writer publication, and the engine behind hot-reload (Ch. 73). This chapter frames the reclamation problem (§36.2), measures the read-side cost of the schemes (§36.3), explains RCU / hazard pointers / epochs and when each fits (§36.4), and — heavily — warns about use-after-free and unbounded deferral (§36.5), while keeping the "avoid it entirely" escape in view throughout.

## 36.2 Mental model: the reclamation problem

**The core hazard.** A lock-free removal has two phases that *cannot be made atomic together*: (1) **unlink** the node (CAS it out of the structure so new readers can't reach it) and (2) **free** it. Between a reader's "load the pointer" and "dereference it," the writer can unlink and free — use-after-free:

```
   reader:  p = head.load();   ............................  p->data   // dereference — but p was freed!
   writer:        ......  unlink(p)  ......  free(p)  ......            // freed between the reader's load and use
```

Unlinking makes the node unreachable to *future* readers, but a reader that already loaded `p` still holds it. **Reclamation = deferring the `free` until no reader can still hold a reference to the node.** The schemes differ in how they track "can any reader still hold this?"

**The grace-period idea (RCU).** The key insight behind RCU: if readers access the structure inside a **read-side critical section** that contains no blocking/quiescent point, then once *every* thread has passed through a **quiescent state** (a moment when it definitely holds no references — e.g. a context switch, or an explicit `rcu_read_unlock`) *after* the unlink, no reader can possibly still hold the removed node. That interval is a **grace period**; after one grace period elapses, the unlinked node is safe to free. Readers do essentially nothing special (mark the critical section); the *writer* waits for a grace period (`synchronize_rcu`) or defers the free to a callback (`call_rcu`) and reclaims later. Readers are nearly free; reclamation latency is the grace period.

**The per-pointer-protection idea (hazard pointers).** A different answer: each reader, before dereferencing a node, **publishes** the pointer it's about to use into a per-thread "hazard pointer" slot (an atomic store the writer can scan). When the writer wants to free an unlinked node, it **scans all threads' hazard pointers**; if any thread has published that node, the writer defers (the node is "hazardous") and retries later; if none has, it's safe to free. Readers pay a per-access atomic store + a validation re-read (more read-side cost than RCU); reclamation is prompt and memory is bounded (a node is freed as soon as no hazard pointer protects it).

**The epoch idea (EBR).** A middle ground: a global **epoch** counter; readers, on entering a critical section, record the current epoch in a per-thread slot. The writer, on removal, tags the node with the current epoch and reclaims nodes from *old* epochs once all threads have advanced past them. Cheaper read-side than hazard pointers (one epoch read/write per critical section, not per pointer), but a single stalled reader (stuck in an old epoch) can stall *all* reclamation (unbounded memory — §36.5).

**The trade-off space:**

```
   scheme           read-side cost        reclamation latency / memory      stalled-reader risk
   RCU (classic)    ~nothing (best)       grace period (can be long)        a stalled reader delays frees
   Hazard pointers  per-access atomic     prompt, bounded memory            none (per-pointer, robust)
   Epoch (EBR)      per-CS epoch r/w      cheap, but...                     one stuck reader stalls ALL frees
   (avoid it)       n/a                   n/a                               n/a  ← prefer this on hot path
```

The model: **reclamation defers `free` until no lock-free reader can still reference an unlinked node. RCU makes readers nearly free and waits a grace period; hazard pointers make readers publish what they hold so the writer can scan-and-defer (bounded memory, robust); epochs are a cheaper-read middle ground that risks unbounded deferral if a reader stalls. And the best option on a hot path is often to *avoid dynamic shared structures* so there's nothing to reclaim (Ch. 24, 34, 35).**

## 36.3 Measure it: reclamation overhead comparison

The decisive metric for a read-mostly structure is **read-side cost** (what every reader pays on every access), since reads dominate. Measure lookups in a shared structure protected by: (a) a `shared_mutex` (baseline), (b) RCU (read-side ~free), (c) hazard pointers (per-access atomic). Plus the writer's reclamation cost.

```cpp
// reclaim.cpp — read-side cost of shared_mutex vs RCU-style vs hazard-pointer reads.
// (Illustrative skeleton — real RCU/HP via liburcu / folly::hazptr; here a sketch of the read path.)
// Build: g++ -O2 -std=c++20 -march=native reclaim.cpp -o reclaim -pthread
// Run:  ./reclaim rwlock 8 | ./reclaim rcu 8 | ./reclaim hazptr 8   (N readers + 1 occasional writer)
#include <atomic>
#include <shared_mutex>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>
#include <chrono>

struct Node { int key; int val; };
std::atomic<Node*> g_ptr;                       // the read-mostly published pointer (pointer-swap)

// RCU-style read: just load the pointer and use it inside a (marked) read-side critical section.
// (Classic RCU read-side is ~free: rcu_read_lock()/unlock() compile to almost nothing.)
int rcu_read() { /* rcu_read_lock(); */ Node* p = g_ptr.load(std::memory_order_acquire);
                 int v = p->val; /* rcu_read_unlock(); */ return v; }

// Hazard-pointer read: publish the pointer, validate, then use; clear when done.
std::atomic<Node*> hp_slot[64];                 // one hazard slot per reader thread (simplified)
int hp_read(int tid) {
    Node* p;
    do { p = g_ptr.load(std::memory_order_acquire);
         hp_slot[tid].store(p, std::memory_order_seq_cst);   // PUBLISH what I'm about to use
    } while (p != g_ptr.load(std::memory_order_acquire));     // validate it didn't change
    int v = p->val;
    hp_slot[tid].store(nullptr, std::memory_order_release);   // done
    return v;
}

std::shared_mutex rw; Node* rw_node;
std::atomic<bool> stop{false};

int main(int argc, char** argv) {
    const char* mode = argc>1?argv[1]:"rcu"; int R = argc>2?std::atoi(argv[2]):8;
    g_ptr = new Node{1, 42}; rw_node = new Node{1, 42};
    std::vector<std::thread> readers; std::vector<long> cnt(R);
    auto t0 = std::chrono::steady_clock::now();
    for (int i=0;i<R;++i) readers.emplace_back([&,i]{ long n=0; volatile int s=0;
        while(!stop.load()){ if(!std::strcmp(mode,"rwlock")){ std::shared_lock l(rw); s=rw_node->val; }
                             else if(!std::strcmp(mode,"hazptr")) s=hp_read(i);
                             else s=rcu_read(); n++; } cnt[i]=n; });
    std::this_thread::sleep_for(std::chrono::seconds(2));
    stop.store(true); for(auto&t:readers) t.join();
    auto t1 = std::chrono::steady_clock::now();
    double sec = std::chrono::duration_cast<std::chrono::duration<double>>(t1-t0).count();
    long tot=0; for(long c:cnt) tot+=c;
    std::printf("%-7s R=%d  %.1f Mreads/s  (%.1f ns/read)\n", mode, R, tot/1e6/sec, sec*1e9/tot);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), R readers + occasional writer, pinned, turbo off (illustrative; use `liburcu`/`folly::hazptr` for real numbers):

```
   R readers (read-mostly):     ns/read (R=8)     scales with readers?      reclamation
   shared_mutex                 ~250 ns           NO (readers contend)      trivial (lock)
   hazard pointers              ~15 ns            ~yes (per-thread slot)    prompt, bounded memory
   RCU (classic, read ~free)    ~3 ns             YES (read = a load)       grace period (deferred)
```

Read it: **classic RCU read-side is almost free** (~a single acquire load — ~3 ns) and scales perfectly with reader count, because readers do *nothing* that writes shared state (just mark a critical section that compiles to ~nothing and load the pointer). **Hazard pointers** cost more per read (~15 ns — the published-pointer atomic store + validation re-read) but still scale far better than a lock and give **bounded memory / prompt reclamation**. **`shared_mutex` collapses** under readers (the Ch. 35 lesson again — read-lock acquisition writes the lock). The trade-off the numbers encode: **RCU minimizes read cost (best for read-mostly hot paths) but defers reclamation by a grace period (and memory grows until it elapses); hazard pointers cost more per read but reclaim promptly with bounded memory and no stalled-reader risk.** For a read-mostly reference structure read by many strategies (symbol table, routing map, config — Ch. 73), RCU's near-free reads are exactly right. The writer's side (grace-period wait / hazard scan) costs more, but writes are rare. (And the meta-lesson: all of these beat a lock, but *none* beat not-sharing-a-dynamic-structure at all — §36.5.)

## 36.4 Techniques

### 36.4.1 RCU

**Read-Copy-Update** — the read-mostly champion, and the large-state generalization of Ch. 35's single-writer publication:

- **The pattern.** Readers enter a **read-side critical section** (`rcu_read_lock`/`rcu_read_unlock` — in classic/kernel RCU these compile to ~nothing, just disabling preemption or marking a per-thread flag) and access the structure through a published pointer. To update: the writer **copies** the data, modifies the copy, and **atomically swaps** the published pointer to the new version (`rcu_assign_pointer` — a release store). Old readers keep using the old version; new readers see the new one. The writer then waits for a **grace period** (`synchronize_rcu`) — until every reader that could have seen the old version has exited its critical section — and only then frees the old version (or defers via `call_rcu`).
- **Why it's fast for reads.** Readers take no locks, do no atomic RMW, write nothing — read-side is essentially a pointer load (§36.3). This is why RCU scales to enormous reader counts at near-zero cost — ideal for **read-mostly** data.
- **Userspace RCU (`liburcu`).** The kernel's RCU has a userspace port (`liburcu`) with several flavors (QSBR — quiescent-state-based, the fastest reads but needs readers to report quiescent states; memb/signal-based — no reader cooperation needed but slightly costlier). QSBR is the lowest read-overhead choice when you can place quiescent points (e.g. at the top of an event loop — natural in a polling hot path, Ch. 37, 41).
- **HFT fit.** Read-mostly reference/config structures read by many threads and updated rarely: symbol/instrument tables, venue routing maps, risk-limit tables, large book snapshots, strategy parameters. The update = publish-new-version-then-reclaim-old is **exactly hot-reload** (Ch. 73). Atomic pointer-swap *is* the minimal RCU when the "structure" is a single immutable object you replace wholesale (no traversal of internal nodes), with deferred reclamation of the old version.
- **The cost is on the writer / grace period.** `synchronize_rcu` can be slow (wait for all readers to pass a quiescent state); `call_rcu` defers it off the hot path. Memory grows by retired-but-not-yet-freed versions until grace periods elapse (§36.5).

### 36.4.2 Hazard pointers

**Hazard pointers** — per-pointer protection with bounded memory, the robust choice when you can't tolerate unbounded deferral:

- **The pattern (§36.3).** Each thread has a small set of **hazard-pointer slots**. Before dereferencing a node, a reader **stores** the node's pointer into a slot (an atomic store, made visible to writers) and then **re-validates** that the node is still reachable (re-read the source pointer; retry if it changed) — this ordering ensures a writer that frees the node either sees the hazard pointer or the reader sees the node is gone. When done, the reader clears the slot. To reclaim, the writer **scans all threads' hazard slots**; a retired node is freed only if **no** slot points to it, otherwise it's kept on a retired list and retried later.
- **Properties.** **Bounded memory** (a node is freed as soon as no hazard pointer protects it — at most O(threads × slots) nodes pending), **robust to stalled readers** (a stuck reader only protects the specific nodes in its slots, not all reclamation), and **prompt** reclamation. The cost: a per-access atomic store + validation on the read side (more than RCU — §36.3), and the writer's scan.
- **Standardization (`std::hazard_pointer`, C++26; `folly::hazptr`).** Hazard pointers are being standardized; `folly::hazptr` is a production implementation. Prefer these over hand-rolling (the validation/ordering is subtle).
- **When to choose over RCU.** When **bounded memory** matters (you can't let retired nodes accumulate during a long grace period), when readers can **stall** (so RCU's grace period could be unbounded), or when reclamation must be **prompt**. The price is higher per-read cost. For a latency system that can't tolerate memory growth or a stalled-reader stall, hazard pointers are the safer reclamation choice even though reads cost more.

### 36.4.3 Epoch-based reclamation

**EBR** — a cheaper-read middle ground between RCU and hazard pointers:

- **The pattern.** A global **epoch** counter (advances periodically). On entering a critical section, a reader records the current global epoch in its per-thread slot (and marks itself "active"). The writer, on retiring a node, tags it with the current epoch; a node can be freed once **all active threads** have advanced to an epoch *beyond* the one in which it was retired (so no thread can still be in a critical section that observed it). Reclamation happens in batches as the epoch advances.
- **Properties.** **Cheap read-side** — one epoch read/write per *critical section* (not per pointer like hazard pointers), so cheaper than HP and close to RCU; simpler than RCU's grace-period machinery in userspace. Used by Crossbeam (Rust), and common in C++ lock-free libraries.
- **The danger: unbounded deferral.** A single thread that **stalls inside a critical section** (descheduled, blocked, slow) keeps the global epoch from advancing, so **no memory can be reclaimed** across the whole structure — retired nodes pile up unboundedly (§36.5). This is EBR's Achilles heel and why it's risky where a reader can stall (the opposite of hazard pointers' robustness). On a system with pinned, non-blocking, fast readers (a dedicated-core hot path — Ch. 32, 41) the risk is low; on a general system it's real.
- **When to choose.** When you want cheaper reads than hazard pointers, the structure is genuinely dynamic (so RCU's pointer-swap doesn't fit as cleanly), and readers are **short and non-stalling** (bounded critical sections, pinned cores). Otherwise hazard pointers' bounded memory is safer.

### The escape hatch: avoid reclamation

Throughout, keep the strategic option front-and-center (ties Ch. 24, 34, 35): **don't have a dynamic shared structure that needs reclamation.** Fixed-size rings (Ch. 34, 37), object pools with integer **indices** instead of pointers (an index can't be "freed" out from under you — the slot is reused only when you decide — Ch. 24), seqlock **copies** of small data (Ch. 35), single-writer **double-buffering** (two fixed buffers, swap an index), and **type-stable** memory (a pool that never returns memory to the OS, so a stale pointer at worst reads a valid-but-old object of the same type — defusing UAF into a benign stale read). On the hot path these are usually *better* than any reclamation scheme — lower overhead, simpler, no SMR bugs. Reach for RCU/HP/EBR only when the structure genuinely must be dynamic and pointer-shared.

## 36.5 Pitfalls & anti-patterns: use-after-free, unbounded deferral

- **Freeing immediately after unlinking (the cardinal UAF).** Unlinking a node makes it unreachable to *future* readers but not to readers that already loaded the pointer — freeing it now is use-after-free (§36.2, Ch. 72). You **must** defer the free via a reclamation scheme (RCU/HP/EBR) or avoid the situation (indices/pools). The defining lock-free reclamation bug.
- **Unbounded deferral / memory blow-up.** RCU with a long grace period (or `call_rcu` callbacks not running), or **EBR with a stalled reader stuck in a critical section**, lets retired nodes accumulate without bound — a memory leak that can OOM under load (§36.4). Bound it: hazard pointers (bounded by construction), short/non-blocking critical sections, prompt grace periods, and **never block/stall inside a read-side critical section**.
- **Blocking or stalling inside a read-side critical section.** A reader that page-faults (Ch. 23), makes a syscall, or gets descheduled while inside an RCU/EBR critical section delays the grace period/epoch for *everyone* → unbounded deferral. Read-side critical sections must be **short, non-blocking, fault-free** (pre-faulted — Ch. 26). Hazard pointers are more robust here (only the held pointers are protected).
- **Hazard-pointer ordering bugs.** The publish-then-validate ordering (store the hazard pointer, *then* re-check reachability) is subtle; getting the fences wrong reopens the UAF window. Use a vetted implementation (`folly::hazptr`, `std::hazard_pointer`) — don't hand-roll the protocol.
- **ABA via reclamation (Ch. 34).** Reclamation and ABA are linked: reusing freed memory at the same address causes ABA. Reclamation schemes (defer the free) and tagged pointers/indices solve ABA together. A structure with reclamation bugs often has ABA bugs too.
- **Wrong scheme for the workload.** RCU for a *write*-heavy structure (grace periods can't keep up, memory blows up) or hazard pointers where the per-read cost dominates a read-extreme path — mismatched. Match: **RCU/pointer-swap = read-mostly, rare writes**; **hazard pointers = bounded-memory/robustness needed, stalled readers possible**; **EBR = cheap reads, short non-stalling readers**.
- **Reinventing SMR.** RCU, hazard pointers, and EBR are genuinely hard to implement correctly (ordering, the validation races, grace-period detection). Use `liburcu`, `folly::hazptr`/`std::hazard_pointer`, Crossbeam-style libraries — **don't hand-roll** for production; reserve hand-rolling for learning, and validate with TSan/model-checking (Ch. 40).
- **Using reclamation where you could avoid it.** Reaching for RCU/HP when a fixed ring, an index-based pool, a seqlock copy, or double-buffering would do — paying SMR overhead and risking SMR bugs for no reason (§36.4 escape). On the hot path, prefer the no-reclamation design.
- **Type-instability assumptions.** Some "lock-free without reclamation" tricks rely on **type-stable** memory (pooled objects never returned to the OS, so a stale pointer reads a valid old object). This requires the pool to *never* free to the allocator and the stale-read to be benign — verify those assumptions hold (Ch. 24) before relying on it.

## 36.6 Exercises & checklist

**Exercises**

1. **Read-side cost.** Build `reclaim.cpp` (or use `liburcu`/`folly::hazptr`); measure ns/read for `shared_mutex` vs RCU-style vs hazard pointers at R=1,2,4,8,16 readers. Confirm RCU ~free and scaling, hazard pointers cheap-and-scaling, `shared_mutex` collapsing (§36.3).
2. **Grace-period reclamation.** With `liburcu`, build a read-mostly map; update it (publish new version), and measure the writer's `synchronize_rcu` cost and the memory of retired-but-unfreed versions over time. How long is a grace period under load?
3. **Stalled-reader / unbounded deferral.** In an EBR (or RCU) setup, make one reader *stall* inside a critical section (sleep). Watch retired memory grow without bound (§36.5). Then do the same with hazard pointers — confirm memory stays bounded (only the stalled reader's held nodes are protected). Why is HP robust here?
4. **Avoid it.** Replace a lock-free linked structure that needs reclamation with an **index-based** pool design (Ch. 24) or a fixed ring (Ch. 34). Show the UAF/ABA problem disappears entirely. Compare complexity and overhead.
5. **UAF demo.** Build a lock-free stack that frees immediately on pop; under concurrent readers, reproduce a use-after-free (ASan — Ch. 40). Fix it with hazard pointers (or by not freeing — type-stable pool). Confirm ASan is clean.

**Checklist — safe memory reclamation**

- [ ] I first asked whether reclamation can be **avoided**: fixed rings (Ch. 34), **index-based** pools (Ch. 24), seqlock copies (Ch. 35), double-buffering, type-stable memory — the no-SMR design preferred on the hot path (§36.4 escape).
- [ ] Where a dynamic shared structure is genuinely needed, removed nodes are **never freed immediately** — reclamation is **deferred** via RCU / hazard pointers / EBR (no use-after-free — §36.2, §36.5).
- [ ] The scheme **matches the workload**: **RCU/pointer-swap** for read-mostly + rare writes (near-free reads); **hazard pointers** when bounded memory / robustness to stalled readers is needed; **EBR** for cheap reads with short, non-stalling readers.
- [ ] Read-side critical sections are **short, non-blocking, fault-free** (pre-faulted — Ch. 26) — no blocking/syscall/stall inside them (avoids unbounded deferral — §36.5).
- [ ] Memory is **bounded** (hazard pointers by construction; for RCU/EBR I monitored retired-node growth and grace-period/epoch progress) — no unbounded deferral.
- [ ] I used **vetted implementations** (`liburcu`, `folly::hazptr`/`std::hazard_pointer`) — not hand-rolled SMR — and validated with **ASan/TSan/model-checking** (Ch. 40).
- [ ] **ABA** is handled together with reclamation (tagged pointers/indices — Ch. 34); the read-mostly *update = publish-new-version-reclaim-old* is recognized as the **hot-reload** pattern (Ch. 73).
- [ ] The writer-side cost (grace-period wait / hazard scan) is kept **off the hot path** (rare writes; `call_rcu`-style deferral).

## 36.7 References

- P. McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* and the RCU papers — the definitive treatment of RCU, grace periods, and reclamation (the foundation of §36.2, §36.4.1).
- M. Michael, *"Hazard Pointers: Safe Memory Reclamation for Lock-Free Objects"* (IEEE TPDS 2004) — the original hazard-pointer scheme (§36.4.2).
- K. Fraser, *"Practical Lock-Freedom"* (PhD thesis) — epoch-based reclamation (§36.4.3).
- The `liburcu` (userspace RCU) and `folly::hazptr` / C++26 `std::hazard_pointer` documentation — production implementations to use rather than hand-roll (§36.4, §36.5).
- M. Herlihy & N. Shavit, *The Art of Multiprocessor Programming* — reclamation, ABA, and the memory-management problem in lock-free structures.

## 36.8 Additional Reading

- The Crossbeam (Rust) epoch-based reclamation documentation — a clear, modern EBR design and its stalled-reader caveats.
- D. Bakhvalov / lock-free reclamation surveys and benchmarks comparing RCU/HP/EBR read-side and reclamation costs.
- Ch. 34 (*Lock-Free*) — where reclamation is deferred from; Ch. 35 (*Seqlocks*) — the small-state single-writer publication (RCU is the large-state version); Ch. 24 (*Allocators*) — index-based pools that avoid reclamation; Ch. 37 (*Disruptor*) — a ring that needs no reclamation; Ch. 73 (*Hot Reload*) — publish-reclaim as configuration update; Ch. 40 (*Tooling*) — ASan/TSan/model-checking for SMR bugs.
- **Appendix E** — read-side/reclamation latency numbers; **Appendix F** — RCU/hazard-pointer/grace-period/ABA glossary.

---

*Next: Ch. 37 — The Disruptor Pattern, the capstone of Part VI: the LMAX ring-buffer + sequence-barrier architecture that combines the SPSC ring (Ch. 34), single-writer publication (Ch. 35), no-reclamation design (this chapter), batching, and mechanical sympathy into a complete, proven low-latency pipeline.*
