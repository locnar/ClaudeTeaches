# Part I — Foundations & Methodology

# Chapter 1 — The Latency Mindset

> **Prerequisites:** Solid C++17/20 and Linux fundamentals. No special hardware or tooling yet — this chapter is about *how to think*, not what to install. A C++20 compiler (GCC 11+ or Clang 14+) is enough to run the one worked example.
>
> **Leads into:** Ch. 2 (*Measure First: Profiling & Hardware Performance Counters*) and Ch. 3 (*Micro-benchmarking Done Right*) turn the mindset established here into tools and rigor. Appendix E (*Latency Numbers Every Trading Developer Should Know*) is the quantitative companion to §1.2.3's budget.

---

## 1.1 Why it matters: when a microsecond is the trade

In most software, "fast enough" means the user does not notice the wait. A web request that completes in 50 ms or 80 ms is, to a human, the same request. The engineering culture that grows around that reality optimizes **average throughput**: requests per second, jobs per hour, the area under the curve.

Electronic trading inverts this. Consider the canonical event: an exchange publishes a quote that makes a resting order profitable to take. Every participant who sees that quote races to send the same order. The matching engine fills them **in arrival order**. There is exactly one winner per price level, and the runner-up — however close — gets nothing, or worse, gets filled on a now-stale price and loses money. The payoff function is not smooth. It is a step function at the moment your order crosses the wire.

That single fact reorganizes everything:

- **The metric is a deadline, not a rate.** You are not trying to process more messages per second on average; you are trying to *never* be late on the message that matters. The message that matters is unpredictable, so "never late" has to hold for *every* message.
- **The mean is irrelevant; the tail is the product.** If your tick-to-trade path is 1.2 µs on average but spikes to 40 µs once every ten thousand events, you will lose the races that happen during those spikes — and those races cluster exactly when the market is most active and most profitable. The 40 µs *is* your latency, for business purposes.
- **Determinism beats peak speed.** A path that is reliably 2.0 µs ± 0.1 µs is worth more than one that is 1.5 µs on a good day and 25 µs on a bad one. You can build a strategy around a number you can trust. You cannot build one around a number that occasionally betrays you.

Carl Cook's CppCon talk title puts it exactly: *"When a Microsecond Is an Eternity."* At a tick-to-trade budget measured in hundreds of nanoseconds to low single-digit microseconds, a single L3 cache miss (≈ 50–80 ns; Ch. 7), one minor page fault, one unlucky branch mispredict (Ch. 13), or one trip through the kernel's scheduler can be the difference between the fill and the miss. The rest of this book is a catalogue of where those nanoseconds hide. This chapter is about the discipline that makes the hunt worth doing: **always reason about the distribution, never the mean.**

A note on scope and honesty up front. Not every system in a trading firm lives on the tick-to-trade hot path. Risk calculations, backtesting, and research (Ch. 67–68) are throughput problems and should be engineered as such. The latency mindset is a tool you apply where the payoff is a deadline — applying it everywhere is its own kind of waste. Knowing *which* tier a piece of code lives in is the first design decision, and we return to it in §1.4.2.

---

## 1.2 Mental model

### 1.2.1 Latency vs throughput — two different objectives

**Latency** is the time between a cause and its effect: a quote arrives on the NIC, and some number of nanoseconds later your order leaves the NIC. **Throughput** is how many such operations complete per unit time. They are related but not the same, and optimizing for one routinely *degrades* the other.

The classic illustration is batching. Suppose you can amortize a fixed per-operation cost by processing messages in groups of 32. Throughput goes up — fewer fixed costs per message. But the first message in each batch now waits for 31 siblings before it is processed, and even the last message paid the cost of *assembling* the batch. Average service time per message dropped; **latency of any individual message rose.** A throughput-maximizing engineer reaches for batching reflexively. On the hot path, it is often exactly the wrong move.

A useful mental anchor is Little's Law from queueing theory:

```
L = λ × W
```

where `L` is the average number of items in the system, `λ` is the arrival rate, and `W` is the average time an item spends in the system (its latency). The law is a statement about *averages in a stable system*, and that is precisely its limitation for us: it tells you nothing about the *tail* of `W`, which is what we actually care about. Worse, it warns of a trap. As utilization `ρ` (the fraction of capacity in use) approaches 1, queueing latency does not rise linearly — it explodes. For an idealized M/M/1 queue the mean waiting time scales as:

