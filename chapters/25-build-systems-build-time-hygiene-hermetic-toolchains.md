# Part III — Compile-Time & Language Mechanics

# Chapter 25 — Build Systems, Build-Time Hygiene & Hermetic Toolchains

> **Prerequisites:** Ch. 24 (the toolchain flags whose *policy* this chapter enforces — `-march`, LTO, PGO, frame pointers), Ch. 18 (the template cost model — every instantiation is build time and code), Ch. 20 (the compile-time decoder whose per-message-type build cost §20.3 measured), Ch. 19 (the policy templates that make a trading binary template-heavy), Ch. 3 (measurement discipline — a build is a latency path too).
>
> **Leads into:** Part IV and every later chapter's code, which is built the way this chapter says; Ch. 87 §87.4.7 (supply-chain integrity: reproducible, signed builds — the mechanism is §25.4.6), Ch. 93 (build IDs that key a core dump to its symbols), Ch. 103 (the latency-regression gate runs on a build that must be the same build every time); **Appendix D** (per-build flags), **Appendix H** (modules and `import std` availability), **Appendix I.12** (the one-liners).

---

## 25.1 Why it matters: the build is a latency path, and a correctness boundary

Two things in this book are decided by the build system and by nothing else. The first is the **edit–measure loop**: Ch. 3's method is to change one thing and measure it, and a trading binary whose hot translation unit takes 1.3 s to compile and whose full build takes eight minutes gets measured less often than one that rebuilds in nine seconds — not as a matter of discipline but of arithmetic. Part III's techniques make this worse on purpose: `OrderBook<Storage, Locking, Alloc>` (Ch. 19), the schema-generated decoder (Ch. 20 — 4–6 ms per message type per TU), `constexpr` tables (Ch. 18) all move work into the front end, and a header that pulls `<chrono>` costs **566 ms per TU that includes it** (§25.3) before a line of the TU itself is parsed. The second is **flag drift**: Ch. 24 §24.5 warns that a TU built with a different `-march`, a different exception setting or a different `-fno-strict-aliasing` from its neighbours can violate the one-definition rule or the ABI, and Ch. 87 §87.4.7 asks for builds that are reproducible and attributable — but neither chapter says how to *prevent* the drift or *produce* the reproducibility, because both are properties of the build graph, not of any source file.

This chapter treats the build the way the rest of the book treats a hot loop: profile it (`-ftime-trace`, §25.3), find the structural cost (header parsing and template instantiation, not optimization), apply the techniques that change the shape (precompiled headers, unity builds, `extern template` where it actually helps, C++20 modules where the toolchain can — §25.4.2–§25.4.4) and measure each one, because one of them (`extern template` on inline-defined class templates) made the build *slower* on this project. Then it addresses the policy half: one flag policy expressed in the build system so that a per-TU `-O3`/`-march` override cannot change a type's layout or an inline function's meaning (§25.4.5), and a hermetic, pinned toolchain producing bit-identical binaries with a build ID that a core dump, a profile and a latency regression can be keyed to (§25.4.6). CMake+Ninja and Bazel/Buck2 appear as *policies with different defaults*, not as products to review.

---

## 25.2 Mental model

### 25.2.1 Where the time goes: parse, instantiate, optimize, emit, link

