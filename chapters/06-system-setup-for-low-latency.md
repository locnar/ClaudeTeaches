# Part I — Foundations & Methodology

# Chapter 6 — System Setup for Low Latency

> **Prerequisites:** Ch. 1–4. You think in distributions (Ch. 1), measure with the PMU (Ch. 2), benchmark honestly (Ch. 3), and can read codegen (Ch. 4). For the hands-on parts: root on a Linux box you're allowed to reconfigure (BIOS access ideal), `cpupower`, `cyclictest` (`rt-tests` package), `lscpu`, and `hwloc`/`lstopo`.
>
> **Leads into:** This chapter sets the *foundation* every later measurement stands on — a noisy box makes Ch. 2–3 lie. It introduces knobs developed in depth later: thread/IRQ pinning (Ch. 42), SMT (Ch. 43), real-time scheduling and `isolcpus`/`nohz_full` (Ch. 45), keeping the path warm (Ch. 46), and huge pages (Ch. 15). **Appendix C** is the copy-paste, ordered checklist; this chapter is the *why*.

---

## 6.1 Why it matters: the box is part of the program

The first four chapters treated performance as a property of *code*. It isn't — not entirely. The same binary, on the same silicon, can show a p99.9 of 1.2 µs or 30 µs depending entirely on how the machine underneath it is configured. A modern server is, by default, tuned for the opposite of what you want: it is optimized for **throughput, energy efficiency, and fairness** across many tenants — sleeping idle cores to save power, ramping clocks up and down with load, balancing interrupts across all CPUs, migrating threads for fairness, and running every speculative-execution mitigation the kernel ships. Every one of those defaults is a *jitter source* (Ch. 1's §1.4.3) on a latency-critical box.

The insight that reorganizes this chapter: **most low-latency system tuning is about removing *variance*, not adding *speed*.** A power-saving idle state doesn't make your code slower on average — it makes the *first event after idle* slow, because the core has to wake up and re-clock (Ch. 46). Frequency scaling doesn't lower your throughput — it makes the *same code take a different number of nanoseconds run-to-run*, which is exactly the unpredictability that loses races and ruins benchmarks (Ch. 3). The enemy is the long right tail, and the box's default "smart" power and fairness features are some of its biggest contributors.

There's also a hard prerequisite buried here: **you cannot trust Ch. 2–3's measurements on an untuned box.** If the clock is ramping and threads are migrating and interrupts are landing on your hot core, your benchmark variance reflects the *machine*, not your code, and every conclusion you draw is suspect. So system setup is not a late-stage deployment concern — it is the ground you have to level *before* the methodology of the previous chapters even works. That's why it closes Part I (Foundations & Methodology) rather than sitting in Part VII with the rest of the OS material: a quiet machine is a foundation, not an optimization.