```
W ∝ 1 / (1 − ρ)
```

At 50% utilization you have headroom. At 90% utilization a small burst sends latency through the roof, and bursts are exactly what market opens and news events deliver. The throughput-minded instinct — "the box is only 60% busy, we have room" — is how you build a system that is fast in the demo and catastrophic at 09:30:00.000. **Hot-path systems run deliberately under-utilized.** Idle cores are not waste; they are latency insurance.

### 1.2.2 The shape of the distribution: percentiles, tails, jitter

Stop thinking of "the latency" as a number. It is a *distribution* — a histogram of many measured times. The whole game is reading its shape.

The vocabulary you will use constantly:

- **Median (p50):** the typical experience. Half of events are faster, half slower. Useful for sanity, useless as a target.
- **Tail percentiles (p99, p99.9, p99.99):** the value below which that fraction of events fall. p99.9 = 40 µs means one event in a thousand took *at least* 40 µs. **These are the numbers that describe your worst races.**
- **Maximum:** the single worst event observed. Noisy and sample-size-dependent, but on a hot path the max is not an outlier to be discarded — it is a real event that happened to real money. Treat it as a defect to be explained, not a statistic to be smoothed.
- **Jitter:** the *variability* of latency — informally the spread between p50 and the tail. Low, stable latency with tight jitter is the goal. A system with p50 = 1 µs and p99.9 = 1.2 µs has excellent jitter; one with p50 = 1 µs and p99.9 = 30 µs does not, even though their medians are identical.

Latency distributions in real systems are almost never Gaussian. They are **heavily right-skewed with a long tail**: a tight cluster of fast events near a hard physical floor (you cannot beat the speed of light through the NIC and PCIe bus; Ch. 66), and a long thin tail stretching right, populated by cache misses, page faults, context switches, interrupts, TLB misses (Ch. 15), and frequency transitions (Ch. 6). The mean sits somewhere in the sparsely populated middle, describing almost none of the actual events. This is the single most important picture in the book; internalize it:

```
 count
   ^
   |        ##
   |       ####          <- the bulk: fast events near the physical floor (p50)
   |      ######
   |     ########
   |    ##########
   |   ############ #
   |  ############# ## #                    .     <- the tail: rare, expensive,
   | ############## ### ##  #   .    .   .  .  .     and where you LOSE races
   +-----------------------------------------------> latency
     floor  p50      p90   p99      p99.9        max
                          mean ~here, describing almost nobody
```

Each lump and spike in that tail has a *cause* in the hardware or the OS. The art of this book is learning to recognize the signature of each cause and to push the tail left. You do not "make the average faster." You **find and eliminate the sources of the tail.**

### 1.2.3 The tick-to-trade budget, broken down by stage

**Tick-to-trade** is the end-to-end latency that matters: from the first bit of a market-data packet arriving at your NIC ("tick") to the first bit of your resulting order leaving your NIC ("trade"). It is the number your business is measured on, and true wire-to-wire measurement of it requires hardware timestamping and PTP (Ch. 55, 58) — software clocks are not trustworthy at this resolution (Ch. 17).

The discipline is to treat that end-to-end number as a **budget** and to allocate it across stages, because you cannot improve what you have not decomposed. A representative breakdown for a competitive software (kernel-bypass) path on modern x86-64 — *orders of magnitude, not promises; your hardware and code decide the real values, and the whole point of this book is to teach you to measure your own*:

| Stage | Rough budget | Book chapters |
|---|---:|---|
| Wire → NIC → host memory (DMA, kernel bypass) | 250–800 ns | Ch. 55, 62, 66 |
| Feed decode / book update | 100–400 ns | Ch. 25, 53 |
| Strategy / signal logic | 50–300 ns | Ch. 8–14, 18–21 |
| Risk checks & order encode | 50–200 ns | Ch. 27, 53, 72 |
| Host memory → NIC → wire | 250–800 ns | Ch. 55, 62, 66 |
| **Tick-to-trade total** | **≈ 0.7–2.5 µs** | the whole book |

