# Part VII — OS, Scheduling & Isolation

# Chapter 55 — Timers & Deadlines Without Jitter

> **Prerequisites:** Ch. 54 §54.4.2 (busy-poll vs block — the fork this chapter's two halves sit on either side of), Ch. 49 §49.4.2 (the hot loop that polls the deadline queue as one of its sources), Ch. 20 (`rdtsc`, invariant TSC, calibration — the loop's clock), Ch. 32 §32.4.3–§32.4.4 (intrusive lists and slot maps — the timer nodes' containers), Ch. 31 §31.4.2 (pools — where timer nodes come from), Ch. 37 §37.4.3 (power-of-two masks — the wheel's slot arithmetic), Ch. 102 §102.2.2 (time is an *input* to the deterministic core), Ch. 6 §6.3 and Ch. 59 §59.3 (`cyclictest` — the numbers this chapter finally explains).
>
> **Leads into:** Ch. 56–61 (the isolation layers that make the sleeping half's tail small), Ch. 62 (warming cadence as a timer), Ch. 70 §70.2.4 (the reorder-window timeout), Ch. 102 §102.4.3 (heartbeat timeouts), Ch. 103 (throttle windows and kill-switch timers), Ch. 104 (cancel-if-no-ack and unacked-order ageing — this chapter's largest customer), Ch. 109 (timer fires as sequenced, replayable events). Ch. 54 said a hot thread never sleeps and a housekeeping thread may; this chapter is what each of them does about *time*: the busy-poll core's deadline queue — 10⁴–10⁶ armed timers expired without a syscall — and the Linux timer stack beneath every thread that does sleep.

---

## 55.1 Why it matters: every order carries a deadline, and the kernel's clock is 50 µs late by default

A quoting engine with 2 000 instruments and ten working orders each has 20 000 live deadlines at any instant: an order's time-in-force, a cancel-if-no-ack guard on every send (Ch. 104), a quote refresh per instrument, a heartbeat per session (Ch. 102 §102.4.3, Appendix J.3), a throttle window per venue port (Ch. 103, Appendix K.4), a reorder-window timeout per feed line (Ch. 70 §70.2.4), a recovery deadline per gap (Ch. 72). Each is armed and — far more often — *cancelled* (the ack arrives, the quote is replaced) tens of thousands of times a second, and the busy loop of Ch. 49 must ask "has anything expired?" on every iteration without that question costing more than the work it protects. The book has had **no timer management at all**: Ch. 102 lists "timer fires" as an input to the deterministic core with no mechanism, Ch. 70 names a reorder timeout with no way to check it, Ch. 3 and Ch. 20 use `nanosleep` for pacing, and the cyclictest tables of Ch. 6 §6.3 and Ch. 59 §59.3 report wake-up latencies without saying what a wake-up *is*.

The second half is the kernel's. Every thread that is allowed to sleep — supervisor, telemetry scraper, logger flush, the housekeeping cores of Ch. 59's layout — sleeps on a timer, and Linux's default behaviour makes that timer **50 µs late on purpose**: `timer_slack_ns` defaults to 50 000 for every `SCHED_OTHER` thread so the kernel can coalesce wake-ups and save power. §55.3 measures a 200 µs periodic `clock_nanosleep` arriving at p50 **59 µs** late under the default and **9 µs** late with slack set to one nanosecond, and a `timerfd` that ignores the slack entirely; it also shows what `SCHED_FIFO` does to the tail, what a busy-polling thread's deadline accuracy is on a shared core (p99 2 µs, p99.9 1.6 ms — preemption), and the trap that turns a busy-polling `SCHED_FIFO` thread on a non-isolated core into a 38 ms stall every second (the RT throttle). §55.2 gives both mental models; §55.3 measures the deadline-queue structures (sorted list vs 4-ary heap vs hierarchical timing wheel) at 10⁴–10⁶ armed timers and the kernel's wake-up path; §55.4 builds the polled deadline queue, makes its fires replayable, and sets up the sleeping threads correctly.

## 55.2 Mental model

### 55.2.1 Two kinds of thread, two kinds of timer

```
   the busy-poll core (Ch. 54 §54.4.2: never sleeps)              the housekeeping thread (may sleep)
   ───────────────────────────────────────────────────            ──────────────────────────────────────────────────
   now = rdtsc()  once per loop iteration (Ch. 49 §49.4.2)         clock_nanosleep(TIMER_ABSTIME) / timerfd / futex-timeout
   deadline_queue.expire(now, fire)  — user space, no syscall       → kernel hrtimer armed on this CPU's clock-event device
   fire(timer) == push an EVENT into the core's input stream        → hardware interrupt at expiry (+ slack!) → wake-up → schedule
   precision: one loop iteration (tens of ns … 1 µs)                precision: slack + IRQ latency + scheduling latency (µs … ms)
   cost: O(1) per poll when nothing is due                          cost: a syscall, an interrupt, two context switches
   the queue IS the timer; the kernel is not involved               the kernel IS the timer; cyclictest measures exactly this path
```