The trade-off is explicit and worth stating up front: you are **spending throughput, energy, and often whole cores to buy determinism.** A tuned HFT box runs hot, draws more power, leaves cores deliberately idle (Ch. 1's "idle cores are latency insurance"), and disables features that would help a general-purpose workload. That is the right trade for the tick-to-trade path and the wrong trade for your build server. Knowing *which box is which* is the meta-skill.

---

## 6.2 Mental model

### 6.2.1 C-states, P-states, turbo, and frequency scaling

A modern x86 core constantly adjusts its power and clock. Two orthogonal mechanisms, both jitter sources:

**C-states (idle states)** — how deeply an *idle* core sleeps. `C0` is active; `C1`, `C1E`, `C3`, `C6`… progressively power down more of the core (flush caches, gate clocks, drop voltage) to save energy, at the cost of **exit latency**: a core in a deep C-state takes longer — potentially **tens of microseconds** — to return to `C0` and execute your code. This is poison for a hot path that wakes on a rare event: the *first* message after a quiet spell pays the full wake-up cost, landing squarely in your tail. The fix is to forbid deep C-states (cap at `C1`, or busy-poll so the core never idles — Ch. 41, 46).

**P-states (performance states / DVFS)** — the voltage/frequency operating point of an *active* core, managed on Intel by the `intel_pstate` driver and a *governor*. The default governors (`powersave`, `schedutil`) ramp frequency with load: your code runs at 1.0 GHz when idle-ish and scales up under load. **Turbo Boost** goes further, opportunistically clocking *above* base frequency when thermal/power headroom allows — but how far, and for how long, depends on how many cores are active, the temperature, and power limits. The consequence for us is brutal: **the same instruction sequence takes a different number of nanoseconds depending on the clock**, and the clock depends on history you don't control. Ch. 3 already bit you with this; the fix is to *pin the frequency*: governor `performance` and, for the tightest determinism, **disable turbo** so the clock is a constant, not a variable. You give up peak single-core speed to get a number you can trust.

```
   default (throughput/efficiency)        low-latency (determinism)
   -------------------------------        -------------------------
   deep C-states (C6): big wake cost  ->  cap at C1 / busy-poll (no sleep)
   governor ramps freq with load      ->  governor = performance (fixed)
   turbo: clock varies with thermal   ->  turbo off (constant clock)
   result: low power, high jitter         result: high power, low jitter
```

### 6.2.2 Hyperthreading and CPU topology

**Simultaneous Multi-Threading (SMT)** — Intel's *Hyper-Threading* — presents one physical core as two logical CPUs that **share** the core's execution resources: execution ports, L1/L2 caches, the µop cache, store buffers, TLBs (Ch. 43 dissects which resources are partitioned vs competitively shared). For a throughput workload this is a win: when one thread stalls on memory, the other uses the idle execution units. For a *latency* workload it is usually a liability: your hot thread now **contends** for its own core's resources with whatever runs on the sibling, and that contention is exactly the kind of unpredictable interference that fattens the tail. The common HFT posture is to **leave the SMT sibling of a hot core idle, or disable HT entirely** on hot cores — a full chapter's nuance (Ch. 43), but the headline is: shared resources mean shared jitter.

To reason about any of this you must know the machine's **topology** — the mapping of logical CPUs to physical cores, to L1/L2/L3 cache domains, to NUMA nodes (Ch. 16), to PCIe roots (Ch. 66). Crucially, **logical-CPU numbering is not intuitive**: CPU 0 and CPU 1 may be two threads of the *same* physical core, or two different cores, depending on the BIOS's enumeration. Pinning your hot thread to "CPU 1" without checking the topology can accidentally place it on the sibling of a busy core, or on a remote NUMA node. §6.4.2's discovery tools exist precisely so you pin based on the real layout, not a guess.

### 6.2.3 Speculative-execution mitigations (Spectre/Meltdown/retpoline)

Starting in 2018, a family of **speculative-execution side-channel vulnerabilities** — Spectre, Meltdown, MDS, L1TF, Retbleed, and relatives — forced CPU vendors and the kernel to ship **mitigations** that deliberately slow down or restrict the very speculation that makes modern cores fast. The relevant ones for latency:

- **Retpoline / IBRS / eIBRS** — defenses against branch-target-injection (Spectre v2) that constrain indirect-branch speculation. They add cost to **indirect calls** — virtual dispatch, function pointers, PLT calls (Ch. 14) — which is exactly the kind of thing on a feed-handler's hot path.
- **KPTI (Kernel Page-Table Isolation)** — the Meltdown fix; separates kernel and user page tables, making **every syscall and interrupt more expensive** (extra TLB/page-table work — Ch. 15, 41). It taxes precisely the kernel-boundary crossings you're already trying to eliminate.
- **MDS/TAA/L1TF flushes** — buffer/cache flushes on context switch or VM exit, adding per-transition cost.

These mitigations exist for a real reason: they defend against information leakage across security boundaries. The judgment call for an HFT box is that an **isolated, single-tenant, colocated machine running only your trusted code may not share a meaningful security boundary with an attacker** — there's no hostile co-tenant on the core to leak to. In that specific, carefully-reasoned setting, shops often **selectively disable** some mitigations (via the kernel cmdline `mitigations=off` or finer-grained per-vuln flags) to reclaim the latency, *accepting and documenting the security trade-off*. This is **not** generic advice — on a multi-tenant, cloud, or internet-facing box it would be reckless (Ch. 72 treats the security side seriously). The point of this section is that you should be able to **measure** each mitigation's cost (§6.3) and make the call deliberately, not flip `mitigations=off` as a cargo-cult.

---

## 6.3 Measure it: jitter before/after tuning

The right way to *see* system jitter — independent of any application — is **`cyclictest`** (from `rt-tests`), the standard tool for measuring scheduling/wake-up latency. It sleeps a thread for a fixed interval, then measures how late the wake-up actually was versus the intended time; the distribution of that lateness *is* the machine's jitter floor. (Note the kinship with Ch. 1: `cyclictest` reports min/avg/**max**, and the **max** is the number that matters — it's the worst stall a perfectly-timed thread would suffer from the OS and hardware alone.)

