# Part XIII — Observability & Operations in Production

# Chapter 105 — Latency-Spike Forensics & Anomaly-Triggered Diagnostics

> **Prerequisites:** Ch. 1 §1.4.3 (the jitter taxonomy this chapter turns into a procedure), Ch. 19 (the TSC and its calibration — every timeline here is anchored to it), Ch. 58 (the kernel-noise catalog, `rtla`, and stop-on-threshold ftrace — consumed here by reference, not re-taught), Ch. 73 §73.4.1 (`PTP_SYS_OFFSET_PRECISE`, the TSC↔PHC bridge), Ch. 91 and Ch. 92 (the binary record and the trace header that name the over-budget message), Ch. 93 §93.4.4 (triggers: the one threshold the hot thread evaluates), Ch. 100 (the same flight-recorder ring, dumped on a fault), Ch. 101 (capture — the replay that reproduces the message), Ch. 2 §2.4.4 (Intel PT and the uncore PMUs as evidence sources), Ch. 76 (bpftrace).
>
> **Leads into:** Ch. 106 and Ch. 107 (the host and network signals this chapter's suspect table is sampled from, continuously), Ch. 111 §111.4.4 (profiling the live system — the always-on and targeted-live tiers whose snapshots this chapter freezes), Appendix N (the incident bundle: what to collect, how to keep it alignable). Every chapter so far has removed mechanisms from the hot path; this one is for the morning when a p99.99 outlier appears anyway — one 40 µs stall at 09:31:42.117 that cost a fill — and the task is not to make the system faster but to say *why that one message was slow*, from evidence that was being recorded before anyone knew it would be needed.

---

## 105.1 Why it matters: the book can say it happened; this chapter says why

Ch. 92 produces, for every message, a trace record with five stage stamps; Ch. 93 turns them into per-stage histograms with SLOs; Ch. 106 and Ch. 107 sample the host's and the network's invariants every second. On a good day those three say "p99.9 within budget, zero isolation deltas, zero drops". On the day that matters they say something more specific and far less useful: at 09:31:42.117 one message's decode→decide stage took 41.3 µs instead of 180 ns, the end-to-end histogram's p99.99 moved, nothing else changed, and the desk lost a fill it should have had. The book, up to this chapter, can *name* the suspects — Ch. 1 §1.4.3's table lists ten mechanisms with their signatures, Ch. 58 catalogues the kernel's noise sources with a fingerprint and a knob each — and it can *reproduce* logic bugs by replaying the capture (Ch. 101) through the deterministic core (Ch. 96). But a one-off stall is neither: it is not in the logic (the replay runs the message in 180 ns), and it will not happen again on demand. The only evidence that can explain it is evidence that was recorded *at 09:31:42.117, on core 5, by things that were already running* — and that evidence has to be recorded in a way that (a) costs the hot thread nothing it cannot afford, (b) survives the next millisecond of normal traffic instead of being overwritten by it, and (c) can be laid on one timeline with the application's own stamps, which were taken on a different clock from the kernel's, which is different again from the NIC's and the venue's.

That is the chapter: a **forensic method** that goes from one outlier in a histogram to a named mechanism, and the **anomaly-triggered capture** that makes the method possible. §105.2 lays out the loop — localise the event to a core, a thread and a window; reconstruct a single timeline across the TSC, `CLOCK_MONOTONIC`, the kernel's `trace_clock`, `dmesg`, the PHC and the venue's clock; test each suspect against its fingerprint; confirm with a knob or a reproduction — and the always-on/frozen-on-trigger model that supplies the evidence. §105.3 measures what the always-on flight recorder costs the hot thread, how fast a trigger freezes it, and how precisely the clocks can be aligned. §105.4 builds the pieces: the timeline reconstruction, the per-thread flight recorder frozen by a pointer swap, the kernel and hardware captures (ftrace, `perf record` in overwrite mode, Intel PT snapshots) armed on the same trigger, the fingerprint tests as a decision table, and the hand-off to an incident bundle (Appendix N) and to reproduction. §105.5 and §105.6 are the pitfalls, exercises and checklist. Ch. 58 owns kernel-noise *attribution tooling* (`rtla`, the ftrace recipe); Ch. 100 owns the *crash-time* dump; Ch. 111 §111.4.4 owns *continuous* profiling of the live box. This chapter owns the method and the flight recorder, and cross-references the rest.

## 105.2 Mental model

### 105.2.1 From one outlier to a mechanism: the forensic loop

```
   1. OBSERVE     one record over budget: Ch. 92 trace record {corr, t[0..4]} with t[2]−t[1] = 41.3 µs
        │          (or: Ch. 93 histogram bucket, Ch. 106 invariant delta, Ch. 107 drop, venue reject)
        ▼
   2. LOCALISE    core (rdtscp aux / affinity), thread (tid), window [t_start, t_end] in RAW TSC
        │          — the window is the stage that stretched, widened by one stage on each side
        ▼
   3. ALIGN       convert every evidence stream's timestamps INTO that TSC window (§105.2.2):
        │          flight recorder (TSC) · ftrace ring (trace_clock x86-tsc) · perf/PT snapshot (perf_clock →
        │          CLOCK_MONOTONIC) · dmesg (CLOCK_BOOTTIME) · Ch. 106/107 samples (CLOCK_REALTIME) · NIC/venue (PHC/UTC)
        ▼
   4. TEST        each suspect against its fingerprint inside the window (§105.2.3): what is IN the window
        │          (an IPI, a sched_switch, an SMI count delta, a C-state exit) and what is NOT
        ▼
   5. CONFIRM     turn the knob (Ch. 58 §58.4.3) and watch the fingerprint disappear; or replay the capture
                  (Ch. 101) and show the message itself is innocent; then file the bundle (Appendix N)
```

Two properties of the loop matter more than any tool. **The window comes first and everything is converted into it**, never the other way round: the application's stamp is the most precise (raw TSC, ~0.4 ns ticks, taken by the thread that suffered), so the kernel's, the NIC's and the venue's evidence are mapped onto *its* axis with their own error bars (§105.2.2), and a suspect whose fingerprint falls outside the window — a `sched_switch` 300 µs *after* the stall ended — is exonerated by arithmetic, not by opinion. And **the loop is only as good as what was being recorded at step 1**: the evidence streams of step 3 must already have been running, cheaply, before the outlier, and must have *stopped overwriting themselves* when it was detected. That is §105.2.4.

### 105.2.2 One timeline across clocks

Each evidence source stamps in its own clock. The conversions, with what each one costs in accuracy:

```
   source                     clock                      converted to the TSC window by                             typical error
   ────────────────────────── ────────────────────────── ────────────────────────────────────────────────────────── ────────────────
   trace record (Ch. 92),     raw TSC                    identity — same counter on every core (Ch. 19 §19.2.2)         0
   flight recorder (§105.4.2)
   ftrace ring (Ch. 58)       trace_clock = x86-tsc      identity (set `echo x86-tsc > trace_clock` BEFORE the           0
                                                         incident; the default `local` is sched_clock: ns, per-CPU,
                                                         not the TSC)
   perf.data / Intel PT       perf_clock = sched_clock   `perf record -k CLOCK_MONOTONIC` makes perf stamp in            ±20 ns (the
   snapshot (§105.4.3)         (CLOCK_MONOTONIC-based)    MONOTONIC; then MONOTONIC→TSC via the linear map of             sandwich)
                                                         §105.4.1 (two anchored sandwiches, rate from their distance)
   dmesg / journal            printk: CLOCK_BOOTTIME     BOOTTIME = MONOTONIC + suspend time (0 on a server);            ±1 µs
                              (6.x: local_clock → ns)    `dmesg --time-format=iso` rounds to µs; map via MONOTONIC
   Ch. 106 / 107 samples        CLOCK_REALTIME (1 Hz)      REALTIME = MONOTONIC + offset (stable unless stepped);          ±half the
                                                         the SAMPLE INTERVAL, not the clock, is the resolution           sample period
   NIC hardware stamps,       PHC (UTC, PTP)             PTP_SYS_OFFSET_PRECISE gives (TSC-derived system time, PHC)     ±10–50 ns
   capture (Ch. 101)                                      pairs — Ch. 73 §73.4.1; a fresh pair every second from          (Ch. 73 §73.3)
                                                         the Ch. 106 sampler is the anchor
   venue timestamps           venue's UTC                compare to the PHC, never to the TSC (Ch. 92 §92.4.5)           venue tolerance
```

Three mechanics make this routine instead of heroic. **Anchor the TSC to `CLOCK_MONOTONIC` with sandwiches**: `t0 = rdtscp; m = clock_gettime(MONOTONIC); t1 = rdtscp` — the clock read happened inside `[t0, t1]`, and the narrowest sandwich out of a few thousand attempts is ~100 cycles wide (§105.3: ±18 ns); two anchors half a second apart give the TSC rate to a part in 10⁷, and the linear map holds for hours on an invariant TSC with NTP/PTP *slewing* (a *step* — Ch. 73 §73.5 — breaks it, which is one of the fingerprints). **Set every kernel ring's clock to the TSC before you need it** — `trace_clock x86-tsc` for ftrace (Ch. 58 §58.4.2 already does), `-k CLOCK_MONOTONIC` for perf — so the conversion is identity or one linear map. **Publish the anchors continuously**: the Ch. 106 sampler already reads the PHC cross-timestamp once a second (Ch. 73 §73.4.1); add the TSC↔MONOTONIC sandwich to the same record and every stream in the table is alignable from the telemetry log alone, without touching the hot cores after the fact.

### 105.2.3 Suspects and fingerprints

Ch. 1 §1.4.3's table says what each mechanism *looks like* in the latency; this one says what it *leaves behind* in the evidence streams — the test of step 4:

```
   suspect                      fingerprint inside the window                                      sampled from / by               knob (Ch.)
   ──────────────────────────── ─────────────────────────────────────────────────────────────────── ─────────────────────────────── ───────────
   preemption / stray thread    sched:sched_switch prev=hot next=X on the hot CPU; nonvoluntary_    ftrace ring; Ch. 106 1 Hz        42, 45, 46
                                ctxt_switches +1                                                    sampler
   IRQ on the hot core          irq:irq_handler_entry / softirq_entry; /proc/interrupts row delta   ftrace; Ch. 106 sampler           42, 46
   IPI: TLB shootdown           tlb:tlb_flush reason=REMOTE_SHOOTDOWN; TLB column delta; the        ftrace (+ipi:ipi_send_cpu for    46 §58.4.3,
                                sender's munmap/mprotect/madvise in ITS flight recorder             the sender)                      23, 24
   IPI: function call (CAL)     irq_vectors:call_function_entry; CAL column delta; sender =         ftrace                           46
                                text_poke (a probe being armed), perf -C, cpufreq, vmstat…
   timer tick / hrtimer         timer:tick_stop success=0 dependency=…; hrtimer_expire_entry;        ftrace                           45, 46
                                LOC column delta (nohz_full fell back)
   RT throttling                sched_rt_period exceeded: "sched: RT throttling activated" in       dmesg; ftrace sched_switch      45
                                dmesg; a ~50 ms gap in the ring                                     to swapper
   THP / compaction / reclaim   compaction:mm_compaction_begin, vmscan:*; khugepaged in sched_       ftrace; /proc/vmstat deltas     15, 23
                                switch; thp_* counters delta; a minor fault (minflt +1) on the hot   (Ch. 106)
                                thread with a 4K→2M collapse behind it
   SMI                          a gap with NOTHING in the ftrace ring; MSR 0x34 SMI_COUNT delta;     Ch. 106 §106.4.2 (read from the   6 §6.4.4, 82
                                hwlat/osnoise "hw noise"                                             housekeeping core)
   C-state exit / frequency     first event after an idle gap on a *neighbour* (SMT sibling — 43);   turbostat / cpupower monitor     6, 43, 47
                                cpufreq_transition tracepoint; aperf/mperf ratio drop (Ch. 106)       sampled by Ch. 106
   LLC / memory-bandwidth       resctrl MBM/CMT for the neighbour's RMID spikes; LLC-miss rate on    Ch. 55 §55.4.2 monitor          44, 16
   neighbour                    the hot core up with IPC down (rdpmc, Ch. 111 §111.4.4)
   PTP step / servo             ptp4l "clockcheck" / phc2sys "step" in the journal; the TSC→UTC       Ch. 73; Ch. 106 clock signals    60
                                residual jumps                                                      (note: a step moves the VENUE
                                                                                                    comparison, not the TSC stages)
   NIC: pause / drop / ring     rx_missed / rx_no_buffer / pause frames delta; a gap in the          Ch. 107 sampler; capture (80)     57, 83
   full                         capture's hardware stamps with intact venue sequence numbers
   venue-side                   t_hw_rx − SendingTime jumps for THIS packet with flat neighbours;    Ch. 92 §92.4.5 feed-latency      74, 62
                                the stage stamps are all normal                                     series
   the message itself           replay reproduces the stage time (a pathological book depth, a       Ch. 101 replay; Ch. 5            25, 54
                                decode path never taken before); flight recorder shows the work
```

Reading the table is the method: a 41 µs stall with a `sched_switch` to `kworker/5:1` inside the window is a stray thread (go to Ch. 58 §58.4.3); the same stall with *nothing* in the ftrace ring, on a `nohz_full` core, is firmware (SMI) or a sibling's frequency event; the same stall with every stage stamp normal and a jump in `t_hw_rx − SendingTime` is the venue's, and the bundle goes to them.

### 105.2.4 Always-on, frozen on trigger

The evidence in the table comes from rings that are **always recording** — because the event cannot be predicted — and that are **frozen when the event is detected** — because a ring that keeps recording overwrites the evidence within milliseconds (a 2 MiB flight recorder at 58 cycles per 32-byte record is ~1.4 ms of history on a saturated thread; an 8 MiB per-CPU ftrace buffer on a quiet isolated core is seconds). Freezing has two shapes:

- **Stop:** `echo 0 > tracing_on` (or a `trace_marker` write with `rtla -t`'s stop-on-threshold — Ch. 58 §58.4.2), `perf record --switch-output=signal` on `SIGUSR2`, `perf record --snapshot` + `SIGUSR2` for Intel PT. The ring stops; a housekeeping process reads it; recording resumes when it is re-armed. Simple, and right for *kernel* rings that a housekeeping thread owns.
- **Swap:** for the hot thread's *own* ring, stopping is wrong — the hot thread must keep recording (the next message is already arriving, and the incident may have a second act). The thread **exchanges the ring pointer** for a spare (one `xchg`, §105.3: ~1,700 cycles including the detection's own stamps) and keeps writing into the spare; a drainer on a housekeeping core notices the frozen ring (~300 cycles later), writes it out, and hands it back as the next spare. No syscall on the hot thread, no wait, one branch per message to decide.

What triggers the freeze is Ch. 93 §93.4.4's *only* hot-path threshold — the per-message stage or end-to-end time over budget — plus a short list evaluated *off* the hot thread by the scraper/supervisor at their own cadence: a stall detector (the heartbeat/progress counter stopped — Ch. 96 §96.4.3), a gap or reject burst (Ch. 68 §68.4.4), a PTP desync (Ch. 73), a CMT/MBM spike (Ch. 55), any Ch. 106 invariant delta. Off-thread triggers freeze the kernel rings and *request* a flight-recorder swap through a flag the hot thread checks on its next message (one load, Ch. 93's segment). The design rule: **the hot thread never does more than one compare and, rarely, one exchange**; everything else is somebody else's core.