The split follows Ch. 54 §54.4.2 exactly: a thread that busy-polls has a clock in hand every iteration and needs a *data structure*; a thread that blocks needs the *kernel* to wake it, and must know what that costs.

### 55.2.2 Deadline queues for the busy loop: list, heap, wheel

The operations and their frequencies in a trading engine: **arm** (every send, every quote), **cancel** (every ack — the most frequent operation of all; a TTL-style timer is far more often cancelled than fired), **poll** (every loop iteration, almost always finding nothing due), **fire** (rare, bursty at the open and at session events). Three structures:

- **Sorted intrusive list** (Ch. 32 §32.4.3): nodes linked in deadline order. Poll: look at the head — O(1). Cancel: unlink — O(1). Arm: *scan* to the insertion point — O(n), and with 10⁵ timers that is a 0.5 ms insert (§55.3). Only viable when deadlines are armed in nearly-sorted order (all TTLs equal → append at the tail is O(1)); a single out-of-order arm pays the scan.
- **Binary / 4-ary min-heap over node indices**, with each node recording its heap position so cancel is O(log n) (a `std::priority_queue` cannot cancel at all without lazy deletion, which leaves dead entries to be skipped at the top). Poll: compare the root's deadline — O(1). Arm and cancel: O(log n) with a cache miss per level once the heap is larger than L2 — at 10⁶ timers that is ~60 ns per cancel (§55.3). Fires come out in exact deadline order.
- **Hierarchical timing wheel** (Varghese & Lauck, 1987): an array of 2ᵇ slots per level, each slot an intrusive list; a timer *delta* ticks away goes into slot `(deadline >> (b·level)) & mask` of the lowest level that can hold it. Arm: compute the level and slot, push — O(1). Cancel: unlink — O(1). Poll: advance the current tick, process the slot(s) passed — O(1) per *tick* plus the fires. The costs: fires come out at tick granularity, not exact order (a 1 µs tick is fine for TTLs; it is not for a 50 ns schedule); far timers must be **cascaded** down a level when the wheel wraps, and a naive cascade moves a whole slot's worth of timers at once — at 10⁶ timers, 2 ms (§55.3's wheel p99.9), which is a latency spike the heap does not have. Linux's own wheel abandoned cascading in 4.8 (far timers simply get coarser, which is acceptable for timeouts that almost never fire); a user-space wheel either does the same, bounds cascade work per tick, or hands far timers to a heap.

The decision: **a wheel for the TTL-shaped bulk (arm/cancel-heavy, tolerant of tick granularity, bounded per-tick work), a heap for the few timers that need exact ordering or lie beyond the wheel's horizon, a sorted list only when arrival order is already sorted** — and all three over pooled, index-addressed nodes (Ch. 31, 32 §32.4.4) so a timer ID is a slot + generation and a cancelled-and-reused node cannot fire a stale callback.

### 55.2.3 Timer fires are inputs

Ch. 102 §102.2.2's rule — *time is an input* — makes a timer fire a **record in the ordered event stream**, not a callback: the loop reads `now` once (Ch. 49 §49.4.2), the queue emits `TimerFired{id, deadline, now}` events, the core applies them like any message, and the journal (Ch. 107) or the sequencer (Ch. 109) records them in sequence with everything else. Replaying the journal replays the fires *at the same positions* in the stream — which is the only way a timeout-driven decision ("cancel-if-no-ack fired before the ack arrived") is reproducible. A timer that calls into the core directly from the queue, or a core that reads the clock itself, is not replayable (§55.5).

### 55.2.4 The Linux timer stack beneath a sleeping thread

```
   nanosleep / clock_nanosleep / futex(FUTEX_WAIT, timeout) / epoll_wait(timeout) / timerfd
          │  expiry = requested + [0, timer_slack_ns]      ← hrtimer_set_expires_range_ns(): the kernel MAY fire anywhere in the window
          ▼                                                    (default 50 µs; 0 for SCHED_FIFO/RR/DEADLINE; prctl(PR_SET_TIMERSLACK) per thread)
   hrtimer (red-black tree per CPU, CLOCK_MONOTONIC base)  — high-resolution timers, ns programming of the clock-event device
          │  programs the local APIC timer (TSC-deadline mode on this hardware) for the earliest expiry (+ slack coalescing)
          ▼
   interrupt → hrtimer_interrupt → wake_up_process → the thread is runnable
          │  + IRQ entry/exit, + the scheduler's pick (FIFO: immediate if highest; OTHER: EEVDF fairness, possibly later)
          ▼
   context switch → the thread runs                          ← cyclictest's "latency" = this whole column (Ch. 6 §6.3, Ch. 59 §59.3)

   the OTHER timer: the timer wheel (add_timer/mod_timer) — jiffies granularity (CONFIG_HZ: 1–4 ms), non-cascading since 4.8,
   used for kernel TIMEOUTS (TCP retransmit, …), never for a user sleep; its tick is one of the Ch. 59 noise sources nohz_full removes.
```

