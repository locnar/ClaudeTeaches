# Part XIII — Observability & Operations in Production

# Chapter 101 — Secure Programming for Low-Latency Systems

> **Prerequisites:** Ch. 70 (zero-copy wire decoding — the untrusted-input surface this chapter hardens), Ch. 31 (arena/zero-allocation — where memory-safety bugs live without a GC to catch them), Ch. 35 (price/quantity arithmetic — integer-overflow safety), Ch. 24–26 (abstractions / aliasing — UB as a vulnerability class), Ch. 6 (system setup — spec-exec mitigations and their cost), Ch. 53 (sanitizers/fuzzing tooling), Ch. 98 (logging — keeping secrets out of it).
>
> **Leads into:** Ch. 103 (process topology — privilege reduction, fault domains, kill-switch as a safety mechanism), Ch. 109–119 (capture/case study — integrity of the captured record). A cross-cutting Part XIII chapter: security treated as a *hot-path* and *correctness* concern, not a bolt-on.

---

## 101.1 Why it matters: security as a hot-path concern

A trading system is a network service that takes **untrusted bytes off a wire** — market-data multicast (Ch. 70), order-entry sessions, drop-copy feeds — and acts on them in microseconds, with money on the line. That makes it a security target with an unusual constraint: the defenses must be **cheap enough to leave on the hot path**, or they won't be used. Most security literature assumes you can afford a bounds check, a copy, a validation pass, a sandbox round-trip. On a tick-to-trade path with a sub-microsecond budget, "just add validation" collides with everything in this book. The discipline of this chapter is making security and low latency *coexist* — finding the defenses that are free (compile away, Ch. 21/26), the ones worth their nanoseconds, and the ones to push off the hot path entirely.

The threat model is concrete and dual. **External:** a malformed or hostile packet — a market-data feed glitch, a truncated message, a length field that says 64 KB in a 64-byte packet, a sequence designed to overflow a parser (Ch. 70). A feed handler that trusts its input is one bad packet away from a buffer overflow, a crash mid-session, or — worst — silently corrupted state that trades wrong. **Internal:** the bugs your own zero-allocation, arena-based, UB-courting hot path is *prone* to — a use-after-free in a pool (Ch. 31), an integer overflow in price math (Ch. 35), an aliasing violation (Ch. 26) that the optimizer turns into a security hole. The very techniques that make the path fast (no bounds-checked containers, manual lifetime management, `reinterpret_cast` off the wire) remove the guardrails that a slower system would have. **Low-latency code is *more* security-sensitive, not less**, precisely because it strips the safety nets.

And the stakes are categorical. A crash on the order path during market open (Ch. 70 microburst) is not a 500 error — it's an unhedged position, a runaway algo, a regulatory event. Memory corruption that doesn't crash but *corrupts an order* is worse. So security here is not "harden the perimeter and move on"; it's woven through the whole path: **validate the wire** (§101.4.1), **be memory-safe in the arena** (§101.4.2), **be arithmetic-safe in price math** (§101.4.3), **treat UB as the vulnerability class it is** (§101.2.2), choose **hardening that fits the latency budget** (§101.4.4), **handle secrets** (§101.4.5), **reduce privilege** (§101.4.6), **secure the build** (§101.4.7), and **fuzz the parsers** (§101.4.8). Security as a property of the hot path, measured (§101.3) like everything else.

## 101.2 Mental model

### 101.2.1 Market-data/order-entry messages as untrusted input

**Every byte that arrives from outside the process is hostile until validated.** This is the foundational stance, and it's in direct tension with Ch. 70's zero-copy decoding, which *overlays a struct on the wire bytes and reads fields directly* for speed. Zero-copy is fast precisely because it *doesn't* copy or check — so the safety must be added *without* giving up the zero-copy win:

```
   wire bytes (UNTRUSTED) ─► [ validate: lengths, bounds, ranges, sequence ] ─► overlay struct / parse ─► act
                                         ▲
                          this gate is the security boundary; everything past it is trusted
```

The dangerous fields are the **variable-length and length-prefixed** ones (Ch. 70): a message says "the symbol is N bytes" or "there are K repeating groups" — and if you trust N or K without checking them against the actual packet bounds, you read (or write) out of bounds. ITCH/OUCH/FIX/SBE/FAST all have these. The rule: **before** you use any length/count from the wire, validate it against the bytes you actually received; **before** you index by a field, bounds-check it; **before** you trust a price/quantity, range-check it. The validation gate is the security boundary — small, early, and (with care) cheap (§101.4.1).

### 101.2.2 Undefined behavior as a vulnerability class

In a low-latency C++ codebase, **undefined behavior is not just a correctness bug — it is a security vulnerability and a latency hazard at once.** UB (Ch. 24–26) — buffer overflow, use-after-free, signed-integer overflow, strict-aliasing violation, reading uninitialized memory, data races (Ch. 41) — does three bad things simultaneously: (1) it's *exploitable* (the classic memory-safety vulnerability classes are all UB); (2) the optimizer *assumes UB never happens* and transforms code accordingly, so a UB bug can make the compiler *delete your security check* or generate code that does something wholly unintended (Ch. 26's aliasing examples); (3) it's *nondeterministic*, which on a path that prizes determinism (Ch. 1) is its own failure. The HFT hot path deliberately walks close to UB — `reinterpret_cast` off the wire (Ch. 26), manual lifetimes (Ch. 31), `[[assume]]` (C++23; Appendix H lists which versions accept it and the `__builtin_assume` fallback)/`__builtin_unreachable` for codegen — so UB is *more* likely here. Treat every UB as a latent vulnerability: the same tools that find UB for correctness (UBSan, ASan — Ch. 53) are your security tools, and the same `std::bit_cast`/`std::launder` discipline (Ch. 26) that keeps codegen correct keeps it secure.

### 101.2.3 The hardening-vs-latency trade-off

