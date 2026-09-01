# Part VII — OS, Scheduling & Isolation

# Chapter 46 — Keeping the Hot Path Warm

> **Prerequisites:** Ch. 7/12/15 (caches / I-cache / TLB — the state that goes cold), Ch. 13–14 (branch predictors — also cold), Ch. 41 (context switching — the cold-restart cost this chapter generalizes), Ch. 23/26 (pre-faulting / `mlock`), Ch. 1 (warm vs cold *tail*), Ch. 42, 43, 45 (a quiet core that *can* stay warm).
>
> **Leads into:** Ch. 55, 58, 61, 62 (NIC/bypass — pre-establishing connections, warming the RX path), Ch. 73–74 (hot reload / fast restart — warm-up after a reload/restart), Ch. 76 (the end-to-end case study). Closes **Part VII** — the last enemy after the core is quiet.

---

## 46.1 Why it matters: the first real message must not be cold

You've built the perfect quiet core: pinned (Ch. 42), isolated and tickless (Ch. 45), sibling handled (Ch. 43), no migrations, no interrupts, no syscalls (Ch. 41). And then the cruel irony of low-latency trading reveals itself: **the hot path runs *rarely*.** Between market events — in the gaps between the messages that matter — the hot code *isn't executing*, and during those gaps its state **decays**. Other (housekeeping, even on other cores via shared LLC) code evicts its data from the caches (Ch. 7), its instructions from the L1i (Ch. 12), its translations from the TLB (Ch. 15); the branch predictors forget its patterns (Ch. 13–14); the TCP connection's congestion window shrinks; the NIC's RX path goes idle. So when *the one message that matters* finally arrives — the quote that triggers your trade, the burst at the open — it hits a **cold** hot path: the first execution after a quiet spell is a cascade of cache misses, TLB misses, and mispredicts, running *thousands of cycles slower* than the warm steady-state you so carefully measured. **The message you most need to be fast is, by its nature, the one most likely to be cold.**

This is the final enemy of Part VII, and it's a *tail*-latency problem of the purest kind (Ch. 1). Your benchmark — running the hot loop millions of times in a tight loop — measures the *warm* path, because the loop keeps everything resident. But production isn't a tight loop; it's long idle gaps punctuated by rare, critical events. So the warm number you optimized is the *p50* of a quiet market; the cold number you *didn't* measure is the *p99.9* that hits exactly when volatility spikes and your trade matters most. A hot path that's 200 ns warm and 4 µs cold doesn't have a 200 ns latency — it has a 200 ns *median* and a 4 µs *tail* that activates precisely under the market conditions you built the system for. **Warming is the technique that makes the cold case never happen — that keeps every microarchitectural and OS resource the hot path needs resident and ready, so the first real message runs as fast as the millionth.**

