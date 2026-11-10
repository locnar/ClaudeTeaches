# Part XIII — Observability & Operations in Production

# Chapter 94 — Secure Programming for Low-Latency Systems

> **Prerequisites:** Ch. 67 (zero-copy wire decoding — the untrusted-input surface this chapter hardens), Ch. 30 (arena/zero-allocation — where memory-safety bugs live without a GC to catch them), Ch. 34 (price/quantity arithmetic — integer-overflow safety), Ch. 23–25 (abstractions / aliasing — UB as a vulnerability class), Ch. 6 (system setup — spec-exec mitigations and their cost), Ch. 50 (sanitizers/fuzzing tooling), Ch. 91 (logging — keeping secrets out of it).
>
> **Leads into:** Ch. 96 (process topology — privilege reduction, fault domains, kill-switch as a safety mechanism), Ch. 101–111 (capture/case study — integrity of the captured record). A cross-cutting Part XIII chapter: security treated as a *hot-path* and *correctness* concern, not a bolt-on.

---

## 94.1 Why it matters: security as a hot-path concern

A trading system is a network service that takes **untrusted bytes off a wire** — market-data multicast (Ch. 67), order-entry sessions, drop-copy feeds — and acts on them in microseconds, with money on the line. That makes it a security target with an unusual constraint: the defenses must be **cheap enough to leave on the hot path**, or they won't be used. Most security literature assumes you can afford a bounds check, a copy, a validation pass, a sandbox round-trip. On a tick-to-trade path with a sub-microsecond budget, "just add validation" collides with everything in this book. The discipline of this chapter is making security and low latency *coexist* — finding the defenses that are free (compile away, Ch. 20/25), the ones worth their nanoseconds, and the ones to push off the hot path entirely.

The threat model is concrete and dual. **External:** a malformed or hostile packet — a market-data feed glitch, a truncated message, a length field that says 64 KB in a 64-byte packet, a sequence designed to overflow a parser (Ch. 67). A feed handler that trusts its input is one bad packet away from a buffer overflow, a crash mid-session, or — worst — silently corrupted state that trades wrong. **Internal:** the bugs your own zero-allocation, arena-based, UB-courting hot path is *prone* to — a use-after-free in a pool (Ch. 30), an integer overflow in price math (Ch. 34), an aliasing violation (Ch. 25) that the optimizer turns into a security hole. The very techniques that make the path fast (no bounds-checked containers, manual lifetime management, `reinterpret_cast` off the wire) remove the guardrails that a slower system would have. **Low-latency code is *more* security-sensitive, not less**, precisely because it strips the safety nets.

And the stakes are categorical. A crash on the order path during market open (Ch. 67 microburst) is not a 500 error — it's an unhedged position, a runaway algo, a regulatory event. Memory corruption that doesn't crash but *corrupts an order* is worse. So security here is not "harden the perimeter and move on"; it's woven through the whole path: **validate the wire** (§94.4.1), **be memory-safe in the arena** (§94.4.2), **be arithmetic-safe in price math** (§94.4.3), **treat UB as the vulnerability class it is** (§94.2.2), choose **hardening that fits the latency budget** (§94.4.4), **handle secrets** (§94.4.5), **reduce privilege** (§94.4.6), **secure the build** (§94.4.7), and **fuzz the parsers** (§94.4.8). Security as a property of the hot path, measured (§94.3) like everything else.

## 94.2 Mental model

### 94.2.1 Market-data/order-entry messages as untrusted input

**Every byte that arrives from outside the process is hostile until validated.** This is the foundational stance, and it's in direct tension with Ch. 67's zero-copy decoding, which *overlays a struct on the wire bytes and reads fields directly* for speed. Zero-copy is fast precisely because it *doesn't* copy or check — so the safety must be added *without* giving up the zero-copy win:

```
   wire bytes (UNTRUSTED) ─► [ validate: lengths, bounds, ranges, sequence ] ─► overlay struct / parse ─► act
                                         ▲
                          this gate is the security boundary; everything past it is trusted
```

The dangerous fields are the **variable-length and length-prefixed** ones (Ch. 67): a message says "the symbol is N bytes" or "there are K repeating groups" — and if you trust N or K without checking them against the actual packet bounds, you read (or write) out of bounds. ITCH/OUCH/FIX/SBE/FAST all have these. The rule: **before** you use any length/count from the wire, validate it against the bytes you actually received; **before** you index by a field, bounds-check it; **before** you trust a price/quantity, range-check it. The validation gate is the security boundary — small, early, and (with care) cheap (§94.4.1).