## 105.3 Measure it: the always-on record, the freeze, and the clock alignment

*Toolchain and machine: Appendix L.3. The listings were built with GCC 13.3 (`-O2 -std=c++20 -march=native -pthread`) and run on a 4-vCPU Intel Xeon VM (TSC 2.70 GHz, no PMU exposed) under Linux 6.18. The flight recorder and clock measurements are CPU-bound and transfer within the VM's `rdtscp` overhead (~46 cycles here vs ~25–35 on the reference machine); the `perf`/Intel PT overheads cannot be measured without a PMU and are labelled.*

**1. The always-on flight recorder.** A book update (one level, two fields) with and without a 32-byte record appended to the thread's ring, then the trigger: an injected 50 µs stall makes one iteration exceed a 2,000-cycle budget; the hot thread swaps rings and a drainer on another core notices:

```
   measurement (fr_ring.cpp)                                                    result
   ──────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────
   book update alone                                                              1.8 cycles/op
   book update + always-on record (rdtscp + 3 stores into a 2 MiB ring)          58.2 cycles/op   (≈ 46 of them are the rdtscp)
   detect (dt > budget) → ring pointer swapped, hot thread                       1,700 cycles  (≈ 0.63 µs; includes two rdtscp)
   drainer (other core, spinning on `frozen`) saw the frozen ring after            310 cycles  (≈ 0.11 µs)
   frozen ring: 65,536 records = 2 MiB written to disk by the drainer             ~1 ms on NVMe (representative); 0 on the hot thread
```