The methods are a coherent set, all variations on "exercise the hot path during the idle gaps so it never goes cold": **shadow/dummy traffic** (continuously run synthetic messages through the *real* hot-path code — not a separate warming routine, the actual path — so caches, predictors, and TLB stay primed with the real working set), **pre-touching** (fault and `mlock` all pages — Ch. 23, 26 — so the first access never page-faults), **pre-establishing connections** (open and keep TCP/sessions warm so the first send doesn't pay connection setup / slow-start — Ch. 51), and **periodic re-warming** tuned to the decay rate. This chapter explains the state-decay mental model (§46.2), measures the warm-vs-cold gap that benchmarks hide (§46.3), details shadow traffic and pre-touching (§46.4), and warns about the subtle traps — warming the *wrong* state (a warming path that diverges from the real one), or warming so aggressively you cause harm (§46.5). It's the capstone of the OS/isolation arc: a quiet core (Ch. 41, 42, 43, 45) that stays *warm* is the complete low-latency execution environment.

## 46.2 Mental model: cache/TLB/branch-predictor state decay

**Every microarchitectural resource the hot path depends on is *stateful* and *decays* when the path is idle.** The warm steady-state you measured depends on all of this being *resident*; idleness lets other code evict it:

```
   resource                  warm (hot path running)        cold (after idle gap)         re-warm cost
   L1/L2/L3 data (Ch.6)      working set resident           evicted by other code         hundreds of misses
   L1i / µop cache (Ch.11)   hot code resident              evicted                       front-end stalls
   TLB (Ch.14)               translations cached            flushed/evicted               page-walk storm
   branch predictors (Ch.12) patterns learned               forgotten/polluted            mispredicts
   prefetcher state          stream trained                 reset                         no prefetch first pass
   TCP cwnd / slow-start     window grown                   shrunk after idle (Ch.47)     slow first send
   NIC RX / driver state     primed                         idle/cold                     first-packet latency
```

The crucial points:

- **Decay happens during idle, not just during a context switch.** Even on a perfectly isolated core (Ch. 45) where *your* thread is never switched out, the hot *path* still goes cold if the thread is *busy-polling but not finding work* (spinning in the wait loop, not executing the processing code) — the *processing* code and its data aren't being touched, so they decay even though the thread is "running." And shared resources (LLC, memory) are affected by *other* cores' activity. Isolation keeps the *thread* on-core; it doesn't keep the *hot code* warm if the code isn't running.
- **The decay rate varies by resource.** L1 is small and decays fast (other code evicts it in microseconds); the LLC is large and holds longer; the branch predictors and TLB are in between; the TCP window decays on a timescale of RTTs/idle-timeouts (Ch. 51). Warming must refresh each on its own timescale.
- **The cold penalty is a *cascade*.** A cold first message misses in L1 *and* L2 *and* the TLB *and* mispredicts — the penalties stack (Ch. 41's cold-restart, generalized). A path that's 200 ns warm can be 5-20× slower cold.
- **Warming = keep it resident by exercising it.** The only way to keep stateful microarchitectural resources warm is to *use* them — run code that touches the same cache lines, executes the same instructions, takes the same branches, translates the same pages. The most faithful warming *is the real hot-path code itself*, run on synthetic input (§46.4.1) — anything else risks warming the *wrong* state (§46.5).

The model: **the hot path's speed depends on stateful resources (caches, TLB, predictors, prefetcher, TCP window, NIC) that *decay during idle gaps* — so the rare, critical first message hits a cold path that's 5-20× slower (the tail benchmarks hide). Warming keeps these resident by continuously exercising the *real* hot-path code (shadow traffic) and pre-faulting/pre-connecting, so the first real message is as warm as the millionth.**

## 46.3 Measure it: warm vs cold tail latency

The measurement benchmarks *don't* do by default: the **cold** latency. Process a message after an *idle gap* (so the path went cold) vs in a tight loop (warm), and show the gap — and that shadow traffic during the gap closes it.

```cpp
// warm.cpp — warm vs cold hot-path latency; show idle gaps cause cold spikes.
// Build: g++ -O2 -std=c++20 -march=native warm.cpp -o warm
// Run pinned, isolated core:  taskset -c 2 ./warm
#include <cstdio>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <chrono>
#include <thread>
#include <x86intrin.h>

constexpr int WS = 1 << 16;                       // hot-path working set (data) ~512KB - exceeds L1/L2
std::vector<std::uint64_t> book(WS, 1);           // stand-in for the order book / hot data
std::vector<std::uint64_t> code_touch(WS, 1);

// the REAL hot path: touch the working set as a real message would (loads, branches, a store).
std::uint64_t process(std::uint64_t msg) {
    std::uint64_t s = 0;
    for (int i = 0; i < 256; ++i) {               // touch scattered hot data (cache/TLB working set)
        std::size_t idx = (msg * 2654435761u + i * 4096) & (WS - 1);
        s += book[idx]; if (s & 1) book[idx] = s;  // load + data-dependent branch + store
    }
    return s;
}

std::uint32_t timed(std::uint64_t msg) {
    std::uint64_t t0 = __rdtsc(); volatile std::uint64_t r = process(msg); std::uint64_t t1 = __rdtsc();
    (void)r; return std::uint32_t(t1 - t0);
}

int main() {
    // (1) WARM: tight loop — everything stays resident
    std::vector<std::uint32_t> warm; warm.reserve(100000);
    for (long i = 0; i < 100000; ++i) warm.push_back(timed(i));
    std::sort(warm.begin(), warm.end());

    // (2) COLD: idle gap before each measured message (evict the working set by sleeping + thrashing)
    std::vector<std::uint32_t> cold; cold.reserve(2000);
    std::vector<std::uint64_t> evict(1 << 22, 1);   // 32MB to thrash the LLC during the "gap"
    for (long i = 0; i < 2000; ++i) {
        for (auto& x : evict) x += i;                // evict the hot working set (simulate idle activity)
        cold.push_back(timed(i));                    // first message after the gap = COLD
    }
    std::sort(cold.begin(), cold.end());

    auto pct=[&](std::vector<std::uint32_t>&v,double p){return v[std::size_t(p*(v.size()-1))];};
    std::printf("WARM (tight loop):  p50=%u  p99=%u cycles\n", pct(warm,0.5), pct(warm,0.99));
    std::printf("COLD (after gap):   p50=%u  p99=%u cycles   <- the tail benchmarks HIDE\n",
                pct(cold,0.5), pct(cold,0.99));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), isolated core, turbo off (illustrative; convert cycles→ns at ~2.9 GHz):

```
   WARM (tight loop):   p50 ~3,000 cycles   (~1.0 µs)   everything resident — the number you benchmarked
   COLD (after gap):    p50 ~18,000 cycles  (~6.2 µs)   ~6x SLOWER — cache/TLB/predictor cold (§43.2)

   with SHADOW TRAFFIC during the gap (run process() on dummy msgs):
   "cold" path:         p50 ~3,200 cycles   (~1.1 µs)   ~warm again — shadow traffic kept it resident
```

Read it: the **warm** path (tight loop — what your benchmark measures) is ~1 µs; the **cold** path (first message after an idle gap that evicted the working set) is **~6× slower** — and *that* is the latency the real, rare, critical message sees, because production has idle gaps, not tight loops. This gap is **invisible in a normal benchmark** (which keeps everything warm) and **invisible in the average** (most messages in a busy period are warm) — it lives in the *tail* and activates exactly when the market goes quiet-then-active (the worst time). The third row is the cure: running **shadow traffic** (the *real* `process()` on dummy messages) during the idle gap keeps the working set, predictors, and TLB resident, so the "cold" path is ~warm again. The lesson that closes Part VII: **measure the cold path, not just the warm one** — and **keep it warm with shadow traffic + pre-faulting (§46.4)** so the first real message is never the cold one. (To measure cold latency honestly in production: tag the first message after each idle gap and watch *its* latency distribution separately — Ch. 76.)

## 46.4 Techniques

### 46.4.1 Shadow/dummy traffic

The primary warming technique: continuously run synthetic messages through the **real hot-path code** during idle gaps, so its microarchitectural state never decays:

- **Run the *actual* hot path, not a separate warming routine.** The warming work must execute the *same code*, touch the *same data structures* (the order book — Ch. 25, the same lookup tables), take the *same branches*, and translate the *same pages* as a real message — because that's the state you need warm. A *separate* "warm-up function" that touches *different* memory warms the *wrong* state (§46.5). The faithful approach: feed synthetic/dummy messages into the real processing function, stopping just short of the side effects (don't actually send an order — §46.4.1 below).
- **Suppress the side effects.** Shadow traffic must run the hot path *without acting on it*: process the dummy message through decode → book-update → strategy → *order-decision*, but **don't transmit** the order (a flag/branch at the very end that drops the synthetic order, or a "dry-run" mode). The trick is to make that suppression branch *itself* predictable and warm (so it doesn't perturb the real path), and to ensure the dummy messages exercise the *common* paths (not just one). Some designs warm right up to the NIC and drop at the last gate; others warm the compute and pre-warm the send path separately.
- **Rate and timing.** Run shadow traffic frequently enough to beat the decay rate of the fastest-decaying resource you care about (L1 decays in microseconds — so warming must be near-continuous for L1-resident state; LLC/TLB hold longer). On a busy-polling hot thread (Ch. 41), interleave shadow messages into the wait loop (when no real message is available, process a dummy one) — so the thread is *always* exercising the hot path, real or shadow. This is the cleanest model: **the busy-poll loop never idles the hot code — it either processes a real message or a shadow one.**
- **Realistic shadow data.** The dummy messages should resemble real ones (plausible symbols, prices, sizes — so they hit the real book entries and take realistic branches), ideally drawn from captured data (Ch. 75) or a model of the real distribution. Warming with degenerate dummies (all the same symbol) warms a narrow slice and leaves the rest cold.
- **Warm the branch predictors and prefetcher too.** Shadow traffic that takes the *same branch patterns* keeps the predictors (Ch. 13–14) trained, and the *same access streams* keeps the prefetcher (Ch. 10) trained — both decay during idle and both are warmed by running the real path. This is another reason the warming must be the *real* code, not an approximation.

### 46.4.2 Pre-touching pages and connections

The setup-time warming that complements continuous shadow traffic — done at startup and maintained:

- **Pre-fault and `mlock` all pages (Ch. 23, 26).** Touch every page of the hot path's memory (order book, pools, ring buffers, code) at startup so the first access never **page-faults** (Ch. 23 — a major fault is milliseconds, a minor fault hundreds of ns), and `mlock`/`mlockall` so they stay resident (no swap/reclaim). This warms the *page tables and residency*; shadow traffic keeps the *cache* warm. Both are needed: pre-faulting stops the fault, shadow traffic stops the cache miss.
- **Pre-establish connections (Ch. 51).** Open the TCP/session connections to venues *before* trading starts (and keep them alive), so the first real order doesn't pay connection setup (handshake — multiple RTTs) or run into TCP **slow-start** with a cold congestion window. Send periodic keep-alive / heartbeat traffic (Ch. 51, FIX heartbeats — Ch. 53) to keep the window grown and the path warm — connection-level shadow traffic. (Some venues' connections idle-timeout or reset `cwnd`; heartbeats prevent that.)
- **Warm the NIC RX/TX path (Ch. 55, 62).** The first packet after idle can be slower (driver/NIC state cold, interrupt coalescing, busy-poll not primed). Continuous shadow RX/TX (or the venue's heartbeat/snapshot traffic) keeps the NIC path warm; kernel-bypass busy-polling (Ch. 62) keeps the RX ring actively polled.
- **Pre-touch / pre-compute lookup state.** Pre-populate caches of derived data (symbol→info maps — Ch. 25, tick tables — Ch. 27) and touch them so they're resident; pre-warm any lazily-initialized state so the first real message doesn't trigger initialization on the hot path.
- **Warm after every cold-start event (Ch. 73–74).** Warming isn't only at boot: after a **hot reload** (Ch. 73 — new config/code, cold), a **process restart** (Ch. 74 — crash-only fast restart lands cold), or a reconnect, the path is cold again — re-warm (shadow traffic + pre-touch) *before* declaring the system live and routing real traffic to it. Fast restart (Ch. 74) must include fast *re-warming*, or the restarted process is slow exactly when it just recovered.

## 46.5 Pitfalls & anti-patterns: cold-start stalls, warming the wrong state

- **Benchmarking only the warm path (the measurement blind spot).** A tight-loop benchmark keeps everything resident and reports the *warm* latency — hiding the cold tail that the rare, critical message actually sees (§46.3). **Measure the cold path** (first message after an idle gap), tag-and-track cold-start latency in production (Ch. 76), and treat the cold number as the real tail (Ch. 1).
- **Warming the *wrong* state (the subtle trap).** A warm-up routine that touches *different* code/data than the real hot path warms the wrong caches/predictors — the real path is still cold. Warming must run the **real hot-path code on real-shaped data** (§46.4.1); a divergent "warm-up function" gives false confidence. Verify the warmed state *is* the production state.
- **Shadow traffic with side effects (sending real orders!).** The catastrophic bug: shadow/dummy messages that aren't properly suppressed actually **transmit orders** — sending synthetic trades to the market. The side-effect suppression (drop the synthetic order at the final gate) must be *bulletproof* and tested; a warming bug that sends real orders is a financial/regulatory disaster. Isolate the dry-run path carefully.
- **Warming the cold path *on* the hot path (perturbing it).** If shadow traffic and real traffic interleave badly, the warming work can *delay* a real message (the thread is busy processing a dummy when the real one arrives). The busy-poll design must *prioritize real messages* — process a shadow message only when *no* real one is available, and make the real-message check cheap so warming yields instantly. Don't let warming add latency to the thing it's protecting.
- **Forgetting connection/TCP warming (Ch. 51).** Warming the *compute* but leaving the *network* cold (cold TCP window, un-established connection) means the first *send* is slow even if the decision was fast. Pre-establish and heartbeat the connections (§46.4.2).
- **Not re-warming after a cold-start event (Ch. 73–74).** Treating warming as boot-only, then a hot reload (Ch. 73) or restart (Ch. 74) lands the path cold and slow — exactly when (post-recovery) you can least afford it. Re-warm after *every* cold-start (reload, restart, reconnect) before going live.
- **Decay-rate mismatch (warming too infrequently).** Warming at a rate slower than the resource decays (e.g. warming once a second when L1 decays in microseconds) leaves the fast-decaying state cold between warm-ups. Match the warming rate to the fastest-decaying resource you need (near-continuous for L1 — §46.4.1).
- **Over-warming wasting resources / heat.** Excessive shadow traffic burns CPU/power and generates heat (which can trigger thermal throttling — Ch. 6) and, on shared LLC, can evict *other* useful state. Warm *enough* to beat decay, not maximally. Measure the cold tail and warm to close it, no more.
- **Assuming an isolated core stays warm by itself (Ch. 45).** Isolation keeps the *thread* on-core but doesn't keep the *hot code* warm if the thread is idle-spinning (not executing the processing code — §46.2). A quiet core still needs warming; the two are complementary, not substitutes.

## 46.6 Exercises & checklist

**Exercises**

1. **Measure the cold gap.** Build `warm.cpp`; compare WARM (tight loop) vs COLD (after an LLC-thrashing gap) p50/p99. Quantify the cold penalty (~×). Then enable shadow traffic during the gap (run `process()` on dummies) and confirm the "cold" path returns to ~warm (§46.3).
2. **Decay rate.** Vary the idle-gap length / eviction size from "small" to "thrash the whole LLC"; plot cold latency vs gap. Find where each resource (L1 → L2 → LLC → TLB) starts costing. How often must you warm to keep L1-resident state warm (§46.4.1)?
3. **Warm the wrong state.** Write a warm-up that touches a *different* array than `process()` uses; confirm it does *not* warm the real path (cold latency unchanged). Then warm with the real `process()`; confirm it works (§46.5) — the "warm the real code" lesson.
4. **Shadow without side effects.** Build a hot path that ends in a "send order" step; add shadow traffic with a dry-run gate that drops synthetic orders. Verify (a) no synthetic order is ever sent, and (b) the path stays warm. Stress the suppression (§46.5).
5. **Re-warm after reload.** Simulate a hot reload (Ch. 73 — swap the config/data, cold) and measure the first real message's latency before vs after a re-warm pass. Confirm re-warming closes the post-reload cold gap (§46.4.2).

**Checklist — keeping the hot path warm**

- [ ] I **measure the COLD path** (first message after an idle gap), not just the warm tight-loop number — and track **cold-start latency** separately in production (Ch. 76). The cold number is the real tail (Ch. 1).
- [ ] **Shadow/dummy traffic** runs the **real hot-path code on real-shaped data** during idle gaps (interleaved into the busy-poll loop — process a shadow message when no real one is available) — keeping caches, predictors, TLB, and prefetcher warm (§46.4.1).
- [ ] Shadow traffic's **side effects are bulletproof-suppressed** (no synthetic order ever transmitted) — tested and isolated (§46.5).
- [ ] Warming **prioritizes real messages** — it never delays a real message (cheap real-check, instant yield) (§46.5).
- [ ] All hot-path pages are **pre-faulted + `mlock`ed** (Ch. 23, 26) — no first-touch fault — and lazily-initialized state is pre-warmed.
- [ ] **Connections are pre-established and heartbeated** (warm TCP window, no slow-start/handshake on the first order — Ch. 51), and the **NIC RX/TX path** is kept warm (Ch. 55, 62).
- [ ] The **warming rate matches the decay rate** of the fastest resource I need warm (near-continuous for L1) — not too infrequent, not wastefully over-warming (heat/LLC eviction).
- [ ] I **re-warm after every cold-start event** (hot reload — Ch. 73, restart — Ch. 74, reconnect) before routing real traffic — fast restart includes fast re-warm.

## 46.7 References

- The "Mechanical Sympathy" community and Carl Cook, *"When a Microsecond Is an Eternity"* (CppCon) — keeping the hot path warm, shadow traffic, and measuring the cold tail in HFT (the chapter's ethos).
- U. Drepper, *What Every Programmer Should Know About Memory* — cache state and the cost of cold access (the decay model of §46.2).
- Intel *Optimization Reference Manual* — branch-predictor and prefetcher training/decay behavior (§46.2).
- The TCP slow-start / congestion-window-after-idle literature (RFC 5681 and `tcp_slow_start_after_idle`) — connection warming (§46.4.2, ties Ch. 51).
- Ch. 76 (*Production Profiling & Case Study*) — measuring warm-vs-cold tail latency in production and the end-to-end walkthrough.

## 46.8 Additional Reading

- HFT engineering talks on warm-up, shadow orders, and cold-start mitigation — practical patterns for §46.4.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — cold vs warm measurement and cache/predictor state.
- Ch. 7/12/15 (*Caches / I-cache / TLB*) — the state that decays; Ch. 13–14 (*Branch Prediction / Dispatch*) — predictor warming; Ch. 23/26 (*Memory / mmap*) — pre-faulting/`mlock`; Ch. 41, 42, 43, 45 (*Context Switching → RT Tuning*) — the quiet core warming completes; Ch. 51 (*Socket/TCP*) — connection/slow-start warming; Ch. 73–74 (*Hot Reload / Process Topology*) — re-warm after reload/restart; Ch. 75 (*Capture*) — realistic shadow data.
- **Appendix C** (System Tuning Checklist) — warming/pre-faulting in the box-setup recipe; **Appendix E** — the cold-vs-warm latency numbers.

---

*Next: Ch. 47 — Linux Native I/O, opening Part VIII (Kernel I/O, Sockets & Zero-Copy). Having built a quiet, warm, isolated execution environment (Part VII), we turn to getting data in and out of it: blocking vs non-blocking I/O, `epoll`, readiness vs completion, and syscall batching — the baseline kernel I/O path before io_uring and kernel bypass.*