### 94.2.2 Undefined behavior as a vulnerability class

In a low-latency C++ codebase, **undefined behavior is not just a correctness bug — it is a security vulnerability and a latency hazard at once.** UB (Ch. 23–25) — buffer overflow, use-after-free, signed-integer overflow, strict-aliasing violation, reading uninitialized memory, data races (Ch. 38) — does three bad things simultaneously: (1) it's *exploitable* (the classic memory-safety vulnerability classes are all UB); (2) the optimizer *assumes UB never happens* and transforms code accordingly, so a UB bug can make the compiler *delete your security check* or generate code that does something wholly unintended (Ch. 25's aliasing examples); (3) it's *nondeterministic*, which on a path that prizes determinism (Ch. 1) is its own failure. The HFT hot path deliberately walks close to UB — `reinterpret_cast` off the wire (Ch. 25), manual lifetimes (Ch. 30), `[[assume]]` (C++23; Appendix H lists which versions accept it and the `__builtin_assume` fallback)/`__builtin_unreachable` for codegen — so UB is *more* likely here. Treat every UB as a latent vulnerability: the same tools that find UB for correctness (UBSan, ASan — Ch. 50) are your security tools, and the same `std::bit_cast`/`std::launder` discipline (Ch. 25) that keeps codegen correct keeps it secure.

### 94.2.3 The hardening-vs-latency trade-off

Compiler/OS hardening features (stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks, and the Spectre/Meltdown mitigations of Ch. 6) each buy security at some latency cost — and the cost ranges from *zero* to *significant*:

```
   ~free (keep almost always):     ASLR/PIE (load-time only), _FORTIFY_SOURCE level 1-2 (compile-time bounds where size known),
                                    stack canaries on non-hot functions, UBSan-found bugs FIXED (not the runtime checks shipped)
   cheap-ish (measure, usually keep): stack protector on hot functions (a load+compare per call), CET shadow stack (HW-assisted)
   potentially costly (measure, decide on an isolated box):  CFI (indirect-call checks — Ch.15), retpoline/IBRS spec-exec
                                    mitigations (Ch.6 — can be large on indirect-branch-heavy code), heavy bounds-check instrumentation
```