```bash
# Measure wake-up latency: 1 thread per CPU, 10us interval, highest RT prio, for a while.
# -m locks memory (no page-fault jitter, Ch.22); -p99 RT priority; -h builds a histogram.
sudo cyclictest --smp -p99 -m -i 10 -h 200 -D 60s
```

Run it twice — once on the box as shipped, once after applying the §6.4 tuning — and compare the **max** and the tail of the histogram. Representative results, reference machine **Intel Xeon Gold 6326** (Ice Lake-SP), Linux 6.x (illustrative — your hardware and kernel decide the real numbers; the *shape* of the improvement is the point):

```
                                   min      avg      max
untuned (defaults):                2 us      9 us   3,180 us   <- the max is the story
  (deep C-states, ondemand gov,
   irqbalance on, no isolation)

tuned (perf gov, C1 cap, IRQs
  steered off core, isolcpus,
  nohz_full, THP off, mitigations
  reviewed):                       1 us      1 us       4 us   <- ~800x tighter tail
```

Read it the Ch. 1 way: the *average* barely mattered (9 µs vs 1 µs); the **max collapsed from ~3.2 ms to ~4 µs**. That three-millisecond outlier on the untuned box — a deep C-state wake plus a stray interrupt plus a scheduler migration, all landing together — is a catastrophically lost race, and *nothing in your code* could have prevented it. Only the box tuning did. This single before/after is the entire justification for the chapter.

To attribute the improvement to specific knobs, change **one at a time** and re-measure (the Ch. 2–3 discipline applies to system tuning too): disable turbo alone, then add the governor change, then C-state capping, then IRQ steering, then isolation. Each step should move the tail; if one doesn't, you've learned that knob doesn't matter for *your* workload on *your* hardware — which is itself worth knowing. To measure a *specific mitigation's* cost, boot with and without its flag and compare a syscall-heavy or indirect-call-heavy microbenchmark (Ch. 3) — not just `cyclictest`, since mitigations tax kernel crossings and indirect branches specifically (§6.2.3).

---

## 6.4 Techniques

### 6.4.1 BIOS/firmware tuning for determinism

Some of the most important knobs live in **firmware**, below the OS, and can't be changed at runtime. The determinism-oriented settings (exact names vary by vendor — Appendix C maps the common ones):