The record's cost is the stamp: with `rdtsc` instead of `rdtscp` (Ch. 19 §19.2.1 — an unordered read is fine for a record whose purpose is "roughly when") the per-record cost on the reference machine is ~25–30 cycles — 10 ns per record, a few percent of a 200 ns stage — and the three stores retire through the store buffer in the shadow of the next message (Ch. 12). The swap is an `xchg` plus a release store; the 1,700 cycles include the detection's two stamps and the first write into the cold spare ring (its first line was not in L1). This is the *entire* hot-path cost of having a frozen snapshot of the last 65k records waiting for the drainer.

**2. Aligning the clocks.** The §105.4.1 listing anchors the TSC to `CLOCK_MONOTONIC` with two sandwiches 0.5 s apart and checks the map:

```
   TSC rate 2.700000 GHz (from two anchors 0.5 s apart); anchor sandwich width 102 / 98 cycles (= ±18 ns)
   CLOCK_REALTIME = MONOTONIC + 1794234113484544763 ns; CLOCK_BOOTTIME = MONOTONIC + 0 ns (no suspend)
   residual of the TSC→MONOTONIC map at a third anchor: 1 ns (sandwich ±18 ns)
```

±18 ns is the alignment precision between the application's stamps and anything the kernel stamped in `CLOCK_MONOTONIC` (perf with `-k CLOCK_MONOTONIC`, the journal via BOOTTIME); ftrace in `x86-tsc` needs no conversion at all. Against a 41 µs window, 18 ns is noise — the fingerprint tests are unambiguous.