The model: **most hardening is cheaper than people fear and should stay on; a few features have real hot-path cost and are decided per-function on a measured, isolated box** (Ch. 6). The mitigations interact (Ch. 6: retpoline changes indirect-call cost, which interacts with CFI and with Ch. 15's devirtualization), so you *measure the combination* (§94.3), not each in isolation. The isolated-box context (Ch. 6, 53, 54, 56) actually *helps*: a dedicated, network-restricted, single-tenant box has a smaller attack surface, so you can rationally drop the most expensive mitigations *there* that you'd keep on a shared machine — a deliberate, documented decision, not a default.

## 94.3 Measure it: hot-path cost of hardening options

*Toolchain and machine: Appendix L.3.*

Treat hardening like any other latency knob (Ch. 26): measure its hot-path cost, don't guess. Build the feed handler / order path at several hardening levels and measure tick-to-trade (Ch. 73 wire-to-wire) and a parse microbenchmark (Ch. 3). Representative deltas (Xeon Gold 6326 — Ice Lake-SP, GCC 13 unless a row says Clang; figures pending real runs):

| Hardening option | What it adds on the hot path | Representative cost | Default stance |
|---|---|---|---|
| **ASLR / PIE** | load-time randomization; `-fPIE` forbids absolute-address immediates (table indexing becomes `lea table[rip]` + indexed load) and routes external symbols through the GOT/PLT unless `-fno-plt -fno-semantic-interposition` (Appendix D) | ~0–2% on x86-64; ASLR also adds run-to-run cache/TLB-alignment variance to benchmarks (Ch. 3) | **on** |
| **`_FORTIFY_SOURCE=3`** (GCC ≥ 12, glibc ≥ 2.34; the distro default — Ubuntu 24.04 predefines it at `-O2`) | bounds checks on `memcpy`/`strcpy`/… where the size is known at compile time (level 2 — folds away) *or* computable at run time via `__builtin_dynamic_object_size` (level 3 — a few size computations) | ~0 at level 2; cheap but non-zero at level 3 — **measure** | **on** |
| **Hardened library mode** (`-D_GLIBCXX_ASSERTIONS`; libc++ `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST`; C++26 standardizes *hardened preconditions*) | one predictable compare per checked `operator[]`/`span` access, folded where the bound is provable | cheap-ish — a few % at worst on index-heavy code; **measure** | **on** unless measured otherwise — the shippable middle between nothing and ASan |
| **Stack protector (`-fstack-protector-strong`)** | canary store/check on functions with buffers | ~1-3 ns per protected call | on; consider `-fstack-protector` (narrower) for the hottest functions |
| **CET shadow stack** | hardware-assisted return-address check | **n/a on Ice Lake-SP** (no CET — Sapphire Rapids+ / Zen 3+ only); small where it exists | on where supported; **measure** |
| **CFI** (Clang 18 only: `-fsanitize=cfi -flto -fvisibility=hidden`; GCC has no software CFI) | check before indirect calls (Ch. 15) | adds to every indirect call; depends on call density | **measure** — interacts with devirtualization (Ch. 15) |
| **Spectre v2 mitigations (Ch. 6)** | kernel-side retpoline/eIBRS/IBPB lands on syscalls and context switches (which the hot path avoids) and on tasks opted into STIBP/SSBD; only compiling *your own* binary with `-mindirect-branch=thunk`/`-mretpoline` puts a retpoline on every indirect branch | ~0 for a syscall-free hot path; large on indirect-heavy code (Ch. 15) if you retpoline the binary | **measure on isolated box**; may drop per Ch. 6 |
| **Shipped bounds-check instrumentation** (ASan-style) | check on every access | large (2-3×) | **off** in production; a *test/fuzz* tool (Ch. 50), not a hot-path shield |

The lessons:

- **Most hardening is ~free and stays on.** ASLR, `_FORTIFY_SOURCE`, and source-level bounds checks that the optimizer *folds away* (§94.4.2) cost nothing measurable — there is no latency excuse to disable them. "We turned off all hardening for speed" is usually a measurement failure, not a real trade-off.
- **The expensive few are about *indirect branches*** — CFI and spec-exec mitigations (Ch. 6, 15) — and their cost is a function of how indirect-call-heavy your hot path is (another reason Ch. 15's devirtualization pays double: fewer indirect calls means cheaper *and* safer). Measure these as a *combination* on the isolated box.
- **Bounds checks that compile away are the goal, not bounds checks removed.** The win is structuring code so the check is *provably* satisfiable at compile time and the compiler elides it (§94.4.2) — you get the safety in the source and the speed in the binary. Removing the check entirely is the false economy.
- **ASan/UBSan/MSan are not production hardening — they're the test bench** (Ch. 50, §94.4.8). You ship the *bug fixes* they find, not the instrumentation.

## 94.4 Techniques

### 94.4.1 Bounds/length validation on variable-length fields and zero-copy parses

Validate the wire **without giving up zero-copy** (Ch. 67). The pattern: a single early gate that checks the structural invariants against the *actual received length*, then trusts the bytes:

- **Check declared lengths against received bytes first.** Before reading a length-prefixed or repeating-group field, verify `offset <= received_len && declared_len <= received_len - offset` — *not* `offset + declared_len <= received_len`, whose sum can wrap if computed in the wire's 32-bit type and pass the comparison for a hostile `declared_len`; the difference cannot wrap once `offset` is validated. One or two comparisons, branch-predicted (Ch. 14) to the valid path. A truncated or oversized message is rejected here, before any out-of-bounds read.
- **Use `std::span` (C++20) and length-carrying views, not raw pointers.** Parse against a `std::span<const std::byte>` that knows its bounds; sub-spanning with `.subspan(off, len)` makes the bounds explicit and the check local. The span carries the length so the bound is never lost (Ch. 67) — but `std::span` does **not** check for you: `operator[]`, `first()`, `last()` and `subspan()` are preconditions and undefined behaviour when out of range (C++20/23; `span::at()` arrives only in C++26), unless the library is built in a hardened mode (`-D_GLIBCXX_ASSERTIONS`, libc++ hardening — §94.3). The span makes the bound *available*; the comparison at the gate is still your code. `std::from_chars` (Ch. 67) for numeric fields is bounds-respecting by construction (it takes a begin/end).
- **Bound every count and index from the wire.** Repeating-group counts, symbol-table indices, level indices into the order book (Ch. 31) — clamp/reject anything outside the valid range *before* using it to index. An attacker-controlled index is an out-of-bounds access waiting to happen.
- **Validate at the boundary, trust within.** Do the checks *once*, at the security gate (§94.2.1); past it, the data is structurally valid and the inner hot loop runs unchecked and fast. This keeps the cost to one early pass, not a check on every field access — the same "validate once, then go fast" structure as a parser's lexer.
- **Reject, don't repair, and stay deterministic.** A malformed message is dropped (and counted/logged off-path, Ch. 91), not "fixed up." On a redundant A/B feed (Ch. 67) the other line likely has the good copy; gap/sequence logic handles the hole. Never let a malformed packet drive a trade.

### 94.4.2 Memory safety in zero-allocation/arena designs; bounds checks that compile away

The arena/pool/zero-allocation designs (Ch. 30) that make the hot path fast also remove the allocator's accidental safety (no per-object guard pages, manual lifetimes), so memory-safety bugs — buffer overflow, use-after-free, lifetime errors — are the dominant internal risk. Defend without a GC and without giving up speed:

- **Bounds checks that compile away.** Prefer `std::array`/`std::span` with *compile-time-known* sizes so the optimizer can *prove* an index in range and **elide the check** (Ch. 20, 25). `gsl::at`/`.at()` or an explicit `if (i < n)` that the compiler folds out (because `n` is a constant or the loop bound implies it) gives you the safety in source and zero cost in the binary — verify in the codegen (Ch. 4) that the check is gone. This is the central trick: *write the check, let the compiler delete it*, rather than not writing it. For the checks the compiler cannot prove away, the shippable answer is a **hardened library mode** (§94.3 — libstdc++ `-D_GLIBCXX_ASSERTIONS`, libc++'s hardening modes, the C++26 hardened preconditions) or a trap-mode sanitizer (`-fsanitize=bounds -fsanitize-trap=bounds`: one `cmp`/`ja ud2` per access, no runtime library): a predictable compare per checked access, folded wherever the bound is provable, a few percent at worst — nothing like the 2-3× of full ASan, which stays a test/fuzz tool (Ch. 50).
- **Arena lifetime discipline.** Use-after-free in a pool (Ch. 30) is silent (the slot is reused, not unmapped). Defenses: generation counters / tagged handles instead of raw pointers (an index + generation, validated on deref — catches stale handles cheaply; the slot map of Ch. 31 §31.4.4, one compare per dereference), clear-on-free in debug builds, and ASan over the arena *in test/fuzz* (Ch. 50) to catch UAF the hardware won't. Single-writer-per-segment discipline (Ch. 43, 96) avoids the cross-thread lifetime races.
- **Prefer types that make overflow impossible.** Fixed-capacity inline buffers (`small_vector`, a bounded ring — Ch. 31/42) with an explicit capacity check on push; never an unbounded `memcpy` into a fixed buffer. The capacity check is one predictable branch (Ch. 14).
- **UBSan/ASan in CI and fuzzing (Ch. 50), fixes in production.** You don't ship the instrumentation (§94.3); you ship code that ran clean under it. Every UB the sanitizers find is a §94.2.2 vulnerability fixed.

### 94.4.3 Integer/arithmetic safety in price/quantity math

Price/quantity arithmetic (Ch. 34) is a security surface: an integer overflow in a scaled-integer price, a signed/unsigned confusion in a quantity, or a wrong rounding can produce a *wrong order* — a financial bug with a security character (and signed overflow is UB, §94.2.2, so the optimizer may do anything).

- **Use the right type and know its range (Ch. 34).** Scaled integers for prices; pick a width that *cannot* overflow across the operations you do (notional = price × quantity can overflow a 32-bit or even 64-bit intermediate — check the headroom). Prefer unsigned where negatives are meaningless *but* beware unsigned wraparound and signed/unsigned comparison surprises.
- **Checked arithmetic where it matters, free where it doesn't.** Use `__builtin_add_overflow`/`__builtin_mul_overflow` (GCC/Clang) to *detect and reject* on the *risk-relevant* computations (notional, position, exposure) — C++26 adds only *saturating* arithmetic (`std::add_sat` et al. — libstdc++ 14, Appendix H), which clamps silently and gives no overflow signal: for a notional that is a quiet wrong answer, not a check — — they compile to an `add`/`mul` plus a branch on the overflow flag (a few cycles, Ch. 35), cheap enough for the pre-trade risk check (Ch. 97 §97.2.3 measures the gate at ~11 ns with the checks in). For values provably in range (Ch. 20), no check needed.
- **Validate price/quantity ranges at the wire gate (§94.4.1).** A price of 0, a negative quantity, an absurd notional — reject at the boundary, before the math. Range invariants checked once, then trusted.
- **No floating-point for money on the decision path (Ch. 34).** Beyond determinism, FP introduces rounding the math doesn't expect — a correctness-and-safety hazard. Scaled integers / fixed-point keep price math exact and overflow-analyzable.

### 94.4.4 Stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks

Apply the §94.3 stance as concrete build configuration (Ch. 26, Appendix D):

- **Keep the free ones globally:** `-D_FORTIFY_SOURCE=3` (GCC ≥ 12, glibc ≥ 2.34 — distros already predefine it, so passing `=2` yourself can trigger a redefinition warning; level 3's `__builtin_dynamic_object_size` checks are cheap but worth one measurement), PIE/ASLR (default on modern toolchains), `-fstack-clash-protection`. These cost ~0 on the hot path and close real holes.
- **Stack protector tuned by function:** `-fstack-protector-strong` as the default; for the *hottest* measured functions, consider the narrower `-fstack-protector` or per-function `__attribute__((no_stack_protector))` *only* with a measured justification and a note that the function has no stack buffers to protect.
- **CET shadow stack** (`-fcf-protection`) where the hardware supports it — HW-assisted, usually cheap; measure. The full stack it needs: a CET-capable CPU (Sapphire Rapids+ / Zen 3+ — *not* the Ice Lake-SP reference box), Linux ≥ 6.6 (`CONFIG_X86_USER_SHADOW_STACK`), glibc ≥ 2.39 with `GLIBC_TUNABLES=glibc.cpu.hwcaps=SHSTK`, and *every* DSO in the process built with `-fcf-protection` — one unmarked library and the loader silently disables the shadow stack. **CFI** is Clang-only (`-fsanitize=cfi -flto -fvisibility=hidden`); GCC has no software CFI — use `-fcf-protection=branch` on CET hardware or `-fvtable-verify` for vtables.
- **CFI** (`-fsanitize=cfi`, needs LTO — Ch. 26) protects indirect calls (Ch. 15); its cost tracks indirect-call density, so it pairs naturally with devirtualization (Ch. 15 — fewer indirect calls, cheaper CFI). Measure on the hot path; keep unless it's demonstrably too expensive there.
- **Spectre/Meltdown mitigations (Ch. 6)** are the one place an isolated box justifies *reducing* hardening: on a single-tenant, network-restricted, code-controlled box (Ch. 53, 54, 56), some mitigations can be a documented, measured drop (`mitigations=` tuning) — but that is a deliberate risk decision per Ch. 6, not a reflex.

### 94.4.5 Secret handling and secure wiping; keeping secrets out of logs

Order sessions carry credentials (session passwords, API keys, signing keys). Handle them so they don't leak — including through this book's own machinery (Ch. 91 logging, Ch. 101 capture):

- **Keep secrets out of logs and captures.** The binary logger (Ch. 91) and the capture journal (Ch. 101) record *everything* by default — so secrets must be explicitly excluded or redacted at the source. Never `LOG` a credential; mark secret fields non-capturable. This ties Ch. 91 directly: a fast logger that captures a password is a fast leak.
- **Secure, non-elided wiping.** Zeroing a secret buffer with a plain loop can be *deleted by the optimizer* as a dead store (Ch. 4 — dead-store elimination under the as-if rule; cf. Ch. 25's aliasing rules for what else the optimizer may assume — the compiler sees the buffer is never read again). Use `explicit_bzero` (glibc ≥ 2.25 — the primary Linux choice), C23 `memset_explicit` (glibc ≥ 2.43; `memset_s` from C11 Annex K does **not** exist in glibc or musl), `std::fill` through a `volatile` view, or a wipe the compiler can't elide — verify in the codegen (Ch. 4) that the store survives. A buffer wipe also does not reach copies the compiler spilled to registers or stack slots: wipe in a non-inlined function and keep the secret's lifetime short. A "wiped" secret the optimizer left in memory is a vulnerability.
- **Minimize lifetime and copies.** Hold secrets briefly, in as few places as possible; avoid `std::string` copies that scatter the secret across freed heap blocks (a problem in arena designs too — Ch. 30). `mlock` (Ch. 33) the secret pages so they're never swapped to disk — and exclude them from core dumps with `madvise(MADV_DONTDUMP)`, because Ch. 96 deliberately captures cores of crashed trading processes. The wire capture is the other leak: Ch. 101 journals every outbound packet, and the SoupBinTCP/OUCH Login Request and FIX Logon (tag 554) carry the session password in clear — redact that field in the capture path (and route cores and captures to a restricted sink).

### 94.4.6 Privilege reduction (`seccomp`, capabilities, namespaces)

Reduce what a compromised process *can do* — and structure it so the restrictions don't touch the hot path:

- **`seccomp-bpf` syscall filtering.** After startup, the steady-state hot path makes essentially *no* syscalls (Ch. 29, 51 — that's the whole point). So you can install a tight seccomp filter that allows only the handful of syscalls the steady state needs (and the housekeeping threads' set), blocking the rest. Because the hot path doesn't syscall, the filter never fires on it — **near-zero hot-path cost**, real containment. Two thread-semantics details decide *when* to install it: a filter applies only to the installing thread unless `SECCOMP_FILTER_FLAG_TSYNC` is set (and `prctl(PR_SET_NO_NEW_PRIVS, 1)` must precede it), so a filter installed by the control thread after the hot threads exist silently leaves them unfiltered; and a `setuid`/capability drop after the threads exist is broadcast to every thread by a glibc signal (the `setxid` nudge) — a one-time stall on the hot core. So drop privileges and install the filter (with `TSYNC`) *before* spawning and pinning the hot threads — after setup's broader syscalls, at the steady-state boundary (Ch. 1) — and verify every hot thread's `Seccomp:` line in `/proc/<pid>/task/*/status`. One more interaction to check: on kernels < 5.16, or with `spectre_v2_user=seccomp`/`spec_store_bypass_disable=seccomp`, installing a filter turns on **SSBD + STIBP** for the process — a persistent speculation penalty, STIBP especially with an SMT sibling (Ch. 54). Read `/sys/devices/system/cpu/vulnerabilities/spectre_v2` and `spec_store_bypass` afterwards, and boot with the `prctl` variants if you must keep the mitigations opt-in.
- **Drop capabilities and privileges after setup.** Acquire what setup needs (e.g. `mlockall`, RT priority, raw sockets — Ch. 33, 56, 77), then drop to an unprivileged user and drop capabilities. The hot path runs deprivileged.
- **Who may load eBPF (ties Ch. 76).** eBPF programs are kernel code execution with a verifier-CVE history: loading needs `CAP_BPF` (+ `CAP_PERFMON` for tracing, `CAP_NET_ADMIN` for XDP/tc), so keep `kernel.unprivileged_bpf_disabled=1`, grant those capabilities only to the observability sidecar — never to the trading processes — and treat `bpftool prog list` as part of the box's audit.
- **Namespaces / isolation** for fault and blast-radius containment (ties Ch. 96's process topology) — the feed handler, strategy, and gateway as isolated processes limit what a compromise of one reaches.

### 94.4.7 Build/supply-chain integrity; reproducible and signed builds

The binary that trades must be the binary you built from the source you reviewed:

- **Vet and pin dependencies.** A compromised dependency is a compromised hot path. Minimize the dependency surface (the low-latency ethos already favors few, well-understood libraries), pin versions, and review what goes into the parser/decoder especially (the untrusted-input surface, §94.4.1).
- **Reproducible builds.** A bit-for-bit reproducible build lets you verify the deployed binary matches the audited source — no injected backdoor in the toolchain step. Pin the compiler and flags (Ch. 26, Appendix D).
- **Signed builds and artifacts.** Sign the binary and config; verify signatures at deploy. Combined with the immutable, audited config (Ch. 95) and capture of what actually ran (Ch. 101), this gives a provenance chain for compliance and incident response.

### 94.4.8 Fuzzing message parsers (libFuzzer/AFL++); ASan/UBSan/MSan

The wire parser (Ch. 67) is the highest-value fuzz target in the system — it's the untrusted-input boundary (§94.2.1), and a bug there is remotely triggerable:

- **Fuzz the decoder with libFuzzer/AFL++ under ASan+UBSan (+MSan).** Feed the ITCH/OUCH/FIX/SBE/FAST parser random and mutated inputs; the sanitizers (Ch. 50) turn latent memory-safety/UB bugs (§94.2.2) into loud, reproducible crashes. Seed the corpus with real captured packets (Ch. 101) and known-tricky messages (max-length, zero-length, truncated, oversized counts).
- **MSan for uninitialized reads, ASan for spatial/temporal, UBSan for the rest.** The three catch complementary classes; run all (ASan+UBSan together, MSan separately due to its requirements). The arena (§94.4.2) especially benefits from ASan's use-after-free detection.
- **Continuous, in CI (Ch. 50, 111).** Fuzzing is not a one-time audit — run it continuously so new parser code and new message types stay covered. A latency-regression gate (Ch. 111) and a fuzzing gate belong in the same CI.
- **Ship the fixes, not the instrumentation (§94.3).** Production runs the hardened-but-fast binary; the sanitizer builds are the test bench.

## 94.5 Pitfalls & anti-patterns: trusting the wire; mitigation interactions

- **Trusting the wire (the cardinal sin).** Reading a length/count/index from a packet and using it without validating against the actual received bytes (§94.4.1). One malformed packet → out-of-bounds read/write → crash or corruption mid-session. Zero-copy (Ch. 67) makes this *easy* to get wrong because there's no copy step to bound the data — add the validation gate (§94.2.1).
- **Treating UB as a performance feature and forgetting it's a vulnerability.** Walking up to UB for codegen (Ch. 25) and not realizing the optimizer may delete a security check or that the UB is exploitable (§94.2.2). Run UBSan (Ch. 50); fix every finding.
- **"Turn off all hardening for speed" without measuring.** Most hardening is ~free (§94.3); disabling `_FORTIFY_SOURCE`/ASLR/stack-protector wholesale trades real security for usually-immeasurable latency. Measure per feature; drop only the demonstrably expensive ones (CFI/spec-exec) and only with justification.
- **The optimizer deleting your secret-wipe.** A plain `memset` to zero a key is a dead store the compiler removes (§94.4.5) — the secret stays in memory. Use `explicit_bzero`/C23 `memset_explicit`; verify in the asm (Ch. 4).
- **Secrets in logs/captures.** The fast logger (Ch. 91) and capture journal (Ch. 101) record everything — a credential passed to `LOG` is leaked at full speed. Redact at the source.
- **Integer overflow in price/notional math.** Signed overflow is UB (§94.2.2) and a wrong-order bug (Ch. 34). Size types for headroom; check the risk-relevant ops (§94.4.3).
- **Ignoring mitigation interactions.** Measuring CFI, retpoline, and devirtualization (Ch. 6, 15) *in isolation* and summing — they interact (retpoline changes indirect-call cost, CFI adds to it, devirt removes the calls entirely). Measure the *combination* on the isolated box (§94.3).
- **Sanitizers in production as a shield.** ASan/UBSan are a 2-3× test/fuzz tool (§94.3, Ch. 50), not hot-path hardening. Shipping them tanks latency; *not* fixing what they find ships the bug. Do neither — fix and ship clean.
- **Privilege/seccomp installed before setup, or never.** Install seccomp at the *steady-state* boundary (after setup's broad syscalls, §94.4.6) so it never fires on the hot path; "never" leaves the containment win on the table for ~zero cost.

## 94.6 Exercises & checklist

**Exercises:**

1. **Break a trusting parser.** Take a zero-copy ITCH/SBE parser (Ch. 67) that trusts its length fields and craft packets (truncated, oversized count, out-of-range index) that make it read out of bounds — confirm with ASan. Then add the §94.4.1 validation gate and re-run: the malformed packets are rejected, and measure the gate's hot-path cost (one early pass).
2. **Fuzz the decoder.** Wire the parser to libFuzzer under ASan+UBSan (§94.4.8), seed with captured packets (Ch. 101), and run until it finds (or fails to find) a memory-safety/UB bug. Fix what it finds; add it to CI (Ch. 111).
3. **Bounds checks that vanish.** Write an order-book index access two ways — unchecked, and `.at()`/`span`-checked with a compile-time-known bound — and verify in Godbolt (Ch. 4) that the checked version compiles to *identical* code (the check folded away, §94.4.2). Then make the bound runtime-unknown and watch the check reappear; structure the code so it doesn't.
4. **Measure the hardening ladder.** Build the feed handler at the §94.3 hardening levels and measure tick-to-trade (Ch. 73) at each. Reproduce "most are free, CFI/spec-exec are the costly few," and decide per-feature for your isolated box (Ch. 6).
5. **Un-deletable secret wipe.** Zero a key buffer with a plain `memset` and with `explicit_bzero`; show in the asm (Ch. 4) that the plain one is deleted and the explicit one survives (§94.4.5).
6. **seccomp at the boundary.** Install a steady-state seccomp filter (§94.4.6) allowing only the hot path's syscall set; confirm the hot path never trips it (near-zero cost) and that an injected forbidden syscall is blocked.

**Checklist:**

- [ ] Every length/count/index from the wire is **validated against received bytes** at a single early gate before use (§94.2.1, §94.4.1); malformed messages are **rejected, not repaired**, deterministically.
- [ ] Zero-copy parses use **`std::span`/length-carrying views** (Ch. 67), not raw pointers; numeric fields via bounds-respecting `std::from_chars` (§94.4.1).
- [ ] Arena/zero-alloc code uses **tagged handles/generation counters**, bounds checks that **compile away** (verified in asm, Ch. 4), and runs **clean under ASan/UBSan** in CI (§94.4.2).
- [ ] Price/quantity/notional math uses **scaled integers with overflow headroom** and **checked arithmetic on risk-relevant ops** (`__builtin_*_overflow`); no FP for money (§94.4.3, Ch. 34).
- [ ] **UB is treated as a vulnerability** — UBSan in CI, every finding fixed (§94.2.2).
- [ ] Hardening: **free features on globally** (`_FORTIFY_SOURCE`, PIE/ASLR, stack-clash); stack-protector/CET tuned and **measured**; CFI/spec-exec **measured as a combination** on the isolated box and dropped only with justification (§94.3, §94.4.4).
- [ ] **Secrets** never logged/captured (Ch. 91/101), wiped with **non-elided** `explicit_bzero`/`memset_explicit` (verified in asm), `mlock`'d, `MADV_DONTDUMP`'d, short-lived (§94.4.5).
- [ ] **Privilege dropped after setup**; tight **seccomp** filter installed at the **steady-state boundary** so it never fires on the hot path (§94.4.6).
- [ ] Build is **reproducible and signed**, dependencies vetted/pinned (§94.4.7); the wire **parser is continuously fuzzed** (libFuzzer/AFL++ under sanitizers) in CI (§94.4.8).

## 94.7 References

- The CERT C/C++ Secure Coding Standards and the SEI guidance — bounds validation, integer safety, and UB-as-vulnerability (§94.2.2, §94.4.1, §94.4.3).
- The compiler hardening documentation (GCC/Clang): `_FORTIFY_SOURCE`, `-fstack-protector*`, `-fcf-protection` (CET), `-fsanitize=cfi`, `-fstack-clash-protection` (§94.3, §94.4.4; Appendix D).
- The Linux `seccomp`, capabilities, and namespaces man pages and documentation — privilege reduction (§94.4.6).
- libFuzzer, AFL++, and the sanitizer (ASan/UBSan/MSan) documentation (Ch. 50 references) — fuzzing and finding UB (§94.4.8).
- Ch. 67 (wire decoding — the untrusted surface), Ch. 30 (arena designs), Ch. 34 (price math), Ch. 23–25 (abstractions/aliasing/UB), Ch. 6 (spec-exec mitigations), Ch. 91 (logging/secrets).

## 94.8 Additional Reading

- *Secure Coding in C and C++* (Seacord) and the OWASP/secure-design literature adapted to a systems/hot-path context.
- Spectre/Meltdown and transient-execution mitigation analyses (Ch. 6 references) — the cost and interaction of spec-exec hardening (§94.4.4).
- Talks on fuzzing network parsers and on memory safety in C++ (sanitizers, `std::span`, hardened standard libraries) — the parser-hardening and bounds-check-elision patterns (§94.4.1-2).
- Ch. 96 (*Process Topology*) — fault domains, kill-switch/safe-state, privilege isolation as architecture; Ch. 95 (*Hot Reload*) — validated, signed config; Ch. 101 (*Capture*) — integrity and secret-redaction of the record; Ch. 50 (*Correctness Tooling*) — the sanitizer/fuzzing bench; Ch. 111 (*Case Study*) — fuzzing + latency-regression gates in CI.
- **Appendix D** (Compiler Flag Reference) — the optimization flags the hardening flags of §94.4.4 sit beside (`-fno-plt`/`-fno-semantic-interposition` for PIE, `-fsanitize=cfi`); **Appendix C** — `mitigations=` and isolated-box tuning; **Appendix F** — security/UB glossary.

---

*Next: Ch. 95 — Hot Reload & Live Reconfiguration, changing strategy parameters, reference data, and even code without restarting or dropping a tick: atomic pointer swaps and double-buffering, seqlock-/RCU-published config read lock-free on the hot path (Ch. 38, 43–44), validate-before-swap, and dynamic-library reload with warm-up (Ch. 59) — and proving a reload never tears or stalls the hot path.*