- **C-states:** disable deep package/core C-states (allow `C0`/`C1` only), or set the BIOS power profile to "Maximum Performance" / "Low Latency." Eliminates the multi-µs wake-up tail (§6.2.1).
- **P-states / turbo / SpeedStep:** many shops set a **fixed frequency** (disable SpeedStep/Cool'n'Quiet and turbo) so the clock is constant. Others keep turbo but pin the governor and accept bounded variance — measure and decide (§6.3).
- **Hyper-Threading:** disable on hot cores (or globally) if your analysis (Ch. 43) says the sibling contention hurts more than HT helps.
- **NUMA / sub-NUMA clustering / snoop mode:** set the memory-interleave and snoop policy appropriately for your access pattern (Ch. 16); on a single-socket box, ensure it's presented as one node.
- **Uncore/power limits, RAPL, fan/thermal profile:** raise power/thermal limits so turbo (if used) doesn't throttle mid-burst; a thermally-throttled core is a tail event.

The principle: **firmware first**, because no amount of `sysctl` can undo a deep-C-state wake the BIOS allows. Firmware sets the floor; the OS knobs tune above it.

### 6.4.2 Topology discovery (`lscpu`, `hwloc`)

Before you pin anything (Ch. 42), map the machine. Two essential tools:

```bash
lscpu                 # summary: sockets, cores/socket, threads/core, cache sizes, NUMA
lscpu --extended      # per-logical-CPU: which CORE, SOCKET, NODE each CPU belongs to
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list   # CPU0's SMT sibling(s)
lstopo --of console   # hwloc's ASCII topology map: cores, caches, NUMA, PCIe/NIC
```

`lscpu --extended` is the one that saves you: it shows, per logical CPU, the physical `CORE`, `SOCKET`, and NUMA `NODE`. From it you learn, e.g., that CPUs `0` and `32` are the two SMT siblings of physical core 0 — so if you pin your hot thread to CPU 0, you must leave CPU 32 idle (or put only housekeeping on it) to avoid sibling contention (§6.2.2, Ch. 43). `lstopo` renders the whole hierarchy graphically, including **which NUMA node your NIC hangs off** (Ch. 16, 66) — critical, because you want your feed-handler thread and its NIC on the *same* node to avoid cross-socket latency. **Pin based on this map, never on raw CPU numbers.** A guess here silently places your hot thread on a sibling or a remote node and you spend a week chasing a "code" regression that was a pinning mistake.

### 6.4.3 Building a quiet machine; selectively disabling mitigations on isolated boxes

With firmware set and topology known, the OS-level knobs that build a quiet core. Most are kernel-cmdline (require reboot) or runtime `sysctl`/`sysfs`; **Appendix C** is the ordered, copy-paste checklist — here is the *why* behind the headline ones, each pointing at its deep-dive chapter:

- **Frequency:** `cpupower frequency-set -g performance` and disable turbo (`echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo`). Constant clock (§6.2.1).
- **Idle:** cap C-states via cmdline `intel_idle.max_cstate=1 processor.max_cstate=1 idle=poll` (or per-core via the `cpuidle` sysfs / a busy-poll design — Ch. 41, 46).
- **Core isolation:** `isolcpus=`, `nohz_full=`, `rcu_nocbs=` on the hot cores remove them from the scheduler's general pool, stop the periodic timer tick, and offload RCU callbacks — the core runs *your* thread and almost nothing else (Ch. 45).
- **Interrupts:** stop `irqbalance` and **steer IRQs off** the hot cores (`/proc/irq/*/smp_affinity`), so no device interrupt lands on your hot thread (Ch. 42).
- **Huge pages / THP:** disable *transparent* huge pages (their background compaction is a jitter source) and use *explicit* huge pages instead to cut TLB misses (Ch. 15).
- **Memory:** `mlockall` to prevent paging (Ch. 23, 26); tune `vm.*` to keep the kernel off your back.
- **Mitigations:** on a **carefully-justified, isolated, single-tenant** box, evaluate `mitigations=off` (or per-vuln flags) against its *measured* cost (§6.3) — and **document the security decision** (Ch. 72). Default to leaving them on; turning them off is a deliberate, measured, accountable choice, never a reflex.

