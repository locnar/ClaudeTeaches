# Part III — Compile-Time & Language Mechanics

# Chapter 24 — The Cost of Modern Idioms & Strings Without Allocation

> **Prerequisites:** Ch. 15 (indirect calls — the `std::function` measurement this chapter extends to lambdas, function pointers and the lighter erasers of §15.4.5), Ch. 21 (zero-cost abstraction as an asm claim; strong types), Ch. 23 (the cost of abstractions — and §23.4.5, which decides whether a `string_view`, an `optional` or an `expected` travels in registers), Ch. 4 (reading the loop in asm; `-Rpass-missed`), Ch. 10 (object layout — a closure is a struct).
>
> **Leads into:** Ch. 25 (aliasing — the one reason a `string_view` over a byte buffer stays scalar), Ch. 31 (containers keyed by the symbol types built in §24.4.4), Ch. 35 (what the vectorizer needs from a loop, which §24.4.2 shows a view can take away), Ch. 65 (the receive buffer every `string_view` field points into) and Ch. 89 (log arguments: symbols as integers, not strings).

---

## 24.1 Why it matters: idiomatic C++ is not free by default, only by inspection

Part III has argued that abstraction is free *when the optimizer can see through it* (Ch. 21) and expensive when it cannot (Ch. 23). The idioms that make modern C++ pleasant — lambdas, `std::ranges` pipelines, `std::function` callbacks, `std::tuple`/`std::apply`, `std::string_view`, `std::optional`, `std::expected`, and `std::string` everywhere a symbol or a venue name appears — sit on both sides of that line, and a feed handler written in 2026 is full of them. A `for_each` over a book with a lambda compiles to the loop you would have written. The same loop as `views::filter | views::transform` compiles, with one compiler, to a loop that the vectorizer declines and that costs 35 % more per level; with another, to the same loop again. A `std::string` symbol compares in 1.3 ns and looks up in 14 ns; the same eight characters as a `uint64_t` compare in one `cmp` and look up in 2–5 ns, and as an interned index they look up in nothing. None of these are reasons to avoid the idiom. They are reasons to know which ones the compiler collapses, which ones leave a shape the optimizer cannot cross, and which ones allocate — then to use them where they cost nothing and replace them where they do.

This chapter measures the idioms on one book-update loop and one symbol table (§24.3), explains the mechanism behind each result (§24.2), gives the hot-path form of each (§24.4) and reads the asm (§24.5). The thesis is the book's: a lambda passed as a template parameter and a `std::apply` over a tuple are *provably* zero-cost — the asm is identical to the hand-written form; a `std::function` and an unconstrained `views::filter` are not, for reasons that are structural, not accidental; and a symbol is an integer that happens to print as letters.

---

## 24.2 Mental model

### 24.2.1 A lambda is a class; a `std::function` is a pointer to one

A lambda expression denotes an object of a unique, unnamed **closure type**: a `struct` with one data member per capture (laid out like any struct — Ch. 10) and an `operator()` whose body is the lambda's. `[](int64_t x){…}` is an empty struct (`sizeof` 1); `[k]` with an `int64_t` is 8 bytes; `[&k]` is a pointer, 8 bytes; four captured pointers are 32 bytes. Nothing about this is indirect: when a lambda is passed to a function template (`template <class F> void drive(F f)`), `F` *is* the closure type, the call `f(x)` names a specific `operator()` and the optimizer inlines it like any other member function. A generic lambda (`[](auto x)`) is a closure type whose `operator()` is a *template* — one instantiation per argument type, the Ch. 20 cost model.