Compiler/OS hardening features (stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks, and the Spectre/Meltdown mitigations of Ch. 6) each buy security at some latency cost — and the cost ranges from *zero* to *significant*:

```
   ~free (keep almost always):     ASLR/PIE (load-time only), _FORTIFY_SOURCE level 1-2 (compile-time bounds where size known),
                                    stack canaries on non-hot functions, UBSan-found bugs FIXED (not the runtime checks shipped)
   cheap-ish (measure, usually keep): stack protector on hot functions (a load+compare per call), CET shadow stack (HW-assisted)
   potentially costly (measure, decide on an isolated box):  CFI (indirect-call checks — Ch.16), retpoline/IBRS spec-exec
                                    mitigations (Ch.6 — can be large on indirect-branch-heavy code), heavy bounds-check instrumentation
```

The model: **most hardening is cheaper than people fear and should stay on; a few features have real hot-path cost and are decided per-function on a measured, isolated box** (Ch. 6). The mitigations interact (Ch. 6: retpoline changes indirect-call cost, which interacts with CFI and with Ch. 16's devirtualization), so you *measure the combination* (§101.3), not each in isolation. The isolated-box context (Ch. 6, 56, 57, 59) actually *helps*: a dedicated, network-restricted, single-tenant box has a smaller attack surface, so you can rationally drop the most expensive mitigations *there* that you'd keep on a shared machine — a deliberate, documented decision, not a default.

## 101.3 Measure it: hot-path cost of hardening options

*Toolchain and machine: Appendix L.3.*

Treat hardening like any other latency knob (Ch. 27): measure its hot-path cost, don't guess. Build the feed handler / order path at several hardening levels and measure tick-to-trade (Ch. 78 wire-to-wire) and a parse microbenchmark (Ch. 3). Representative deltas (Xeon Gold 6326 — Ice Lake-SP, GCC 13 unless a row says Clang; figures pending real runs):

| Hardening option | What it adds on the hot path | Representative cost | Default stance |
|---|---|---|---|
| **ASLR / PIE** | load-time randomization; `-fPIE` forbids absolute-address immediates (table indexing becomes `lea table[rip]` + indexed load) and routes external symbols through the GOT/PLT unless `-fno-plt -fno-semantic-interposition` (Appendix D) | ~0–2% on x86-64; ASLR also adds run-to-run cache/TLB-alignment variance to benchmarks (Ch. 3) | **on** |
| **`_FORTIFY_SOURCE=3`** (GCC ≥ 12, glibc ≥ 2.34; the distro default — Ubuntu 24.04 predefines it at `-O2`) | bounds checks on `memcpy`/`strcpy`/… where the size is known at compile time (level 2 — folds away) *or* computable at run time via `__builtin_dynamic_object_size` (level 3 — a few size computations) | ~0 at level 2; cheap but non-zero at level 3 — **measure** | **on** |
| **Hardened library mode** (`-D_GLIBCXX_ASSERTIONS`; libc++ `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST`; C++26 standardizes *hardened preconditions*) | one predictable compare per checked `operator[]`/`span` access, folded where the bound is provable | cheap-ish — a few % at worst on index-heavy code; **measure** | **on** unless measured otherwise — the shippable middle between nothing and ASan |
| **Stack protector (`-fstack-protector-strong`)** | canary store/check on functions with buffers | ~1-3 ns per protected call | on; consider `-fstack-protector` (narrower) for the hottest functions |
| **CET shadow stack** | hardware-assisted return-address check | **n/a on Ice Lake-SP** (no CET — Sapphire Rapids+ / Zen 3+ only); small where it exists | on where supported; **measure** |
| **CFI** (Clang 18 only: `-fsanitize=cfi -flto -fvisibility=hidden`; GCC has no software CFI) | check before indirect calls (Ch. 16) | adds to every indirect call; depends on call density | **measure** — interacts with devirtualization (Ch. 16) |
| **Spectre v2 mitigations (Ch. 6)** | kernel-side retpoline/eIBRS/IBPB lands on syscalls and context switches (which the hot path avoids) and on tasks opted into STIBP/SSBD; only compiling *your own* binary with `-mindirect-branch=thunk`/`-mretpoline` puts a retpoline on every indirect branch | ~0 for a syscall-free hot path; large on indirect-heavy code (Ch. 16) if you retpoline the binary | **measure on isolated box**; may drop per Ch. 6 |
| **LSM hooks (SELinux/AppArmor) enforcing** | one or more hook calls per syscall on the objects it touches (`inode_permission`, `socket_sendmsg`, …) with an AVC/policy-cache lookup; nothing on a syscall-free hot path | ~+30–80 ns on `write`/`sendmsg`, ~+100–200 ns on `openat`, ms on an AVC *miss* (policy reload) — representative, housekeeping threads only; **measure** (§101.4.9) | **enforcing**, with the trading unit's domain permissive/unconfined if the per-syscall cost shows in a housekeeping budget |
| **Kernel audit** | `audit_syscall_entry/exit` per syscall when rules match (or any syscall when the task is auditable with no rules); a record per audited syscall | ~+1–3 µs per *audited* syscall, ~+50–100 ns per unaudited one while enabled, ~0 with `-a task,never` for the trading unit — representative; **measure** (§101.4.9) | on for the box, **off for the trading task** (`-a task,never` / a `-F uid` filter), not `audit=0` |
| **seccomp-bpf filter** (measured, §101.4.9) | a BPF program per syscall (JIT-compiled; rule count barely matters) | **+10 ns per syscall** (83 → 94 ns on the guest) for an 8-rule allow-list; +0.1 ns for 64 more rules; **0 on the syscall-free hot path** | **on**, with `SECCOMP_FILTER_FLAG_SPEC_ALLOW` |
| **SSBD via seccomp** (the trap — measured, §101.4.9) | on a `spec_store_bypass_disable=seccomp` kernel, installing *any* filter sets SSBD (and STIBP) for the task: loads may no longer bypass older stores with unresolved addresses | **3.5×** on a store-forwarding-bound loop (1.11 → 3.92 ns/iter); 0 on a loop with no disambiguation opportunity — *depends on the code shape, is invisible in counters, and never fires a syscall* | boot `=prctl`; install filters with `SPEC_ALLOW`; verify `PR_GET_SPECULATION_CTRL` on every hot thread |
| **Yama `ptrace_scope`, kernel lockdown** | nothing per syscall; they constrain *tooling* — attach/`rr`/`perf` uprobes, `/dev/mem`, `resourceN`, MSR writes, unsigned modules | **0** latency; the cost is operational (§101.4.9) | on, with the exceptions documented |
| **Shipped bounds-check instrumentation** (ASan-style) | check on every access | large (2-3×) | **off** in production; a *test/fuzz* tool (Ch. 53), not a hot-path shield |

The lessons:

- **Most hardening is ~free and stays on.** ASLR, `_FORTIFY_SOURCE`, and source-level bounds checks that the optimizer *folds away* (§101.4.2) cost nothing measurable — there is no latency excuse to disable them. "We turned off all hardening for speed" is usually a measurement failure, not a real trade-off.
- **The expensive few are about *indirect branches*** — CFI and spec-exec mitigations (Ch. 6, 16) — and their cost is a function of how indirect-call-heavy your hot path is (another reason Ch. 16's devirtualization pays double: fewer indirect calls means cheaper *and* safer). Measure these as a *combination* on the isolated box.
- **Bounds checks that compile away are the goal, not bounds checks removed.** The win is structuring code so the check is *provably* satisfiable at compile time and the compiler elides it (§101.4.2) — you get the safety in the source and the speed in the binary. Removing the check entirely is the false economy.
- **ASan/UBSan/MSan are not production hardening — they're the test bench** (Ch. 53, §101.4.8). You ship the *bug fixes* they find, not the instrumentation.

## 101.4 Techniques

### 101.4.1 Bounds/length validation on variable-length fields and zero-copy parses

Validate the wire **without giving up zero-copy** (Ch. 70). The pattern: a single early gate that checks the structural invariants against the *actual received length*, then trusts the bytes:

- **Check declared lengths against received bytes first.** Before reading a length-prefixed or repeating-group field, verify `offset <= received_len && declared_len <= received_len - offset` — *not* `offset + declared_len <= received_len`, whose sum can wrap if computed in the wire's 32-bit type and pass the comparison for a hostile `declared_len`; the difference cannot wrap once `offset` is validated. One or two comparisons, branch-predicted (Ch. 15) to the valid path. A truncated or oversized message is rejected here, before any out-of-bounds read.
- **Use `std::span` (C++20) and length-carrying views, not raw pointers.** Parse against a `std::span<const std::byte>` that knows its bounds; sub-spanning with `.subspan(off, len)` makes the bounds explicit and the check local. The span carries the length so the bound is never lost (Ch. 70) — but `std::span` does **not** check for you: `operator[]`, `first()`, `last()` and `subspan()` are preconditions and undefined behaviour when out of range (C++20/23; `span::at()` arrives only in C++26), unless the library is built in a hardened mode (`-D_GLIBCXX_ASSERTIONS`, libc++ hardening — §101.3). The span makes the bound *available*; the comparison at the gate is still your code. `std::from_chars` (Ch. 70) for numeric fields is bounds-respecting by construction (it takes a begin/end).
- **Bound every count and index from the wire.** Repeating-group counts, symbol-table indices, level indices into the order book (Ch. 32) — clamp/reject anything outside the valid range *before* using it to index. An attacker-controlled index is an out-of-bounds access waiting to happen.
- **Validate at the boundary, trust within.** Do the checks *once*, at the security gate (§101.2.1); past it, the data is structurally valid and the inner hot loop runs unchecked and fast. This keeps the cost to one early pass, not a check on every field access — the same "validate once, then go fast" structure as a parser's lexer.
- **Reject, don't repair, and stay deterministic.** A malformed message is dropped (and counted/logged off-path, Ch. 98), not "fixed up." On a redundant A/B feed (Ch. 70) the other line likely has the good copy; gap/sequence logic handles the hole. Never let a malformed packet drive a trade.

### 101.4.2 Memory safety in zero-allocation/arena designs; bounds checks that compile away

The arena/pool/zero-allocation designs (Ch. 31) that make the hot path fast also remove the allocator's accidental safety (no per-object guard pages, manual lifetimes), so memory-safety bugs — buffer overflow, use-after-free, lifetime errors — are the dominant internal risk. Defend without a GC and without giving up speed:

- **Bounds checks that compile away.** Prefer `std::array`/`std::span` with *compile-time-known* sizes so the optimizer can *prove* an index in range and **elide the check** (Ch. 21, 26). `gsl::at`/`.at()` or an explicit `if (i < n)` that the compiler folds out (because `n` is a constant or the loop bound implies it) gives you the safety in source and zero cost in the binary — verify in the codegen (Ch. 4) that the check is gone. This is the central trick: *write the check, let the compiler delete it*, rather than not writing it. For the checks the compiler cannot prove away, the shippable answer is a **hardened library mode** (§101.3 — libstdc++ `-D_GLIBCXX_ASSERTIONS`, libc++'s hardening modes, the C++26 hardened preconditions) or a trap-mode sanitizer (`-fsanitize=bounds -fsanitize-trap=bounds`: one `cmp`/`ja ud2` per access, no runtime library): a predictable compare per checked access, folded wherever the bound is provable, a few percent at worst — nothing like the 2-3× of full ASan, which stays a test/fuzz tool (Ch. 53).
- **Arena lifetime discipline.** Use-after-free in a pool (Ch. 31) is silent (the slot is reused, not unmapped). Defenses: generation counters / tagged handles instead of raw pointers (an index + generation, validated on deref — catches stale handles cheaply; the slot map of Ch. 32 §32.4.4, one compare per dereference), clear-on-free in debug builds, and ASan over the arena *in test/fuzz* (Ch. 53) to catch UAF the hardware won't. Single-writer-per-segment discipline (Ch. 46, 103) avoids the cross-thread lifetime races.
- **Prefer types that make overflow impossible.** Fixed-capacity inline buffers (`small_vector`, a bounded ring — Ch. 32/45) with an explicit capacity check on push; never an unbounded `memcpy` into a fixed buffer. The capacity check is one predictable branch (Ch. 15).
- **UBSan/ASan in CI and fuzzing (Ch. 53), fixes in production.** You don't ship the instrumentation (§101.3); you ship code that ran clean under it. Every UB the sanitizers find is a §101.2.2 vulnerability fixed.

### 101.4.3 Integer/arithmetic safety in price/quantity math

Price/quantity arithmetic (Ch. 35) is a security surface: an integer overflow in a scaled-integer price, a signed/unsigned confusion in a quantity, or a wrong rounding can produce a *wrong order* — a financial bug with a security character (and signed overflow is UB, §101.2.2, so the optimizer may do anything).

- **Use the right type and know its range (Ch. 35).** Scaled integers for prices; pick a width that *cannot* overflow across the operations you do (notional = price × quantity can overflow a 32-bit or even 64-bit intermediate — check the headroom). Prefer unsigned where negatives are meaningless *but* beware unsigned wraparound and signed/unsigned comparison surprises.
- **Checked arithmetic where it matters, free where it doesn't.** Use `__builtin_add_overflow`/`__builtin_mul_overflow` (GCC/Clang) to *detect and reject* on the *risk-relevant* computations (notional, position, exposure) — C++26 adds only *saturating* arithmetic (`std::add_sat` et al. — libstdc++ 14, Appendix H), which clamps silently and gives no overflow signal: for a notional that is a quiet wrong answer, not a check — — they compile to an `add`/`mul` plus a branch on the overflow flag (a few cycles, Ch. 37), cheap enough for the pre-trade risk check (Ch. 104 §104.2.3 measures the gate at ~11 ns with the checks in). For values provably in range (Ch. 21), no check needed.
- **Validate price/quantity ranges at the wire gate (§101.4.1).** A price of 0, a negative quantity, an absurd notional — reject at the boundary, before the math. Range invariants checked once, then trusted.
- **No floating-point for money on the decision path (Ch. 35).** Beyond determinism, FP introduces rounding the math doesn't expect — a correctness-and-safety hazard. Scaled integers / fixed-point keep price math exact and overflow-analyzable.

### 101.4.4 Stack protector, `_FORTIFY_SOURCE`, PIE/ASLR, CFI, CET shadow stacks

Apply the §101.3 stance as concrete build configuration (Ch. 27, Appendix D):

- **Keep the free ones globally:** `-D_FORTIFY_SOURCE=3` (GCC ≥ 12, glibc ≥ 2.34 — distros already predefine it, so passing `=2` yourself can trigger a redefinition warning; level 3's `__builtin_dynamic_object_size` checks are cheap but worth one measurement), PIE/ASLR (default on modern toolchains), `-fstack-clash-protection`. These cost ~0 on the hot path and close real holes.
- **Stack protector tuned by function:** `-fstack-protector-strong` as the default; for the *hottest* measured functions, consider the narrower `-fstack-protector` or per-function `__attribute__((no_stack_protector))` *only* with a measured justification and a note that the function has no stack buffers to protect.
- **CET shadow stack** (`-fcf-protection`) where the hardware supports it — HW-assisted, usually cheap; measure. The full stack it needs: a CET-capable CPU (Sapphire Rapids+ / Zen 3+ — *not* the Ice Lake-SP reference box), Linux ≥ 6.6 (`CONFIG_X86_USER_SHADOW_STACK`), glibc ≥ 2.39 with `GLIBC_TUNABLES=glibc.cpu.hwcaps=SHSTK`, and *every* DSO in the process built with `-fcf-protection` — one unmarked library and the loader silently disables the shadow stack. **CFI** is Clang-only (`-fsanitize=cfi -flto -fvisibility=hidden`); GCC has no software CFI — use `-fcf-protection=branch` on CET hardware or `-fvtable-verify` for vtables.
- **CFI** (`-fsanitize=cfi`, needs LTO — Ch. 27) protects indirect calls (Ch. 16); its cost tracks indirect-call density, so it pairs naturally with devirtualization (Ch. 16 — fewer indirect calls, cheaper CFI). Measure on the hot path; keep unless it's demonstrably too expensive there.
- **Spectre/Meltdown mitigations (Ch. 6)** are the one place an isolated box justifies *reducing* hardening: on a single-tenant, network-restricted, code-controlled box (Ch. 56, 57, 59), some mitigations can be a documented, measured drop (`mitigations=` tuning) — but that is a deliberate risk decision per Ch. 6, not a reflex.

### 101.4.5 Secret handling and secure wiping; keeping secrets out of logs

Order sessions carry credentials (session passwords, API keys, signing keys). Handle them so they don't leak — including through this book's own machinery (Ch. 98 logging, Ch. 109 capture):

- **Keep secrets out of logs and captures.** The binary logger (Ch. 98) and the capture journal (Ch. 109) record *everything* by default — so secrets must be explicitly excluded or redacted at the source. Never `LOG` a credential; mark secret fields non-capturable. This ties Ch. 98 directly: a fast logger that captures a password is a fast leak.
- **Secure, non-elided wiping.** Zeroing a secret buffer with a plain loop can be *deleted by the optimizer* as a dead store (Ch. 4 — dead-store elimination under the as-if rule; cf. Ch. 26's aliasing rules for what else the optimizer may assume — the compiler sees the buffer is never read again). Use `explicit_bzero` (glibc ≥ 2.25 — the primary Linux choice), C23 `memset_explicit` (glibc ≥ 2.43; `memset_s` from C11 Annex K does **not** exist in glibc or musl), `std::fill` through a `volatile` view, or a wipe the compiler can't elide — verify in the codegen (Ch. 4) that the store survives. A buffer wipe also does not reach copies the compiler spilled to registers or stack slots: wipe in a non-inlined function and keep the secret's lifetime short. A "wiped" secret the optimizer left in memory is a vulnerability.
- **Minimize lifetime and copies.** Hold secrets briefly, in as few places as possible; avoid `std::string` copies that scatter the secret across freed heap blocks (a problem in arena designs too — Ch. 31). `mlock` (Ch. 34) the secret pages so they're never swapped to disk — and exclude them from core dumps with `madvise(MADV_DONTDUMP)`, because Ch. 103 deliberately captures cores of crashed trading processes. The wire capture is the other leak: Ch. 109 journals every outbound packet, and the SoupBinTCP/OUCH Login Request and FIX Logon (tag 554) carry the session password in clear — redact that field in the capture path (and route cores and captures to a restricted sink).

### 101.4.6 Privilege reduction (`seccomp`, capabilities, namespaces)

Reduce what a compromised process *can do* — and structure it so the restrictions don't touch the hot path:

- **`seccomp-bpf` syscall filtering.** After startup, the steady-state hot path makes essentially *no* syscalls (Ch. 30, 54 — that's the whole point). So you can install a tight seccomp filter that allows only the handful of syscalls the steady state needs (and the housekeeping threads' set), blocking the rest. Because the hot path doesn't syscall, the filter never fires on it — **near-zero hot-path cost**, real containment. Two thread-semantics details decide *when* to install it: a filter applies only to the installing thread unless `SECCOMP_FILTER_FLAG_TSYNC` is set (and `prctl(PR_SET_NO_NEW_PRIVS, 1)` must precede it), so a filter installed by the control thread after the hot threads exist silently leaves them unfiltered; and a `setuid`/capability drop after the threads exist is broadcast to every thread by a glibc signal (the `setxid` nudge) — a one-time stall on the hot core. So drop privileges and install the filter (with `TSYNC`) *before* spawning and pinning the hot threads — after setup's broader syscalls, at the steady-state boundary (Ch. 1) — and verify every hot thread's `Seccomp:` line in `/proc/<pid>/task/*/status`. One more interaction to check: on kernels < 5.16, or with `spectre_v2_user=seccomp`/`spec_store_bypass_disable=seccomp`, installing a filter turns on **SSBD + STIBP** for the process — a persistent speculation penalty, STIBP especially with an SMT sibling (Ch. 57). Read `/sys/devices/system/cpu/vulnerabilities/spectre_v2` and `spec_store_bypass` afterwards, and boot with the `prctl` variants if you must keep the mitigations opt-in — §101.4.9 measures the trap and the rest of the kernel's own security machinery.
- **Drop capabilities and privileges after setup.** Acquire what setup needs (e.g. `mlockall`, RT priority, raw sockets — Ch. 34, 59, 82), then drop to an unprivileged user and drop capabilities. The hot path runs deprivileged.
- **Who may load eBPF (ties Ch. 81).** eBPF programs are kernel code execution with a verifier-CVE history: loading needs `CAP_BPF` (+ `CAP_PERFMON` for tracing, `CAP_NET_ADMIN` for XDP/tc), so keep `kernel.unprivileged_bpf_disabled=1`, grant those capabilities only to the observability sidecar — never to the trading processes — and treat `bpftool prog list` as part of the box's audit.
- **Namespaces / isolation** for fault and blast-radius containment (ties Ch. 103's process topology) — the feed handler, strategy, and gateway as isolated processes limit what a compromise of one reaches.

### 101.4.7 Build/supply-chain integrity; reproducible and signed builds

The binary that trades must be the binary you built from the source you reviewed:

- **Vet and pin dependencies.** A compromised dependency is a compromised hot path. Minimize the dependency surface (the low-latency ethos already favors few, well-understood libraries), pin versions, and review what goes into the parser/decoder especially (the untrusted-input surface, §101.4.1).
- **Reproducible builds.** A bit-for-bit reproducible build lets you verify the deployed binary matches the audited source — no injected backdoor in the toolchain step. Pin the compiler and flags (Ch. 27, Appendix D).
- **Signed builds and artifacts.** Sign the binary and config; verify signatures at deploy. Combined with the immutable, audited config (Ch. 102) and capture of what actually ran (Ch. 109), this gives a provenance chain for compliance and incident response.

### 101.4.8 Fuzzing message parsers (libFuzzer/AFL++); ASan/UBSan/MSan

The wire parser (Ch. 70) is the highest-value fuzz target in the system — it's the untrusted-input boundary (§101.2.1), and a bug there is remotely triggerable:

- **Fuzz the decoder with libFuzzer/AFL++ under ASan+UBSan (+MSan).** Feed the ITCH/OUCH/FIX/SBE/FAST parser random and mutated inputs; the sanitizers (Ch. 53) turn latent memory-safety/UB bugs (§101.2.2) into loud, reproducible crashes. Seed the corpus with real captured packets (Ch. 109) and known-tricky messages (max-length, zero-length, truncated, oversized counts).
- **MSan for uninitialized reads, ASan for spatial/temporal, UBSan for the rest.** The three catch complementary classes; run all (ASan+UBSan together, MSan separately due to its requirements). The arena (§101.4.2) especially benefits from ASan's use-after-free detection.
- **Continuous, in CI (Ch. 53, 119).** Fuzzing is not a one-time audit — run it continuously so new parser code and new message types stay covered. A latency-regression gate (Ch. 119) and a fuzzing gate belong in the same CI.
- **Ship the fixes, not the instrumentation (§101.3).** Production runs the hardened-but-fast binary; the sanitizer builds are the test bench.

### 101.4.9 The cost of the kernel's own security subsystems: LSM, audit, seccomp (and the SSBD trap), Yama, lockdown — measured

*Toolchain and machine: Appendix L.3; the seccomp and SSBD rows are measured on the 4-vCPU KVM guest (kernel 6.18, `spec_store_bypass_disable` in `prctl` mode, g++ 13.3 `-O2`, pinned); the guest runs no LSM and no `auditd`, so those rows are representative pending the bare-metal run — the harness is the same `getppid`/`write`/`sendmsg` loop under `perf stat`, flipped with `setenforce`/`auditctl`.*

Appendix C.2 used to say `audit=0 selinux=0 "if your security policy permits"`. The policy usually does not permit it, and — the point of this section — it mostly does not need to: the hot path makes no syscalls (Ch. 54, §101.4.6), so every per-syscall cost below lands on **housekeeping threads**, where it is a budget item, not a tail event. The one exception is the mitigation a seccomp filter can switch on *for the whole task*, which taxes code that never enters the kernel. Measure, then keep what you can afford:

**LSM hooks (SELinux, AppArmor).** Every security-relevant syscall calls the active LSM's hook for the objects involved; SELinux resolves the (source context, target context, class) triple through the **AVC** (access vector cache) — a hash lookup, ~30–80 ns on a hit for `write`/`sendmsg`, ~100–200 ns on `openat` (several hooks: directory search, file open, inode permission). Permissive mode runs the same hooks and only changes the verdict; `selinux=0` removes them. The costs that *matter* are elsewhere: an AVC miss (first access after a **policy reload**, which also flushes the cache) is a slow-path policy walk of tens of µs to ms — never reload policy in the session; and file-system labelling (`restorecon`) walks the disk. The compliance-friendly shape: keep the LSM **enforcing** for the box, give the trading unit a **permissive domain** (`semanage permissive -a trading_t`) or run it `unconfined_t` with the justification recorded, and confine everything around it. The housekeeping threads then pay the hook cost; the hot threads pay nothing, having no syscalls.

**Audit.** The audit subsystem hooks syscall entry/exit for *auditable* tasks and emits a record when a rule matches — ~1–3 µs per audited syscall (the record is formatted and queued to `auditd`), and even an unaudited syscall pays the entry/exit filter walk (~50–100 ns) while the task is auditable. `audit=0` on the cmdline disables it for everyone; the targeted alternatives keep the box's audit trail (which the security team and the regulator want) and exempt the one task that should not pay: `auditctl -a task,never` as the **first** rule (every task created after it is non-auditable — put it before the trading unit starts; `-a exit,never -F uid=<trading>` for the per-syscall path), keep `-a exit,always -F arch=b64 -S execve -F uid!=<trading>` and the file-watch rules for everyone else, and set `auditd`'s own thread to the housekeeping cores (its `write` to the log is a disk I/O you do not want on a hot core anyway). Verify with `auditctl -s` (`enabled 1`) and `auditctl -l` (rule order — `task,never` must come first); a `task` rule added *after* the unit started does not apply to its existing threads, so it belongs in the unit's `ExecStartPre=` (Ch. 103 §103.4.9).

**seccomp — the filter, measured.** §101.4.6's claim was "near-zero hot-path cost"; here is the number. `seccomp_cost.cpp` installs allow-list filters with `seccomp(SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_SPEC_ALLOW, …)` and times `getppid` (the cheapest real syscall) before and after:

```
   syscall (getppid) no filter:              83.4 ns
   syscall with an 8-rule filter:            93.7 ns     <- +10 ns: the seccomp dispatch itself
   syscall with 8+64-rule filters stacked:   93.8 ns     <- +0.1 ns for 64 more compares: the BPF is JIT-compiled; rule count is free
```

Ten nanoseconds per syscall, on threads that make syscalls; zero on the thread that makes none. Keep the filter, install it before the hot threads exist with `TSYNC` (§101.4.6), and write it as an allow-list of the housekeeping set.

**seccomp — the SSBD trap, measured.** On a kernel booted with `spec_store_bypass_disable=seccomp` (the default until Linux 5.16 on Intel/AMD parts that expose SSBD; still the default in some distribution kernels — `/sys/devices/system/cpu/vulnerabilities/spec_store_bypass` says `… via prctl and seccomp` when it is), installing any seccomp filter *without* `SECCOMP_FILTER_FLAG_SPEC_ALLOW` sets **SSBD** (Speculative Store Bypass Disable) — and, under `spectre_v2_user=seccomp`, **STIBP** — for the task, for good. SSBD forbids a load from executing ahead of an older store whose address is not yet known (memory disambiguation); it costs nothing on code with no such opportunity and a lot on code with many. `ssbd_probe.cpp` forces the bit through `prctl(PR_SET_SPECULATION_CTRL)` — the same bit a filter would set — around a loop whose store address resolves late and whose next load could bypass it:

```
   boot state 0x3: 1.309 ns/iter        (PR_SPEC_PRCTL | PR_SPEC_ENABLE: SSBD off)
   SSBD on  0x5: 3.921 ns/iter          (PR_SPEC_PRCTL | PR_SPEC_DISABLE)   <- 3.5x on a disambiguation-bound loop
   SSBD off 0x3: 1.114 ns/iter
   SSBD on  0x5: 3.979 ns/iter
   # the Ch. 33-shaped book-modify loop (stores and loads to the same few lines, addresses known early): 0.86 ns/iter either way
```

Three and a half times, on a thread that makes no syscalls, with no counter that says why (it is not a miss, not a mispredict — it is a load waiting for a store-address resolution the hardware was told not to guess). Whether *your* hot loop has the shape is unknowable without measuring: run the hot path with the bit forced on and off (`prctl` is per-thread; `taskset`-free) and compare. The defences: boot `spec_store_bypass_disable=prctl spectre_v2_user=prctl` (Appendix C.2), always pass `SECCOMP_FILTER_FLAG_SPEC_ALLOW` (kernel 4.17+), and **assert** `prctl(PR_GET_SPECULATION_CTRL, PR_SPEC_STORE_BYPASS)` returns a value without `PR_SPEC_DISABLE` (and the same for `PR_SPEC_INDIRECT_BRANCH`) in every hot thread's startup, after the filter is installed — the Appendix M gate reads `/proc/<pid>/status`'s `Speculation_Store_Bypass:` line (`thread vulnerable` is the state you want; `thread force mitigated` is the trap).

**Yama and lockdown: zero latency, real constraints.** `kernel.yama.ptrace_scope` (1: only descendants may be ptraced; 2: `CAP_SYS_PTRACE` only; 3: nobody) costs nothing per syscall and decides whether `rr record`/`gdb -p`/`perf trace -p` (Ch. 5 §5.4.2, Ch. 54) can attach to a running strategy — on a production box, 1 with the debugging user granted `CAP_SYS_PTRACE`. Kernel **lockdown** (`integrity` under Secure Boot; `confidentiality` by choice) also costs nothing per syscall and refuses `/dev/mem`, `resourceN` mmap and `iomem=relaxed` (Ch. 88 §88.5), MSR *writes* (`wrmsr` — the prefetcher control of Ch. 12 §12.4.3, the PM QoS MSRs; `msr.allow_writes=on` does not override lockdown), `kexec` of unsigned kernels (Ch. 103 §103.4.9), and under `confidentiality` kprobes/BPF that read kernel memory (Ch. 81) and `perf`'s kernel samples — read `/sys/kernel/security/lockdown`. Neither is a reason to weaken the box; both are reasons to do the prefetcher MSR writes and the `kexec` in a signed, early-boot unit and to grant the forensics user its capabilities explicitly (Ch. 108, Appendix N).

The summary for Appendix C.2: **not `audit=0 selinux=0` — LSM enforcing with the trading domain permissive, audit on with `-a task,never` first for the trading unit, seccomp with `SPEC_ALLOW` on a `=prctl` kernel, and a startup assertion that no hot thread is `force mitigated`.**

## 101.5 Pitfalls & anti-patterns: trusting the wire; mitigation interactions

- **Hash flooding through a wire-chosen key.** A gateway or web-venue table keyed by a client's `ClOrdID`, `SenderCompID` or symbol string with an *unkeyed* hash lets a counterparty pick colliding keys and turn every lookup into a thousand-slot probe — a denial of service that costs the attacker nothing. Seed the hash per process (wyhash/XXH3 take a seed for free; SipHash where the cryptographic argument is needed), bound the table, and fuzz its probe lengths with adversarial keys (Ch. 38 §38.4.6).
- **Trusting the wire (the cardinal sin).** Reading a length/count/index from a packet and using it without validating against the actual received bytes (§101.4.1). One malformed packet → out-of-bounds read/write → crash or corruption mid-session. Zero-copy (Ch. 70) makes this *easy* to get wrong because there's no copy step to bound the data — add the validation gate (§101.2.1).
- **Treating UB as a performance feature and forgetting it's a vulnerability.** Walking up to UB for codegen (Ch. 26) and not realizing the optimizer may delete a security check or that the UB is exploitable (§101.2.2). Run UBSan (Ch. 53); fix every finding.
- **"Turn off all hardening for speed" without measuring.** Most hardening is ~free (§101.3); disabling `_FORTIFY_SOURCE`/ASLR/stack-protector wholesale trades real security for usually-immeasurable latency. Measure per feature; drop only the demonstrably expensive ones (CFI/spec-exec) and only with justification.
- **The optimizer deleting your secret-wipe.** A plain `memset` to zero a key is a dead store the compiler removes (§101.4.5) — the secret stays in memory. Use `explicit_bzero`/C23 `memset_explicit`; verify in the asm (Ch. 4).
- **Secrets in logs/captures.** The fast logger (Ch. 98) and capture journal (Ch. 109) record everything — a credential passed to `LOG` is leaked at full speed. Redact at the source.
- **Integer overflow in price/notional math.** Signed overflow is UB (§101.2.2) and a wrong-order bug (Ch. 35). Size types for headroom; check the risk-relevant ops (§101.4.3).
- **Ignoring mitigation interactions.** Measuring CFI, retpoline, and devirtualization (Ch. 6, 16) *in isolation* and summing — they interact (retpoline changes indirect-call cost, CFI adds to it, devirt removes the calls entirely). Measure the *combination* on the isolated box (§101.3).
- **Sanitizers in production as a shield.** ASan/UBSan are a 2-3× test/fuzz tool (§101.3, Ch. 53), not hot-path hardening. Shipping them tanks latency; *not* fixing what they find ships the bug. Do neither — fix and ship clean.
- **The seccomp filter that silently turned on SSBD/STIBP for the hot thread.** On a `spec_store_bypass_disable=seccomp` / `spectre_v2_user=seccomp` kernel, a filter installed without `SECCOMP_FILTER_FLAG_SPEC_ALLOW` sets the mitigation bits for the whole task — a measured 3.5× on a disambiguation-bound loop that never makes a syscall, invisible in every counter (§101.4.9). Boot `=prctl`, pass `SPEC_ALLOW`, and assert `Speculation_Store_Bypass: thread vulnerable` in each hot thread's `/proc/<pid>/task/<tid>/status` at the gate.
- **`audit=0 selinux=0` as the latency fix.** It removes an audit trail and an access policy the firm needs, to save per-syscall costs that a syscall-free hot path never paid; the housekeeping threads' share is bought back with `-a task,never` for the trading unit and a permissive domain, keeping both subsystems on for the box (§101.4.9).
- **Privilege/seccomp installed before setup, or never.** Install seccomp at the *steady-state* boundary (after setup's broad syscalls, §101.4.6) so it never fires on the hot path; "never" leaves the containment win on the table for ~zero cost.

## 101.6 Exercises & checklist

**Exercises:**

1. **Break a trusting parser.** Take a zero-copy ITCH/SBE parser (Ch. 70) that trusts its length fields and craft packets (truncated, oversized count, out-of-range index) that make it read out of bounds — confirm with ASan. Then add the §101.4.1 validation gate and re-run: the malformed packets are rejected, and measure the gate's hot-path cost (one early pass).
2. **Fuzz the decoder.** Wire the parser to libFuzzer under ASan+UBSan (§101.4.8), seed with captured packets (Ch. 109), and run until it finds (or fails to find) a memory-safety/UB bug. Fix what it finds; add it to CI (Ch. 119).
3. **Bounds checks that vanish.** Write an order-book index access two ways — unchecked, and `.at()`/`span`-checked with a compile-time-known bound — and verify in Godbolt (Ch. 4) that the checked version compiles to *identical* code (the check folded away, §101.4.2). Then make the bound runtime-unknown and watch the check reappear; structure the code so it doesn't.
4. **Measure the hardening ladder.** Build the feed handler at the §101.3 hardening levels and measure tick-to-trade (Ch. 78) at each. Reproduce "most are free, CFI/spec-exec are the costly few," and decide per-feature for your isolated box (Ch. 6).
5. **Un-deletable secret wipe.** Zero a key buffer with a plain `memset` and with `explicit_bzero`; show in the asm (Ch. 4) that the plain one is deleted and the explicit one survives (§101.4.5).
6. **seccomp at the boundary.** Install a steady-state seccomp filter (§101.4.6) allowing only the hot path's syscall set; confirm the hot path never trips it (near-zero cost) and that an injected forbidden syscall is blocked.

**Checklist:**

- [ ] Every length/count/index from the wire is **validated against received bytes** at a single early gate before use (§101.2.1, §101.4.1); malformed messages are **rejected, not repaired**, deterministically.
- [ ] Zero-copy parses use **`std::span`/length-carrying views** (Ch. 70), not raw pointers; numeric fields via bounds-respecting `std::from_chars` (§101.4.1).
- [ ] Arena/zero-alloc code uses **tagged handles/generation counters**, bounds checks that **compile away** (verified in asm, Ch. 4), and runs **clean under ASan/UBSan** in CI (§101.4.2).
- [ ] Price/quantity/notional math uses **scaled integers with overflow headroom** and **checked arithmetic on risk-relevant ops** (`__builtin_*_overflow`); no FP for money (§101.4.3, Ch. 35).
- [ ] **UB is treated as a vulnerability** — UBSan in CI, every finding fixed (§101.2.2).
- [ ] Hardening: **free features on globally** (`_FORTIFY_SOURCE`, PIE/ASLR, stack-clash); stack-protector/CET tuned and **measured**; CFI/spec-exec **measured as a combination** on the isolated box and dropped only with justification (§101.3, §101.4.4).
- [ ] **Secrets** never logged/captured (Ch. 98/109), wiped with **non-elided** `explicit_bzero`/`memset_explicit` (verified in asm), `mlock`'d, `MADV_DONTDUMP`'d, short-lived (§101.4.5).
- [ ] **Privilege dropped after setup**; tight **seccomp** filter installed at the **steady-state boundary** so it never fires on the hot path (§101.4.6).
- [ ] Build is **reproducible and signed**, dependencies vetted/pinned (§101.4.7); the wire **parser is continuously fuzzed** (libFuzzer/AFL++ under sanitizers) in CI (§101.4.8).

## 101.7 References

- The CERT C/C++ Secure Coding Standards and the SEI guidance — bounds validation, integer safety, and UB-as-vulnerability (§101.2.2, §101.4.1, §101.4.3).
- The compiler hardening documentation (GCC/Clang): `_FORTIFY_SOURCE`, `-fstack-protector*`, `-fcf-protection` (CET), `-fsanitize=cfi`, `-fstack-clash-protection` (§101.3, §101.4.4; Appendix D).
- The Linux `seccomp`, capabilities, and namespaces man pages and documentation — privilege reduction (§101.4.6).
- libFuzzer, AFL++, and the sanitizer (ASan/UBSan/MSan) documentation (Ch. 53 references) — fuzzing and finding UB (§101.4.8).
- Ch. 70 (wire decoding — the untrusted surface), Ch. 31 (arena designs), Ch. 35 (price math), Ch. 24–26 (abstractions/aliasing/UB), Ch. 6 (spec-exec mitigations), Ch. 98 (logging/secrets).

## 101.8 Additional Reading

- *Secure Coding in C and C++* (Seacord) and the OWASP/secure-design literature adapted to a systems/hot-path context.
- Spectre/Meltdown and transient-execution mitigation analyses (Ch. 6 references) — the cost and interaction of spec-exec hardening (§101.4.4).
- Talks on fuzzing network parsers and on memory safety in C++ (sanitizers, `std::span`, hardened standard libraries) — the parser-hardening and bounds-check-elision patterns (§101.4.1-2).
- Ch. 103 (*Process Topology*) — fault domains, kill-switch/safe-state, privilege isolation as architecture; Ch. 102 (*Hot Reload*) — validated, signed config; Ch. 109 (*Capture*) — integrity and secret-redaction of the record; Ch. 53 (*Correctness Tooling*) — the sanitizer/fuzzing bench; Ch. 119 (*Case Study*) — fuzzing + latency-regression gates in CI.
- **Appendix D** (Compiler Flag Reference) — the optimization flags the hardening flags of §101.4.4 sit beside (`-fno-plt`/`-fno-semantic-interposition` for PIE, `-fsanitize=cfi`); **Appendix C** — `mitigations=` and isolated-box tuning; **Appendix F** — security/UB glossary.

---

*Next: Ch. 102 — Hot Reload & Live Reconfiguration, changing strategy parameters, reference data, and even code without restarting or dropping a tick: atomic pointer swaps and double-buffering, seqlock-/RCU-published config read lock-free on the hot path (Ch. 41, 46–47), validate-before-swap, and dynamic-library reload with warm-up (Ch. 62) — and proving a reload never tears or stalls the hot path.*