The end state is a core that does one thing, at a constant clock, never sleeps, never gets interrupted, never gets migrated, and never faults — a deterministic substrate on which the rest of the book's code can hit its tail-latency targets. Verify it with `cyclictest` (§6.3) before declaring victory.

---

## 6.5 Pitfalls & anti-patterns: turbo-induced variance, noisy neighbors

- **Turbo-induced variance.** Leaving turbo on (or relying on it for your latency numbers) means your clock — and thus your nanoseconds — depends on core count, temperature, and power headroom. A benchmark that "got faster" may have just gotten a better turbo bin. For *measurement* and for deterministic latency, pin the frequency; if you keep turbo for the production speed, characterize its variance and never quote a single-run number (Ch. 3).
- **Noisy neighbors.** Anything sharing a resource with your hot core injects jitter: another process on the SMT sibling (§6.2.2), an interrupt steered onto the core (Ch. 42), a kernel thread, a cron job, `irqbalance` reshuffling, even another tenant on the same socket touching shared L3/memory bandwidth (Ch. 16). Isolation (`isolcpus`/`nohz_full`) and IRQ steering exist to evict these neighbors. A single un-evicted neighbor can own your entire tail.
- **Tuning without measuring.** Flipping knobs because a blog said to, without a before/after `cyclictest` (§6.3), is how you end up with a box that's *differently* broken — or where a setting that helped someone else's hardware does nothing on yours. Change one knob, measure, keep or revert (Ch. 2–3 discipline).
- **`mitigations=off` as a reflex.** Disabling speculative-execution mitigations without (a) establishing that the box is genuinely isolated/single-tenant, (b) *measuring* the latency you actually reclaim, and (c) documenting the accepted risk, is a security incident waiting to happen — and sometimes a *non*-improvement, if your hot path isn't syscall- or indirect-call-heavy. Justify it or leave it on (Ch. 72).
- **Pinning to the wrong CPU number.** Trusting that "CPU N" is a distinct physical core without checking `lscpu --extended`/`lstopo` (§6.4.2) — and landing on an SMT sibling or a remote NUMA node. The classic "my code regressed" that was really a topology mistake.
- **Tuning the box but not the process.** A perfectly quiet core does nothing if your thread isn't *pinned* to it (Ch. 42), its memory isn't *locked* (Ch. 23), and its path isn't *warmed* (Ch. 46). System setup is necessary, not sufficient — it's the substrate the later chapters build on.
- **Forgetting the trade-off / wrong box.** Applying HFT box tuning to a build server, database, or shared dev machine wastes power and cores for determinism nobody needs; applying *defaults* to a hot-path box loses races. Match the tuning to the box's job.

---

## 6.6 Exercises & checklist

**Exercises**

