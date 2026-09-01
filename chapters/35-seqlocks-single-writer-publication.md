# Part VI — Concurrency

# Chapter 35 — Seqlocks & Single-Writer Publication

> **Prerequisites:** Ch. 30 (atomics & memory ordering — the seqlock's correctness rests entirely on acquire/release and fences), Ch. 31 (single-writer discipline — the seqlock *is* the single-writer/many-reader primitive), Ch. 33 (false sharing — the sequence counter and data layout), Ch. 34 (lock-free — the SPSC ring is for *streams*, the seqlock for *snapshots*), Ch. 7 (coherence — readers don't write, so they don't bounce the line).
>
> **Leads into:** Ch. 36 (safe reclamation — the alternative for *large/pointer* state), Ch. 37 (Disruptor — publishes via sequences), Ch. 73 (hot reload — config published via seqlock/pointer swap), Ch. 26/50 (shared-memory publication across processes). The single-writer pattern reappears in Ch. 74.

---

## 35.1 Why it matters: publishing market data to many readers

A huge fraction of trading-system communication isn't a *stream* of messages (the SPSC ring of Ch. 34) — it's a *snapshot* that's updated frequently by one writer and read frequently by many readers: the **current top-of-book (BBO)** updated by the feed handler and read by every strategy; the latest **price/quote** for a symbol; a **reference-data** record; a **risk-limit** or **config** value; the current **position**. The access pattern is asymmetric and specific: **one writer, many readers, reads vastly outnumber writes, and readers must get a *consistent* (non-torn) view of the latest value with the lowest possible latency.** A mutex is wrong here — it serializes all the readers and lets a reader block the writer (and vice versa), spiking the tail (Ch. 32). An SPSC ring is wrong — it's for consuming a *stream* once, not for many readers each grabbing the *current* state. The right primitive is the **seqlock** (sequence lock).

The seqlock's defining property is that **readers never write** — they don't acquire anything, don't take a lock, don't modify any shared state, so they generate **no coherence traffic** (Ch. 7) and **never contend with each other or block the writer**. A reader just reads a sequence counter, reads the data, reads the counter again, and retries if the writer was mid-update; in the common case (no concurrent write) it's two atomic loads around a plain read — a handful of nanoseconds, perfectly scalable to any number of readers (each reads its own cached copy of the line until the writer changes it). This is exactly what publishing market data to many strategies needs: the writer updates the BBO at line rate, and a hundred reader threads each see the latest consistent value at ~nanosecond cost with no contention. It's the single-writer/many-reader (Ch. 31) discipline made into a concrete, lock-free, allocation-free primitive.

The catch — and the reason this chapter exists separately from Ch. 34 — is that seqlocks are **subtle under the C++ memory model**, and a naive implementation has a genuine data race (Ch. 30). The reader reads the data *speculatively* (it might be reading while the writer is writing — a **torn read**), detects the tear via the sequence counter, and retries — but "reading data that's concurrently being written" is, formally, a data race (UB), and making it *correct* requires careful use of fences/atomics and acceptance that the speculatively-read (possibly-torn) value is *discarded*. Getting the ordering right (the sequence-counter protocol, the fences, the torn-read detection) is the whole technical content (§35.4), and getting it *wrong* produces a reader that occasionally returns a torn/stale value — a silent, rare, catastrophic bug in something as critical as a price. So this chapter builds the seqlock mental model (§35.2), measures its beautiful read scalability (§35.3), details the sequence-publication and torn-read-avoidance protocol with correct ordering (§35.4), and is unusually heavy on the memory-model correctness pitfalls (§35.5) — because a seqlock is easy to write and easy to write *wrong*.

## 35.2 Mental model: versioned/sequence-lock snapshots; single-writer/multi-reader

**The seqlock protocol.** A seqlock protects a piece of data with a **sequence counter** (an atomic integer) that the writer bumps before and after each update. The convention: **odd = a write is in progress; even = stable.**

```
   WRITER (single):                          READER (many):
     seq++  (even→odd: "writing")              do {
     ... write the data ...                       s1 = seq           (must be even, else writer active)
     seq++  (odd→even: "done")                     ... read the data into a local copy ...
                                                    s2 = seq
                                                } while (s1 is odd  OR  s1 != s2);  // retry if torn
                                                use the local copy   // guaranteed consistent
```

The reader **snapshots** the sequence, reads the data, snapshots the sequence again, and accepts the read **only if** (a) the first snapshot was even (no write in progress when it started) **and** (b) both snapshots are equal (no write happened *during* the read). If either fails, the data it read might be **torn** (a mix of old and new — the writer was updating mid-read), so it **retries**. In the common case (no concurrent write) the two snapshots match on the first try and the read is two atomic loads bracketing a plain copy.

**Why it's so good for read-heavy snapshots:**

- **Readers never write → zero coherence traffic, infinite read scalability.** A reader only *loads* the sequence and the data; it modifies nothing. So the cache line stays in *shared* state across all readers (Ch. 7) — no ping-pong, no contention, each reader reads its local cached copy until the writer changes it. A thousand readers cost the same per-read as one. This is the killer property (§35.3) and the opposite of a mutex (where every reader writes the lock).
- **The writer is never blocked by readers.** The writer just bumps the sequence and writes — it never waits for a reader. (It assumes single-writer; two writers would need a lock between them — §35.4.) So the writer publishes at full speed regardless of reader load.
- **Optimistic, retry-based.** Readers are *optimistic*: they read speculatively and validate. Under heavy writing a reader may retry a few times, but writes are typically far rarer than reads (a BBO updates per tick; strategies read it constantly), so retries are rare. (If writes were as frequent as reads, a different structure might fit better.)

**When to use a seqlock vs the alternatives:**

- **Seqlock — small, frequently-updated, value-type snapshots** read by many: a BBO struct, a price, a small config record, a position. The data must be **copyable** (the reader copies it out) and small enough that copying it is cheap (a torn read just re-copies).
- **vs Mutex/RWLock** — readers don't block or write (no contention); far better read scalability and lower latency for the read-heavy snapshot case.
- **vs SPSC ring (Ch. 34)** — the ring is for a *stream* consumed once; the seqlock is for the *current value* read repeatedly. Different needs.
- **vs RCU / atomic pointer-swap (Ch. 36)** — for **large** state or **pointer-linked** structures (a whole order book, a big table) where copying-per-read is too expensive, publish a *new immutable version* via an atomic pointer swap and reclaim the old version safely (Ch. 36). Seqlock copies the data each read (good for small data); pointer-swap/RCU shares it by reference (good for large data) at the cost of reclamation. The crossover is roughly "is copying the snapshot cheaper than the reclamation machinery?"

The model: **a seqlock publishes a small snapshot from one writer to many readers via an odd/even sequence counter; readers optimistically read-and-validate (retry on tear), never writing — so reads are contention-free and scale to any reader count. It's the single-writer/many-reader primitive for small frequently-updated state; use pointer-swap/RCU (Ch. 36) for large state.**

## 35.3 Measure it: seqlock read latency under writer activity

The seqlock's signature is **read latency that stays flat as readers scale**, because readers don't contend. Measure read latency/throughput vs reader count, compared to a `std::shared_mutex` (reader-writer lock) doing the same job, with a writer continuously updating.

```cpp
// seqlock.cpp — seqlock vs shared_mutex for a small snapshot; many readers, one writer.
// Build: g++ -O2 -std=c++20 -march=native seqlock.cpp -o seqlock -pthread
// Run:  ./seqlock seq 8 | ./seqlock rwlock 8   (1 writer + N readers, pinned)
#include <atomic>
#include <shared_mutex>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>

struct BBO { std::int64_t bid, ask; std::int32_t bidsz, asksz; };   // small snapshot

class SeqLock {
    std::atomic<std::uint64_t> seq_{0};
    BBO data_{};
public:
    void write(const BBO& v) {                       // single writer
        seq_.store(seq_.load(std::memory_order_relaxed) + 1, std::memory_order_relaxed); // ->odd
        std::atomic_thread_fence(std::memory_order_release);
        data_ = v;                                   // write the data
        std::atomic_thread_fence(std::memory_order_release);
        seq_.store(seq_.load(std::memory_order_relaxed) + 1, std::memory_order_relaxed); // ->even
    }
    BBO read() const {                               // many readers
        BBO v; std::uint64_t s1, s2;
        do {
            s1 = seq_.load(std::memory_order_acquire);
            std::atomic_thread_fence(std::memory_order_acquire);
            v = data_;                               // speculative copy (may tear)
            std::atomic_thread_fence(std::memory_order_acquire);
            s2 = seq_.load(std::memory_order_acquire);
        } while ((s1 & 1) || s1 != s2);              // retry if writer active or changed
        return v;
    }
};

SeqLock seq; BBO rw_data{}; std::shared_mutex rw;
std::atomic<bool> stop{false};

int main(int argc, char** argv) {
    bool use_seq = (argc < 2) || std::strcmp(argv[1], "rwlock") != 0;
    int R = argc > 2 ? std::atoi(argv[2]) : 8;
    std::thread writer([&]{ BBO v{100,101,5,5}; while(!stop.load()) {           // continuous writer
        if (use_seq) seq.write(v); else { std::unique_lock l(rw); rw_data = v; } ++v.bid; } });

    std::vector<std::thread> readers; std::vector<long> counts(R);
    auto t0 = std::chrono::steady_clock::now();
    for (int i=0;i<R;++i) readers.emplace_back([&,i]{ long n=0; BBO v;
        while (!stop.load()) { if (use_seq) v=seq.read(); else { std::shared_lock l(rw); v=rw_data; }
                               n++; } counts[i]=n; });
    std::this_thread::sleep_for(std::chrono::seconds(2));
    stop.store(true); writer.join(); for(auto&t:readers) t.join();
    auto t1 = std::chrono::steady_clock::now();
    double s = std::chrono::duration_cast<std::chrono::duration<double>>(t1-t0).count();
    long total=0; for(long c:counts) total+=c;
    std::printf("%-7s R=%d  %.1f Mreads/s total  (%.1f ns/read)\n",
                use_seq?"seqlock":"rwlock", R, total/1e6/s, s*1e9/total);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), 1 writer + R readers pinned, turbo off (illustrative):

```
   R readers (1 continuous writer):
                       seqlock                shared_mutex (rwlock)
   R=1                 ~12 ns/read            ~25 ns/read
   R=4                 ~12 ns/read            ~90 ns/read     <- rwlock readers contend (they WRITE the lock)
   R=8                 ~13 ns/read            ~250 ns/read    <- rwlock collapses; seqlock FLAT
   R=16                ~14 ns/read            ~500 ns/read    <- seqlock ~unchanged; reads scale perfectly
   retry rate (seq)    < 1% (writes ≪ reads)
```

Read it: the **seqlock read latency is essentially flat** as readers scale from 1 to 16 — ~12-14 ns regardless — because readers don't write anything, so there's no contention and each reader reads its own cached copy of the line (Ch. 7); adding readers adds *zero* coherence cost. The **`shared_mutex`** (reader-writer lock) *collapses* — even "shared" read-lock acquisition **writes** the lock's internal state (a counter/flag), so readers contend on that line and ping-pong it, and latency grows ~linearly with reader count. This is the entire value proposition: **for a read-heavy snapshot, the seqlock gives flat, contention-free read latency at any reader count, while a reader-writer lock — which *looks* read-optimized — actually serializes readers on the lock's own cache line.** The retry rate is negligible because writes are far rarer than reads (the common case). For publishing a BBO/price/config to many strategies, the seqlock is the right primitive — and the measurement shows why a `shared_mutex` is not.

## 35.4 Techniques

### 35.4.1 Sequence-number publication

The writer side — publish an update via the odd/even sequence protocol (§35.2):

- **Bump-write-bump.** Increment the sequence to **odd** (signal "write in progress"), write the data, increment to **even** (signal "done"). Both increments are by the single writer; readers detect the odd state and the change. The data between the two bumps is what readers validate against.
- **Memory ordering is the crux (Ch. 30).** The first bump (→odd) and the data writes must be ordered so a reader that sees the data *cannot* see it before the odd marker, and the data writes must complete *before* the second bump (→even) is visible. Use a **release fence** (or release stores) after the first bump and before the second bump so the data writes are bracketed: a reader doing acquire loads of the sequence will correctly order the data reads. (The exact fence placement is the fiddly part — §35.5; many implementations use `atomic_thread_fence(release)` around the data writes, as in §35.3.)
- **Single writer only.** The seqlock protocol assumes **one writer**. Two concurrent writers would both bump the sequence and interleave data writes — corruption. If you have multiple writers, serialize them with a lock *between writers* (the readers still go lock-free) or, better, restructure to single-writer (Ch. 31) — the whole point is that the *reader* side is lock-free, and single-writer is the natural trading pattern (the feed handler owns the BBO).
- **Small, trivially-copyable data.** The published snapshot should be a small POD the reader can copy in one go (a BBO, a price+size, a config struct). Large data makes the copy expensive and the torn-read window bigger; for large/pointer state, use pointer-swap/RCU (Ch. 36) instead.
- **Pad the sequence counter (Ch. 33).** The sequence + data on the writer's cache line(s); ensure they don't false-share with *unrelated* hot data, but the sequence and its data are read together (constructive interference — Ch. 33). The writer's line will be pulled by readers on each update (unavoidable — that's the publication), but readers don't write it back contended.

### 35.4.2 Torn-read avoidance

The reader side — read optimistically and validate, never accepting a torn value:

- **Snapshot-read-snapshot-validate.** Read the sequence (`s1`), read the data into a **local copy**, read the sequence again (`s2`); accept only if `s1` is even **and** `s1 == s2` (no write started during the read). Otherwise **retry**. The local copy is essential — you never *use* data in place; you copy it out and validate the copy.
- **Acquire ordering on the sequence reads, fences around the data read (Ch. 30).** The first sequence load is `acquire` (so the data reads can't hoist above it); a fence after the data read before the second sequence load ensures the data reads can't sink below the validation. This guarantees: *if validation passes, the data copy corresponds to the sequence value observed* — i.e. a consistent snapshot.
- **The torn read is real but discarded.** The reader *may* copy data while the writer is mid-update — the copy is then **torn** (mixed old/new). That's fine: validation fails (the sequence changed or was odd), the copy is **thrown away**, and the reader retries. The torn value is never *used*. (The formal subtlety — reading concurrently-written data is technically a race — is handled by reading the data through atomics / relaxed atomic loads or accepting the well-defined-by-fences pattern; §35.5.)
- **Bounded retries / liveness.** Under a continuous high-rate writer a reader could retry many times; in practice writes ≪ reads so retries are rare (§35.3). If writes are frequent, bound the retries or reconsider the structure (a double-buffer/pointer-swap may fit better — Ch. 36). For a hard latency bound, note the (rare) retry adds to the tail — measure it (Ch. 1).
- **Double-buffering as an alternative.** A related single-writer/many-reader pattern: keep two buffers, the writer fills the inactive one and atomically swaps an index/pointer to publish (readers read the active one). No retry (readers always see a complete buffer), at the cost of two buffers and (for pointer-linked) reclamation (Ch. 36). Good when the snapshot is too big to copy-and-retry cheaply.

## 35.5 Pitfalls & anti-patterns: correctness under the memory model

- **Getting the memory ordering wrong (the cardinal seqlock danger).** A seqlock is *easy to write and easy to write subtly wrong*. Missing the release ordering on the writer (data writes leak past the sequence bump) or the acquire ordering on the reader (data reads hoist before the sequence read) produces a reader that occasionally returns a **torn or stale** value — a silent, rare, architecture-dependent bug (works on x86, fails on ARM — Ch. 30). Use a vetted implementation or get the fences reviewed/model-checked (Ch. 40); test on ARM.
- **The data-race formality.** Reading data the writer may be concurrently writing is, strictly, a data race (UB — Ch. 30). The standard-blessed way is to make the data accesses atomic (relaxed loads/stores per field, or `atomic_ref`) so the speculative read is well-defined, then validate. Plain non-atomic reads of concurrently-written data are technically UB even though the value is discarded — use the atomic/fence pattern, and know your implementation handles this.
- **Multiple writers.** The protocol assumes **one** writer; two writers corrupt the sequence/data (both bump, interleave). Serialize writers with a lock, or restructure to single-writer (Ch. 31). Don't use a bare seqlock with concurrent writers.
- **Large snapshots.** A seqlock copies the whole snapshot per read and re-copies on tear; for large data this is expensive and widens the torn-read window (more retries). Use **pointer-swap/RCU** (Ch. 36) for large or pointer-linked state — publish a new immutable version by reference, don't copy per read.
- **Using a `shared_mutex`/RWLock thinking it's read-optimized.** A reader-writer lock *writes* shared state on read-lock acquisition, so readers contend and it collapses under reader count (§35.3). For a read-heavy snapshot the seqlock is dramatically better. Don't reach for `shared_mutex` reflexively.
- **Unbounded retry under heavy writing.** If writes approach read frequency, readers retry a lot (latency tail — Ch. 1) and may even starve. Seqlocks assume reads ≫ writes; if not, use double-buffering/pointer-swap (no retry) or reconsider the design.
- **False sharing / layout (Ch. 33).** The sequence and data are pulled by every reader on each update (inherent); ensure they don't false-share with *other* unrelated hot data, and keep the snapshot compact (constructive interference for the reader's single copy).
- **Forgetting the retry can observe a *consistent but stale* value.** Validation guarantees *consistency* (not torn), not *freshness* — a reader gets *a* consistent snapshot, possibly one update old (the writer published a newer one just after). That's correct and usually fine (you want the latest *consistent* value), but don't assume "validated" means "absolutely newest." For sequencing/ordering needs, use a stream (Ch. 34).
- **Hand-rolling without testing on weak memory / model checking.** Like all lock-free code (Ch. 34), x86 hides ordering bugs. Validate on ARM (Appendix A) and with a model checker / TSan (Ch. 40) before trusting a hand-written seqlock with something as critical as a price.

## 35.6 Exercises & checklist

**Exercises**

1. **Read scalability.** Build `seqlock.cpp`; run `seq` vs `rwlock` with R=1,2,4,8,16 readers + 1 writer (pinned). Plot ns/read vs reader count. Confirm the seqlock is flat and the `shared_mutex` grows ~linearly. Why does the "shared" read lock contend (§35.3)?
2. **Retry rate vs write rate.** Add a counter for reader retries; vary the writer's update rate (add delay) from rare to continuous. At what write rate does the retry rate (and read latency tail) become significant? Relate to "reads ≫ writes" (§35.2).
3. **Break the ordering.** Remove the release/acquire fences (use `relaxed` everywhere). On x86 it likely still passes; cross-compile/run on ARM (Appendix A) or model-check (Ch. 40) and detect a torn read. Restore the fences (§35.5).
4. **Torn-read demo.** Make the snapshot large (a big struct) and the writer slow mid-update (write half, pause, write half); show readers detect the tear and retry (never returning a torn value). Then shrink the snapshot — retries drop.
5. **Seqlock vs pointer-swap.** Publish a *large* table both ways: a seqlock (copy-per-read) and an atomic pointer-swap to a new immutable version (Ch. 36). Compare read latency and the writer cost. Where's the crossover (snapshot size) where pointer-swap wins (§35.2, §35.5)?

**Checklist — seqlocks & single-writer publication**

- [ ] Small, frequently-updated, read-heavy **snapshots** (BBO, price, config, position) are published via a **seqlock** (single-writer, odd/even sequence) — **not** a mutex/`shared_mutex` (which serializes readers — §35.3).
- [ ] There is exactly **one writer** (single-writer discipline — Ch. 31); multiple writers are serialized or the design is restructured.
- [ ] The reader does **snapshot-read-snapshot-validate** into a **local copy**, retrying on odd/changed sequence — never using data in place, never returning a torn value (§35.4.2).
- [ ] **Memory ordering is correct** (release on the writer's data publication, acquire on the reader's sequence reads, fences around the data) — and **validated on a weakly-ordered machine / model checker** (Ch. 30, 40), not x86 alone.
- [ ] Concurrently-read data uses the **atomic/fence pattern** (not plain non-atomic reads of written data — the data-race formality, §35.5).
- [ ] The snapshot is **small and trivially-copyable**; **large/pointer state** uses **pointer-swap/RCU** (Ch. 36) instead.
- [ ] **Reads ≫ writes** (retry rate is low) — verified; if writes are frequent, I used double-buffering/pointer-swap or reconsidered the design.
- [ ] The sequence+data layout avoids false sharing with unrelated hot data (Ch. 33); I accept "validated = consistent, possibly one update stale" (not necessarily newest).

## 35.7 References

- C. Lameter & the Linux kernel `seqlock` implementation/documentation — the canonical seqlock and its memory-ordering discipline (the kernel uses seqlocks for `jiffies`/timekeeping — ties Ch. 17).
- H. Boehm, *"Can Seqlocks Get Along With Programming Language Memory Models?"* — the definitive analysis of seqlock correctness under the C/C++ memory model and the data-race subtlety (§35.5).
- ISO C++ / cppreference — `std::atomic`, `std::atomic_thread_fence`, `std::atomic_ref`, and `memory_order` (the primitives the seqlock is built on — Ch. 30).
- P. McKenney, *Is Parallel Programming Hard?* — sequence locks, publication, and the single-writer/many-reader pattern (and RCU as the large-state alternative — Ch. 36).
- The LMAX Disruptor paper — sequence-number publication for the streaming case (Ch. 37), a useful contrast to the snapshot case here.

## 35.8 Additional Reading

- J. Preshing's blog — memory ordering and publication patterns, helpful for getting the seqlock fences right.
- Talks/posts on "single-writer principle" (Martin Thompson / Mechanical Sympathy) — why single-writer is the foundation of fast publication (ties Ch. 31, 74).
- Ch. 30 (*Atomics*) — the ordering the seqlock depends on; Ch. 31 (*Foundations*) — single-writer discipline; Ch. 34 (*Lock-Free*) — the streaming (ring) alternative; Ch. 36 (*Safe Reclamation*) — pointer-swap/RCU for large state; Ch. 37 (*Disruptor*) — sequence publication; Ch. 73 (*Hot Reload*) — config/reference-data publication; Ch. 26/50 (*mmap/IPC*) — cross-process publication.
- **Appendix A** — ARM weak ordering (where naive seqlocks break); **Appendix F** — seqlock/single-writer/torn-read glossary.

---

*Next: Ch. 36 — Safe Memory Reclamation, the answer to the question seqlocks dodge by copying and lock-free queues dodge by not allocating: when you DO have a dynamic, pointer-linked shared structure, how do you free a node when lock-free readers might still be looking at it? — RCU, hazard pointers, and epoch-based reclamation.*