A C++ translation unit's compile time has four parts, and the hot-path TUs of a trading system are dominated by the first two. **Parsing** includes every header transitively: the TU of §25.3 spends 921 of 1,341 ms inside `#include`s, 566 ms of it in `<chrono>` (which drags in `<sstream>`, `<istream>`, `<ios>` and the locale machinery for `chrono_io`). **Instantiation** is Ch. 18's cost model made visible: each distinct `Message<Schema>::get<"field">`, each `OrderBook<…>` member, each `std::vector<T>` method used — 542 ms here, overlapping the parse (instantiation happens as uses are seen). **Optimization and code generation** were 174 ms + 17 ms — about 14 % — which is why `-O0` builds are not the fast-iteration answer people hope (they remove the 14 %, and lose Ch. 4's requirement that you read `-O2` code). **Linking** is a separate cost that LTO (Ch. 24 §24.4.2) multiplies: a ThinLTO link of a large binary can cost as much as the compile. Every technique in §25.4 attacks the first two parts: PCH and modules parse the headers once instead of per TU; unity builds parse them N times less often; `extern template` suppresses repeated instantiation — but only of what would otherwise be instantiated, which is the catch §25.3 measures.

### 25.2.2 The one-definition rule across flags: how builds drift

The ODR says every inline function and every class has one definition in the program; the compiler enforces it per TU and *assumes* it across TUs. Flags break the assumption without changing a character of source. `-fno-exceptions` in one TU and not another gives the same inline function two meanings (one with landing pads, one calling `abort`). `-march=icelake-server` in one TU and `-march=x86-64-v2` in another can give `std::simd`/`native_simd<float>` (Ch. 32) a *different size* — 16 lanes vs 4 — in two TUs that share a header, which is an ODR violation that links fine and corrupts memory. `-D_GLIBCXX_ASSERTIONS` in a test TU and not in production changes `std::vector::operator[]`'s body. `-fpack-struct`, `-fshort-enums`, a different `-std=` with a library that keys layout on it, a sanitizer in one object: the same. The linker keeps *one* copy of each inline function (whichever object it saw first) and never checks. The mental model: **the flag set is part of the type system**, and anything that changes a type's layout or an inline function's semantics must be the same for every TU that can see the type — which is exactly what §25.4.5's policy enforces, and what makes the *safe* per-TU overrides (`-O3`, `-funroll-loops`, `-fno-plt`) safe: they change codegen, not meaning.

### 25.2.3 Hermeticity and reproducibility: controlling inputs, proving outputs

A **hermetic** build is one whose output depends only on declared inputs: the sources, the pinned toolchain (compiler, standard library, binutils/linker, target libc headers), the flags and nothing from the host — not `/usr/include`, not `$PATH`, not the time of day. A **reproducible** build is one that produces bit-identical output from the same inputs, which is hermeticity *proved* by a checksum. They matter here for three reasons: the latency-regression gate (Ch. 103) compares builds, and a build that differs by its embedded date differs by a layout shift that moves a hot function across a cache-line boundary (Ch. 12) — noise that looks like a regression; a crash's core (Ch. 93) must be matched to the *exact* binary and debug info by build ID; and Ch. 87's supply-chain argument rests on being able to rebuild the deployed binary from source and compare. The sources of non-reproducibility are few and known: `__DATE__`/`__TIME__`, absolute paths in debug info and `__FILE__`, the random seed GCC uses for anonymous-namespace mangling, build-directory-dependent hashes, and uncontrolled toolchain versions. Each has a flag or an environment variable (§25.4.6), and `cmp` is the test.

### 25.2.4 What a build system is for — CMake+Ninja and Bazel/Buck2 as policies

A build system does three jobs: it owns the **dependency graph** (what rebuilds when a header changes), it owns the **flag policy** (which TU is compiled how), and it decides how **hermetic and cacheable** the build is. The two families on offer make different default choices. **CMake generating Ninja** describes targets and lets the host toolchain in by default: `CMAKE_CXX_COMPILER` is whatever is found, system headers are the host's, caching is bolted on with `ccache`/`sccache`, and discipline comes from conventions — `CMakePresets.json` pinning compiler paths and flags, an interface target that carries the policy flags, a toolchain file for cross/pinned builds. It is simple, universal, and every HFT shop's third-party dependency builds with it. **Bazel and Buck2** make hermeticity the default: every action's inputs are declared, the toolchain is a dependency (a pinned GCC/Clang tarball or a Nix derivation, not `/usr/bin/g++`), outputs are content-addressed so **remote caching and remote execution** come for free, and a flag policy lives in a *toolchain* definition that every target inherits — there is no place to type `-march` into one target. The cost is a steeper model, a harder time with third-party CMake projects and a culture shift. The policy question this chapter cares about — one flag set, pinned toolchain, reproducible output, cached — can be met with either; Bazel/Buck2 make the right thing the default, CMake makes it a convention you must enforce in review. Choose by the team's ability to hold a convention, not by benchmark.

---

## 25.3 Measure it: profiling the build like a latency path

*Measured on the 4-vCPU Intel Xeon VM of Appendix L.2 (Linux 6.18), GCC 13.3 and Clang 18.1, CMake 3.28.3, Ninja 1.11.1, `-std=c++23 -O2 -c`, wall time best of three. The project is 24 translation units that each include one 40-line template-heavy header (`book.hpp`: the Ch. 20 `ct::Message` decoder, three `OrderBook<Storage, Locking>` instantiations, and `<algorithm> <array> <chrono> <functional> <map> <memory> <optional> <ranges> <string> <unordered_map> <vector>`) and define one handler that uses all of them. VM-measured; the ratios are what transfer.*

**Where one TU's 1.3 s goes** (`clang++ -ftime-trace -c tu0.cpp`, read from the JSON — the §25.4.1 script):

```
total 1341 ms:  frontend 1157 ms  (source parse 921 — of which book.hpp 921 incl. nested:
                                     <chrono> 566, chrono_io.h 190, <sstream> 185, <istream> 178, <ios> 163;
                                   template instantiation 542)
                backend    174 ms  (optimization)        codegen 17 ms
```

Seventy percent of this TU is *parsing headers it did not write*, and 42 % is one header, `<chrono>`, included for `steady_clock` in a file that does not format a time. That is the first and cheapest finding `-ftime-trace` produces on most trading codebases.

**Five build strategies for the same 24 TUs** (parallel `-j4`, like a 4-core CI runner):

```
                 one TU    24 TUs, plain   extern template   precompiled header   unity (24 TUs in 1)
GCC 13.3        1,063 ms    6,491 ms          7,381 ms  (+14 %)   5,072 ms  (−22 %)    1,379 ms  (−79 %)
Clang 18.1      1,300 ms    7,771 ms          8,865 ms  (+14 %)   4,227 ms  (−46 %)    1,713 ms  (−78 %)
C++20 named module (Clang 18): precompile book.pcm 1,294 ms (15.3 MB) + 24 importing TUs 2,894 ms = 4,188 ms (−46 %)
```

Read it as a latency table. The **unity build** wins by the width of the problem — the header is parsed once instead of 24 times, and 24 handlers' worth of instantiation is done once — at the cost of one giant TU that cannot build in parallel and in which every TU's anonymous namespace shares a scope (§25.5). **PCH** and the **module** remove the *parse* (the 921 ms) but not the per-TU *instantiation* of the templates each handler uses, which is why they land at −22…−46 % rather than −78 %: GCC's `.gch` is 91 MB and is read 24 times, Clang's `.pch`/`.pcm` is 15 MB and lazily deserialised, which is the difference between the two PCH rows. **`extern template` made the build slower** — +14 % with both compilers — and the reason is structural: `OrderBook<…>`'s members are defined *inside* the class and are therefore implicitly `inline`; the `extern template class` declaration suppresses the out-of-line instantiation that the linker would have deduplicated anyway, while every TU still instantiates the inline bodies to *inline* them — and the explicit-instantiation TU adds a 25th compile. `extern template` pays when the template's members are large, out-of-line and not inlined (libstdc++ does this for `std::basic_string` and the streams, and it is the reason `<string>` is cheap); it does nothing for the small-inline-method policy classes of Ch. 19, and §25.4.2 says when to reach for it.

**Reproducibility** (two builds of the same source):

```
naive (g++ -O2 r.cpp twice, one second apart):                      binaries DIFFER  (__DATE__/__TIME__ embedded)
SOURCE_DATE_EPOCH=1700000000 g++ -O2 -ffile-prefix-map=$PWD=/src -frandom-seed=r.cpp -Wl,--build-id=sha1 r.cpp
   …from two different directories, minutes apart:                   IDENTICAL (cmp exit 0); build ID equal
```

and `-frecord-gcc-switches` leaves the exact flag set in the binary (`readelf -p .GCC.command.line`: `GNU C++17 13.3.0 -mtune=generic -march=x86-64 -O2 -fasynchronous-unwind-tables -fstack-protector-strong …`) — the attribution Ch. 24 §24.4.4 and Ch. 93 want, at zero runtime cost.

---

## 25.4 Techniques

### 25.4.1 Profile the build: `-ftime-trace`, ClangBuildAnalyzer, `-ftime-report`

- **`clang++ -ftime-trace -c tu.cpp`** writes `tu.json` (Chrome trace format: open in `chrome://tracing`/Perfetto, or read it with ten lines of Python). The events that matter: `Source` (per header, nested — the include cost), `InstantiateClass`/`InstantiateFunction` (with the template's name in `args.detail`), `PerformPendingInstantiations`, `Total Frontend`/`Total Backend`. The script that produced §25.3's breakdown:

  ```python
  import json, collections
  ev = [e for e in json.load(open("tu0.json"))["traceEvents"] if e.get("ph") == "X"]
  total = lambda n: next(e["dur"] for e in ev if e["name"] == n) / 1000
  print(f"frontend {total('Total Frontend'):.0f} ms  backend {total('Total Backend'):.0f} ms")
  c = collections.Counter()
  for e in ev:
      if e["name"] == "Source": c[e["args"]["detail"].split("/")[-1]] += e["dur"]          # per-header, incl. nested
  for k, v in c.most_common(8): print(f"  {k:24s} {v/1000:7.1f} ms")
  ```

- **ClangBuildAnalyzer** aggregates the per-TU traces of a whole build (`ClangBuildAnalyzer --all build/ out.bin && ClangBuildAnalyzer --analyze out.bin`): the headers most expensive *in total* across TUs, the templates instantiated most, the functions slowest to optimize. It is the flame graph of the build; run it once per quarter and after any header that goes from 200 ms to 2 s.
- **GCC: `-ftime-report`** (per-pass times, textual; `-ftime-report-details` for more) and `-fdump-lang-class`/`-ftemplate-backtrace-limit` for instantiation chains. GCC has no per-header trace; use Clang for the profile even if GCC is the shipping compiler — the header costs are the same headers.
- **Act on the top row first.** The `<chrono>` finding of §25.3 is typical: replace a convenience include with a forward declaration or a narrow header (`<chrono>` for a clock in one `.cpp`, not in the shared header), and the 24 TUs each lose half a second. Include-what-you-use (`iwyu`) automates the audit.

### 25.4.2 Explicit instantiation and `extern template` — when it pays, and when it costs

```cpp
// book_inst.cpp — ONE TU owns the instantiations:          // book_extern.hpp — every other TU declares them:
template class OrderBook<FlatLevels<64>, NoLock>;            extern template class OrderBook<FlatLevels<64>, NoLock>;
template class OrderBook<MapLevels, NoLock>;                 extern template class OrderBook<MapLevels, NoLock>;
```

The mechanism suppresses *implicit* instantiation of the named specialisation's non-inline members in every TU that sees the `extern` declaration; they are emitted once, in `book_inst.cpp`. It pays when (a) the members are **defined out of class** (or the class is marked so they are not implicitly inline), (b) they are **large** (a decoder's dispatch table, a `std::map` rebalance, an allocator's slow path), and (c) **many TUs** use the same specialisation — the libstdc++ `std::string`/iostream case. It costs when the members are small and inline, because every TU instantiates them *anyway* to inline them, and you have added a TU (§25.3: +14 %). The Ch. 19/Ch. 20 policy classes and decoders are mostly the second kind; the right move for them is the one-decoder-TU-per-venue structure of Ch. 20 §20.4.5 with a *non-template* interface (`decode_add_order(const uint8_t*, Parsed&)`) that other TUs call — the instantiation is then hidden behind an ordinary function and happens once by construction. Measure before adopting `extern template`; it is the one technique in this section that can go either way.

### 25.4.3 Precompiled headers and unity builds — and their ODR traps

- **PCH**: `g++ -x c++-header common.hpp -o common.hpp.gch` then `-include common.hpp` (GCC finds the `.gch` beside it); `clang++ -x c++-header common.hpp -o common.pch` then `-include-pch common.pch`. One PCH per target, containing the stable third-party and standard headers (not your fast-changing ones — a PCH rebuild is the full header cost again); every TU of the target must be compiled with the *same flags* as the PCH or the compiler rejects it (the one place the toolchain enforces §25.2.2 for you). CMake: `target_precompile_headers(hot PRIVATE <vector> <chrono> "book.hpp")`. §25.3: −22 % (GCC, 91 MB `.gch`) to −46 % (Clang).
- **Unity (jumbo) builds**: concatenate N `.cpp` files into one TU (`#include "tu0.cpp" …`; CMake `set_target_properties(hot PROPERTIES UNITY_BUILD ON UNITY_BUILD_BATCH_SIZE 16)`). §25.3: −78 %, the largest win available, because header parsing *and* instantiation are shared. The traps are all ODR-shaped: two files' anonymous-namespace helpers with the same name now collide (or worse, one silently shadows the other); a `static` function or a `using namespace` in one file leaks into the next; macros defined in one `.cpp` survive into the following one; and a TU that was `-O3 -march=icelake` and one that was `-O2` can no longer differ — unity forces §25.4.5's policy, which is a feature if you meant it. Batch by 8–16 files, keep per-file namespaces named, and run the non-unity build in CI too so the collision is caught.

### 25.4.4 C++20 modules today: what works, what does not

A **named module** (`export module book;`) is parsed once into a binary module interface (`book.pcm`/`.gcm`) and `import book;`ed by its consumers; unlike a PCH it is a language feature with real isolation (macros do not leak, the importer sees only exports) and composes (modules import modules). On this host, with the §25.3 project:

- **Clang 18 + libstdc++ 13: works.** `clang++ -std=c++23 --precompile book.cppm -o book.pcm` (1.3 s, 15 MB), then `-fmodule-file=book=book.pcm` on each importer; 24 importers in 2.9 s, total −46 % versus plain — the PCH result, with isolation. The standard headers go in the *global module fragment* (`module; #include <vector> … export module book;`), because `import std;` is **not available**: it needs libc++ 17+ or libstdc++ 15+ (`__cpp_lib_modules`), and libstdc++ 13 has no `std.cppm`.
- **GCC 13 `-fmodules-ts`: does not build this module.** The standard headers in the global module fragment produce `redefinition of 'struct std::__truth_type<true>'` errors; GCC 13's modules are usable for self-contained interfaces without `<bits/…>` in the GMF, and GCC 14/15 fix much of this — Appendix H carries the row. A build that must compile with both GCC 13 and Clang 18 cannot use modules yet.
- **CMake 3.28 + Ninja 1.11 + Clang 18: works, with two conditions.** `target_sources(book PUBLIC FILE_SET CXX_MODULES FILES book_iface.cpp)`, `-DCMAKE_CXX_COMPILER_CLANG_SCAN_DEPS=/usr/bin/clang-scan-deps-18` (CMake runs `clang-scan-deps` to discover the import graph and generates the `--precompile`/`-fmodule-file=` steps and the `.modmap` files for Ninja's dyndep); the interface had to be named `.cpp` (`.cppm` was "not scheduled for compilation" by 3.28 — later CMake versions recognise the extension). The whole scan + precompile + 3 TUs + link took 2.1 s. Bazel has module support behind rules_cc flags; Buck2 has it in progress.
- **Verdict for a trading codebase in 2026:** adopt modules for *your own* stable headers (the decoder, the book, the wire structs) if Clang is your only compiler and your CMake is ≥ 3.28; otherwise PCH + a unity batch gives the same wall-clock win today with no toolchain risk, and the module migration is a mechanical change later because a module interface *is* a header with `export` added.

### 25.4.5 One flag policy without ODR/ABI drift

- **State the policy once, in the build system, as an interface target or a toolchain** — never on individual targets. CMake: an `INTERFACE` library `hft::flags` carrying `-std=c++23 -O2 -march=icelake-server -fno-omit-frame-pointer -fno-plt -fvisibility=hidden -g -frecord-gcc-switches` (the Appendix D.9 line), linked by every target; `CMakePresets.json` pins the compiler path and the preset per configuration; a toolchain file pins the sysroot. Bazel/Buck2: a `cc_toolchain` with the flags in `compiler_flags`/`cxx_builtin_include_directories`, selected by `--config=prod`; targets cannot add contradicting flags without `copts` that review can grep for.
- **Partition flags into *meaning* and *codegen*.** Meaning-changing (must be identical across every TU that shares a type): `-std=`, `-fno-exceptions`/`-fno-rtti`, `-fno-strict-aliasing`, `-fwrapv`, `-D_GLIBCXX_ASSERTIONS`, `-fpack-struct`, `-fshort-enums`, sanitizers, anything that sets `native_simd`'s width (`-march` *when a header uses `std::simd`/`__m256` types* — Ch. 32), the `-stdlib=`. Codegen-only (may vary per TU): `-O2` vs `-O3`, `-funroll-loops`, `-fno-tree-vectorize`, `-falign-functions`, `-fno-plt`, `-march` *for a TU whose shared headers have no ISA-dependent types* (the common case: a `-march=icelake-server` kernel TU in a `-march=x86-64-v3` binary is fine, provided the SIGILL question of Ch. 24 §24.5 is answered by a runtime check and the dispatch of Ch. 32 §32.4.3). Write the partition down beside the policy.
- **Per-TU overrides are declared, not typed.** CMake: `set_source_files_properties(kernel.cpp PROPERTIES COMPILE_OPTIONS "-O3;-funroll-loops")` in *one* file that review watches; Bazel: a `select()`ed `copts` on the one `cc_library`. A `-march` override must come with a `static_assert` that the TU shares no ISA-sized type with the rest (`static_assert(sizeof(Level) == 16)` on every wire struct is the cheap version) — and with the `-fno-plt`/`-fvisibility` rows of Appendix D.4 kept identical, since they change how *symbols* are referenced, which the linker also will not check.
- **Verify with the binary, not the build log.** `readelf -p .GCC.command.line hot.o` (GCC, with `-frecord-gcc-switches`) and `llvm-dwarfdump --debug-info hot.o | grep DW_AT_producer` (Clang records the flags in the DWARF producer string) per object; a 20-line script that extracts both across every `.o` and fails the build on a meaning-class difference is the §25.2.2 check the toolchain does not perform. Appendix I.12 has the one-liners.

### 25.4.6 Hermetic, pinned toolchains and reproducible builds

- **Pin the toolchain as a dependency.** The compiler, libstdc++/libc++, binutils or lld, and the *target* glibc headers come from a versioned artefact — a container image with a digest (`FROM ghcr.io/…/hft-toolchain@sha256:…`), a Nix derivation, or a Bazel `http_archive` toolchain with a `sha256` — never from the host's package manager. The deployment host's glibc version is a *build input* (`--sysroot`), so a binary built against glibc 2.39 is not run on a 2.35 box. The same artefact builds the Appendix L harness, so a benchmark number is tied to a toolchain digest.
- **Make the build reproducible and test that it is.** `SOURCE_DATE_EPOCH` (both compilers honour it for `__DATE__`/`__TIME__`), `-ffile-prefix-map=$SRC=/src` (and `-fdebug-prefix-map`/`-fmacro-prefix-map` as needed) so `__FILE__` and DWARF paths do not carry the checkout directory, `-frandom-seed=<file>` (GCC) so anonymous-namespace mangling is deterministic, `-Wl,--build-id=sha1` so the build ID is a function of the content, a deterministic archiver (`ar D`), and a CI job that builds twice in two directories and `cmp`s — §25.3's result. `-frecord-gcc-switches` and the DWARF producer string record *what* was built; `-g` with debug info **split** (`-gsplit-dwarf`, or `objcopy --only-keep-debug`) and archived by build ID (Ch. 24 §24.4.4, Ch. 93).
- **Cache by content, not by timestamp.** `ccache`/`sccache` in front of CMake+Ninja (hash of preprocessed source + flags + compiler digest → object), or Bazel/Buck2's native content-addressed cache with a remote backend shared by CI and every developer: a hot TU that nobody touched is never rebuilt, and the cache hit rate is a build metric worth a dashboard (Ch. 86's telemetry habit applied to the build). A cache is only safe when the build is hermetic — a host-header dependency the cache key does not see is a stale object that *looks* fresh, which is why §25.2.4's defaults matter.
- **Sign and attest** (Ch. 87 §87.4.7): the release pipeline records the source commit, the toolchain digest, the flag policy hash and the build ID of every binary; the deployment gate (Appendix M) checks the running binary's build ID against the manifest. Reproducibility is what makes the attestation *checkable* by rebuilding.

---

## 25.5 Pitfalls & anti-patterns: the build system is part of the type system

- **`extern template` on inline-defined class templates.** +14 % slower (§25.3): the bodies are instantiated for inlining anyway, and you added a TU. Use it for large out-of-line members; hide small templates behind a non-template interface instead (§25.4.2).
- **A convenience include in the shared header.** `<chrono>` for one clock cost 566 ms × 24 TUs (§25.3). Narrow headers in `.cpp` files; `iwyu`; re-run `-ftime-trace` after every header change to the common set.
- **Unity builds without namespace hygiene.** Two files' anonymous-namespace `helper()` collide or shadow; a `using namespace` leaks; a macro survives into the next file. Named per-file namespaces, and the non-unity build in CI (§25.4.3).
- **A PCH built with different flags from its TUs.** The compiler rejects it (good) — or, with `-Winvalid-pch` silenced, ignores it and you lose the win silently. One PCH per flag set (§25.4.3).
- **`-march` typed on a target whose headers carry ISA-sized types.** Two TUs with a different `native_simd<float>::size()` or `alignas(__m512)` in a shared struct link cleanly and corrupt memory — an ODR violation no tool reports (§25.2.2, §25.4.5). Partition the flags; `static_assert` the shared sizes.
- **Trusting the build log for the flags.** The log shows what the build system *intended*; `readelf -p .GCC.command.line` / the DWARF producer string show what the *object* got (§25.4.5). Check the objects.
- **`import std;` on a toolchain that has no `std` module.** libstdc++ 13 has none; the error is immediate, but a `#ifdef __cpp_lib_modules` fallback to `#include` keeps the TU building on both (Appendix H).
- **A module build with GCC 13.** `-fmodules-ts` fails on standard headers in the global module fragment (§25.4.4). Clang 18 or PCH until GCC 14/15 is the pinned compiler.
- **A toolchain from the host.** `apt-get install g++` on the build box is an undeclared input: the next image refresh changes codegen, layout decisions and your latency numbers. Pin by digest (§25.4.6).
- **A cache in front of a non-hermetic build.** A host header the key does not see produces a stale-but-fresh-looking object. Hermetic first, cache second (§25.4.6).
- **Measuring the build on one core.** CI runners are 4–8 cores; the unity build's −78 % assumes the other strategies had `-j4` to use (§25.3). Measure wall time at the parallelism the real build gets.

---

## 25.6 Exercises & checklist

**Exercises**

1. **Profile your hot TU.** `-ftime-trace` it, run the §25.4.1 script, and name the three most expensive headers. Remove or narrow one; measure the TU and the full build.
2. **Reproduce the strategy table.** Build your own 20–30 TU target plain, with PCH, as a unity batch of 8, and (Clang) as a module; tabulate wall time at `-j4` and `-j16`. Which wins at each parallelism? Why does the unity build's advantage shrink at `-j16`?
3. **Prove `extern template` can hurt.** Take a class template with inline members, add `extern template`, measure (+). Move two large members out of class, measure again (−?). Write down the rule you observed.
4. **Reproducible or not.** Build your production binary twice in two directories five minutes apart; `cmp`. Add the §25.4.6 flags one at a time until it is identical, recording which flag fixed which difference (`diffoscope` shows them).
5. **Catch the drift.** Write the 20-line script that extracts `.GCC.command.line`/`DW_AT_producer` from every object in a build and fails on any difference in a meaning-class flag. Plant a `-fno-strict-aliasing` on one TU and confirm it fires.
6. **Modules, if you can.** With Clang 18 and CMake ≥ 3.28, turn one stable header into a named module; measure the build; note what had to move into the global module fragment.

**Checklist — build hygiene and hermeticity**

- [ ] The build is **profiled** (`-ftime-trace`/ClangBuildAnalyzer) and the top headers and instantiations are known; no convenience include lives in a shared header (§25.4.1).
- [ ] Template instantiation is **structured** — one TU per venue decoder/policy set behind a non-template interface; `extern template` only where measured to help (§25.4.2).
- [ ] PCH and/or unity batches are used for the hot targets, **with the non-unity build still run in CI** (§25.4.3).
- [ ] Modules are adopted only where the pinned compiler and CMake support them; `import std` is gated on `__cpp_lib_modules` (§25.4.4).
- [ ] The **flag policy is one interface target / toolchain**, partitioned into meaning-class (identical everywhere) and codegen-class (overridable per TU, declared in one place) (§25.4.5).
- [ ] Every object's recorded flags (`.GCC.command.line` / DWARF producer) are **checked by a script**, not read from the log (§25.4.5).
- [ ] The toolchain (compiler, stdlib, linker, sysroot) is **pinned by digest** and treated as a build input (§25.4.6).
- [ ] Two builds of the same commit are **bit-identical** (`SOURCE_DATE_EPOCH`, prefix maps, `-frandom-seed`, `--build-id=sha1`), tested in CI; debug info is archived by build ID (§25.4.6).
- [ ] Objects are **cached by content** (ccache/sccache or Bazel/Buck2 remote cache) and the hit rate is tracked (§25.4.6).

---

## 25.7 References

- Clang documentation, *`-ftime-trace`* and the Chrome trace event format; A. Kuchling / Aras Pranckevičius, **ClangBuildAnalyzer** (github.com/aras-p/ClangBuildAnalyzer) — build-time profiling (§25.3, §25.4.1).
- ISO C++20, [temp.explicit] *Explicit instantiation* and [basic.def.odr] — `extern template` semantics and the one-definition rule the flag policy protects (§25.2.2, §25.4.2).
- ISO C++20, [module] and P1103R3, *Merging Modules* (R. Smith) — named modules, the global module fragment; P2465R3, *Standard Library Modules `std` and `std.compat`* (C++23) — `import std;` (§25.4.4).
- CMake documentation: `target_precompile_headers`, `UNITY_BUILD`, `CMakePresets.json`, `CXX_MODULES` file sets and `CMAKE_CXX_COMPILER_CLANG_SCAN_DEPS` (3.28); Ninja's *dyndep* — the generated module build (§25.4.3–§25.4.5).
- Bazel documentation: *C++ toolchain configuration*, *Remote caching*, *Remote execution*; Buck2 documentation: *Toolchains* — hermetic builds and content-addressed caching as defaults (§25.2.4, §25.4.6).
- The **Reproducible Builds** project, *SOURCE_DATE_EPOCH specification* and *Documentation*; GCC manual, `-ffile-prefix-map`, `-frandom-seed`, `-frecord-gcc-switches`; GNU ld, `--build-id` (§25.4.6).
- `ccache` and `sccache` documentation — compiler-output caching keyed by preprocessed source, flags and compiler hash (§25.4.6).

## 25.8 Additional Reading

- Titus Winters, Tom Manshreck, Hyrum Wright, *Software Engineering at Google*, chapters on build systems and dependency management — the case for hermetic, declared-input builds (§25.2.3–§25.2.4).
- Craig Scott, *Professional CMake: A Practical Guide* — presets, toolchain files, interface targets and the module file-set support (§25.4.5).
- Daniel Ruoso, *"C++ Modules: Build Systems Integration"* and the SG15 tooling papers (P1689 dependency-scanning format) — how `clang-scan-deps` and Ninja's dyndep make module builds work (§25.4.4).
- Ch. 18 (*Compile-Time Mechanics*) — the instantiation cost model; Ch. 20 (*Compile-Time Protocol Decoders*) — the per-message-type cost this chapter structures; Ch. 24 (*Build Toolchain for Speed*) — the flags the policy carries; Ch. 87 §87.4.7 — supply-chain integrity; Ch. 93 — build IDs and debug info; **Appendix D** (flag reference), **Appendix H** (modules/`import std` rows), **Appendix I.12** (the one-liners).

---

*Next: Ch. 26 — Memory Management Fundamentals, opening Part IV. With the language mastered and the build under control, we turn to the resource that dominates real hot-path tails: memory — stack vs heap, allocation cost, page faults, and why `malloc` is the most dangerous function on the tick-to-trade path.*