Three facts to carry: (1) **slack is added to every user sleep except `timerfd`** — `timerfd_settime` arms its hrtimer with the exact expiry (and `TFD_TIMER_ABSTIME` for an absolute one); `clock_nanosleep`, `futex` timeouts and `epoll_wait`/`poll` timeouts go through `schedule_hrtimeout_range` and get the slack; (2) **`TIMER_ABSTIME` is how a periodic loop does not drift** — sleeping *until* `t₀ + k·T` instead of *for* `T` makes each wake-up's lateness independent rather than cumulative; (3) **the wake-up tail is the Ch. 56–61 story** — IRQ latency, C-state exit (Ch. 6), the scheduler's choice, other work on the core — which is why cyclictest's max drops from milliseconds to microseconds across Ch. 59 §59.3's isolation layers and why the busy-poll half exists at all.

## 55.3 Measure it: the deadline queue at 10⁴–10⁶ timers, and the kernel's wake-up

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -march=native`), run on the 4-vCPU Intel Xeon VM of Ch. 49 §49.3 (TSC 2.70 GHz, no isolation, hypervisor steal visible in every tail). The deadline-queue measurements are CPU- and cache-bound and transfer to the reference machine within the memory-latency ratio; the wake-up latencies are the VM's virtualized timer/IPI path (~9 µs floor) and will be ~2–5 µs on the reference box before isolation (Ch. 59 §59.3's table) — the *differences* between rows transfer. Labelled VM-measured.*

**1. Deadline-queue structures.** Three queues over the same pool of `Node{deadline, prev, next, pos, gen, armed}`: the sorted intrusive list (insert scanning from the tail — the common "newest deadline is latest" case), a 4-ary heap of indices with position tracking (O(log n) cancel), and a four-level × 256-slot hierarchical wheel with a 1 µs tick (horizon 2³² µs; farther timers held in an overflow list). Time is *virtual* so a 10 s horizon costs no wall time; the operations are arm N timers with uniform deadlines over 10 s, 200 × 1 000 **empty polls** (same tick, nothing due — the busy loop's common case), 200 000 **advancing polls** (one virtual µs each, with whatever expires), **cancel** half the live timers in random order (the ack arrives), **re-arm** them (the quote refresh), and an **expiry burst** (every timer armed for the same tick, then that tick polled).

```cpp
// timerq.cpp (the wheel; list and heap in the full file). Build: g++ -O2 -std=c++20 -march=native timerq.cpp -o timerq ; taskset -c 2 ./timerq
template <int LEVELS = 4, int BITS = 8> struct Wheel {
    static constexpr u32 SLOTS = 1u << BITS, MASK = SLOTS - 1;
    std::vector<Node>& n; u64 now; u32 head[LEVELS][SLOTS]; u32 far_head = NIL;     // slot heads: intrusive doubly-linked lists of node indices
    u32* slot_for(u64 d) { u64 delta = d > now ? d - now : 0;
        for (int l = 0; l < LEVELS; ++l) if (delta < (u64(1) << (BITS * (l + 1)))) return &head[l][(d >> (BITS * l)) & MASK];   // Ch. 37: shift + mask
        return nullptr; }                                                              // beyond the horizon → overflow list
    void arm(u32 i, u64 d) { n[i].deadline = d; n[i].armed = true; u32* s = slot_for(d); n[i].pos = s ? u32(s - &head[0][0]) : NIL; push(s ? s : &far_head, n, i); }   // O(1)
    void cancel(u32 i) { if (!n[i].armed) return; n[i].armed = false; unlink(n[i].pos == NIL ? &far_head : &head[0][0] + n[i].pos, n, i); }                   // O(1): the node knows its slot
    template <class F> void poll(u64 t, F&& fire) {                                   // O(1) per tick + the fires
        while (now < t) { ++now; u32 s0 = now & MASK; if (s0 == 0) cascade(1);        // level 0 wrapped: pull level 1's current slot down (bounded in §55.4.1)
            u32* hd = &head[0][s0]; while (*hd != NIL) { u32 i = *hd; cancel(i); fire(i); } } }
    void cascade(int l) { if (l >= LEVELS) { refill_far(); return; } u32 idx = (now >> (BITS * l)) & MASK; if (idx == 0) cascade(l + 1);
        u32* hd = &head[l][idx]; u32 i = *hd; *hd = NIL; while (i != NIL) { u32 nx = n[i].next; n[i].armed = false; arm(i, n[i].deadline); i = nx; } }   // re-arm each at a finer level
};
```

```
                     arm (ns)   cancel (ns)   re-arm (ns)   empty poll (ns)   +1 µs poll: mean / p99.9 (ns)   expiry burst: ns per timer fired