Two lessons live in this table. First, **no single stage dominates** — there is no one trick that wins; competitiveness is the sum of dozens of disciplined decisions, which is why the book has 64 chapters. Second, the I/O stages (wire in, wire out) are a large fraction of the budget, which is why kernel bypass, NIC offloads, and even FPGAs (Ch. 62, 69) exist: at some point the only way to cut the budget is to stop touching the general-purpose CPU and OS at all. A pure-software path that does everything right still spends most of its time getting bytes on and off the wire.

Crucially, **every number in that table is itself a distribution.** The budget must hold at p99.9, not at p50, or you have budgeted for the races you win and ignored the ones you lose. "Decode is 200 ns" is meaningless; "decode is 180 ns at p50 and 240 ns at p99.9" is an engineering statement.

---

## 1.3 Measure it: percentiles vs the mean on a real latency sample

Let us make the abstract concrete and demonstrate, with running code, *why means lie*. We will generate a latency sample with the characteristic shape of a real hot path — a tight bulk plus a sparse heavy tail — and then look at it two ways: through the mean, and through percentiles.

The sample is synthetic so the chapter is self-contained and reproducible on the macOS authoring machine; the *analysis* is exactly what you would run on a real captured sample. (Capturing the real thing — with a trustworthy clock — is Ch. 17; doing it without coordinated omission is Ch. 3.)

```cpp
// latency_shape.cpp — why the mean lies on a heavy-tailed latency sample.
// Build: g++ -std=c++20 -O2 latency_shape.cpp -o latency_shape
//   (Clang: clang++ -std=c++20 -O2 ...). No external deps.
#include <algorithm>
#include <cstdint>
#include <print>      // C++23 <print>; see note below for the C++20 fallback.
#include <random>
#include <vector>

// Returns the value at the given percentile (0..100) of an already-sorted vector,
// using nearest-rank. For real analysis prefer HdrHistogram (Ch. 3); this is the
// from-scratch version so the arithmetic is visible.
static double percentile(const std::vector<double>& sorted, double p) {
    if (sorted.empty()) return 0.0;
    const auto n = sorted.size();
    auto rank = static_cast<std::size_t>((p / 100.0) * (n - 1) + 0.5);
    return sorted[std::min(rank, n - 1)];
}

int main() {
    constexpr int N = 1'000'000;
    std::mt19937_64 rng(12345);  // fixed seed => reproducible.

    // Model a hot path: a tight bulk near a hard floor, plus a rare heavy tail.
    // 99.5% of events: ~1.0 us floor + small lognormal jitter (cache-warm path).
    // 0.5% of events: a tail spike (page fault / context switch / IRQ / freq step).
    std::lognormal_distribution<double> bulk(/*mu=*/0.0, /*sigma=*/0.25);
    std::lognormal_distribution<double> tail(/*mu=*/2.3, /*sigma=*/0.6);
    std::bernoulli_distribution is_tail(0.005);

    std::vector<double> samples;
    samples.reserve(N);
    double sum = 0.0;
    for (int i = 0; i < N; ++i) {
        double us = is_tail(rng) ? (1.0 + tail(rng))   // ~10-60+ us spikes
                                 : (1.0 + 0.15 * bulk(rng));  // ~1.0-1.4 us bulk
        samples.push_back(us);
        sum += us;
    }

    const double mean = sum / N;
    std::sort(samples.begin(), samples.end());

    std::println("samples   : {}", N);
    std::println("mean      : {:8.3f} us", mean);
    std::println("min       : {:8.3f} us", samples.front());
    std::println("p50       : {:8.3f} us", percentile(samples, 50.0));
    std::println("p90       : {:8.3f} us", percentile(samples, 90.0));
    std::println("p99       : {:8.3f} us", percentile(samples, 99.0));
    std::println("p99.9     : {:8.3f} us", percentile(samples, 99.9));
    std::println("p99.99    : {:8.3f} us", percentile(samples, 99.99));
    std::println("max       : {:8.3f} us", samples.back());

    // How many events are SLOWER than the mean? On a right-skewed distribution,
    // far fewer than half — the mean is dragged right by the tail and stops
    // describing the typical event.
    auto slower_than_mean =
        samples.end() - std::upper_bound(samples.begin(), samples.end(), mean);
    std::println("");
    std::println("fraction of events slower than the mean : {:.2f}%",
                 100.0 * static_cast<double>(slower_than_mean) / N);
    return 0;
}
```

Representative output (synthetic sample, fixed seed `12345`; values are illustrative of the *shape*, not a benchmark of any machine):