`std::function<Sig>` replaces the closure *type* with a *signature*: it stores the closure (inline if it fits the small buffer — 16 bytes in libstdc++, so two pointers' worth of capture; on the heap otherwise) plus a pointer to a type-specific invoker. The call becomes `invoker(storage, args…)`: an indirect call whose target the call site cannot see, so nothing inlines across it, plus — in libstdc++ — the arguments pass by forwarding reference, so a `const Level&` argument that was in a register is spilled to the stack for the invoker to take its address (§24.5). That is why Ch. 15 measured `std::function` at 1.4 ns against 0.3 ns for the template form, and why §24.3 measures it at 1.55 ns on this loop with both compilers: the cost is the call shape, not the lambda. A function pointer has the same shape minus the spill, and one compiler (Clang) proves the pointer constant through the call and specialises the callee — the fastest form of the indirect callable is the one the optimizer could devirtualise.

### 24.2.2 Iterators, sentinels and views: what a pipeline compiles to

A `std::ranges` *algorithm* over a `vector` is the raw loop with a different spelling: `ranges::fold_left(v, 0, plus)` iterates pointer to pointer, and both compilers emit the same code as the `for`. A *view* is different in kind: `v | views::transform(f)` is a range whose iterator wraps the underlying one and applies `f` on dereference — still a pointer underneath, still collapsible, and §24.3 shows it is (Clang: identical; GCC 13 at `-O2`: +18 %, at `-O3`: identical). `views::filter(p)` is where the shape changes. Its iterator's `++` is a `find_if` to the next element satisfying `p`, its `end()` is a *sentinel* reached by that search, and the loop's trip count is unknown until the search finishes. The vectorizer wants a counted loop with a conditional *value* (a select/blend); the filter view gives it a loop whose *control flow* depends on the data — and Clang's `-Rpass-missed=loop-vectorize` says exactly that: `ranges_util.h:510: loop not vectorized` for the view, `vectorized loop (width 4, interleaved 4)` for the raw `if (qty > 50) s += …` (§24.5). `views::take(n)` goes the other way: it *knows* its bound, so the loop is counted and vectorizes, while the hand-written `i < n && i < b.size()` has two conditions and does not — the view was faster by 2–5× (§24.3). The rule is not "views are slow"; it is "a view that turns a counted loop into a searched one costs what a searched loop costs".

### 24.2.3 Small values by value: `string_view`, `optional`, `expected` and the ABI

A `std::string_view` is a pointer and a length — 16 bytes, trivially copyable, so the SysV ABI passes it in two registers (`rdi`/`rsi`) and returns it in `rax`/`rdx` (Ch. 23 §23.4.5; Ch. 4 §4.2.1). `std::optional<int64_t>` is 16 bytes (value + engaged flag, padded) and returns in `rax:rdx`; `std::expected<int64_t, Err>` likewise. Passing any of these *by value* costs exactly what passing two integers costs, and the §23.5 asm proves it. The cliff is size and triviality: an `expected<Fill, Err>` whose `Fill` is 16 bytes is 24 bytes and returns through a hidden pointer (`sret`), and any type with a non-trivial destructor is passed by invisible reference however small. `string_view`, `optional<trivial>` and `expected<trivial, enum>` are on the right side of the cliff by design; the discipline is to keep them there (§24.6).

### 24.2.4 Where strings hide the allocation

`std::string` is 32 bytes in libstdc++ with a 15-character small-string buffer (libc++: 24 bytes, 22 characters); a symbol of 7 characters constructed from the wire costs 1.8 ns and no heap, a 30-character instrument name costs 9–11 ns and a `malloc` (§24.3) — on the hot path, Ch. 29's forbidden syscall-in-waiting. Comparison of two `std::string`s is a length compare and a `memcmp` call (1.3–1.6 ns for 7 characters); hashing for an `unordered_map` is a byte loop over the key (the 11–14 ns lookup). A `string_view` removes the allocation and the ownership but not the `memcmp`, the hash loop or the 16-byte key (and adds a lifetime obligation). An eight-character symbol *is* a 64-bit integer: copying it from the wire is one load, comparing it is one `cmp`, hashing it is one multiply, and a table keyed by it is a `flat_map`/open-addressing lookup at 2–5 ns (Ch. 31). An *interned* symbol — a dense id assigned at startup — turns the lookup into an array index at zero cost. The hot path should see symbols as integers; strings are for the humans at the edges (logs — Ch. 89 — and configuration).

---

## 24.3 Measure it: one book loop, one symbol table

*Measured on the 4-vCPU Intel Xeon VM of Appendix L.2 (TSC 2.7 GHz, Linux 6.18, no isolation), GCC 13.3 and Clang 18.1, `-std=c++23 -march=x86-64-v3`; VM-measured — Appendix L.3 for the reference machine. The loop body is a book of 4 096 `Level{px, qty, orders}` (16 bytes each, L1-resident), 20 000 passes; each variant is `[[gnu::noinline]]` and the book is clobbered between passes so nothing hoists.*

```cpp
// idioms.cpp (excerpt) — the variants; the harness is the usual steady_clock loop.
struct Level { std::int64_t px; std::int32_t qty; std::int32_t orders; };
std::int64_t raw_loop(const std::vector<Level>& b)      { std::int64_t s = 0; for (const auto& l : b) s += l.px * l.qty; return s; }
std::int64_t ranges_fold(const std::vector<Level>& b)   { return std::ranges::fold_left(b | std::views::transform([](const Level& l){ return l.px * l.qty; }), std::int64_t{0}, std::plus<>{}); }
std::int64_t raw_filtered(const std::vector<Level>& b)  { std::int64_t s = 0; for (const auto& l : b) if (l.qty > 50) s += l.px * l.qty; return s; }
std::int64_t views_filtered(const std::vector<Level>& b){ return std::ranges::fold_left(b | std::views::filter([](const Level& l){ return l.qty > 50; })
                                                                                           | std::views::transform([](const Level& l){ return l.px * l.qty; }), std::int64_t{0}, std::plus<>{}); }
std::int64_t raw_take(const std::vector<Level>& b, std::size_t n)  { std::int64_t s = 0; for (std::size_t i = 0; i < n && i < b.size(); ++i) s += b[i].px; return s; }
std::int64_t views_take(const std::vector<Level>& b, std::size_t n){ std::int64_t s = 0; for (const auto& l : b | std::views::take(n)) s += l.px; return s; }
std::int64_t template_cb(const std::vector<Level>& b, auto&& f)                                        { std::int64_t s = 0; for (const auto& l : b) s += f(l); return s; }
std::int64_t fnptr_cb(const std::vector<Level>& b, std::int64_t (*f)(const Level&))                    { std::int64_t s = 0; for (const auto& l : b) s += f(l); return s; }
std::int64_t stdfunction_cb(const std::vector<Level>& b, const std::function<std::int64_t(const Level&)>& f) { std::int64_t s = 0; for (const auto& l : b) s += f(l); return s; }
```

```
                                            GCC 13.3 -O2    GCC 13.3 -O3    Clang 18.1 -O2
raw loop (sum px*qty)                        0.44 ns/elem    0.42            0.37
ranges::fold_left | views::transform         0.52  (+18 %)   0.42  (same)    0.36  (same)
raw loop, filtered (qty > 50)                0.75            0.52            0.37  ← vectorized as a select
views::filter | transform | fold             0.88  (+17 %)   0.58  (+12 %)   0.50  (+35 %) ← not vectorized
raw loop, first n (two loop conditions)      0.31            0.26            0.13
views::take(n)                               0.13            0.06            0.05  ← the view KNOWS its bound
callback: template parameter (lambda)        0.35            —               0.36  (= raw loop)
callback: function pointer                   1.04            —               0.36  (Clang specialised the constant callee)
callback: std::function                      1.55            —               1.55
```

Three readings. **Algorithms and `transform` are free once the optimizer has room**: GCC's `-O2` (whose vectorizer runs only the very-cheap cost model) leaves 0.08 ns/elem of adaptor on the table that `-O3` and Clang remove entirely. **`filter` changes the loop's shape** and costs 12–35 % at every optimization level with both compilers — the raw `if` became a branchless vector select, the view's `find_if` did not. **`take` is faster than the hand-written bounded loop** because it supplies one counted bound where the hand loop had two conditions. The callback column is Ch. 15's result restated on a real loop: template parameter = raw loop; `std::function` = 4× slower, independent of compiler; the function pointer sits wherever the optimizer's interprocedural analysis leaves it.

**The symbol table** (2 048 seven-character symbols, a probe hit on every lookup, `-O2`):

```
                                                   GCC 13.3     Clang 18.1
compare: std::string == std::string                 1.3 ns       1.6 ns      length check + memcmp call
compare: string_view == string_view                 1.4          1.5         same, minus the ownership
compare: Sym8 (uint64) == Sym8                      0.1          0.1         one cmp
lookup: unordered_map<std::string>                 14.0         11.3         byte-loop hash + node chase (Ch. 31)
lookup: unordered_map<string_view>                 14.9         18.6         same hash, 16-byte key
lookup: unordered_map<Sym8>                         4.6          2.3         one-multiply hash
lookup: interned id → array index                   0.1          0.0         the id IS the index
construct: std::string from 7 wire chars (SSO)      1.9          1.8         no heap — under the 15-char buffer
construct: std::string from 30 chars (heap)        11.4          8.7         malloc + free per message
construct: FixedString<32> from 30 chars            5.2          0.5         memcpy into inline storage
construct: Sym8 from a 7-char string_view           6.0          1.4         variable-length memcpy (GCC calls libc) — use the fixed 8-byte load from the wire (§24.4.4)
sizeof: std::string 32 (SSO capacity 15), string_view 16, Sym8 8, FixedString<32> 33
```

The 14 ns `std::string` lookup against the 0.1 ns interned index is the whole argument of §24.4.4 in one row — and the 11 ns heap construction is the row that puts a `std::string` in Ch. 29's category of things that must not happen per message.

---

## 24.4 Techniques

### 24.4.1 Callables: the template parameter first, the pointer second, erasure last

- **Take callables as template parameters** (`template <class F> … F&& f`, or C++20's abbreviated `auto&& f`) wherever the callee is fixed at the call site — strategy hooks, per-level visitors, comparators. The closure type reaches the optimizer; the call inlines; §24.3's template column equals the raw loop. This is also the form that lets a captured constant (`[k]`) be folded into the loop body.
- **A plain function pointer when the set of callees is open but small.** One indirect call, no storage, no allocation, predictable when monomorphic (Ch. 15 §15.3) — and devirtualisable when the optimizer can see the constant (Clang did; GCC 13 did not). Keep it monomorphic per call site.
- **`std::function_ref` (C++26) / a two-word `function_ref` for passing a callable *down* a call chain** without templating the chain — one indirect call, no allocation, no empty check; Ch. 15 §15.4.5 measures it at the function-pointer cost. **`std::move_only_function` (C++23)** when the callable must be *stored* and owned (a completion callback) — same call cost as `std::function`, no copy requirement, a 24-byte small buffer in libstdc++ 13. **`std::function`** for configuration and setup, never inside the per-message loop (§24.3: 4×).
- **Generic lambdas are templates** — `[](const auto& l)` instantiates once per argument type (fine) and, when stored in a `std::function`, erases *one* instantiation (fine, but the others are not reachable). Prefer a concrete parameter type on the hot path so the signature documents the instantiation that will run.

### 24.4.2 Ranges and views: algorithms freely, `filter` with the asm open

- **Use `std::ranges` algorithms as the default loop.** `ranges::for_each`, `fold_left`, `transform`, `sort`, `lower_bound` with projections are the raw loop with better names and the same codegen (§24.3, §24.5). A projection (`ranges::sort(levels, {}, &Level::px)`) is a member pointer resolved at compile time — free.
- **`views::transform`, `views::take`, `views::iota`, `views::zip` (C++23) keep the loop counted** and compile to the same or better code (`take` *removed* a loop condition in §24.3). `views::zip` over two parallel SoA arrays (Ch. 9) is the idiomatic way to iterate a struct-of-arrays book without an index variable, and it vectorizes.
- **`views::filter` and `views::take_while` turn a counted loop into a searched one.** Measured 12–35 % slower than the equivalent `if` inside the loop (§24.3), because the vectorizer cannot form a counted trip. On a hot loop, write the condition *inside* the body (a select), or filter into a dense index array once per update and iterate that. Check with `clang++ -Rpass-missed=loop-vectorize` / `g++ -fopt-info-vec-missed` (§24.5): if the remark names a `ranges_util.h` or `ranges_algo.h` line, the view took the vectorization.
- **Know the two gotchas that are correctness, not speed:** `views::filter` caches `begin()` and must not be iterated twice after modifying the range, and a view over a temporary dangles — `auto v = make_levels() | views::take(5);` holds a reference to a destroyed vector. Views on the hot path should be over named, long-lived containers and consumed immediately.

### 24.4.3 `std::invoke`, `std::apply` and `std::tuple`: free in the call, not always in the layout

`std::invoke(&Level::px, l)` compiles to the member load; `std::apply(add3, std::tuple{a, b, c})` compiles to `add3(a, b, c)` — §24.5 shows the bodies are instruction-identical to the direct forms with both compilers, because `invoke`/`apply` are `constexpr` forwarding wrappers that vanish at `-O2`. The cost to watch is elsewhere: a `std::tuple<int64_t, int32_t, int8_t>` is laid out by the library's recursive-inheritance scheme (libstdc++ stores elements in *reverse* order), so a tuple used as a *stored record* may pad differently from the equivalent struct (Ch. 10 — check with `static_assert(sizeof(tuple) == sizeof(Record))`), and `std::get<I>` through a `std::tuple&` adds nothing while a tuple *passed by value* follows the ABI classification of its layout (a 16-byte tuple of two `int64_t` rides in registers; a 24-byte one does not — Ch. 23 §23.4.5). Use `tuple`/`apply` for compile-time plumbing (fold over a parameter pack, return multiple values by structured binding from a *named* struct); use a named struct for anything stored in a container or crossing a hot ABI boundary.

### 24.4.4 Strings without allocation: symbols as integers, views over the buffer, fixed capacity, interning

- **An 8-character symbol is a `uint64_t`.** Load it from the wire with one fixed-size `memcpy`/`bit_cast` (one `mov` — §24.5), compare with `==` (one `cmp`), hash with one multiply. Pad short symbols with `\0` (or spaces, as the venue does — pick one and normalise on ingest). Ch. 22's `Chars` encoding gives a `string_view` over the 8 wire bytes; the first thing the handler does with it is `bit_cast` the eight bytes into a `Sym8`. For 12-character venue symbols use two words or a 16-byte struct (still two registers).

  ```cpp
  struct Sym8 { std::uint64_t v; bool operator==(const Sym8&) const = default; };
  inline Sym8 sym_from_wire(const std::uint8_t* p) { Sym8 s; std::memcpy(&s.v, p, 8); return s; }   // one load (§24.5)
  struct Sym8Hash { std::size_t operator()(Sym8 s) const noexcept { return (s.v * 0x9E3779B97F4A7C15ull) >> 32; } };
  ```

- **`std::string_view` for anything you only *read*** — parsing FIX tags, matching a venue name at startup, logging. It carries a lifetime obligation: a view over the receive buffer is valid until the ring slot is reused (Ch. 65), so a view that must survive the message is copied into a `Sym8` or a fixed-capacity string *before* the handler returns. Never store a `string_view` in a long-lived structure that outlives the buffer it points into.
- **Fixed-capacity strings for the few variable-length strings the hot path must hold** (a client order id, a venue-assigned order token): `FixedString<N>` — `char buf[N]; uint8_t len;` — is trivially copyable, allocation-free, and constructs at 0.5–5 ns against `std::string`'s 9–11 ns plus a heap round-trip (§24.3). `std::inplace_vector<char, N>` (C++26, Appendix H) is the standard spelling of the same object; `etl::string<N>` and `boost::static_string` are the shipping ones.
- **Intern symbols at startup.** Build the `symbol → dense id` table *once* (a `flat_map<Sym8, uint32_t>` or open-addressing map, Ch. 31), hand every per-symbol structure an array indexed by id, and never look a symbol up on the hot path again: the id arrives *with* the subscription, and the lookup is `book[id]` (0.1 ns in §24.3 — it is an indexed load). The one hot lookup that remains — mapping a wire symbol to an id for an *unexpected* instrument — hits the `Sym8` map at 2–5 ns, and only once per instrument per session.
- **Keep strings where humans are.** Configuration, logs (Ch. 89 logs the `Sym8` or the id and renders the string off the hot path), exception messages, the session logon. The hot path sees integers.

---

## 24.5 Verify the codegen: what collapsed, what did not

**Verified** Clang 18.1 and GCC 13.3 output (`-O2 -std=c++23 -march=x86-64-v3 -S -masm=intel`).

**`std::apply`/`std::invoke` are the direct call.** The wrapper leaves nothing:

```asm
; via_apply(tuple<long,long,long>)  == add3(a,b,c):       ; via_invoke(const Level&) == direct(const Level&):
        mov     rax, qword ptr [rdi + 8]                    ;         movsxd  rax, dword ptr [rdi + 8]   ; l.qty
        add     rax, qword ptr [rdi + 16]                   ;         imul    rax, qword ptr [rdi]       ; * l.px
        add     rax, qword ptr [rdi]                        ;         ret
        ret                                                 ; (direct() is these same two instructions)
```

**`views::filter` kept the branch; the raw `if` became a select.** Clang's remarks for the §24.3 file:

```
idioms.cpp:16: remark: vectorized loop (vectorization width: 4, interleaved count: 4)   ← raw_filtered
…/ranges_util.h:510: remark: loop not vectorized  [-Rpass-missed=loop-vectorize]      ← views_filtered's find_if
```

and the loop bodies:

```asm
; raw_filtered — the qty > 50 test is a vector compare feeding a blend; 4 levels per iteration:
        vpcmpgtd …                          ; qty > 50 lanes
        vpmuldq / vpand / vpaddq ymm…        ; px*qty where true, 0 where false, accumulated
; views_filtered — a scalar search loop: advance until qty > 50, then one multiply, then search again:
.Lnext: cmp     dword ptr [rcx + 8], 50      ; filter predicate on THIS element
        jg      .Ltake                       ;   data-dependent branch — no counted trip, no vector
        add     rcx, 16
        cmp     rcx, rdx
        jne     .Lnext
.Ltake: movsxd  rax, dword ptr [rcx + 8]
        imul    rax, qword ptr [rcx]         ; the transform
        add     rcx, 16                      ; …and back into the search
```

**`std::function` spills the argument.** Inside `stdfunction_cb`'s loop (Clang): `cmp qword ptr [r14 + 16], 0` (empty check), `mov qword ptr [rsp + 8], r13` (the `const Level&` argument written to the stack so the invoker can take its address), `call qword ptr [r14 + 24]` — three things the template version does not do. The `function_ref` call site in Ch. 15 §15.5 is the comparison: two loads and one `call`, no spill.

**A symbol is an integer.**

```asm
; bool eq(Sym8 a, Sym8 b)            ; Sym8 from(const char* p)  — memcpy(&v, p, 8)
        cmp     rdi, rsi             ;         mov     rax, qword ptr [rdi]      ; ONE load; no call, no loop
        sete    al                   ;         ret
        ret
```

Compare with `std::string::operator==`: a length compare, a `memcmp@PLT` call and the return-value test — and with `std::hash<std::string>`, a call into `_Hash_bytes`. The habits: `-Rpass-missed=loop-vectorize` / `-fopt-info-vec-missed` on any loop written as a view pipeline; `grep -c call` on the hot function's asm (a callable idiom that inlined has none); `nm -C | grep memcmp\|_Hash_bytes\|operator new` on the hot TU — any hit is a string on the hot path.

---

## 24.6 Pitfalls & anti-patterns: idioms that leave a shape the optimizer cannot cross

- **`std::function` as the per-message callback.** 4× the template form (§24.3) and a possible allocation at construction (Ch. 15 §15.6). Template parameter for a fixed callee, `function_ref` for a passed-through one, `move_only_function` only for a *stored* one.
- **`views::filter` in a hot loop and no asm check.** 12–35 % slower, both compilers, every level (§24.3); the raw `if` vectorizes. Filter into an index array once, or write the condition in the body.
- **A view over a temporary.** `make_levels() | views::take(5)` dangles after the full expression; `views::filter` iterated twice after mutation is UB. Views over named containers, consumed at once.
- **`std::string` as the symbol key or the per-message field.** 11 ns and a heap allocation for anything over the SSO threshold (§24.3); 14 ns per lookup against 0.1 ns for an interned id. `Sym8`/id on the hot path; strings at the edges.
- **`string_view` stored past its buffer's life.** The receive ring recycles the slot; the view now reads the *next* datagram. Copy to `Sym8`/`FixedString` before returning from the handler.
- **Variable-length `memcpy` where the field is fixed.** `Sym8::from(string_view)` with `std::min(8, size)` became a libc call in GCC (6 ns, §24.3); the wire field is always 8 bytes — copy 8.
- **A `std::tuple` as a stored record.** Library layout order and padding are not the struct's (§24.4.3); a `sizeof` surprise in a 64-byte-aligned ring slot. Name the struct.
- **An `expected`/`optional` that grew past 16 bytes or gained a destructor.** It now returns through memory (Ch. 23 §23.4.5); keep `T` trivially copyable and ≤ 8 bytes plus the discriminant, or return by out-parameter deliberately.
- **Generic lambdas stored in erased callables.** `[](const auto&)` in a `std::function<…(const Level&)>` erases one instantiation; a second call type silently instantiates another elsewhere. Concrete parameter types on the hot path.
- **Trusting `-O2` alone for a ranges-heavy TU on GCC 13.** The adaptor cost disappears at `-O3` (§24.3) — or stays if the TU must be `-O2` for size; measure the TU at the level it ships at (Ch. 26).

---

## 24.7 Exercises & checklist

**Exercises**

1. **Vectorize the filter.** Rewrite `views_filtered` three ways — condition in the body, a `views::filter` into a pre-sized index `vector<uint32_t>` rebuilt once per update, and a masked `std::simd` (Ch. 35 §35.4.4) — and measure each against §24.3. Which keeps the pipeline readable and the loop counted?
2. **Find the spill.** Compile `stdfunction_cb` and `template_cb` with `-S` and count stack stores inside the loop. Replace `std::function` with the two-word `function_ref` of Ch. 15 §15.4.5 and count again.
3. **The 12-character symbol.** Design a `Sym12` (two words or a 16-byte struct) and confirm with the §23.5 method that it passes and returns in two registers; measure its map lookup against `Sym8` and `std::string`.
4. **Intern a venue.** Load a 10 000-symbol reference file at startup into a `flat_map<Sym8, uint32_t>` (Ch. 31); measure the one-time interning cost and the per-message `book[id]` access. Then measure what a *miss* (an unknown symbol) costs and decide where it is handled.
5. **`views::zip` over SoA.** Store the book as parallel `px[]`/`qty[]` arrays (Ch. 9) and compute the notional sum with `views::zip` (C++23; GCC 13 / Clang 18 with libstdc++ 13) vs an index loop; check vectorization with `-Rpass=loop-vectorize`.
6. **Audit the hot TU.** `nm -C hot.o | grep -E 'memcmp|_Hash_bytes|operator new|_M_create|_Function_handler'` — each hit is a string, a hash or an erased callable on the hot path. Remove them and re-run.

**Checklist — modern idioms on the hot path**

- [ ] Callables are **template parameters** where the callee is fixed; `function_ref`/function pointer where passed through; **no `std::function` per message** (§24.4.1).
- [ ] Range **algorithms** and `transform`/`take`/`zip` views are used freely; **`filter`/`take_while` are kept out of hot loops** or checked with `-Rpass-missed=loop-vectorize` (§24.4.2).
- [ ] No view is built over a temporary or iterated after the range mutates (§24.4.2).
- [ ] `invoke`/`apply`/`tuple` are confined to compile-time plumbing; **stored records are named structs** with asserted sizes (§24.4.3).
- [ ] Symbols are **integers** (`Sym8`/`uint64`, or an interned id) on the hot path; **no `std::string` constructed or compared per message** (§24.4.4).
- [ ] Every `string_view` over the receive buffer is **copied out before the slot is recycled** (§24.4.4).
- [ ] `string_view`/`optional`/`expected` passed by value are **≤ 16 bytes and trivially copyable** (Ch. 23 §23.4.5).
- [ ] The hot TU's symbol table has **no `memcmp`, `_Hash_bytes`, `operator new` or `_Function_handler`** (§24.5).

---

## 24.8 References

- ISO C++20, [expr.prim.lambda] — closure types, captures and the `operator()` template of a generic lambda (§24.2.1); [func.wrap.func] `std::function` and its small-object storage.
- P0896R4, *The One Ranges Proposal* (E. Niebler, C. Carter, C. Di Bella) — `std::ranges` algorithms, iterator/sentinel pairs and the view adaptors; P2321R2 (`views::zip`, C++23); P2214R2 (*A Plan for C++23 Ranges*) for the `filter` caching and lifetime rules (§24.2.2, §24.4.2).
- P0254R2, *Integrating `std::string_view` and `std::string`* (N. MacIntosh / M. Clow) and [string.view] — the two-word view and its non-owning semantics (§24.2.3).
- P0323R12, *`std::expected`* (V. Botet, J. Bastien) and [optional] — the value-plus-discriminant layout that keeps both in registers when `T` is trivial (§24.2.3).
- P0792R14, *`function_ref`: a type-erased callable reference* (V. Zverovich, T. Brindle, et al., C++26) and P0288R9, *`move_only_function`* (M. Klimek et al., C++23) — the lighter erasers §24.4.1 points at, measured in Ch. 15 §15.4.5.
- P0843R14, *`std::inplace_vector`* (G. Dos Reis et al., C++26) — the standard fixed-capacity container behind `FixedString<N>` (§24.4.4).
- System V Application Binary Interface, AMD64 Architecture Processor Supplement, §3.2.3 *Parameter Passing* — the classification that puts a 16-byte `string_view` in two registers (§24.2.3; Ch. 23 §23.4.5).

## 24.9 Additional Reading

- Chandler Carruth, *"There Are No Zero-Cost Abstractions"* (CppCon 2019) — `std::unique_ptr` by value and the general argument that an abstraction's cost is decided by the optimizer's view of it (§24.1, Ch. 23 §23.4.5).
- Eric Niebler, *"Range-v3 and the Future of C++ Algorithms"* and Tristan Brindle, *"An Overview of Standard Ranges"* (CppCon 2019) — the view model and where the sentinel comes from (§24.2.2).
- Jason Turner, *"Lambdas: From C++11 to C++20"* (CppCon 2019) — closure types, generic lambdas and what they compile to (§24.2.1).
- Nicolai Josuttis, *C++20 — The Complete Guide*, chapters on ranges and views — the caching and dangling rules of §24.4.2 in detail.
- Ch. 15 (*Indirect Calls, Virtual Dispatch & Devirtualization*) — the callable measurements this chapter extends; Ch. 31 (*Cache-Friendly Containers*) — the `flat_map`/open-addressing tables keyed by `Sym8`; Ch. 35 (*SIMD & Vectorization*) — the select the raw loop became; Ch. 65 (*Zero-Copy Wire Handling*) — the buffer every `string_view` points into; **Appendix H** — `ranges`, `zip`, `expected`, `function_ref`, `inplace_vector` availability.

---

*Next: Ch. 25 — Aliasing, `restrict` & Type Punning: the strict-aliasing rules and pointer-overlap assumptions that gate vectorization and reordering, the sanctioned punning channels (`bit_cast`, `memcpy`, `launder`) — and, new to this edition, the object-lifetime model behind every struct-over-bytes overlay in the book and C++23's `std::start_lifetime_as`, the zero-instruction way to make one legal.*