N = 10^4   list       11 287        3.3        17 308           0.4                12.6 /    28.9                      2.6
           heap4          11.3      15.8           15.9         0.4                12.7 /   132.6                      6.7
           wheel           5.6       3.2            5.1         0.0                13.2 /    71.9                      8.0
N = 10^5   list      531 441       23.3     1 168 287           0.4                13.3 /   175.6                      5.1
           heap4          11.8      33.0           17.5         0.4                15.6 /   405.9                      6.9
           wheel           6.0      10.7           13.2         0.0                14.2 /   146.7                     10.1
N = 10^6   list           (not run: O(n) insert — 10^6 × ~5·10^5 scans)
           heap4          13.5      64.4           25.1         0.4                69.3 / 1 303.7                      6.9
           wheel           6.2      24.5           17.6         0.0                27.7 / 2 254.1                     12.2
(the "+1 µs poll" columns include the ~12 ns rdtsc pair around each poll; "empty poll" is per poll over 1 000 polls)
```

Reading it:

- **An empty poll is free in all three structures** — 0–0.4 ns: one compare of the head's deadline (list, heap) or one check that the tick has not advanced (wheel). This is why the busy loop can ask "anything due?" every iteration (Ch. 49 §49.4.2) at any N.
- **The sorted list is disqualified by arm**: 11 µs at 10⁴, 0.5 ms at 10⁵, per insertion — one out-of-order arm (and TTLs are armed at *now + T* with varying T, so most are) scans half the list. Its O(1) cancel and perfectly ordered fires do not survive that.
- **The wheel's arm and cancel are 2–3× the heap's and stay flat** (5–6 ns arm; cancel 3 → 25 ns from 10⁴ to 10⁶ — that growth is the random node's cache miss, not the structure) while the heap's cancel grows with log n *and* a miss per level: 16 → 64 ns. At 20 000 arms and 20 000 cancels a second the difference is noise; at 2 M/s on a feed-driven quote refresh it is 100 µs/s of core — real, and the wheel's.
- **The wheel has a cascade spike; the heap does not.** The wheel's advancing-poll p99.9 at 10⁶ timers is 2.25 ms: every 65 536 ticks a level-2 slot holding ~6 500 timers is cascaded down in one poll, each a random node touch. The heap's worst poll (1.3 ms) is an unlucky run of expiries plus VM steal. §55.4.1 bounds the cascade; without that bound a wheel is a periodic stall.
- **Firing is cheap in all three** (3–12 ns per timer) and dominated by the fire callback's own cost in practice; the burst column is the cost of a session event that expires every order's guard at once — 10⁴ timers in 26–80 µs.

**2. The kernel's wake-up path.** A thread sleeping on a 200 µs period, lateness = actual − intended wake-up, 5 000 iterations each (`ktimer.cpp`, `spin.cpp`; `taskset -c 2`, the core shared with the rest of the VM):

```
                                                                 p50      p99     p99.9      max   (µs late)