```
samples   : 1000000
mean      :    1.212 us
min       :    1.041 us
p50       :    1.150 us
p90       :    1.208 us
p99       :    1.285 us
p99.9     :   17.337 us
p99.99    :   34.645 us
max       :   83.205 us

fraction of events slower than the mean : 8.62%
```

(Numbers above are from one real run on the libc++ toolchain; the exact figures shift a little across standard-library implementations because `lognormal_distribution` is not specified bit-for-bit, but the *shape* — tight bulk, sharp cliff, long max — is the point and is stable.)

Read that output the way a hot-path engineer reads it:

1. **The mean (1.21 µs) describes almost no event.** Only ~8.6% of events are *slower* than the mean — the mean sits up near the 91st percentile, dragged rightward by the tail, while the bulk of events cluster around p50 = 1.15 µs. The mean is not "the typical latency"; it is an artifact of a handful of expensive events. If you report "average latency 1.21 µs" to your trading desk, you have quoted a number that ~91% of events beat and that still hides the only number that matters.
2. **The cliff is between p99 and p99.9.** p50 through p99 sit in a tight band (1.15–1.29 µs): the cache-warm, no-syscall, no-fault path doing its job. Then p99.9 leaps to ~17 µs — a **~13×** jump over p99. *That* is the tail, and that is where you lose races. An average could never reveal this cliff; only percentiles can.
3. **The max (83 µs) is a real event.** One time in a million, this path took 83 µs. On a hot path that is not a statistical curiosity to discard — it is a lost trade with a cause (a page fault, a stolen time slice, an interrupt) that Ch. 2 and Part VII will teach you to hunt down.

This is the entire mindset in one program: **the mean told you the system was fine; the percentiles told you the truth.**

> **C++20 fallback for `<print>`.** `std::println` (`<print>`, P2093) ships in GCC 14+ and Clang 17+ (libc++). On an older toolchain, replace the `std::println(...)` calls with `std::cout << std::format(...) << '\n';` (`<format>`, C++20, GCC 13+/Clang 14+), or with classic `printf`/iostreams. We use `<print>` throughout the book for *off-hot-path* output and explain in Ch. 71 why formatted I/O never belongs on the hot path itself.

---

## 1.4 Techniques

These are not coding techniques yet — those begin in Part II. These are *thinking* techniques: the habits that make every later chapter land correctly.

### 1.4.1 Reasoning about p99/p99.9 instead of averages

Adopt a few rules and apply them without exception:

- **Never state a latency as a single number.** "The handler takes 200 ns" is not an engineering claim. "The handler is 180 ns at p50, 220 ns at p99, 310 ns at p99.9, over 10M samples on \[CPU/kernel/flags]" is. If a number does not carry a percentile and a sample size, treat it as a rumour.
- **Pick the percentile that matches the business.** How often does the race that matters occur, and how costly is losing it? If the profitable event happens thousands of times a day, p99.9 is the floor of your attention and p99.99 may matter. Budget to the percentile your money actually lives at — see §1.4.2.
- **Watch the *gap* between percentiles, not just their values.** The distance from p50 to p99.9 *is* your jitter. Two systems with the same p50 can be worlds apart; the one with the tighter tail wins every contested race. Optimizing the tail is usually about *removing variance sources*, not shaving the median.
- **One number can only get worse.** Every percentile you stop watching is a place the tail can grow unobserved until it costs you a race in production. Watch the whole curve.

This is the founding rule of the book, and it earns its own memory: **reason about the distribution, never the mean.** Every "is this faster?" question in later chapters is really "what did this do to the p99.9?"

### 1.4.2 Budgeting latency across the pipeline

Treat latency like a financial budget. Three steps:

1. **State the end-to-end target** as a percentile, e.g. "tick-to-trade ≤ 2.5 µs at p99.9." A target without a percentile is undefined.
2. **Decompose into stages** and allocate a sub-budget to each — wire-in, decode, strategy, risk, encode, wire-out (§1.2.3). The allocation forces the question "is this stage worth its slice?" and exposes the stages with no slack.
3. **Measure each stage against its sub-budget at the target percentile**, continuously, in production (Ch. 76). A stage that blows its budget at p99.9 is a defect even if its mean is fine.

The budget is also where you decide a piece of code's **latency tier**, which is the highest-leverage decision you will make about it:

- **Hot path (steady state):** runs per market event; on the critical race. The non-negotiable disciplines of this book apply — zero allocation (Ch. 23–24), no syscalls, no locks that can block (Ch. 32, 34), no logging that formats inline (Ch. 71), nothing that can fault or context-switch (Ch. 23, 41). Every nanosecond and, more importantly, every *variance source* is fought.
- **Warm path (near hot):** runs occasionally — a new-symbol setup, a parameter change (Ch. 73). Should not stall the hot path, but a few microseconds is acceptable. Keep it *off* the hot core's critical section.
- **Cold path (setup / control / research):** startup, configuration, risk batch jobs, backtests (Ch. 67–68). A throughput problem. Spending hot-path effort here is wasted effort, and the latency mindset, misapplied, becomes premature optimization.

Most of the bytes in a trading system are cold. The latency mindset's *first* job is to tell you which 5% is hot, so the other 95% can be written for clarity and the hot 5% can be fought for at the nanosecond level. Misclassifying cold code as hot wastes engineering; misclassifying hot code as cold loses races. Get the tier right first.

### 1.4.3 Identifying and attacking sources of jitter

The tail is not random — every spike has a mechanism. The rest of the book is, in large part, a field guide to these mechanisms. Train yourself to recognize the usual suspects, because naming the cause is most of the cure:

| Jitter source | Typical signature | Where it's fought |
|---|---|---|
| Cache / TLB miss | tens of ns to ~100 ns spikes | Ch. 7–10, 15 |
| Branch mispredict | ~10–20 ns, data-dependent | Ch. 13–14 |
| Page fault (minor/major) | µs (minor) to ms (major) | Ch. 23, 26 |
| Heap allocation (`malloc`) | µs spikes, unbounded tail | Ch. 23–24 |
| System call | ~hundreds of ns + pollution | Ch. 41, 47 |
| Context switch / preemption | µs–ms; correlated with load | Ch. 41, 42, 43, 45 |
| Interrupt (IRQ) | µs; correlated with NIC traffic | Ch. 42, 55 |
| Lock contention | unbounded; load-dependent | Ch. 31–34 |
| Frequency / C-state transition | µs on first event after idle | Ch. 6, 46 |
| NUMA-remote access | ~1.5–2× local DRAM | Ch. 16 |

The strategic insight is that the worst tail offenders — page faults, allocations, syscalls, context switches, lock contention — are **not "slow code." They are the code leaving its lane:** touching the kernel, the allocator, or another core when the hot path should be a self-contained, steady-state loop on an isolated core that owns its memory. This is why so much of low-latency engineering is *subtractive* — removing syscalls, removing allocations, removing locks, removing trips to the kernel — rather than making operations faster. The fastest operation is the one that no longer happens, and the most reliable tail is the one with no mechanism to grow.

---

## 1.5 Pitfalls & anti-patterns: why means lie; throughput-driven design

- **Reporting (or optimizing) the average.** The cardinal sin, demonstrated in §1.3. A mean on a heavy-tailed distribution describes almost no real event and actively *hides* the cliff that costs you races. If a dashboard, a benchmark, or a colleague reports "average latency," the correct response is "at what percentile, and what does the tail look like?"
- **Throughput-driven design on the hot path.** Batching, buffering, and pipelining all trade latency for throughput. They are the right tools for the cold/research tier and the wrong tools for the race. Reaching for them reflexively — because that is how general-purpose backend systems are built — is the most common way good engineers build slow trading systems.
- **Running the box hot.** "We have 40% idle CPU, plenty of room" is the queueing-theory trap of §1.2.1: latency explodes as utilization approaches 1, and the bursts that push you there arrive exactly when the money is on the table. Hot-path cores are kept deliberately under-utilized and, ideally, isolated (Ch. 42, 45).
- **Trusting a single run, or a tiny sample.** Tail percentiles are sample-size hungry: you cannot measure p99.9 honestly with 1,000 samples (you have only one event out there). And the tail is where the variance lives, so one run tells you almost nothing. Ch. 3 makes this rigorous.
- **Coordinated omission.** A subtle, vicious measurement bug: if your measurement loop stalls during the very event that caused a latency spike, it *fails to record* the requests that were waiting, systematically erasing the worst of the tail and making a bad system look good. Named and defeated in Ch. 3 — flagged here so the phrase is in your head from page one.
- **Optimizing before measuring.** The mindset says *care about the tail*; it does not say *guess where the tail comes from*. Intuition about performance is wrong often enough that the entire next chapter (Ch. 2) is "measure first." Premature optimization on a cold path is wasted work; premature optimization on the hot path, *without measurement*, often makes the tail worse while you congratulate yourself on the mean.