**3. The kernel and hardware rings (representative; no PMU in the VM).** `perf record` in overwrite mode costs the *sampled* core an NMI plus a stack walk per sample — Ch. 111 §111.4.4 puts the number at ~1–3 µs per sample with frame-pointer unwinding, so at 999 Hz it is 0.1–0.3 % of the core and one 1–3 µs gap per millisecond (which is why it is a *targeted-live* tool on an isolated core, not an always-on one) — and nothing when no sample fires. Intel PT in snapshot mode records every branch of the traced CPU into a per-CPU buffer with 2–15 % slowdown depending on code density (Intel's and `perf`'s documentation; a branchy decoder sits at the high end), zero when stopped, and decodes offline at roughly 10–100 µs of trace per second of decode time (`perf script --itrace` on a housekeeping box — never on the trading host). ftrace with the Ch. 58 §58.4.2 event set costs ~50–100 ns per event on the *traced* core and is silent when nothing happens — the right always-on kernel ring for an isolated core, because on a quiet core *nothing happens*.

## 105.4 Techniques

### 105.4.1 Reconstructing the timeline

```cpp
// clockmap.cpp — anchor the TSC to CLOCK_MONOTONIC and derive REALTIME/BOOTTIME offsets, with the error bound.
// Build: g++ -O2 -std=c++20 clockmap.cpp -o clockmap      (the Ch. 106 sampler runs this once a second and logs the anchors)
#include <cstdint>
#include <cstdio>
#include <time.h>
#include <unistd.h>
#include <x86intrin.h>
static inline std::uint64_t tsc() { unsigned a; return __rdtscp(&a); }
static inline std::uint64_t ns(clockid_t c) { timespec t; clock_gettime(c, &t); return std::uint64_t(t.tv_sec) * 1'000'000'000ull + t.tv_nsec; }
struct Anchor { std::uint64_t tsc, ns, width; };
static Anchor sandwich(clockid_t c) {                      // tsc | clock | tsc: the clock read happened inside [t0, t1]
    Anchor best{0, 0, ~0ull};
    for (int i = 0; i < 2000; ++i) { std::uint64_t t0 = tsc(); std::uint64_t n = ns(c); std::uint64_t t1 = tsc();
        if (t1 - t0 < best.width) best = {t0 + (t1 - t0) / 2, n, t1 - t0}; }
    return best;
}
int main() {
    Anchor a = sandwich(CLOCK_MONOTONIC); usleep(500'000); Anchor b = sandwich(CLOCK_MONOTONIC);
    double hz = double(b.tsc - a.tsc) / double(b.ns - a.ns) * 1e9;                      // the TSC rate, from the anchors themselves
    auto to_mono = [&](std::uint64_t t) { return a.ns + std::uint64_t((double(t) - double(a.tsc)) / hz * 1e9); };
    Anchor r = sandwich(CLOCK_REALTIME), bt = sandwich(CLOCK_BOOTTIME);
    std::printf("tsc_hz %.0f  mono_at_tsc %llu@%llu  realtime_off %lld  boottime_off %lld  err_ns %.0f\n", hz,
        (unsigned long long)a.ns, (unsigned long long)a.tsc, (long long)(std::int64_t(r.ns) - std::int64_t(to_mono(r.tsc))),
        (long long)(std::int64_t(bt.ns) - std::int64_t(to_mono(bt.tsc))), double(b.width) / hz * 1e9 / 2);
}
```

With that line logged every second (by the Ch. 106 sampler, beside the PHC cross-timestamp of Ch. 73 §73.4.1), the reconstruction for a window `[T0, T1]` in raw TSC is four commands:

```bash
# 1. the application's own evidence, already in TSC: the trace record and the frozen flight recorder
blogdecode trace.blog | awk -v t0=$T0 -v t1=$T1 '$3 >= t0-2e6 && $3 <= t1+2e6'          # ±0.7 ms of neighbours
frdecode frozen-cpu5.bin | awk -v t0=$T0 -v t1=$T1 '$1 >= t0 && $1 <= t1'                 # every record the thread wrote in the window
# 2. the kernel ring, stamped in x86-tsc (Ch. 58 §58.4.2): the raw numbers are directly comparable
awk -v t0=$T0 -v t1=$T1 '{ split($0, f, /[\[\]: ]+/); if (f[4]+0 >= t0 && f[4]+0 <= t1) print }' trace.txt
# 3. perf / Intel PT snapshot, stamped in CLOCK_MONOTONIC (-k CLOCK_MONOTONIC): convert the window, then filter
read HZ MONO_NS MONO_TSC < <(awk 'NR==1{print $2, $4}' anchors.log | tr '@' ' ')
M0=$(( MONO_NS + (T0 - MONO_TSC) * 1000000000 / HZ )); M1=$(( MONO_NS + (T1 - MONO_TSC) * 1000000000 / HZ ))
perf script -i perf.data --ns -F time,cpu,comm,ip,sym --time "$(printf '%d.%09d' $((M0/1000000000)) $((M0%1000000000))),$(printf '%d.%09d' $((M1/1000000000)) $((M1%1000000000)))"
# 4. the journal, stamped in CLOCK_BOOTTIME (= MONOTONIC here): the same window, µs resolution
journalctl -k -o short-monotonic --since=@$((M0/1000000000)) --until=@$((M1/1000000000 + 1))
```

The ±18 ns of step 3 and the µs rounding of step 4 are the error bars; the window is tens of microseconds; the question "what is inside it" has a sharp answer.

### 105.4.2 The flight recorder: always-on, frozen by a swap

```cpp
// flightrec.hpp — a per-thread ring of binary records (Ch. 91's record discipline), frozen on trigger by swapping the pointer.
#pragma once
#include <atomic>
#include <cstdint>
#include <x86intrin.h>

namespace fr {
struct Rec { std::uint64_t tsc; std::uint32_t site; std::uint32_t a; std::uint64_t b; std::uint64_t c; };  // 32 B
struct Ring { static constexpr std::size_t N = 1 << 16; std::uint64_t head = 0; alignas(64) Rec r[N]; };    // 2 MiB, single writer

struct Recorder {                                   // one per hot thread; the drainer owns the spare's lifecycle
    std::atomic<Ring*> cur;                         // the hot thread writes through this
    std::atomic<Ring*> frozen{nullptr};             // the parked ring, for the drainer
    std::atomic<Ring*> spare{nullptr};              // refilled by the drainer after it drained the frozen one
    inline void rec(std::uint32_t site, std::uint32_t a, std::uint64_t b, std::uint64_t c) {
        Ring* g = cur.load(std::memory_order_relaxed);
        Rec& x = g->r[g->head++ & (Ring::N - 1)]; x.tsc = __rdtsc(); x.site = site; x.a = a; x.b = b; x.c = c;
    }
    inline bool freeze() {                          // hot thread, on trigger: O(1), no syscall, no wait; false if no spare yet
        Ring* s = spare.exchange(nullptr, std::memory_order_acq_rel); if (!s) return false;
        Ring* old = cur.exchange(s, std::memory_order_acq_rel);
        frozen.store(old, std::memory_order_release); return true;
    }
    // drainer (housekeeping core): Ring* f = frozen.exchange(nullptr); if (f) { write(fd, f->r, sizeof f->r); spare.store(f); }
};
}  // namespace fr
```

Design notes that the listing's brevity hides:

- **What to record.** The sites are the things the thread *did* between stage stamps: the message type and sequence number at decode (`site=DECODE, a=type, b=seq`), each book level touched (`site=LEVEL, a=idx, b=qty_after`), the decision and its inputs, the order encoded, and — because they are the stray mechanisms — any slow-path branch taken (a gap detected, a snapshot requested, an allocation that should not have happened: Ch. 29 §29.4's allocation hook writes a record). The `corr` of the current message (Ch. 92) goes into `c` so the ring joins to the trace record. 32 bytes, binary, decoded offline with a site table exactly like Ch. 91's logger — this *is* Ch. 91's logger with "overwrite oldest" (Ch. 91 §91.6) as its policy and a freeze instead of a consumer.
- **The trigger is the stage compare the thread already does.** Ch. 93 §93.4.4: on each message, `if (t_end − t_start > budget) [[unlikely]] { rec.freeze(); write(ftrace_marker_fd, …); }` — one predicted branch, and on the rare true path a swap (~0.6 µs) plus Ch. 58 §58.4.2's two `write`s to stop the kernel ring. The off-thread triggers set a flag in the telemetry segment that the same branch also tests (`| segment.freeze_request`), so a PTP step or a CMT spike freezes the application ring at the next message boundary.
- **Freeze once, then re-arm from the drainer.** `freeze()` returns `false` when the drainer has not yet handed a spare back — the first event wins the evidence, the second act is in the *new* ring and the kernel ring, and a trigger storm cannot cost the hot thread more than one exchange per drained ring. The drainer writes the frozen ring to the incident directory (Appendix N), names it by `corr` and TSC, and returns it as the spare.
- **The same ring on a fault.** Ch. 100's handler dumps `cur`'s bytes raw — the last 65k records before the crash — so the flight recorder serves the spike and the crash with one mechanism. Put the rings in a `MAP_SHARED` segment marked `MADV_DONTDUMP` and the drainer can read a crashed thread's ring after the fact without the handler writing 2 MiB (Ch. 100 §100.4.3).

### 105.4.3 Kernel and hardware capture on the same trigger: ftrace, `perf record` in overwrite mode, Intel PT snapshots

The flight recorder says what the *thread* did; the kernel and the hardware say what happened *to* it. Three rings, each armed before the open and frozen by the same trigger:

```bash
# A. ftrace, per-CPU ring, TSC clock, the Ch. 58 §58.4.2 fingerprint event set — ALWAYS ON on the isolated cores (silent when quiet)
T=/sys/kernel/tracing; echo x86-tsc > $T/trace_clock; echo 20 > $T/tracing_cpumask; echo 16384 > $T/buffer_size_kb
echo 'sched:sched_switch irq:* irq_vectors:* tlb:tlb_flush timer:tick_stop timer:hrtimer_expire_entry ipi:ipi_send_cpu compaction:* signal:signal_deliver' > $T/set_event
echo 1 > $T/tracing_on                 # frozen by: the hot thread's `echo 0 > tracing_on` (fd opened at startup) or a trace_marker write + rtla -t
# B. perf in OVERWRITE mode on the hot core — TARGETED-LIVE (Ch. 111 §111.4.4): each sample costs the core an NMI; arm it for a
#    suspect session, not forever.  --switch-output=signal dumps the ring to a new perf.data.<ts> on SIGUSR2 and keeps going.
perf record -C 5 -e cycles:u -c 2000000 --call-graph fp -k CLOCK_MONOTONIC --overwrite --switch-output=signal -o /inc/perf.data -- sleep 36000 &
PERF_PID=$!                            # the trigger: kill -USR2 $PERF_PID  (from the drainer/supervisor, never the hot thread — it is a syscall + signal)
#    the same with a control fifo (no signal at all): perf record --control=fifo:/inc/ctl,/inc/ack ... ; echo snapshot > /inc/ctl
# C. Intel PT, snapshot mode, user space of the hot core — the EXACT control flow of the last few ms, decoded offline
perf record -e intel_pt/cyc,cyc_thresh=1,mtc,mtc_period=3/u -C 5 --snapshot=e -k CLOCK_MONOTONIC -o /inc/pt.data -- sleep 36000 &
PT_PID=$!                              # trigger: kill -USR2 $PT_PID → the per-CPU AUX buffer (default 4 MiB; -m,64M for more) is written out
#    decode on a HOUSEKEEPING BOX: perf script -i pt.data --itrace=i0ns -F time,ip,sym,srcline --time M0,M1   (Ch. 2 §2.4.4)
```

When to arm which: **A** always (the ftrace ring costs nothing on a core where nothing happens, and it is the ring that names the kernel's intrusions). **B** when the stage histogram says the *application* got slow without a kernel fingerprint — a sample every 2M cycles on `cycles:u` is one NMI per ~0.7 ms, and `--overwrite` keeps the last few seconds of IPs with stacks; the `-c` period rather than `-F` avoids the kernel's frequency-adaptive period changes. **C** for the deepest question — *which instructions executed* during the 41 µs — at the price of a 2–15 % slowdown while armed, so it is a market-open tool for a core under investigation, not a default. `PTWRITE` (Ice Lake+ with the `ptwrite` CPUID bit) lets the thread write a 64-bit value — the `corr` — into the PT stream at each stage so the decode is keyed to messages. All three stamp in `CLOCK_MONOTONIC` or the TSC, so §105.4.1 aligns them. And all three *freeze from a housekeeping core*: the hot thread's part is the one `write` to `tracing_on` (pre-opened fd) and the flag in the segment.

### 105.4.4 Testing the suspects: the decision table in practice

With the window reconstructed and the rings frozen, step 4 is mechanical. For a 41.3 µs decode→decide stall on CPU 5 at TSC `T0..T1`:

```
   question                                           command / evidence                                                        if yes
   ────────────────────────────────────────────────── ───────────────────────────────────────────────────────────────────────── ───────────────────────
   did the thread leave the CPU?                      ftrace: sched_switch prev_comm=strategy in [T0,T1]; Ch. 106: nonvoluntary   stray thread / RT
                                                      _ctxt_switches delta ≠ 0 in that second                                     throttle → Ch. 58 §58.4.3
   did an interrupt land?                             ftrace: irq_handler_entry / softirq_entry / irq_vectors:* in [T0,T1];       IRQ affinity drift →
                                                      /proc/interrupts column for CPU 5 moved (Ch. 106 §106.4.1 diff)              Ch. 53, 106
   was it a TLB shootdown?                            tlb:tlb_flush reason=TLB_REMOTE_SHOOTDOWN; the SENDER's flight recorder     your own allocator /
                                                      shows munmap/madvise in the same window (ipi:ipi_send_cpu names the CPU)    mprotect → Ch. 29–30
   did the tick come back?                            timer:tick_stop success=0 dependency=PERF_EVENTS|SCHED|…; LOC column        a perf counter / a
                                                      delta                                                                       second runnable task
   was the gap EMPTY?                                 no ftrace event in [T0,T1] on a nohz_full core; SMI_COUNT delta ≥ 1 in     firmware (SMI) →
                                                      the Ch. 106 sample covering it                                               Ch. 6 §6.4.4, 82
   did the core slow down rather than stop?           flight recorder shows ALL records in the window stretched ~uniformly;       frequency / thermal /
                                                      Ch. 106 aperf/mperf or cpufreq_transition; sibling (Ch. 54) woke up          sibling → Ch. 6, 54
   did memory get slow?                               flight recorder: the LEVEL records (book touches) stretched, DECODE not;    neighbour bandwidth /
                                                      Ch. 55 MBM for other RMIDs spiked; rdpmc LLC-miss rate up (Ch. 111)          LLC → Ch. 55, 17
   was the message itself unusual?                    flight recorder: more LEVEL records than usual (a deep book walk), a        Ch. 31 / 67: a data-
                                                      slow-path site; Ch. 101 replay of `corr` reproduces the time                  dependent path
   was it even us?                                    all five stage deltas normal; t_hw_rx − SendingTime jumped for this corr    venue / fabric → Ch. 92
                                                      only (Ch. 92 §92.4.5); Ch. 107 shows no drop                                 §92.4.5, Ch. 75
```

Each row is a `grep` over the frozen rings or a diff of two Ch. 106/107 samples, and every row has an exoneration as well as a conviction. The discipline that makes it work is *subtractive*, like Ch. 1 §1.4.3's lesson about the mechanisms themselves: rule out the kernel (rows 1–5) before blaming the hardware (6–7), rule out the hardware before blaming the message (8), and rule out the host before blaming the venue (9) — because the cheap fingerprints are the first rows, and a stall with a `sched_switch` in it needs no Intel PT decode.

### 105.4.5 From spike to incident: the bundle and the reproduction

A spike that has been attributed is still not finished. Two hand-offs close it:

- **The incident bundle (Appendix N).** The frozen rings, the Ch. 106/107 sample windows, the journal window, the anchors, the trace record and its neighbours, the venue-stamp series for the window, the host fingerprint (Appendix M.6), the build ID and `-frecord-gcc-switches` flags of every binary involved, the configuration hash (Ch. 95 §95.4.3) — collected by a bounded script on a housekeeping core within minutes, with a manifest and checksums, so that the attribution can be *checked* by someone who was not there and compared with the next occurrence. Appendix N is the checklist and the script skeleton; this chapter is why each item is on it.
- **Reproduction.** If the message was the mechanism (row 8), the capture (Ch. 101) replays `corr` through the core under `perf` or `rr` (Ch. 5 §5.4.2) and the decode or book path is fixed and gated (Ch. 111 §111.4.1). If the kernel or the host was (rows 1–7), the knob is turned (Ch. 58 §58.4.3) and the fingerprint is added to the readiness gate (Appendix M.2) and the Ch. 106 sampler's alert set so the *next* drift is caught before the open. If the venue was (row 9), the bundle's feed-latency series and the symmetry check (Ch. 92 §92.4.5) go to the venue with the packet's sequence number — the only thing they can act on.

## 105.5 Pitfalls & anti-patterns

- **Looking only at the application.** The stage stamps say *where*; the kernel ring says *who*. A 41 µs stall analysed from the flight recorder alone is "decode was slow" — true and useless. Always-on ftrace with the Ch. 58 event set on every isolated core, or the window is unexplainable.
- **Comparing clocks without converting.** `dmesg -T` is BOOTTIME rounded to the second (`--time-format=iso` to the µs); perf's default clock is `sched_clock`, not the TSC and not MONOTONIC; ftrace's default `local` clock is per-CPU ns. Set `x86-tsc`/`-k CLOCK_MONOTONIC` *before* the incident and log the anchors continuously (§105.2.2); a timeline assembled from unconverted stamps convicts the innocent.
- **A ring that keeps writing.** Without a freeze, the flight recorder has overwritten the window ~1.4 ms after the stall and the ftrace ring a few seconds later. The trigger is not optional, and it must be the hot thread's own compare (plus the off-thread flag), because nothing else sees the event in time.
- **Triggering on a mean or a window percentile.** The event is *one* message; a 1-minute p99.9 breach fires seconds late, after the evidence is gone. Trigger per message on the stage/end-to-end delta (Ch. 93 §93.4.4); let the window alerts page humans.
- **Stopping the hot thread's ring instead of swapping it.** A stopped ring misses the second act, and a thread that "pauses recording" has a branch per record. Swap (§105.4.2); let the drainer re-arm.
- **Freezing with a syscall or a signal from the hot thread.** `kill -USR2` to perf, `ioctl` on a perf fd, `write` to a *not-yet-open* `tracing_on` — each is a syscall-class event on the thread you are diagnosing. The hot thread does one pre-opened `write` at most; the drainer does the rest.
- **`perf record -C <isolated core>` running all day.** Each sample is an NMI and a stack walk on the hot core, and arming or re-arming it sends IPIs (Ch. 111 §111.4.4 measures; Ch. 58's catalog lists `PERF_EVENTS` as a tick-stop dependency). Targeted-live, for a session, with `-c` and `--overwrite`; not always-on.
- **Decoding Intel PT on the trading host.** The decode is CPU-heavy and runs for minutes per megabyte; it belongs on a housekeeping box, from the copied `pt.data` and the matched binary (build ID — Ch. 5 §5.4.1).
- **A bundle without provenance.** Rings and counters from a binary whose build ID and flags are not recorded cannot be re-analysed against the right symbols, and a bundle whose clocks were not anchored cannot be aligned. Appendix N.2–N.3 first, evidence second.
- **Blaming the venue first.** `t_hw_rx − SendingTime` jumps when *your* PTP steps too (row "PTP step" in §105.2.3 moves the venue comparison, not the TSC stages). Rule out the host — including its clock — before the bundle leaves the building.

## 105.6 Exercises & checklist

**Exercises**

1. Build `fr_ring.cpp` with `rdtsc` instead of `rdtscp` in `rec()` and measure the per-record cost; then shrink the record to 16 bytes and measure again. Where does the cost go?
2. Run `clockmap.cpp` once a second for an hour (log the line) and plot the residual of the first anchor's map against each later anchor. Then let `chronyd` or `phc2sys` *step* the clock (`chronyc makestep`) and show the residual jump — that is the PTP-step fingerprint of §105.2.3.
3. Arm the §105.4.3 ftrace ring on an isolated core running Ch. 58's `gapdetect`, then inject each mechanism of §105.4.4's table in turn (a `taskset`-ed busy loop onto the core; `perf stat -C` on it; a `munmap` storm from another core of the same process; `echo 1 > /proc/sys/vm/compact_memory`). For each, freeze on the detector's threshold and identify the mechanism from the ring alone, blind.
4. Wire `fr::Recorder` into the Ch. 96 pipeline's strategy process with the Ch. 93 budget compare as the trigger and a drainer in the supervisor. Replay a captured microburst (Ch. 101) with a 60 µs stall injected into one message's decode. Produce the §105.4.1 timeline for that `corr` and the §105.4.4 table's verdict (it should be "the message itself", and the replay should reproduce it).
5. On a box with Intel PT (`grep intel_pt /proc/cpuinfo`), arm the snapshot recorder on the strategy core for one replayed session, trigger on the stall of exercise 4, and decode the window on another machine with `--itrace=i0ns`. How many instructions executed in the 60 µs, and which function held the most?
6. Write the drainer's output into the Appendix N layout and run the N.5 collection script against it. Hand the bundle to a colleague who did not see the incident; can they reach the same verdict from the bundle alone?

**Checklist**

- [ ] Every hot thread has an `fr::Recorder` with sites at decode, each book touch, decision, encode, and every slow-path branch; records carry the current `corr`.
- [ ] The trigger is the per-message budget compare (Ch. 93 §93.4.4) OR'd with the segment's freeze-request flag; on fire: `freeze()` + one pre-opened `write` to `tracing_on`. Nothing else on the hot thread.
- [ ] A drainer on a housekeeping core writes frozen rings to the incident directory, named by `corr` and TSC, and re-arms the spare.
- [ ] ftrace on every isolated core: `x86-tsc`, the Ch. 58 event set plus `compaction:*`/`signal:*`, ≥ 16 MiB per CPU, always on.
- [ ] `perf record --overwrite --switch-output=signal -k CLOCK_MONOTONIC` and Intel PT `--snapshot` recipes in the runbook, armed per investigation, triggered from the drainer/supervisor.
- [ ] The Ch. 106 sampler logs the TSC↔MONOTONIC anchor and the PHC cross-timestamp every second.
- [ ] §105.4.4's table is the triage order, written down; rows 1–5 before 6–7 before 8 before 9.
- [ ] Each attributed spike ends in a bundle (Appendix N), a knob + gate entry, a replay-gated fix, or a venue ticket with the sequence number.

## 105.7 References

1. Linux kernel documentation: `Documentation/trace/ftrace.rst` (`trace_clock`, `tracing_on`, `trace_marker`, per-CPU buffers, `tracing_cpumask`), `Documentation/trace/events.rst`, `Documentation/tools/rtla/` (Ch. 58).
2. `perf-record(1)` — `--overwrite`, `--switch-output`, `--snapshot`, `--control=fifo`, `-k`/`--clockid`, `--aux-sample`; `perf-intel-pt(1)` — snapshot mode, `cyc`/`mtc` packets, PTWRITE, `--itrace` decoding options; `perf-script(1)` (`--time`, `--ns`).
3. Intel Corporation. *Intel® 64 and IA-32 Architectures Software Developer's Manual*, Vol. 3, Ch. 41 "Intel® Processor Trace" (packets, ToPA, PTWRITE); Vol. 3 Sec. 18.17 (invariant TSC).
4. Linux man pages: `clock_gettime(2)` (`CLOCK_MONOTONIC`, `CLOCK_BOOTTIME`, `CLOCK_REALTIME`), `dmesg(1)` (`--time-format`), `journalctl(1)` (`-o short-monotonic`, `--since=@`), `proc(5)` (`/proc/interrupts`, `/proc/softirqs`, `/proc/vmstat`).
5. Linux `Documentation/driver-api/ptp.rst` — `PTP_SYS_OFFSET_PRECISE` and cross-timestamping (Ch. 73).
6. Gregg, B. *Systems Performance*, 2nd ed., Addison-Wesley 2020 — Ch. 2 "Methodologies" (the USE and drill-down methods this chapter's loop specialises), Ch. 14–15 (`perf`, ftrace).
7. Corbet, J. "Deferred user-space stack unwinding" and related LWN coverage of perf's overwrite/snapshot modes.
8. Jain, R. *The Art of Computer Systems Performance Analysis*, Wiley 1991 — Ch. 2 "Common Mistakes" (comparing unconverted measurements).

## 105.8 Additional Reading

- Gregg, B. "Linux Performance Analysis in 60,000 Milliseconds" (Netflix TechBlog) — the triage-order idea, for a general server.
- Jane Street, *magic-trace* documentation — Intel PT snapshot tracing triggered from the application, the closest open-source relative of §105.4.3 C.
- Rostedt, S. "Finding Origins of Latencies Using Ftrace", Linux Plumbers / Embedded Linux Conference — the stop-on-threshold technique Ch. 58 builds on.
- Kleen, A. *simple-pt* and the `perf` Intel PT wiki pages — practical PT recipes and decode costs.
- Google SRE, *Site Reliability Engineering*, Ch. 13 "Effective Troubleshooting" — hypothesis testing against evidence, the non-HFT statement of §105.2.1.

---

*Next: Ch. 106 — Host Health Telemetry: Proving the Quiet Box Stayed Quiet, the first of two chapters that sample, every second and from a housekeeping core, the signals this chapter's suspect table is tested against: the isolation invariants of Ch. 53–58, scheduler and memory-system noise, frequency, thermal and SMIs, the clock, and the hardware beneath — with the sampler's own footprint measured so that the monitor never becomes the noise.*