clock_nanosleep(TIMER_ABSTIME), slack = default 50 000 ns, OTHER 59.0     75.1   2 435.9  3 187.0  ← the slack IS the median
timerfd + epoll_wait,            slack = default,            OTHER  9.2     26.1      47.6    295.1  ← timerfd ignores slack
clock_nanosleep(TIMER_ABSTIME), slack = 1 ns,                OTHER  9.2     31.6      66.3    243.8
timerfd + epoll_wait,            slack = 1 ns,                OTHER  9.2     29.7      63.7    209.8
clock_nanosleep(TIMER_ABSTIME), SCHED_FIFO 50 (slack forced 0)      9.1     21.6      33.2    207.7
timerfd + epoll_wait,            SCHED_FIFO 50                      9.0     19.3      26.7     34.9
busy-poll rdtsc/clock_gettime to the deadline, SCHED_OTHER          0.02     2.0   1 632.9  2 388.0  ← preempted on a shared core
busy-poll to the deadline, SCHED_FIFO 50, NON-isolated core         0.02 38 013   47 012   47 812   ← RT throttle: 50 ms off per second
```

Reading it:

- **The default slack is the median lateness.** 59 µs ≈ 50 µs of permitted slack plus the ~9 µs VM wake-up floor. Setting `PR_SET_TIMERSLACK` to 1 removes it exactly (9.2 µs) — a one-line `prctl` every sleeping thread in a latency-sensitive process should make, and the reason a supervisor's "100 µs heartbeat check" was never checking at 100 µs.
- **`timerfd` is unaffected by slack**, by construction (§55.2.4): the same 9.2 µs under the default. For a housekeeping loop that already has an `epoll`/io_uring loop (Ch. 63–65), a `timerfd` is both the slack-proof and the structurally cleaner timer.
- **`SCHED_FIFO` buys the tail, not the median**: p99.9 from 64–66 to 27–33 µs, max from 200–300 to 35–200 µs on this shared core; the median is the hardware/hypervisor floor either way. With isolation (Ch. 59 §59.3's lower rows) the max follows the median into single-digit microseconds — on bare metal.
- **Busy-polling is exact until it is preempted**: 20 *nanoseconds* median lateness, 2 µs at p99 (the other work on the shared vCPU), 1.6 ms at p99.9 — Ch. 61's noise, which isolation removes. That is the busy-poll half's promise and its condition.
- **The trap in the last row**: a `SCHED_FIFO` thread spinning at 100 % on a core that is *not* isolated hits `sched_rt_runtime_us = 950000` — the kernel suspends all RT tasks on that core for 50 ms of every second — so a busy-polling RT thread sees a **38 ms** stall once a second. Isolated cores (where nothing else needs the CPU) have the throttle disabled or set to −1 (Appendix C); a hot thread on a shared core must not be both RT and spinning.

## 55.4 Techniques

### 55.4.1 The polled deadline queue: wheel + heap over pooled nodes, bounded per tick

```cpp
// deadlines.hpp — the engine's deadline queue: a timing wheel for the TTL bulk, a 4-ary heap for exact/far timers, pooled nodes
// addressed by slot+generation (Ch. 32 §32.4.4), polled once per loop iteration, emitting events rather than calling back.
struct TimerId { std::uint64_t v; std::uint32_t slot() const { return std::uint32_t(v); } std::uint32_t gen() const { return std::uint32_t(v >> 32); } };
struct TimerFired { TimerId id; std::uint64_t deadline, now; std::uint32_t kind, ref; };          // the EVENT the core applies (Ch. 102 §102.2.2)
template <std::uint32_t CAP> class DeadlineQueue {
    std::vector<Node> nodes = std::vector<Node>(CAP); Wheel<4, 8> wheel{nodes, 0}; Heap4 heap{nodes}; std::uint32_t free_head = 0;
    static constexpr std::uint64_t WHEEL_HORIZON = std::uint64_t(1) << 24;                            // 16.7 s at a 1 µs tick; beyond → heap (exact order, no cascade)
    static constexpr std::uint32_t CASCADE_BUDGET = 64;                                               // nodes moved per poll: bounds the §55.3 spike
public:
    TimerId arm(std::uint64_t deadline_tick, std::uint32_t kind, std::uint32_t ref) {
        std::uint32_t s = free_head; Node& n = nodes[s]; free_head = n.next_free; n.kind = kind; n.ref = ref; ++n.gen;      // gen bump on every arm: old ids die
        if (deadline_tick - wheel.now < WHEEL_HORIZON) wheel.arm(s, deadline_tick); else heap.arm(s, deadline_tick);
        return TimerId{std::uint64_t(n.gen) << 32 | s};
    }
    bool cancel(TimerId id) {                                                                           // O(1), stale-id safe: one compare (Ch. 32 §32.4.4)
        Node& n = nodes[id.slot() & (CAP - 1)]; if (n.gen != id.gen() || !n.armed) return false;
        (n.in_heap ? heap.cancel(id.slot()) : wheel.cancel(id.slot())); release(id.slot()); return true;
    }
    template <class Emit> std::uint32_t expire(std::uint64_t now_tick, Emit&& emit) {                 // called once per loop iteration (Ch. 49 §49.4.2)
        std::uint32_t fired = 0;
        auto fire = [&](std::uint32_t s) { Node& n = nodes[s]; emit(TimerFired{TimerId{std::uint64_t(n.gen) << 32 | s}, n.deadline, now_tick, n.kind, n.ref}); release(s); ++fired; };
        wheel.poll_bounded(now_tick, fire, CASCADE_BUDGET);                                             // ticks advance; cascade work capped per call, remainder next poll
        heap.poll(now_tick, fire);
        return fired;
    }
private:
    void release(std::uint32_t s) { Node& n = nodes[s]; n.armed = false; n.next_free = free_head; free_head = s; }
};
```

- **Ticks, not nanoseconds.** The queue's clock is `rdtsc() >> k` (Ch. 20 — at 2.9 GHz, `>> 11` is a ~0.7 µs tick; calibrate once, Ch. 20 §20.3); one shift per poll, no division, no `clock_gettime`.
- **Bounded cascade.** `poll_bounded` moves at most `CASCADE_BUDGET` nodes from a wrapped higher-level slot per call and remembers where it stopped; a 6 500-node cascade becomes ~100 polls of 64 nodes, each a few hundred nanoseconds, instead of one 2 ms stall (§55.3). Timers in the partially-cascaded slot cannot be *early* (they are still in the future at the coarser level), only late by at most the remaining cascade — bounded and measured in exercise 2.
- **Stale ids are a compare.** Every `arm` bumps the node's generation, so a `TimerId` kept by the order table (Ch. 104) after the timer fired or was cancelled fails `cancel` harmlessly rather than cancelling the node's *new* occupant — the ABA of timers, and §55.5's "timer-ID reuse" pitfall.
- **Fires are events.** `emit` pushes `TimerFired` into the same input path as messages (Ch. 49 §49.4.2 step 3); nothing in the queue calls the core.
- **Per-tick work is bounded by construction**: ≤ one slot of due timers + ≤ `CASCADE_BUDGET` moves + the heap's due prefix. The worst case is a session event firing every order's guard at once (§55.3's burst row: ~10 ns each) — cap *that* too (`MAX_FIRES_PER_POLL`) if the core's per-event cost makes 20 000 fires in one iteration a problem; the rest fire next iteration, microseconds later.

### 55.4.2 Deadlines as data: TTLs, cancel-if-no-ack, quote refresh, heartbeats, throttles

Each timer *kind* is a row in the core's state, not a closure:

- **Cancel-if-no-ack (Ch. 104):** armed on every send with `ref = order slot`, cancelled on the ack; on fire the core marks the order `Unknown` and emits a cancel + an order-status request. Several thousand live at once; wheel.
- **Time-in-force / order TTL:** armed on ack with the venue's expiry; cancelled on fill/cancel; fire → cancel request. Wheel.
- **Quote refresh:** one per instrument, re-armed on every fire or on every update to the instrument (cancel + arm = ~10 ns). Wheel.
- **Heartbeat / session liveness (Ch. 102 §102.4.3, Appendix J.3):** one per session, long interval, needs exact ordering relative to nothing — wheel, or heap if beyond the horizon. On fire: send Heartbeat/TestRequest; a second fire without a reply is a session-dead event (Ch. 103's safe-state input).
- **Throttle windows (Ch. 103):** *not* a timer — a token bucket refilled from `now` on each check (Ch. 103 §103.4.3); timers only for "window reopens" notifications to a strategy that wants them.
- **Reorder-window / recovery deadlines (Ch. 70 §70.2.4, Ch. 72):** one per feed line / per gap; fire → declare the gap, start recovery. Wheel.

### 55.4.3 Replayable time: the fire in the journal, the clock as an input

The core applies `TimerFired` from the stream; the journal (Ch. 107) records it in sequence; the sequencer (Ch. 109) stamps it like any input. Two rules make it hold: (1) the core's *arm* calls go through the same output path as orders — `arm(kind, ref, deadline)` is an *output* record the shell executes on the real queue, so a replay arms the same timers at the same stream positions; (2) `now` reaches the core only inside events (`TimerFired.now`, each message's ingress stamp — Ch. 98), never from `rdtsc()` in core code. Ch. 102 §102.4.6's digest comparison between a live run and its replay is the test; a timer that is not an event shows up there as the first divergence.

### 55.4.4 Sleeping threads done right: slack, `TIMER_ABSTIME`, `timerfd`

```cpp
// housekeeping.cpp — a drift-free periodic loop for a thread that is allowed to sleep. Build: g++ -O2 -std=c++20 housekeeping.cpp -o hk
#include <sys/prctl.h>
#include <sys/timerfd.h>
#include <time.h>
void housekeeping_loop(std::chrono::nanoseconds period) {
    prctl(PR_SET_TIMERSLACK, 1);                                                   // 1 ns: no coalescing window (default 50 µs — §55.3). Per thread; inherited by children.
    timespec next; clock_gettime(CLOCK_MONOTONIC, &next);
    for (;;) {
        next.tv_nsec += period.count(); while (next.tv_nsec >= 1'000'000'000) { next.tv_nsec -= 1'000'000'000; ++next.tv_sec; }   // absolute schedule: no drift
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, nullptr) == EINTR) {}                                        // sleep UNTIL, not FOR
        do_housekeeping();                                                                                                        // scrape telemetry, flush logs, check heartbeats
    }
}
// Or, inside an epoll/io_uring loop: a timerfd carries no slack and no drift —
//   int tfd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
//   itimerspec its{ .it_interval = {0, period}, .it_value = first_absolute_deadline }; timerfd_settime(tfd, TFD_TIMER_ABSTIME, &its, nullptr);
//   … epoll_wait → read(tfd, &expirations, 8): the count says how many periods were missed, so a stall is visible, not silently absorbed.
```

- **Set the slack per thread** (`prctl` is per thread; `/proc/<pid>/timerslack_ns` can set it from outside for a running process — handy for a third-party component). RT policies force it to 0 regardless.
- **Absolute deadlines** for anything periodic (heartbeat senders, telemetry scrapers, the capture flusher), so lateness does not accumulate and the `timerfd` expiration count tells you when the thread fell behind.
- **`SCHED_FIFO` for housekeeping that must be on time** — a heartbeat sender whose lateness looks like a dead process to the peer — *on a core where the RT throttle is disabled or the thread sleeps most of the time* (§55.3's last row). Everything else stays `SCHED_OTHER` with slack 1.
- **What a wake-up costs the hot cores**: nothing, if the sleeping threads are on housekeeping cores (Ch. 59's layout) — their hrtimer interrupts land on *their* CPUs. A sleeping thread pinned to an isolated hot core brings its timer interrupt with it, which is Ch. 61's catalog audit (§61.4.1) in one line: `nohz_full` cores should show no hrtimer activity but the hot thread's own.

### 55.4.5 Measuring your timers in production

- **Deadline-queue health** (Ch. 99): live timers, arms/cancels/fires per interval, max fires per poll, max cascade work per poll, and **fire lateness** = `now − deadline` at fire time in ticks — a histogram whose tail is the loop's iteration time, i.e. a free measurement of how long the hot loop's longest iteration was.
- **Sleeping-thread lateness**: every periodic thread records `actual − intended` (the §55.3 metric) into its own histogram; a p99 drifting up is a shared core getting busier, visible before it matters.
- **cyclictest as the acceptance test** for the housekeeping cores' configuration (Ch. 59 §59.3), *and* `rtla timerlat` (Ch. 61) to attribute a wake-up's lateness to IRQ vs scheduler on the cores that still carry interrupts.

## 55.5 Pitfalls & anti-patterns

- **Reading the clock inside the deterministic core.** Any `rdtsc`/`clock_gettime` in `apply()` makes the output depend on scheduling and kills replay (Ch. 102 §102.2.2); time arrives as `TimerFired.now` and message stamps (§55.4.3).
- **Timers as callbacks.** A closure fired from the queue bypasses the event stream, the journal and the sequencer; it cannot be replayed or reordered deterministically. Emit an event; let the core apply it.
- **`std::this_thread::sleep_for` / `nanosleep` on a hot thread** — a context switch plus up to 50 µs of slack (Ch. 54 §54.5, §55.3). Hot threads poll the deadline queue; they do not sleep.
- **`epoll_wait(…, timeout > 0)` on the hot path** — the same slack-bearing sleep in disguise. A hot loop that must use epoll uses a zero timeout (Ch. 63) or `timerfd` on a housekeeping thread.
- **Forgetting the slack entirely.** Every default-policy sleeping thread is 50 µs late at the median (§55.3). `prctl(PR_SET_TIMERSLACK, 1)` in every thread's prologue; check `/proc/<pid>/task/*/timerslack_ns` in the Appendix M host fingerprint.
- **Relative sleeps for periodic work** — drift accumulates and a stall is absorbed silently. `TIMER_ABSTIME` or `timerfd` with `TFD_TIMER_ABSTIME` (§55.4.4).
- **`SCHED_FIFO` + busy-poll on a shared core** — the RT throttle's 50 ms stall per second (§55.3). RT spinning belongs on isolated cores with the throttle disabled (Appendix C); elsewhere, spin as `SCHED_OTHER` or sleep.
- **Unbounded wheel cascading.** A 10⁶-timer wheel that cascades a whole slot per wrap stalls for milliseconds (§55.3). Bound the work per poll (§55.4.1) or do what the kernel did in 4.8 and accept coarser far timers.
- **A `std::priority_queue` of timers.** No cancel without lazy deletion; lazily-deleted TTL timers accumulate (most TTLs are cancelled) until the heap is 90 % tombstones and every poll skips dozens. A heap with position tracking, or the wheel.
- **Timer-ID reuse.** A cancelled node re-armed for a different order, then cancelled again by the *first* order's stale id — the second order's guard silently disappears. Slot + generation (§55.4.1, Ch. 32 §32.4.4).
- **A sorted list for arbitrary deadlines.** O(n) arm — 0.5 ms at 10⁵ timers (§55.3). Only for deadlines that arrive sorted (uniform TTL → append).
- **Trusting cyclictest's median.** The median is the hardware floor; the engineering is in the max (Ch. 59 §59.3), and a 200 µs max on a heartbeat thread is a false "dead peer" once an hour.

## 55.6 Exercises & checklist

1. Reproduce §55.3's deadline-queue table on an isolated core; add a Vyukov-style hashed wheel (single level, 2¹⁶ slots, per-slot deadline check) and place it in the table.
2. Implement `poll_bounded` with `CASCADE_BUDGET` = 16, 64, 256 at 10⁶ timers; measure the poll p99.9 and the maximum *lateness* a cascaded timer suffers. Pick the budget that keeps both under 2 µs.
3. Run `ktimer.cpp` on an isolated housekeeping core under `nohz_full` with slack 1 and `SCHED_FIFO`; compare with cyclictest on the same core (`-p 80 -i 200 -h 400`). They should agree.
4. Set the slack from outside (`echo 1 > /proc/<pid>/timerslack_ns`) on a running `SCHED_OTHER` sleeper and watch its lateness histogram change live; then confirm with `rtla timerlat` where the remaining lateness comes from.
5. Wire the `DeadlineQueue` into the Ch. 49 `HotLoop`, arm a cancel-if-no-ack guard on every send in a replay harness (Ch. 107), delay the acks past the guard for 1 % of orders, and verify the replay's digest (Ch. 102 §102.4.6) matches the live run with the fires in the journal — then remove the fires from the journal and show the first divergence.
6. Count the live timers in a real engine at the open from §55.4.5's telemetry; estimate the arm/cancel rate and decide, with §55.3's per-operation costs, whether the heap's log n would have been visible.

**Checklist**

- [ ] Hot threads never sleep; they poll a user-space deadline queue once per loop iteration with one `rdtsc`-derived tick.
- [ ] The queue is a timing wheel (TTL bulk) plus a heap (exact/far) over pooled nodes addressed by slot + generation; cancel is O(1) and stale-id safe.
- [ ] Cascade work and fires per poll are bounded; the bound and the resulting maximum lateness are measured.
- [ ] Timer fires are events in the input stream and the journal; arms are output records; the core never reads a clock.
- [ ] Every sleeping thread sets `PR_SET_TIMERSLACK` to 1 (or is RT), uses `TIMER_ABSTIME`/`timerfd` for periodic work, and records its lateness.
- [ ] RT + busy-poll only on isolated cores with the RT throttle disabled.
- [ ] Sleeping threads and their timer interrupts live on housekeeping cores; `nohz_full` cores show no foreign hrtimers.
- [ ] Deadline-queue and lateness telemetry are published (Ch. 99); cyclictest/`rtla timerlat` are the acceptance tests for the housekeeping cores.

## 55.7 References

- Varghese, G., Lauck, T. "Hashed and Hierarchical Timing Wheels: Data Structures for the Efficient Implementation of a Timer Facility." SOSP 1987 / *IEEE/ACM ToN* 1997 — the wheel, its variants and their complexity.
- Linux kernel documentation: `Documentation/timers/` (hrtimers, timekeeping, `timer_slack`), `Documentation/scheduler/sched-rt-group.rst` (the RT throttle), `Documentation/timers/no_hz.rst`; Gleixner, T., Niehaus, D. "Hrtimers and Beyond: Transforming the Linux Time Subsystems." OLS 2006; Gleixner's 2016 LKML series "timers: Refactor the timer wheel" (why the kernel wheel stopped cascading).
- `man 2 clock_nanosleep`, `man 2 timerfd_create`, `man 2 prctl` (`PR_SET_TIMERSLACK`), `man 7 time`, `man 7 sched`.
- Intel SDM Vol. 3, §"TSC-Deadline Mode" (what the APIC timer the hrtimer programs actually is).
- `rt-tests` (`cyclictest`) and `rtla timerlat` documentation — the measurement tools for §55.3's second table.

## 55.8 Additional Reading

- Brown, A. "Timer Wheels at Scale" (CppCon talks on user-space timers; also the Netty `HashedWheelTimer` and Kafka purgatory designs) — production user-space wheels and their tuning.
- Love, R. *Linux Kernel Development*, 3rd ed., Ch. 13 (Timers and Time Management) — the kernel side, readable.
- de Oliveira, D. B. "Real-time Linux Analysis tools: `rtla`" (kernel docs and 2022 talks) — attributing a wake-up's lateness.
- Aeron's `DeadlineTimerWheel` (Agrona) source — a single-level wheel polled from a duty cycle, with the exact bounded-work-per-poll interface of §55.4.1.
- Dean, J. "Achieving Rapid Response Times in Large Online Services" (2012) — hedged requests as deadline-driven design at a different scale.

---

*Next: Ch. 56 — Thread & Interrupt Pinning, the concrete mechanics behind "keep the thread on-core": CPU affinity (`taskset`/`sched_setaffinity`), IRQ affinity, and isolating the hot core so nothing — no thread, no interrupt — shares it.*