---

## 1.6 Exercises & checklist

**Exercises**

1. **Make the tail move.** Build and run `latency_shape.cpp`. Change the tail probability from `0.005` to `0.05` and to `0.0005`. Watch how p99, p99.9, and the *mean* each respond. Which percentile best tracks "how often do I lose a race"? Note how little the mean moves relative to how much the p99.9 moves.
2. **Find the cliff.** Modify the program to also print p99.5 and p99.99, and to report the *ratio* p99.9 / p50. On your numbers, between which two percentiles does the distribution "fall off the cliff"? That ratio is a one-number summary of your jitter.
3. **Budget a path.** Write down a tick-to-trade budget for a hypothetical strategy with a 2 µs p99.9 target, allocating the six stages of §1.2.3. Which stage has the least slack? Which would you attack first, and why (revisit after Appendix E)?
4. **Classify your tiers.** Take any system you have worked on and label each major component hot / warm / cold by the §1.4.2 definitions. Be honest about how much is actually hot. Did anything you assumed was hot turn out to be cold (or vice versa)?
5. **Spot the throughput trap.** Find one place in code you know where batching or buffering improves throughput. Argue whether it helps or hurts latency, and which tier that code lives in.

**Checklist — the latency mindset**

- [ ] I never report or target a latency as a single number — always a percentile (and a sample size, and the machine).
- [ ] I reason about the *distribution*: p50, p99, p99.9, max, and the *gaps* between them (jitter), not the mean.
- [ ] I have an end-to-end tick-to-trade budget, stated at a business-relevant percentile, decomposed across stages.
- [ ] I have classified each component as hot / warm / cold and apply hot-path discipline *only* where it pays.
- [ ] I treat the worst-case (max) as a defect with a cause to be hunted, not an outlier to be smoothed away.
- [ ] I keep hot-path cores deliberately under-utilized and resist throughput-for-latency trades on the race path.
- [ ] When I think "this is faster," I immediately ask "what did it do to the p99.9, and how do I *measure* that?" (→ Ch. 2–3).

---

## 1.7 References

- J. Dean and L. A. Barroso, *"The Tail at Scale,"* Communications of the ACM, 56(2),
  2013. The canonical argument that tail latency, not mean, governs the behaviour of latency-critical systems.
- C. Cook, *"When a Microsecond Is an Eternity: High Performance Trading Systems in C++,"* CppCon 2017. The hot-path mindset, from a practitioner, in trading terms.
- G. Tene, *"How NOT to Measure Latency,"* Strange Loop / numerous venues. The origin of the "coordinated omission" framing and the case for percentile-based measurement (developed here in Ch. 3).
- L. Kleinrock, *Queueing Systems, Volume 1: Theory*, Wiley, 1975. Little's Law and the `1/(1−ρ)` utilization wall behind §1.2.1.
- J. L. Hennessy and D. A. Patterson, *Computer Architecture: A Quantitative Approach*, 6th ed., Morgan Kaufmann, 2017. The "quantitative approach" — measure, then reason — that this whole Part is built on.

## 1.8 Additional Reading

- "Latency Numbers Every Programmer Should Know" (Jeff Dean / Peter Norvig), refreshed for modern server hardware and annotated for trading in **Appendix E** of this book.
- The Mechanical Sympathy community and mailing list (Martin Thompson et al.) — the culture of understanding the hardware to predict latency; the conceptual home of the Disruptor (Ch. 37).
- G. Tene, *HdrHistogram* (project site and docs) — the right tool for recording the tail without coordinated omission; used hands-on in Ch. 3.
- D. Luu, *"Latency mitigation strategies"* and related writing on tail latency in production systems — accessible, measurement-driven essays in the same spirit.

---

*Next: Ch. 2 — Measure First: Profiling & Hardware Performance Counters, where the "always measure" rule of this chapter becomes concrete with `perf`, the PMU, and top-down microarchitecture analysis.*
