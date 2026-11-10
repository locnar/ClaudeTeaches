# Part XIII — Observability & Operations in Production

# Chapter 115 — Performance Engineering in CI & the Profile Lifecycle

> **Prerequisites:** Ch. 3 (micro-benchmarking: warm-up, repetitions, distributions not means, `compare.py`), Ch. 1 (tails), Ch. 114 (the correctness suite this chapter's performance suite runs beside), Ch. 27 §27.4.3 and §27.4.5 (PGO/AutoFDO/BOLT: what they do, and the profile lifecycle this chapter operates), Ch. 14 (why layout and profiles matter), Ch. 102 §102.2.2 and Ch. 107–108 (deterministic replay and captures — the nightly gate's input), Ch. 6 and Appendix C (the quiet box the gate must run on), Appendix L (the harness; §L.6 is this chapter's CI skeleton), Ch. 117 §117.4.1 (the capstone's gate in principle — this chapter is its mechanics).
>
> **Leads into:** Ch. 116 (from the CI gate to production: canaries, shadow runs and paired comparison on identical ticks — why the gate is necessary and not sufficient), Ch. 117 (the capstone's regression-detection tier). The book has said "defend performance in CI" since Ch. 3 and never shown a gate that works: this chapter builds one — the A/A calibration that measures what the gate can see, bootstrap confidence intervals and a rank test on the tail, a median gate for what the tail hides, deterministic proxies for shared runners, bisection by replay — and then operates PGO/BOLT as a lifecycle, with the stale-profile pessimisation measured rather than warned about.

---

## 115.1 Why it matters: a gate that flaps is a gate that gets turned off

Every chapter in this book ends with a number, and every number decays: a compiler bump reorders a hot loop (Ch. 27), a dependency adds an allocation (Ch. 30), a refactor turns a direct call into a `std::function` (Ch. 16), a layout change crosses a cache line (Ch. 11). Ch. 117 §117.4.1 says to catch these with a **latency gate in CI** that compares the candidate's p99.9 against a baseline; Ch. 3 §3.4.4 says to compare distributions, not means. Both are right and both are incomplete, because they skip the question every team that has built such a gate has been burned by: **what is the smallest regression this gate can detect without failing on noise?** §115.3 answers it for one kernel on one host with an **A/A test** — the same binary, twenty runs: per-run p50 identical to the nanosecond, per-run p99 spread **22 %**, p99.9 spread **196 %**, and a Mann–Whitney test that declares the same code "significantly different" at p = 10⁻³³ because two hundred thousand samples resolve a quantisation artefact. A gate that fails on a 5 % p99.9 shift on that host fails every other build; the team disables it within a week; the regressions flow. The method is to **measure the noise floor first** (A/A), set the tolerance above it, gate on the statistic that *is* stable (here the median, and the p99 with a tolerance of 1.5× the A/A spread), run the tail gate only on the box that can resolve tails (the isolated, tuned one — nightly, not per-PR), and use **deterministic proxies** (retired instructions, simulated cache misses) where the runner is shared. The second half of the chapter is the other performance state that decays silently: a **PGO profile**. Ch. 27 warned that a stale profile pessimises; §115.3 measures it — a profile trained on the open costs 3 % at midday, a profile from last week's source costs 17 % after a one-function change — and §115.4.5 gives the procedure that keeps profiles fresh, merged, versioned and validated.

## 115.2 Mental model

### 115.2.1 A benchmark is a test with a noisy oracle

```
   correctness test:   run → compare to expected → pass/fail                       the oracle is exact
   performance test:   run → a SAMPLE from a distribution → compare to a BASELINE SAMPLE → pass/fail    the oracle is statistical
                        ├─ sampling error: finite samples of the candidate's own distribution (bootstrap CI measures it)
                        ├─ machine noise: the distribution itself moves between runs (frequency, cache state, neighbours, steal) — A/A measures it
                        └─ the effect: what the code change did — the only part you want
   gate = "the effect exceeds the noise floor with high probability", never "the candidate's number exceeds the baseline's number"
```

Two consequences. **The noise floor is a property of the runner, not of the benchmark**, so it is calibrated per runner class by running the *same* binary against itself (A/A) and recording the spread of every statistic the gate uses; tolerances come from that table and are re-derived when the runner changes. **Statistical significance is not effect size**: with 200 000 samples a rank test detects a 0.1 ns shift that no trading system cares about; the gate needs *both* a significance test (is this real?) and a **minimum effect** (is it big enough to matter, and bigger than the A/A spread?). §115.3's gate fails only when the bootstrap CI's *lower* bound on the difference clears the tolerance *and* the rank test agrees.

### 115.2.2 Which statistic to gate, on which machine

```
   statistic          what it catches                                   stability on a shared runner (§115.3, VM)   where to gate it
   ────────────────   ────────────────────────────────────────────────  ──────────────────────────────────────────   ──────────────────────────────────
   median (p50)       added work on every call: the new allocation,      identical across 20 runs (15.56 ns)          every PR, any runner
                      indirect call, extra load, de-vectorised loop
   p99                occasional work: a cold path taken more often,     ±22 % run to run; |Δ| between runs ≤ 3.7 ns   every PR with a tolerance ≥ 1.5× A/A spread
                      a branch that mispredicts on the new data
   p99.9 / max        the tail the business cares about (Ch. 1)          ±196 %: hypervisor steal, neighbours          nightly, on the isolated tuned box only
   instructions:u,    deterministic proxies — the WORK, not the time     exact (±0.01 %) on any runner                 every PR on shared runners; a change in
   cachegrind misses                                                                                                   instruction count is a change to explain
   full-replay p99.9  the real tick-to-trade distribution on the real    the production noise floor (Appendix C box)   nightly; the release gate (Ch. 116)
   over a captured open   stages (Ch. 117 §117.3)
```

The median is not what the business measures, but it is what a *per-PR* gate on commodity CI can resolve, and it catches the most common regression class — work added to every call. The tail is measured where it can be measured: on the box that is also production's twin, once a day, with the replayed open (Ch. 108). Between them sit the **proxies**: `perf stat -e instructions:u,branches:u,branch-misses:u -r 10` is deterministic to a rounding error on any machine, and a +4 % instruction count on the hot loop is a regression to explain *even if the timing gate passed*; `valgrind --tool=cachegrind` simulates the cache hierarchy and reports misses that do not depend on the neighbour's job; `llvm-mca` estimates the loop's throughput from the asm alone. A proxy cannot say "slower" — it says "different work" — and the rule is that a proxy *may stand in for time* only where an A/A-calibrated timing gate has been shown to be blind.

### 115.2.3 The pipeline: fast gate, nightly gate, bisection

```
   per PR (minutes, shared runners)          nightly (hours, the tuned box, Appendix C)              on a nightly failure
   ──────────────────────────────────         ─────────────────────────────────────────────────       ─────────────────────────────────────────────
   build (two compilers), Ch. 114 suite       full-session replay (Ch. 108's replayer → the real        bisect by replay: the same capture through each
   micro-benchmarks: median gate with A/A      receive path) with the §117.3 stage decomposition;       commit between the last good and the first bad
     tolerance; instructions:u proxy;         p99/p99.9 per stage vs the stored baseline with           nightly, on the same box — deterministic input, so
     bootstrap CI on p99 vs tolerance         bootstrap CI + rank test + change-point on the trend;      the only variable is the code; ~log₂(N) runs
   store results as artefacts keyed by        PGO/BOLT profile validity (§115.4.5); sanitizer suites     (Ch. 117 §117.4.2)
     revision + runner class                  store as the new baseline candidate; alert on trend
```

Baselines are **artefacts keyed by revision and runner class**, never a number in a YAML file: the gate compares the candidate against the baseline *measured on the same runner class*, and a runner change starts a new baseline series with a new A/A table. The trend (per-stage p99 over weeks) is where slow drift shows — a few ns per release that no single gate catches (Ch. 117 §117.4.1's dashboard) — and a change-point test on it is the alert.

### 115.2.4 Profiles decay like benchmarks

A PGO profile encodes *which branches were taken how often and which functions were hot* for the training run (Ch. 27 §27.4.3). It is stale along two axes: the **workload** changed (the open's message mix is not midday's; a new venue's feed has different branch frequencies — §115.3: −13 % at the open, +3 % at midday from the *same* profile) and the **source** changed (functions whose control flow no longer matches the profile's checksums are compiled *without* profile data, and the layout decisions around them are made with half the picture — §115.3: +17 % after adding one function and one branch, with GCC refusing to build by default and Clang building silently). A profile is therefore a build input with a **validity window**: it is produced from a named capture (Ch. 107) by a named revision, stored by that revision, re-trained on a schedule and whenever the hot code changes, and *gated* — a build whose profile mismatches more than N functions, or whose training capture is older than D days, does not ship.

## 115.3 Measure it: the noise floor, the gate, and the stale profile

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 and Clang 18.1 (`-O2 -std=c++20`), run on the 4-vCPU Intel Xeon VM of Ch. 49 §49.3 (TSC 2.70 GHz, Linux 6.18, no isolation, visible hypervisor steal) — deliberately a *shared-runner-class* host, because that is where per-PR gates run; the tuned box's floor is lower and the method is the same. Labelled VM-measured.*

**The gate harness** (`perfci.cpp`): a Ch. 32-style dense book update (an indexed load, a dependent store, ~15 ns) timed with an `rdtsc` pair per call, 200 000 samples per run; **A/A**: 20 runs of the same binary; then baseline vs candidate where the candidate carries an injected regression of 8 or 30 iterations of a multiply chain (~3 ns / ~12 ns of added work). Statistics: per-run percentiles, |Δp99| between paired A/A runs, a percentile bootstrap (400 resamples) on p99 and on the p99 *difference*, and a Mann–Whitney U test (normal approximation) on the bulk.

```
A/A: 20 runs × 200 000 samples, same binary
  per-run p50    min / median / max   15.56 / 15.56 / 15.56 ns        identical: the median is quantised to the TSC tick and does not move
  per-run p99                        16.30 / 17.04 / 20.00 ns        run-to-run spread 22 % of the median
  per-run p99.9                      17.04 / 19.26 / 54.81 ns        spread 196 %: steal and neighbours — un-gateable here
  |Δp99| between paired A/A runs     median 2.22 ns, max 3.70 ns     → any p99 tolerance below 3.7 ns flaps; the gate uses 1.5 × 3.70 = 5.56 ns
  bootstrap 95 % CI on ONE run's p99 width 0.0–0.7 ns                 the sample is big enough; the MACHINE is the noise — more samples per run buy nothing
  Mann–Whitney on two A/A runs       p = 3.9 × 10⁻³³                  "significant" on identical code: 200 k samples resolve the TSC quantisation. Significance ≠ effect.

gate                                     p50               p99 (Δ)                bootstrap 95 % CI on Δp99   verdict
  A/A (same code)                        15.56 → 15.56     16.30 → 17.78 (+1.48)  [+1.48, +1.48]              PASS  (inside the 5.56 ns tolerance)
  A/B, +8 iterations ≈ +3 ns of work      15.56 → 17.04     17.78 → 18.52 (+0.74)  [+0.74, +0.74]              FAIL — bulk regression: the MEDIAN moved past its own floor (0.40 ns) while the tail stayed inside the noise
  A/B, +30 iterations ≈ +12 ns of work    15.56 → 20.74     16.30 → 22.96 (+6.67)  [+6.67, +6.67]              FAIL — tail regression: the CI's lower bound clears the tolerance
```

**The profile harness** (`pgo_kernel.cpp`, `pgo.sh`): a decoder-style if-chain dispatch over four message types with a cold validation branch and a rare "fat-finger" path, driven by two mixes — *open* (70 % adds, 20 % executions) and *midday* (30 % adds, 55 % cancels). Instrumented build → train → `-fprofile-use`; the same with the other mix; a weighted merge; then a source change (one new function, one new branch) rebuilt against the old profile. Medians of five runs.

```
GCC 13.3                                     open mix      midday mix        Clang 18.1                                 open      midday
  plain -O2                                  6.1–6.3 ns    6.1–6.5 ns          plain -O2                                6.3–6.8   8.2–8.4
  instrumented (-fprofile-generate,          21.6–21.8     —                   instrumented (-fprofile-instr-generate)  7.3       —        (front-end counters: +3 %; GCC's atomic edge counters: 3.5×)
    -fprofile-update=atomic)
  PGO trained on OPEN                        5.46  (−13 %) 6.33  (+3 % stale)  PGO(open)                                5.64 (−10 %)  7.73 (−5 %)
  PGO trained on MIDDAY                      5.73  (−7 %)  5.92  (−4 %)        PGO(midday)                              6.64 (+6 % stale)  8.32 (+2 %: no gain)
                                                                               PGO(3×open + 1×midday, llvm-profdata merge -weighted-input)   6.36      8.53
  source changed, OLD profile:               refuses: "source locations for function … have changed, the profile data may be out of date [-Werror=coverage-mismatch]"
    …with -Wno-error=coverage-mismatch       6.30  (+13 % vs fresh 5.59)   7.09  (+17 % vs fresh 5.93)   8 functions unprofiled   Clang: builds SILENTLY; 7.05 (+9 %)  8.99 (+6 %)
```

Reading it:

- **Calibrate before you gate.** The A/A table is the gate's specification: on this runner class the median is exact, p99 moves 3.7 ns between runs of identical code, and p99.9 moves 3×. A per-PR gate here can promise to catch a +3 ns regression on the median and a +6 ns one on p99, and must not claim anything about p99.9. Those are honest numbers; a gate without them is a guess that will be tuned by whoever is annoyed by it last.
- **Gate the median too.** The +3 ns regression — a realistic one: an extra dependent load, a small allocation's fast path — was invisible in the p99 (+0.74 ns, inside the tolerance) and obvious in the median (15.56 → 17.04 against a 0.40 ns floor). The tail gate alone would have passed it; the business would have found it at the open.
- **A rank test is necessary and not sufficient.** Mann–Whitney separated two identical runs at p = 10⁻³³; the gate uses it to reject "the shift is sampling noise" and the bootstrap CI against the A/A tolerance to reject "the shift is too small to matter". Ch. 3 §3.4.4's `compare.py` is the former; the tolerance is what this chapter adds.
- **More samples do not help; a quieter machine does.** The bootstrap CI on one run's p99 was 0–0.7 ns wide against a 3.7 ns run-to-run spread: the uncertainty is the machine drifting between runs, not the estimator. The remedy is the tuned box (Appendix C) for the tail, and proxies on the shared one.
- **PGO buys 10–13 % on the mix it was trained on and can cost 3–6 % on another.** The open-trained profile made midday *slower* than `-O2` under GCC; the midday-trained profile made the open slower under Clang; Clang's weighted merge (3× open, 1× midday) landed between. Train on a capture that contains the open *and* the day (Ch. 107), weight the open if that is where the money is, and **measure the profile on both mixes** before shipping it — the stale-workload case is a gate, not a warning.
- **A stale *source* is worse than no profile.** One new function and one new branch, rebuilt with last revision's profile: GCC *refused* (coverage-mismatch is an error by default — leave it that way); forced through, the build was 13–17 % slower than the fresh profile and slower than plain `-O2` at midday, because the mismatched functions were compiled unprofiled while their callers' layout still assumed the old counts. Clang 18 built silently and shipped a binary 6–9 % slower. Re-train on every change to the hot code, and make the mismatch diagnostics fatal (§115.4.5).
- **Instrumentation cost decides where training runs.** GCC's `-fprofile-update=atomic` build ran 3.5× slower — fine for a replayed capture on a lab box, unusable live; Clang's front-end instrumentation cost 3 % here and AutoFDO (`perf record -b`, Ch. 27 §27.4.3) costs ~nothing, which is why AutoFDO is the *production-refresh* path and instrumented PGO the *release-build* one.

## 115.4 Techniques

### 115.4.1 A/A calibration: measure the floor, derive the tolerance

```cpp
// aa.cpp (perfci.cpp abridged) — the same binary, R runs; record every statistic the gate will use; the tolerance is a multiple of the worst A/A spread.
std::vector<double> p50s, p99s, p999s, dp99;
for (int r = 0; r < R; ++r) { auto ns = one_run(bench, SAMPLES); p50s.push_back(pct(ns, .5)); p99s.push_back(pct(ns, .99)); p999s.push_back(pct(ns, .999)); }
for (int i = 0; i + 1 < R; i += 2) dp99.push_back(std::fabs(p99s[i] - p99s[i + 1]));       // what two runs of IDENTICAL code disagree by
Tolerance tol{ .p50 = 1.5 * (max(p50s) - min(p50s)) + one_tsc_tick, .p99 = 1.5 * max(dp99), .p999 = gateable(p999s) ? 1.5 * spread(p999s) : INF };
store_artifact("aa-" + runner_class + "-" + revision + ".json", tol);                      // re-derived when the runner class changes; reviewed when it grows
```

- **Run A/A on every runner class**, periodically (the floor drifts with the fleet's load) and whenever the gate is tuned; a tolerance that has to grow is a runner problem to fix, not a number to bump.
- **Interleave A and B runs** (A, B, A, B, …) rather than all-A then all-B, so a slow drift in the machine lands in both; the A/A spread then measures the drift the interleaving does not cancel.
- **Pin, warm, fix the frequency** (Appendix L.3) even on shared runners — the floor shrinks with every Ch. 6 knob the runner honours, and `perf stat -e instructions:u` beside the timing says whether a shift is work or weather.

### 115.4.2 The gate: bootstrap CI, rank test, minimum effect

```cpp
// gate.cpp — baseline a, candidate b (both the latency samples of one run); tolerance from §115.4.1
auto ci = bootstrap_ci_of_difference(a, b, [](auto& x) { return pct(x, .99); }, /*B=*/300);   // resample both, take Δp99; [2.5 %, 97.5 %] quantiles
double p = mann_whitney_p(a, b);                                                                 // one-sided: is b stochastically larger?
bool fail_tail = ci.lo > tol.p99 && p < 1e-3;                   // the LOWER bound clears the floor: noise cannot fail you, a real regression cannot hide in it
bool fail_bulk = pct(b, .5) - pct(a, .5) > tol.p50 && p < 1e-3; // the median moved past its own floor: work was added to every call
emit_json({label, pct(a,.99), pct(b,.99), ci.lo, ci.hi, p, tol.p99, fail_tail ? "FAIL-tail" : fail_bulk ? "FAIL-bulk" : "PASS"});
```

- **Percentile bootstrap, not a t-test**: latency is not normal and p99 is not a mean; resampling the *difference of percentiles* gives a CI without a distributional assumption (Efron & Tibshirani 1993). 300–1000 resamples suffice; the cost is seconds.
- **Direction matters**: a candidate that is *faster* than the tolerance is also a signal — a dropped feature, a dead-code-eliminated benchmark (Ch. 3 §3.4.1) — and is flagged, not celebrated, until explained.
- **Per benchmark, per stage**: the replay gate (§115.4.4) applies the same statistics to each §117.3 stage separately; one stage's regression hides in the end-to-end sum.
- **The JSON line is the artefact**: stored per revision, it is what the trend view and the bisection read; Google Benchmark's `--benchmark_out=json` and `compare.py` produce the same shape for the micro-benchmarks (Appendix L.6).

### 115.4.3 Deterministic proxies for shared runners

```
   perf stat -e instructions:u,branches:u,branch-misses:u,L1-dcache-load-misses:u -r 10 -x, ./bench --iters 1000000     # instructions:u varies < 0.01 % run to run; a change is a change of WORK
   valgrind --tool=cachegrind --cache-sim=yes --branch-sim=yes ./bench     # simulated I1/D1/LL misses and mispredicts: slow (50×), exact, machine-independent (Appendix I.7)
   llvm-mca -mcpu=icelake-server -iterations 100 hot_loop.s                # the loop's throughput bound from the asm alone: catches a de-vectorisation or a new dependency chain (Ch. 13)
   objdump -d --no-show-raw-insn bench | awk '/<hot_fn>:/,/ret/' | sha256sum   # the hot function's instruction stream did not change at all → no timing run needed
```

- **A proxy gates work, not time**: +4 % instructions on the hot loop fails the PR *as a question* ("explain this"), never as a latency claim; the nightly box answers the latency question.
- **Pair a proxy with the median gate**: unchanged instructions and a moved median means the data or the machine changed, not the code — which is itself diagnostic (a layout shift — Ch. 11, Ch. 14 — shows as unchanged `instructions:u` and changed `L1-icache` or `iTLB` counters).
- **The asm hash is the cheapest gate of all**: for the ten functions that matter (Ch. 117 §117.3's stages), a per-PR diff of their disassembly against the baseline revision catches the indirect call, the spilled register and the lost vectorisation before anything is timed (Ch. 4).

### 115.4.4 The nightly replay gate and bisection by replay

The nightly gate is Ch. 117 §117.4.1's: the golden capture (Ch. 107) replayed onto the wire of the tuned box (Ch. 108 §108.4.3 — the real receive path, the real microburst cadence), the §117.3 stage decomposition from the in-band tracer (Ch. 98), N ≥ 30 replays, per-stage p99/p99.9 with the §115.4.2 statistics against the stored baseline, and a change-point test on the trend series. What this chapter adds is the **operating procedure**: the box is *reserved* for the gate (no other job, `cyclictest` quiet before the run — Appendix C's verification pass, Appendix M's host fingerprint), the A/A table for *this* box is re-measured weekly, the baseline is the median of the last seven green nights rather than one run, and a failure triggers **bisection by replay** automatically — the same capture through each commit between the last green nightly and the failure, on the same box, ~log₂(commits) runs, because the input is deterministic and the box is quiet the only variable left is the code (Ch. 117 §117.4.2). The output of a bisection is a commit and a stage; the §115.4.3 asm diff of that stage's functions at that commit is usually the explanation.

### 115.4.5 Operating PGO/AutoFDO/BOLT: the profile lifecycle

```
   1. TRAIN   build instrumented (GCC: -fprofile-generate [-fprofile-update=atomic if the trainer is threaded]; Clang: -fprofile-instr-generate),
              run it on a NAMED capture (Ch. 107) that contains the open AND the day, via the replayer (Ch. 108): LLVM_PROFILE_FILE="prof/%m-%p.profraw" ./app_instr
              — never on a synthetic load, never on an idle session; §115.3: wrong mix = −13 % becomes +3 %
   2. MERGE   Clang: llvm-profdata merge -output=rev.profdata prof/*.profraw   (weighted: -weighted-input=3,open.profdata -weighted-input=1,midday.profdata)
              GCC: .gcda files accumulate across runs of the same object (gcov-tool merge -w 3,1 dirA dirB for explicit weights); keep the OBJECT names identical between the
              instrumented and the -fprofile-use compile, or the profile is silently not found (-Wmissing-profile says so)
   3. STORE   the profile is an artefact keyed by (source revision, capture id, date); the release manifest (Ch. 116 §116.4.2) names it; nothing ships with an unnamed profile
   4. USE     -fprofile-use -fprofile-correction (GCC) / -fprofile-instr-use=rev.profdata (Clang), then LTO, then BOLT on the linked binary from an LBR profile of the SAME replay
              (perf record -e cycles:u -j any,u; perf2bolt; llvm-bolt — Ch. 27 §27.4.3, Appendix I.9)
   5. GATE    mismatch diagnostics FATAL: GCC -Werror=coverage-mismatch (default) + -Werror=missing-profile; Clang -Werror=profile-instr-out-of-date -Werror=profile-instr-unprofiled
              (Clang 18 built §115.3's stale-source case silently with the defaults); the profile's capture must be younger than D days; the §115.4.4 replay gate runs the
              PGO'd binary on BOTH the open and a midday window and fails if either is slower than plain -O2
   6. REFRESH on every change to a hot-path TU (the §115.4.3 asm-hash set), on a schedule (weekly), and after any venue/feed change; AutoFDO (perf record -b on production,
              create_llvm_prof / create_gcov → -fprofile-sample-use / -fauto-profile) refreshes from live traffic between instrumented trainings — Ch. 27 §27.4.5
```

- **The trainer is the replayer.** Instrumented binaries are 1.03–3.5× slower (§115.3) and cannot run live; the capture replayed at 0.3× speed into the instrumented build is both representative and affordable, and it is the same input the nightly gate uses — so "the profile matches the workload" is checkable.
- **Partial training is a flag, not an accident**: `-fprofile-partial-training` (GCC) keeps functions the training never reached optimised for speed instead of size — use it when the capture cannot contain every path (a venue's rare reject types), and know that those paths are then *unprofiled* by design.
- **BOLT profiles age faster than PGO profiles**: BOLT lays out the *final* binary, so any change to the link (a library bump, a new TU) invalidates its profile; re-run `perf2bolt` from the replay as the last build step every time, never reuse an `.fdata`.
- **Appendix D.6 and I.9 carry the commands; Ch. 27 §27.4.5 the build-system side** (where the profile lives in the tree, how the hermetic build (Ch. 28) pins it).

### 115.4.6 Trend, alerting and the human loop

A gate decides a PR; a **trend** decides a quarter. Store every nightly's per-stage percentiles (the JSON lines) in the telemetry store (Ch. 99) and plot them with the deploy markers (Ch. 116); run a change-point detector (CUSUM or PELT) on each stage's p99 series and alert on a detected shift smaller than any single gate's tolerance — the 2 ns-per-release drift that sums to the budget in a year. Alert to the people who can bisect, with the bisection already started; a performance alert that lands in a channel nobody owns is Ch. 117 §117.5's "gate too noisy to trust" with extra steps.

## 115.5 Pitfalls & anti-patterns

- **Gating without an A/A table.** The tolerance is then a guess; the first flap gets it loosened, the second gets it disabled. Calibrate per runner class (§115.4.1).
- **Gating p99.9 on a shared runner.** §115.3: 196 % run-to-run spread. The tail is gated nightly on the tuned box; per-PR gates the median and p99 with tolerances.
- **Significance without effect size.** Mann–Whitney at p = 10⁻³³ on identical code. Pair the rank test with the bootstrap CI against the A/A tolerance (§115.4.2).
- **Comparing against a number in a file.** Baselines are measured artefacts keyed by revision and runner; a hard-coded "p99 < 20 ns" is a runner-specific accident.
- **A profile with no provenance.** A `.profdata` of unknown revision and workload is a stale profile waiting to happen; name it, store it, gate it (§115.4.5).
- **Silencing coverage-mismatch.** GCC's default error is right; `-Wno-error=coverage-mismatch` shipped a 17 % regression in §115.3. Clang's silence is the trap — enable and `-Werror` its profile warnings.
- **Training on the wrong day.** An open-trained profile cost 3 % at midday; a synthetic trainer costs more. Train on the capture, on both mixes, and measure both before shipping.
- **Reusing a BOLT profile across links.** Any link change moves the layout; re-profile as the last build step.

## 115.6 Exercises & checklist

1. **Your A/A table.** Run `perfci` (or your benchmark) 20× on your CI runner and on your tuned box; tabulate the p50/p99/p99.9 spreads and derive tolerances for each; state which statistics each machine can gate.
2. **Catch +3 ns.** Inject a one-load regression into a hot function and show which gate (median, p99, `instructions:u`, asm hash) catches it on the shared runner.
3. **Bisect by replay.** Seed a regression ten commits back and script the bisection over a captured session on one box; report runs and wall time.
4. **Profile lifecycle.** Train on a replayed open, store the profile by revision, change a hot function, and show both compilers' diagnostics with and without `-Werror`; measure the stale-source build against the fresh one on two mixes.
5. **AutoFDO refresh.** Collect `perf record -b` from a replay of yesterday's session, convert, rebuild, and compare against the instrumented profile on today's capture.

Checklist:

- [ ] An **A/A table** per runner class is stored and re-measured; every tolerance derives from it (§115.4.1).
- [ ] Per-PR gates the **median** and **p99 with tolerance**, plus `instructions:u` and the hot functions' **asm hash**; the **tail** is gated nightly on the tuned box (§115.2.2, §115.4.3).
- [ ] The gate fails only when the bootstrap CI's **lower bound clears the tolerance** and the rank test agrees; faster-than-tolerance is flagged too (§115.4.2).
- [ ] Baselines are **artefacts keyed by revision and runner**; the trend is stored and change-point-alerted (§115.2.3, §115.4.6).
- [ ] A nightly failure starts **bisection by replay** automatically (§115.4.4).
- [ ] Every shipped profile is **named** (revision, capture, date), trained on the replayed capture across the day's mixes, **gated** with fatal mismatch diagnostics and a two-mix replay check, and **refreshed** on hot-code changes (§115.4.5).

## 115.7 References

- Bradley Efron and Robert Tibshirani, *An Introduction to the Bootstrap* (1993) — the percentile bootstrap used for the CIs.
- H. B. Mann and D. R. Whitney, "On a Test of Whether One of Two Random Variables Is Stochastically Larger than the Other," *Annals of Mathematical Statistics* 1947 — the rank test behind `compare.py`.
- Tomas Kalibera and Richard Jones, "Rigorous Benchmarking in Reasonable Time," ISMM 2013 — repetition levels, when more runs vs more iterations help; the statistical frame for §115.2.1.
- Google Benchmark `tools/compare.py` documentation and `--benchmark_out=json` (Appendix L.6); Valgrind *Cachegrind* manual; `llvm-mca` documentation; `perf-stat(1)` (`-r`, `-x`).
- GCC manual, *Instrumentation Options* (`-fprofile-generate`, `-fprofile-use`, `-fprofile-correction`, `-fprofile-partial-training`, `-fprofile-update`, `-Wcoverage-mismatch`, `-Wmissing-profile`); `gcov-tool(1)`.
- Clang *Profile Guided Optimization* documentation (`-fprofile-instr-generate`, `LLVM_PROFILE_FILE` `%p`/`%m`, `-fprofile-instr-use`, `-Wprofile-instr-out-of-date`, `-Wprofile-instr-unprofiled`); `llvm-profdata merge -weighted-input`; AutoFDO (`create_llvm_prof`, `llvm-profgen`); the BOLT paper (Panchenko et al., CGO 2019).
- Ch. 3 §3.4.4, Ch. 27 §27.4.3/§27.4.5, Ch. 117 §117.4.1–§117.4.2, Appendix L.6.

## 115.8 Additional Reading

- Emery Berger et al., "Stabilizer" (ASPLOS 2013) and Mytkowicz et al., "Producing Wrong Data Without Doing Anything Obviously Wrong!" (ASPLOS 2009) — layout and environment as noise sources a gate must respect.
- Chen et al., "AutoFDO: Automatic Feedback-Directed Optimization for Warehouse-Scale Applications" (CGO 2016) — the production-refresh path for profiles.
- Ch. 116 — what the gate cannot see: production-only effects, and how canaries and paired comparison on identical ticks measure them.

---

*Next: Ch. 116 — Release Engineering & Measuring Changes in Production: the deploy calendar as a latency decision, blue/green cutover for process-per-role with a one-store flip and instant rollback (measured: one tick of gap, no loss, no duplicate), canary shards, shadow runs with decision parity, venue conformance, and paired comparison on identical ticks — including the 50 ns read-order bias an A/A run exposes in the paired design itself.*