1. **See the jitter floor.** Run `cyclictest --smp -p99 -m -i 10 -h 200 -D 60s` on a box as shipped. Record min/avg/**max** and eyeball the histogram tail. Which is bigger — the gap between min and avg, or between avg and max? Tie this back to Ch. 1: which one is "the latency"?
2. **Attribute the wins.** Change knobs one at a time — disable turbo, then set governor `performance`, then cap C-states (`idle=poll` or cmdline), then steer IRQs off the core — re-running `cyclictest` after each. Which single change moved the **max** the most on your hardware? Did any change nothing?
3. **Map your machine.** Run `lscpu --extended` and `lstopo`. List the SMT sibling pairs. Which NUMA node is your NIC on? If you had to pin one feed-handler thread, which exact CPU would you choose, and which CPU would you deliberately leave idle, and why?
4. **Price a mitigation.** Boot once with default mitigations and once with `mitigations=off` (on a box you're allowed to, and understand the risk). Run a syscall-heavy microbenchmark (Ch. 3, e.g. a tight `getpid`/`read` loop) under each. How many nanoseconds per syscall did the mitigations cost? Would that matter on a hot path that does zero syscalls?
5. **Catch the wake-up tail.** Write a thread that sleeps 1 ms then does a fixed bit of work, timing the work. Run it on a core that allows deep C-states, then on one capped at C1 (or with `idle=poll`). Compare the p99.9 of the *first* operation after the sleep. Where did the multi-µs tail go? (Preview of Ch. 46.)

**Checklist — a deterministic box** *(the full, ordered, copy-paste version is **Appendix C**)*

- [ ] **Firmware:** deep C-states disabled (cap C1), turbo/SpeedStep set deliberately, HT decided per Ch. 43, power/thermal limits raised, NUMA/snoop set for the access pattern.
- [ ] **Frequency:** governor `performance`, turbo policy chosen and **measured**; clock is effectively constant for measurement.
- [ ] **Idle:** hot cores never enter deep C-states (cmdline cap / busy-poll design).
- [ ] **Topology mapped** (`lscpu --extended`/`lstopo`); pinning and NIC placement chosen from the real layout, SMT siblings of hot cores left idle.
- [ ] **Isolation:** `isolcpus`/`nohz_full`/`rcu_nocbs` on hot cores; `irqbalance` off and IRQs steered away (Ch. 42, 45).
- [ ] **Memory:** THP off (explicit huge pages instead — Ch. 15), `mlockall` (Ch. 23).
- [ ] **Mitigations:** reviewed, **measured**, and the on/off decision **documented** with its security rationale (Ch. 72) — never a reflex.
- [ ] **Verified** with `cyclictest` (max, not avg) **before/after**, one knob at a time.
- [ ] Remembered this is the **substrate**: the process must still be pinned (Ch. 42), memory-locked (Ch. 23), and warmed (Ch. 46).

---

## 6.7 References

- *Red Hat Enterprise Linux — Optimizing RHEL for Low Latency* / *Tuning Guide*, and the `tuned` `latency-performance`/`network-latency` profiles — a vendor-maintained codification of most knobs in §6.4.
- The Linux kernel documentation: `admin-guide/kernel-parameters.txt` (`isolcpus`, `nohz_full`, `rcu_nocbs`, `intel_idle.max_cstate`, `mitigations=`), `admin-guide/pm/cpuidle` and `cpufreq`, and `Documentation/admin-guide/hw-vuln/` (the speculative-execution mitigations and their controls).
- `cyclictest` / `rt-tests` documentation and the OSADL QA latency-plot methodology — the basis for §6.3's measurement.
- Intel, *64 and IA-32 Architectures Software Developer's Manual*, Vol. 3 & 4 — C-states, P-states, Turbo Boost, RAPL, and PMU; and the Intel/AMD security advisories for the specific Spectre/Meltdown/MDS/Retbleed mitigations.
- `hwloc`/`lstopo` documentation — topology discovery used in §6.4.2 and throughout Ch. 16, 42–43.

## 6.8 Additional Reading

- C. Cook, *"When a Microsecond Is an Eternity"* (CppCon 2017) — includes the practitioner case for a quiet, isolated box behind the hot path.
- The Linux Foundation **Real-Time Linux** (`PREEMPT_RT`) project and `cyclictest` tutorials — deeper on scheduling-latency measurement (Ch. 45).
- Brendan Gregg's posts on the *performance cost of Linux Spectre/Meltdown mitigations* — a measured treatment complementing §6.2.3.
- Vendor low-latency BIOS tuning guides (Dell, HPE, Supermicro, Lenovo) — the firmware names behind §6.4.1; consolidated in **Appendix C**.
- **Appendix C — System Tuning Checklist** (the ordered, actionable companion to this chapter) and **Appendix A** (the ARM/Graviton equivalents of these knobs).

---

*Next: Ch. 7 — The Memory Hierarchy & Caches, where Part II begins and we descend into the microarchitecture: why DRAM is ~200× an L1 hit, and how cache lines, associativity, and coherence shape every data structure you'll write.*
