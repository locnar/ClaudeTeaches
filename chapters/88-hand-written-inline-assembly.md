# Part XII — Beyond C++: Languages at the Latency Floor

# Chapter 88 — Hand-Written & Inline Assembly

> **Prerequisites:** Ch. 4 (reading x86-64 asm and the SysV calling convention — this chapter is its writing-side sequel), Ch. 3 §3.4.1 (`DoNotOptimize`/`ClobberMemory`, which turn out to be the first two asm statements you ever used), Ch. 20 (`rdtsc`/`rdtscp`/`lfence` — the canonical "the compiler has no name for this" instructions), Ch. 39 §39.4.2 (the compiler barrier vs a hardware fence), Ch. 37 §37.4.2 (intrinsics when the auto-vectorizer won't), Ch. 83 §83.4 (descriptor-then-doorbell ordering and write-combining).
>
> **Leads into:** Ch. 89 (Rust — every codegen-parity claim there is read in the asm this chapter teaches you to write and to argue with), Ch. 90 (the C ABI boundary is the same register contract a hand-written `.asm` function must honour), Ch. 92 (the cross-language benchmark reads each toolchain's asm as its "verify the codegen" step). Opens Part XII — the Part steps outside C++ to ask what each other language, and the machine itself, offers the hot path; it begins at the bottom, with the instructions, because every comparison that follows is settled by reading them.

---

## 88.1 Why it matters: when the compiler has no word for what you mean

Everything the book has measured so far was written in C++ and *read* in assembly (Ch. 4). The optimizer did the writing. That division of labour is right 99 % of the time — Ch. 12, 20, 36, 37 and 41 all reached for *intrinsics* (`__builtin_prefetch`, `__rdtscp`, `_mm_pause`, `_pdep_u64`, `_mm256_shuffle_epi8`) and left register allocation, scheduling and inlining to the compiler, and §88.3 will show that this costs nothing. But a feed handler sits on a boundary where the compiler has no vocabulary at all: a user-space performance-counter read (`rdpmc`) for a cycle-exact per-stage budget (Ch. 2 §2.4.4, Ch. 113 §113.4.4), an architecturally serializing instruction before a timestamp (`cpuid`, `serialize`, `lfence` — Ch. 20 §20.2.1), a 64-byte doorbell write that must leave the core as *one* PCIe transaction (`movdir64b` — Ch. 83 §83.4), a cache-line write-back without eviction (`clwb`) for a persistent journal (Ch. 103 §103.4.3), a branch straight to a C++ label from a flag the compiler never materialises (`asm goto`), or a compiler-only barrier that pins stores in place without emitting a fence (Ch. 39). Each of those is one `asm` statement — if you write it correctly.

"Correctly" is the subject. An asm statement is a contract with the optimizer: *these registers hold my inputs, these are my outputs, these others I destroy, this is whether memory changed*. Get the contract right and the compiler schedules and allocates around your instruction as if it were one of its own. Get it wrong and nothing warns you: §88.6 shows a one-line `mul` with an undeclared `rdx` clobber that turns a 1000-iteration loop into an infinite one under both GCC and Clang, at `-O2`, silently, because the compiler kept the loop counter in the register the instruction overwrote. The second subject is *when*: the book's answer stays intrinsics-first, and §88.3 earns it with numbers — the asm `rdtscp` and the intrinsic cost the same 45 cycles, and a hand-written NASM decoder is slower than the inlined C++ one it replaces, because the thing you lose by leaving the language is inlining.

## 88.2 Mental model

### 88.2.1 Extended asm anatomy: template, outputs, inputs, clobbers

GCC's extended asm (Clang implements the same syntax) has four colon-separated parts:

```cpp
asm [volatile] ( "template"            // the instruction text; %0, %1 … (or %[name]) are operand placeholders
               : output operands        // "=r"(x)  write-only register   "+r"(x) read-write   "=&r"(x) early-clobber   "=m"(x) memory
               : input operands         // "r"(v) any GPR   "m"(v) memory   "i"(c) immediate   "a"/"d"/"c"/"S"/"D" specific registers
               : clobbers );            // "rax", "memory", "cc" — what the instructions destroy beyond the declared outputs
```

The constraint letters are the whole contract. `"=a"(lo), "=d"(hi)` says *this statement produces values in `eax` and `edx`*; `"a"(px)` says *put `px` in `rax` before the statement runs*; `"r"(mult)` lets the compiler pick any general register and substitute its name into the template. `"+r"` is an operand both read and written. `"=&r"` (*early clobber*) says the output is written *before* all inputs have been consumed, so it must not share a register with any input — forget it and a two-instruction sequence that writes its output and then reads an input silently reads garbage. `"cc"` declares the flags destroyed (on x86 GCC assumes it anyway; write it for Clang and for honesty). `"memory"` declares that the statement may read or write *any* memory the compiler cannot see through the operands — which forces every value the compiler had cached in a register to be written back before the statement and reloaded after, and forbids moving loads and stores across it. The statement is a black box to the optimizer: it knows the operands and clobbers and *nothing else*; it never parses the template.

### 88.2.2 What the optimizer may do around an asm statement

Because the box is opaque, the optimizer treats it like a function call whose effects are exactly the declared ones. Three consequences decide most bugs:

- **A non-`volatile` asm is a pure function of its inputs.** With no inputs, two identical statements may be *merged*, one that is loop-invariant may be *hoisted* out of the loop, and one whose outputs are unused may be *deleted*. §88.5 shows GCC deleting an unused `rdtsc` and Clang hoisting one out of a loop. `asm volatile` says *the side effect matters: emit it, where I wrote it, every time* — every timestamp read, counter read, fence and doorbell is `volatile`.
- **Without a `"memory"` clobber, memory may move across it.** `asm volatile("rdtscp" …)` with no memory clobber is a point in the instruction stream, but the compiler may still schedule the loads of the thing you are timing *after* it, or stores *before* it — exactly the hazard Ch. 20 §20.2.1 fences against with `lfence`. A memory clobber is a *compiler* fence; `lfence`/`mfence` are *hardware* ones (Ch. 39 §39.4.2); timing code often needs both, in that order of cheapness.
- **Register allocation is still the compiler's.** It chooses which register `"r"` becomes, may keep live values in registers you did not declare, and will keep them there *across* your statement. Anything your template writes that is not an output or clobber corrupts a live value — the §88.6 bug.

### 88.2.3 The two asm statements you have used since Ch. 3

Ch. 3 §3.4.1 described `benchmark::DoNotOptimize` and `benchmark::ClobberMemory` as "a compiler barrier". They are extended asm, and now you can read them:

```cpp
template <class T> inline void DoNotOptimize(T& value) { asm volatile("" : "+r,m"(value) : : "memory"); }   // "value was read AND written by something you cannot see": it must be materialised, and is live afterwards
inline void ClobberMemory() { asm volatile("" : : : "memory"); }                                           // "all memory changed": flush cached values before, reload after; no instruction emitted
```

The empty template emits *no instruction*. The entire effect is the contract: `"+r,m"` forces the value into a register or memory location that the statement "reads and writes", so the computation that produced it cannot be dead-code-eliminated and the value cannot be constant-folded after; `"memory"` pins stores. Ch. 39 §39.4.2's warning that `asm volatile("" ::: "memory")` is *not* a hardware fence is the same statement seen from the other side: it orders the *compiler's* emission, and the CPU reorders freely within x86-TSO's permissions after that.

### 88.2.4 Hand-written functions: the ABI is the whole contract

A standalone `.asm`/`.S` file has no constraints to declare; instead it must honour the SysV x86-64 calling convention of Ch. 4 §4.2.1 *completely*: arguments in `rdi, rsi, rdx, rcx, r8, r9`, return in `rax` (`rdx:rax` for 128 bits), `rbx, rbp, r12–r15` callee-saved (save them if you touch them), everything else free to clobber, the stack 16-byte aligned at the `call`, the 128-byte red zone below `rsp` usable without adjusting `rsp` in a leaf, and a struct return larger than 16 bytes passed through a hidden pointer in `rdi` (sret — which is why the NASM `decode_asm` in §88.3 takes `out` first). The file also needs a `.note.GNU-stack` section or the linker marks the stack executable. What you cannot get is inlining: a hand-written function is always a `call`, with its own prologue, and §88.3 measures that cost.

## 88.3 Measure it: asm vs intrinsic vs compiler, on this book's own instructions

*Toolchain and machine.* The listings build with GCC 13.3 and Clang 18.1 (`-O2 -std=c++20 -march=native`) and were run on the 4-vCPU Intel Xeon VM used for the book's self-tests (Appendix L.2: AVX-512, `movdir64b` and `serialize` present, 2.7 GHz TSC, Linux 6.18, pinned with `taskset -c 2`) — **not** the Appendix L.3 reference machine, whose numbers differ in absolute terms; the *comparisons* within each block are the point. The VM exposes no core PMU (`perf_event_paranoid=2`, no `cpu` event source), so the `rdpmc` statement of §88.4.2 is shown but not measured here.

```cpp
// anatomy.cpp — extended-asm anatomy, measured against the intrinsic forms.
// Build: g++ -O2 -std=c++20 -march=native anatomy.cpp -o anatomy     (clang++: identical flags)
// Run:   taskset -c 2 ./anatomy
#include <cstdint>
#include <cstdio>
#include <x86intrin.h>

// (1) rdtscp via extended asm — outputs in edx:eax, aux in ecx; the compiler allocates nothing for us
static inline std::uint64_t rdtscp_asm() {
    std::uint32_t lo, hi, aux;
    asm volatile("rdtscp" : "=a"(lo), "=d"(hi), "=c"(aux) :: );     // volatile: never merged/hoisted; no memory clobber: loads may still move across it (§88.2.2)
    return (std::uint64_t(hi) << 32) | lo;
}
static inline std::uint64_t rdtscp_intrin() { unsigned aux; return __rdtscp(&aux); }

// (2) a 64-byte doorbell write — movdir64b (one 64-byte transaction) via asm vs the intrinsic (Ch. 83 §83.4)
static inline void doorbell_asm(void* dst, const void* src) {
    asm volatile("movdir64b (%1), %0" : : "r"(dst), "r"(src) : "memory");   // dst is a register operand, src a 64-byte memory source
}
static inline void doorbell_intrin(void* dst, const void* src) { _movdir64b(dst, src); }

int main() {
    constexpr int R = 2'000'000;
    std::uint64_t s = 0, a, b;
    a = rdtscp_intrin(); for (int i = 0; i < R; ++i) s += rdtscp_asm();    b = rdtscp_intrin();
    std::printf("rdtscp via asm       : %6.2f cycles/call\n", double(b - a) / R);
    a = rdtscp_intrin(); for (int i = 0; i < R; ++i) s += rdtscp_intrin(); b = rdtscp_intrin();
    std::printf("rdtscp via intrinsic : %6.2f cycles/call\n", double(b - a) / R);

    alignas(64) std::uint8_t src[64] = {1}, dst[64] = {};
    a = rdtscp_intrin(); for (int i = 0; i < R; ++i) { src[0] = std::uint8_t(i); doorbell_asm(dst, src); }    b = rdtscp_intrin();
    std::printf("movdir64b via asm    : %6.2f cycles/store (to cacheable memory, not MMIO)\n", double(b - a) / R);
    a = rdtscp_intrin(); for (int i = 0; i < R; ++i) { src[0] = std::uint8_t(i); doorbell_intrin(dst, src); } b = rdtscp_intrin();
    std::printf("movdir64b via intrin : %6.2f cycles/store\n", double(b - a) / R);
    a = rdtscp_intrin(); for (int i = 0; i < R; ++i) { src[0] = std::uint8_t(i); __builtin_memcpy(dst, src, 64); asm volatile("" ::: "memory"); } b = rdtscp_intrin();
    std::printf("plain 64-byte copy   : %6.2f cycles/store\n", double(b - a) / R);
    asm volatile("" : "+r"(s)); std::printf("(sink %llu %u)\n", (unsigned long long)s, dst[0]);
    return 0;
}
```

```
   GCC 13.3                                   Clang 18.1
   rdtscp via asm       :  45.64 cycles/call   46.10 cycles/call     <- identical: the intrinsic IS this asm, with the compiler's own constraints
   rdtscp via intrinsic :  45.40 cycles/call   45.09 cycles/call
   movdir64b via asm    : 430.15 cycles/store  424.24 cycles/store   <- identical to each other; ~30x a cached store — movdir64b is a direct store
   movdir64b via intrin : 442.85 cycles/store  421.53 cycles/store      that bypasses the cache: for an MMIO doorbell, never for cacheable memory
   plain 64-byte copy   :  14.16 cycles/store    3.49 cycles/store   <- the compiler's two 32-byte vmovdqa pairs (GCC kept a store-forwarding stall; Clang did not)
```

Two lessons, both measured. **The asm statement and the intrinsic are the same instruction at the same cost** — the intrinsic is a header-provided asm (or builtin) with the constraints already right, so there is nothing to gain by spelling it yourself, and §88.5 shows the emitted code is byte-identical. **The instruction decides the cost, not the spelling:** `movdir64b` to cacheable memory costs ~430 cycles because it is a *direct store* that writes through to memory as a single 64-byte transaction — precisely the property that makes it the right doorbell write to a write-combining BAR (Ch. 83 §83.4: one PCIe TLP, no partial writes), and precisely why it is the wrong instruction for anything cached. (A 45-cycle `rdtscp` on this VM is also a reminder that the reference machine's ~25–30 cycles — Ch. 20 §20.3 — are not portable across hypervisors.)

The second measurement is the one that settles *hand-written vs compiled*. Ch. 68's `decode()` — four big-endian loads and two bytes, the hottest function in the system — rewritten by hand in NASM against its compiled form:

```nasm
; decode_nasm.asm — hand-written SysV x86-64 implementation of Ch. 68's decode(): Msg* decode_asm(Msg* out, const uint8_t* p)
; Build: nasm -f elf64 decode_nasm.asm -o decode_nasm.o
; Msg layout (Ch. 92's kernel.hpp): seq u64 @0, id u64 @8, qty u32 @16, px u32 @20, type u8 @24, side u8 @25
        default rel
        section .text
        global  decode_asm
decode_asm:                         ; rdi = out (sret), rsi = p — leaf function: no prologue, red zone unused, nothing callee-saved touched
        movbe   rax, [rsi+16]       ; seq  — byte-swapping load, one instruction (Ch. 68 §68.5)
        mov     [rdi], rax
        movbe   rax, [rsi+24]       ; id
        mov     [rdi+8], rax
        movbe   eax, [rsi+4]        ; qty
        mov     [rdi+16], eax
        movbe   eax, [rsi+8]        ; px
        mov     [rdi+20], eax
        movzx   eax, word [rsi]     ; type, side — two bytes in one load
        mov     [rdi+24], ax
        mov     rax, rdi            ; sret convention: return the out pointer
        ret
        section .note.GNU-stack noalloc noexec nowrite progbits   ; or the linker marks the stack executable
```

Measured against the C++ `decode()` called out of line (`__attribute__((noinline))`, the same call shape) and inlined (as Ch. 68 and Ch. 92 use it), 20 M decodes over a 1024-record ring, every field consumed:

```
   C++ decode, out-of-line call : 2.12 ns/msg      <- GCC: two vpshufb byte-swaps + 3 stores, plus call/ret and sret traffic
   NASM decode (call)           : 1.69 ns/msg      <- 10 scalar movbe/mov, plus call/ret: 20% faster than the compiled CALL…
   C++ decode, inlined          : 1.25 ns/msg      <- …and 35% slower than the compiled INLINE, which has no call, no sret store/reload
```

Read it the Ch. 1 way: the hand-written function *won the fair fight it can have* — against another out-of-line call — by a fifth, because for this tiny body ten scalar `movbe`s beat GCC's vector shuffle-and-store sequence. It *lost to the compiler's inlined code* by a third, because the inlined version has no `call`, no return-value round trip through memory, and the surrounding loop's register allocation flows through it. That is the structural fact about hand-written functions: whatever you gain in instruction selection you pay back at the call boundary, and for a ~1 ns body the boundary is most of the cost. The compiler's inlined output is the number to beat, and inline asm — not a separate function — is the only form that competes on equal terms.

## 88.4 Techniques

### 88.4.1 Intrinsics first — and why that is not a cop-out

The rule the book has followed since Ch. 12: **if an intrinsic exists, use it.** `__rdtscp`, `_mm_lfence`, `_mm_pause`, `_mm_prefetch`, `_mm_clflushopt`, `_mm_clwb`, `_movdir64b`, `_serialize`, `_umwait`/`_tpause`, `_pdep_u64`, the whole `<immintrin.h>` SIMD surface. The intrinsic is an asm statement (or a compiler builtin that lowers to the instruction) with the constraints written by the compiler vendor, so it is correct by construction, and because it is a builtin rather than an opaque box the optimizer can *reason* about it: it knows `_mm_prefetch` has no outputs, that `__rdtsc` is volatile, that `_mm256_add_epi32` is pure and may be CSE'd and reassociated. §88.3 showed identical cost; §88.5 shows identical bytes. The only reasons left to write `asm` are the instructions with no intrinsic on your toolchain, a *combination* of instructions whose relative order must be fixed (the §88.4.2 fenced counter read), and the `asm goto`/empty-template forms that are not instructions at all.

### 88.4.2 The irreducible cases

Each of these is an `asm` statement in a real hot path, with the constraints that make it right:

```cpp
// (a) user-space PMU read: core cycles (fixed counter 1) via rdpmc, for cycle-exact per-stage budgets (Ch. 2 §2.4.4; Ch. 113 §113.4.4 shows the perf_event_open setup).
//     The counter index goes in ecx; the result comes back in edx:eax. lfence before: no later load may start before the read is taken.
static inline std::uint64_t rdpmc_cycles() {
    std::uint32_t lo, hi;
    asm volatile("lfence\n\trdpmc" : "=a"(lo), "=d"(hi) : "c"(1u << 30 | 1) : "memory");   // (1<<30)|1 = fixed counter 1 (core clocks); mmap'd perf page gives the index (Ch. 113)
    return (std::uint64_t(hi) << 32) | lo;
}
// (b) architecturally serializing before a start timestamp (Ch. 20 §20.2.1): cpuid clobbers eax,ebx,ecx,edx — declare ALL four, or you corrupt rbx (a callee-saved register!)
static inline std::uint64_t rdtsc_serialized() {
    std::uint32_t lo, hi;
    asm volatile("xor %%eax, %%eax\n\tcpuid\n\trdtsc" : "=a"(lo), "=d"(hi) : : "rbx", "rcx", "memory");   // rax/rdx are outputs; rbx/rcx are clobbers
    return (std::uint64_t(hi) << 32) | lo;                      // on Sapphire Rapids+ prefer the one-instruction `serialize` (_serialize())
}
// (c) write back a journal line without evicting it (Ch. 103 §103.4.3): clwb takes a memory operand; the "+m" says the line's contents are "used"
static inline void writeback_line(const void* p) { asm volatile("clwb %0" : "+m"(*static_cast<const char(*)[64]>(p)) : : "memory"); }
// (d) descriptor-then-doorbell (Ch. 83 §83.4): the sfence orders the WC descriptor stores before the doorbell; movdir64b posts the 64-byte doorbell as one TLP
static inline void ring_doorbell(void* bar, const void* desc64) {
    asm volatile("sfence\n\tmovdir64b (%1), %0" : : "r"(bar), "r"(desc64) : "memory");
}
```

The `cpuid` case is the classic trap: it writes four registers, two of which (`rbx`, `rcx`) are not outputs you want, and one of those (`rbx`) is callee-saved — an undeclared clobber there corrupts the *caller's* value, not yours, and the crash is somewhere else entirely. Intel's own guidance now prefers `lfence; rdtsc` (Ch. 20) or `serialize`, and the book does too; the listing exists to show the declaration discipline.

### 88.4.3 `asm goto`: a branch the compiler never materialises as a flag

`asm goto` lets the template jump to C++ labels, so a check that is naturally a compare-and-branch does not have to produce a boolean the compiler then re-tests. Ch. 68's sequence-gap check, written so the common path is a fall-through:

```cpp
std::uint64_t expect_seq = 1;
void check_seq(std::uint64_t wire_seq) {
    asm goto("cmp %0, %1\n\t"
             "jne %l[gap]"                                   // %l[name] names a goto label
             : : "r"(wire_seq), "r"(expect_seq) : "cc" : gap);   // the fifth section lists the labels; asm goto with outputs needs GCC 11+/Clang 13+
    ++expect_seq;                                            // in sequence: fall through
    return;
gap:
    on_gap(wire_seq);                                        // Ch. 70: request retransmission / recover
    expect_seq = wire_seq + 1;
}
```

GCC 13 emits exactly `cmp rdi, rax / jne .L4` and the fall-through increment — no `setne`, no second `test` (§88.5). The Linux kernel uses this form for static branches and exception tables; in a feed handler its honest use is rare, because `if (seq != expect) [[unlikely]]` (Ch. 15) produces the same two instructions on every modern compiler. Reach for it when the condition comes from an instruction the compiler cannot express — a `rdrand` carry flag, a `xbegin` abort code — not to second-guess the branch predictor.

### 88.4.4 The constraints cookbook

| You mean | Write | Not |
|---|---|---|
| read-write operand (`add %1, %0`) | `"+r"(x)` | `"=r"(x) : "0"(x)` (legal, verbose) or `"=r"(x) : "r"(x)` (wrong: two registers) |
| output written before all inputs are read | `"=&r"(out)` (early clobber) | `"=r"(out)` — may alias an input and corrupt it |
| a specific register the instruction demands | `"a"`, `"d"`, `"c"`, `"S"`, `"D"`; `"=A"` = `rdx:rax` pair | `"r"` plus a `mov` in the template (defeats allocation) |
| a memory operand the instruction reads/writes in place | `"+m"(*(char(*)[64])p)` — size the pointee so the compiler knows the extent | `"m"(*p)` on a `char*` (only one byte is "touched") |
| an immediate | `"i"(constant)` or `"n"` | `"r"` of a constant (wastes a register and a `mov`) |
| flags destroyed | `"cc"` | nothing (GCC assumes it on x86; Clang does not) |
| any memory may have changed / must not move | `"memory"` clobber | `volatile` alone (orders emission, not memory) |
| the statement has side effects | `asm volatile` | plain `asm` (deletable, mergeable, hoistable) |
| GCC-only: forbid inlining decisions from treating the asm as "large" | `asm inline` (GCC 9+) — the template counts as one instruction for inlining heuristics | — |
| Intel syntax in the template | `-masm=intel` (GCC/Clang) or `{att|intel}` dialect alternatives | mixing `%rax` and `rax` in one build |

Operand modifiers earn a line: `%k0` forces the 32-bit name of a 64-bit operand, `%b0` the low byte, `%q0` the 64-bit name — the right tool when one operand is used at two widths, rather than declaring it twice. Named operands (`%[px]`, bound by `[px] "r"(px)`) survive edits better than positional ones.

### 88.4.5 Writing a hand-written function that stays correct

When you do write a `.asm` (NASM) or `.S` (GAS) file — a vector kernel the compiler will not produce, a bespoke `memcpy` for one fixed size, a context switch (Ch. 49's stackful coroutines) — the checklist is the ABI of §88.2.4 plus four build-system facts: emit `.note.GNU-stack`; mark the symbol `global`/`.globl` and, in a shared object, give it `.type …, @function` and `.size` so unwinders and `perf` (Ch. 2) can attribute samples to it; provide `.cfi_*` directives (or `-fasynchronous-unwind-tables`-equivalent hand-written CFI) if a C++ exception may ever unwind *through* it (Ch. 90 §90.4.3 — without CFI, `std::terminate`); and declare it `extern "C"` on the C++ side so the name is not mangled. Keep it a *leaf* where possible — a leaf needs no stack frame and can use the red zone — and keep its body under the inlining horizon of the compiled alternative only if you have measured that the call boundary is cheaper than what you replace (§88.3 says it usually is not for small bodies).

## 88.5 Verify the codegen: the statement in its context

Reading the asm *around* an asm statement is how you check the contract. Four `-S -masm=intel` excerpts from the listings (GCC 13.3 `-O2`, labels and directives stripped):

**The intrinsic and the asm statement are the same instruction.** Both `rdtscp_asm` and `rdtscp_intrin`, inlined into the measurement loop, produce `rdtscp` followed by the identical `shl rdx,32 / or rax,rdx` recombination; `grep rdtscp anatomy.s` finds six, one per use, with no stray `mov` — the compiler consumed `=a`/`=d` directly.

**`asm goto` compiles to the branch you wrote.**

```asm
_Z9check_seqm:
        mov     rax, QWORD PTR expect_seq[rip]
        cmp rdi, rax                     ; <- the template, verbatim (#APP … #NO_APP in the .s file)
        jne .L4
        inc     rax                      ; fall-through: ++expect_seq
        mov     QWORD PTR expect_seq[rip], rax
        ret
.L4:    lea     rax, 1[rdi]              ; gap path: expect_seq = wire_seq + 1, then the call
        call    _Z6on_gapm
        mov     QWORD PTR expect_seq[rip], rax
        ret
```

**Non-`volatile` asm is deleted or hoisted.** `unused_stamp()` — an `asm("rdtsc" : "=a"(lo), "=d"(hi))` whose outputs are never read — compiles to a bare `ret` under both compilers: the statement is gone. A loop-invariant non-volatile `rdtsc` inside a summation loop: GCC 13 left it in the loop body; **Clang 18 hoisted it above the loop** (`rdtsc` once, then the vectorized sum), so every "per-iteration timestamp" was the same value. The `volatile` versions stay in place under both. The permission is the documented contract; which compiler exercises it is an accident you must not depend on.

**The memory clobber pins stores; it emits nothing.**

```asm
_Z23publish_without_barrieri:            ; g_a = v; g_b = v+1; g_a = v+2;
        lea     eax, 1[rdi]
        add     edi, 2
        mov     DWORD PTR g_b[rip], eax  ; the first store to g_a is DEAD (overwritten) — eliminated; g_b is stored before g_a
        mov     DWORD PTR g_a[rip], edi
        ret
_Z20publish_with_barrieri:               ; g_a = v; asm volatile("":::"memory"); g_b = v+1; g_a = v+2;
        mov     DWORD PTR g_a[rip], edi  ; every store kept, in program order; no fence instruction anywhere
        lea     eax, 1[rdi]
        add     edi, 2
        mov     DWORD PTR g_b[rip], eax
        mov     DWORD PTR g_a[rip], edi
        ret
```

That is the entire difference between a compiler barrier and a hardware fence, visible: the barrier changed *which stores exist and in what order the compiler emits them*; a `std::atomic_thread_fence(seq_cst)` would have added an `mfence`/`lock` instruction as well (Ch. 39 §39.4.2). On x86-TSO the barrier alone is sufficient for a single-writer publication (Ch. 44) *only* because the hardware already keeps stores in order; on ARM (Appendix A.1) it is not.

The verification habit for any new asm statement: compile with `-S`, find the `#APP`/`#NO_APP` markers (GCC) or the statement text (Clang), and read **what the compiler put in the registers before it, what it took out after it, and whether anything live sits in a register your template writes**. Godbolt (Ch. 4 §4.3) does this interactively; `objdump -d` (Appendix I.9) does it on the shipped binary.

## 88.6 Pitfalls & anti-patterns: the undeclared clobber, and friends

**The undeclared clobber corrupts silently.** The listing that motivates this chapter: a scaled-price multiply using `mul`, which writes `rdx:rax`, declared with only `rax` as output.

```cpp
static inline std::uint64_t scale_bad(std::uint64_t px, std::uint64_t mult) {
    std::uint64_t lo; asm("mul %2" : "=a"(lo) : "a"(px), "r"(mult)); return lo;              // BUG: mul also writes rdx — undeclared
}
static inline std::uint64_t scale_good(std::uint64_t px, std::uint64_t mult) {
    std::uint64_t lo, hi; asm("mul %3" : "=a"(lo), "=d"(hi) : "a"(px), "r"(mult) : "cc"); return lo;   // rdx is an output; flags declared
}
// int main(): for (std::uint64_t i = 1; i <= 1000; ++i) acc += scale_bad(i, 7) + k;  …
```

Compiled at `-O2`, **both GCC 13.3 and Clang 18.1 produce a program that never terminates** (the run was killed by a timeout). The emitted loop shows why: GCC kept the loop counter `i` in `rdx` (`mov edx, 1 … add rdx, 1 … cmp rdx, 1001`), inlined the asm, and `mul rdi` overwrote `rdx` with the high half of the product — zero — every iteration, so `i` never reached 1001. Nothing in the source is a C++ error; no warning fires; the out-of-line version happens to work because the ABI makes `rdx` caller-saved. The rule: **every register the template writes is an output or a clobber, every flag effect is `"cc"`, every memory effect is an operand or `"memory"`** — and verify it in the asm (§88.5), because the compiler cannot.

- **Missing `volatile` on a side-effecting statement.** The deleted `rdtsc`, the hoisted `rdtsc` (§88.5), the `pause` that disappears from a spin loop (Ch. 41), the `clwb` that is CSE'd into one. If the statement is there for its *effect*, it is `volatile`.
- **Treating the memory clobber as a hardware fence.** It orders the compiler, not the CPU (§88.5, Ch. 39 §39.4.2). Cross-thread publication needs `std::atomic` with the right `memory_order` — which emits the plain `mov` on x86 and the `stlr` on ARM (Appendix A.1) — never a bare clobber.
- **Over-declaring `"memory"`.** The opposite error: a memory clobber on a statement that touches no memory forces every cached register value to be spilled and reloaded around it. In a hot loop that is a dozen extra loads and stores per iteration — measure before adding it to a `pause` or a pure arithmetic statement.
- **Specific-register constraints that fight the allocator.** `"a"`, `"d"`, `"c"` are necessary for `mul`, `div`, `rdtsc`, `rdpmc`, shifts by `cl`; using them where `"r"` would do pins values and generates shuffling `mov`s. Read the surrounding code for them.
- **Templates that assume a syntax.** A build with `-masm=intel` (or an `.S` file assembled with `.intel_syntax`) flips every operand order in an AT&T template; use dialect alternatives `{movq %1, %0|mov %0, %1}` or keep asm in one syntax project-wide (Appendix D).
- **Hand-written functions for small bodies.** §88.3: the call boundary costs more than the instruction selection saves below a few nanoseconds of work. Inline asm or an intrinsic competes; a `.asm` file does not.
- **Forgetting that inlining multiplies your statement.** An inline function containing `asm` is inlined into many call sites, each with different live registers; a constraint that was "fine" at one site corrupts another. The clobber bug above is exactly this: the out-of-line `scale_bad` worked.
- **TSX/RTM as an "irreducible case".** `xbegin`/`xend` are the textbook example and the wrong one: Intel disabled RTM by microcode on Ice Lake-SP-era parts and removed it from later ones. Do not design a hot path around it.

## 88.7 Exercises & checklist

1. **Reproduce the clobber bug.** Build `clobber_bug.cpp` with both compilers at `-O2`; confirm it hangs; read the loop in `-S` output and name the register. Add `"=d"(hi)`; confirm termination and that the loop counter moved.
2. **Hoist or not.** Build `nonvolatile.cpp` with GCC and Clang; find the `rdtsc` in each `-S`; explain in one sentence why Clang's loop reads the same timestamp every iteration and GCC's does not — and why both are allowed.
3. **Beat the compiler, fairly.** Take Ch. 68's `decode()` and write a NASM version; measure out-of-line vs out-of-line, then vs inlined, as §88.3 does. Then inline *your* instruction selection as an `asm` statement inside the C++ function with `"=r"` outputs and compare again.
4. **A doorbell on the reference machine.** With a NIC or FPGA BAR scratch register (Ch. 83 §83.3 — never a live doorbell), measure `movdir64b` vs four `vmovdqa` to a write-combining mapping with `sfence`; count PCIe TLPs with the device's counters if it exposes them.
5. **`rdpmc` per stage.** Using Ch. 113 §113.4.4's `perf_event_open` + `mmap` setup, replace the `rdtscp` stage stamps of Ch. 89 (tracing) with `rdpmc` core-cycle reads in one stage; compare the two distributions and the cost of the read itself.

Checklist for every `asm` statement in a review:

- [ ] Every register the template **writes** is an output (`=`/`+`) or a clobber; `"cc"` if flags change; `"memory"` if memory is touched beyond the operands.
- [ ] `volatile` if the statement exists for its side effect; **not** `volatile` only if it is a pure function of its inputs and may be merged/hoisted/deleted.
- [ ] Early-clobber `&` on any output written before the last input is read.
- [ ] An intrinsic does not exist for this (checked `<immintrin.h>` / the Intel Intrinsics Guide); if it does, the intrinsic is used.
- [ ] The `-S` output has been read at the hottest call site: nothing live sits in a register the template writes.
- [ ] If it is a hand-written function: ABI (callee-saved, alignment, red zone, sret), `.note.GNU-stack`, symbol type/size, CFI if exceptions may pass — and the out-of-line cost was measured against the inlined alternative.
- [ ] The measurement that justifies it exists and names toolchain, flags, CPU, kernel (Appendix L.3).

## 88.8 References

- GCC manual, *Extended Asm — Assembler Instructions with C Expression Operands*, *Constraints for asm Operands* and *x86 Operand Modifiers* — the normative description of outputs, inputs, clobbers, `volatile`, `asm goto`, `asm inline` and the dialect alternatives (§88.2, §88.4.4).
- Clang documentation, *Language Extensions: Inline Assembly* and the LLVM *Inline Assembler Expressions* reference — Clang's constraint support and divergences (§88.5).
- Intel® 64 and IA-32 Architectures Software Developer's Manual, Vol. 2 — `RDTSCP`, `RDPMC`, `CPUID`, `SERIALIZE`, `MOVDIR64B`, `CLWB`, `CLFLUSHOPT`, `SFENCE`; Vol. 3 Ch. 24 (performance monitoring: fixed counters and the `rdpmc` index encoding) (§88.4.2).
- Intel, *How to Benchmark Code Execution Times on Intel IA-32 and IA-64 Instruction Set Architectures* (Paoloni) — the `cpuid; rdtsc` / `rdtscp; cpuid` fencing recipe and its clobbers, superseded on current parts by `lfence`/`serialize` (§88.4.2; Ch. 20).
- *System V Application Binary Interface, AMD64 Architecture Processor Supplement* — the register, stack-alignment, red-zone and struct-return rules a hand-written function must honour (§88.2.4; Ch. 4 §4.2.1).
- NASM manual, *The Netwide Assembler* — syntax, `default rel`, sections and `.note.GNU-stack` (§88.3, §88.4.5).
- Google Benchmark source, `DoNotOptimize`/`ClobberMemory` — the two statements of §88.2.3 as shipped (Ch. 3 §3.4.1).
- Linux kernel, `arch/x86/include/asm/` (`barrier.h`, `msr.h`, `special_insns.h`) and `include/linux/compiler.h` — production-grade asm statements for `rdtsc`, `rdpmc`, `clwb`, `movdir64b`, barriers and `asm goto` static branches, with every clobber declared (§88.4.2–§88.4.3).

## 88.9 Additional Reading

- Agner Fog, *Optimizing subroutines in assembly language* — the hand-written-function craft: instruction selection, register use, calling conventions across OSes (§88.4.5).
- Intel Intrinsics Guide (online) — the first stop before any `asm`: if the instruction is listed, use the intrinsic (§88.4.1).
- Matt Godbolt, *"What Has My Compiler Done for Me Lately?"* (CppCon 2017) — reading asm around your code, the habit §88.5 applies to asm statements (Ch. 4).
- Ch. 20 (fenced timestamps), Ch. 39 §39.4.2 (compiler vs hardware barriers), Ch. 83 §83.4 (doorbells and write-combining), Ch. 113 §113.4.4 (`rdpmc` with `perf_event_open`), Appendix A.1 (why the memory clobber is not enough on ARM), Appendix D (`-masm=`, `-S`, unwind-table flags), Appendix I.9 (`objdump`/`-S` one-liners).

---

*Next: Ch. 89 — Rust for the Hot Path: the same kernels — Ch. 43's SPSC ring, Ch. 68's decoder, Ch. 32's book update — written in Rust and measured against the C++ they shadow; where ownership replaces the zero-allocation discipline, where `unsafe` has to live, which bounds checks the optimizer removes, and the asm read side by side to show where parity holds and where it breaks.*
