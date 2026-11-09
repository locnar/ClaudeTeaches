# Part XIII — Observability & Operations in Production

# Chapter 93 — Crash Handling, Core Dumps & Post-Mortem Diagnostics

> **Prerequisites:** Ch. 89 (process topology — the supervisor, heartbeats and the kill-switch in the surviving gateway, which this chapter's handler hands off to), Ch. 5 (post-mortem in gdb, archived debug info keyed by build ID), Ch. 46 §46.4.3 (what a signal costs and how it is routed — the delivery mechanics this chapter builds on), Ch. 84 (the binary log the crash record joins), Ch. 29 (`mmap`, hugetlbfs and `MAP_SHARED` — the sources of `SIGBUS`), Ch. 24 §24.4.4 (frame pointers and unwind tables — what the production binary carries for the handler to walk), Ch. 21 (the `std::terminate`/`abort` paths), Ch. 48 (housekeeping cores — where the dump is written).
>
> **Leads into:** Ch. 94 (capture — the core shows the end state, the capture shows how it got there), Ch. 98 (spike forensics — the flight recorder this chapter dumps on a fault is the same ring Ch. 98 freezes on a latency trigger), Ch. 103 (the case study's incident path), Appendix I.10 (the crash one-liners) and Appendix N (the incident bundle). Ch. 89 decided that a strategy process may die as long as the gateway survives and cancels its orders; this chapter is the fifty microseconds between the faulting instruction and the core file — what the dying process must do, in what order, with which syscalls, so that the survivors keep trading, the orders are flattened, and the engineer has a usable record by the time the dump finishes.

---

## 93.1 Why it matters: the fault at 09:30:01 with four hundred working orders

The canonical production incident is not a slow tick. It is a strategy process that dereferences a stale pointer at 09:30:01.4, one second into the open, with four hundred working orders in the book and the market moving. Ch. 89 designed the surroundings: the gateway is a separate process and will cancel the dead strategy's orders; the supervisor sees `SIGCHLD` and restarts the shard warm. What Ch. 89 left as a bullet is the *inside* of the crash — the roughly fifty microseconds to fifty seconds between the `mov` that faulted and the moment a core file exists — and that interval decides three things. **Whether the orders get cancelled in time:** the gateway acts when it *learns* of the death, and `SIGCHLD` arrives only after the kernel has finished writing the core, which for a 40 GB process on an ordinary disk is tens of seconds — a lifetime in which the dead strategy's quotes are sitting in the book unmanaged. **Whether the survivors keep their tail:** a core dump is a full-speed read of the dead process's memory and a write of it to disk, and both compete with the live processes for memory bandwidth, LLC and the storage path; §93.3 shows a survivor's maximum gap growing fifty-fold during a dump. **Whether the engineer can do anything with the result:** a core that was written without the debug info archived (Ch. 5), without the flight recorder, without the trace record of the message that was being processed (Ch. 85), from a process whose handler called `printf` and deadlocked on the way down, is a 40 GB file that says `?? ()`.

The C++-on-Linux machinery for doing this right is small, old, and almost entirely absent from codebases that otherwise obsess over nanoseconds: `sigaction` with `SA_SIGINFO | SA_ONSTACK | SA_NODEFER | SA_RESETHAND`; a `sigaltstack` *per thread* so a stack overflow is still handled; a handler that does nothing but `write(2)` pre-formatted bytes to a pre-opened descriptor — because `malloc`, `printf`, `std::cout`, mutexes and, less obviously, glibc's `backtrace()` on its first call are all forbidden in a signal handler and all of them hang or fault at exactly the wrong moment; `madvise(MADV_DONTDUMP)` on the hugepage-backed book and rings so the core is megabytes, not gigabytes; a `core_pattern` pipe handler that runs on a housekeeping core, bounded in time, writing to a disk nobody latency-critical uses; and a kill-switch handoff that happens *in the handler*, before the dump, not when `SIGCHLD` finally arrives. §93.2 gives the mental model — what the kernel does between fault and core, what async-signal safety actually permits, the order in which a dying process must act; §93.3 measures the three costs (a signal's round trip, a stack capture, a dump versus its size, and what the survivors see); §93.4 builds the handler, the pre-warming, the handoff, the dump-size control and the pipe handler, and then the post-mortem workflow that joins the core to the flight recorder, the trace record and the capture; §93.5 and §93.6 are the pitfalls, exercises and checklist. Ch. 46 §46.4.3 covers the *other* use of signals — as a cheap, mis-routed interrupt that lands on a hot thread — and Ch. 98 the flight recorder this handler dumps.

## 93.2 Mental model

### 93.2.1 From the faulting instruction to the core file

```
   faulting insn ──► #PF / #GP / #DE / #UD (hardware exception, ring 0)
       │   kernel: do_page_fault → bad_area → force_sig_fault(SIGSEGV, SEGV_MAPERR, addr)
       ▼
   signal pending on THIS thread (synchronous faults are thread-directed; a kill(2) is process-directed — §46.4.3)
       │   on return to user: handle_signal → setup_rt_frame: pushes siginfo + ucontext (XSAVE area!) on the
       │   signal stack (sigaltstack if SA_ONSTACK, else the current stack — which may be the one that overflowed)
       ▼
   your handler(sig, &si, &uc)      ← the only code that runs with the dying process's address space still intact
       │   SA_RESETHAND: disposition is now SIG_DFL; SA_NODEFER: a fault inside the handler is not blocked, so it
       │   re-faults with SIG_DFL and terminates instead of looping
       ▼   handler returns → sigreturn → the faulting insn re-executes → faults again → SIG_DFL action = core
   do_coredump: freeze every thread of the process (they are stopped — the process does nothing from here on)
       │   decide dumpability (RLIMIT_CORE, PR_SET_DUMPABLE, suid_dumpable); open core file or spawn core_pattern pipe
       │   write ELF header + NT_PRSTATUS (regs) per thread + NT_FILE + NT_AUXV …
       │   then every VMA that coredump_filter / VM_DONTDUMP allows, page by page — time ∝ bytes dumped
       ▼
   exit_group → parent gets SIGCHLD with si_code CLD_DUMPED      ← ONLY NOW does the supervisor learn, if it waited for this
```

Three facts from that path drive the whole chapter. The **handler is the last code that runs with the address space alive and the threads still where they were**: the registers are in `ucontext`, the faulting address in `si_addr`, the frame chain is walkable, the flight recorder is in memory — and after the handler returns, the process is frozen until the dump is done. The **dump's duration is proportional to the bytes it writes** (every private anonymous page that is resident, plus whatever `coredump_filter` adds), the process is stopped for all of it, and `SIGCHLD` arrives at the end. And **nothing about the kernel path knows what matters in your process**: the 40 GB of order book and ring buffers is dumped unless you say otherwise, and the 4 KB of state you actually need — the thread's stack, the message being processed, the last thousand flight-recorder records — is not marked in any way. The handler's job is to produce that 4 KB *itself*, to tell the gateway *before* the dump starts, and to make the dump small enough that the box does not notice.

### 93.2.2 Async-signal safety, and the faults you will see

A handler interrupts the thread at an arbitrary instruction — possibly inside `malloc` with the arena lock held, inside `printf` with `stdout`'s lock held, inside the dynamic loader with `dl_load_lock` held, halfway through updating a `std::map` node. Anything the handler calls that takes one of those locks deadlocks; anything that allocates may corrupt the heap it was just interrupted in. POSIX lists the functions that are safe (`signal-safety(7)`): `write`, `read`, `open`, `close`, `fsync`, `sigaction`, `kill`, `_exit`, `clock_gettime`, `getpid`/`gettid`, `mmap`/`munmap`, `sigaltstack`… — syscall wrappers with no library state. **Not** on the list, and not safe: `malloc`/`new`, `printf`/`std::cout`/`std::format`, `std::string`, anything with a mutex, `exit` (runs `atexit` handlers and flushes stdio), `dlopen` — and therefore **glibc's `backtrace()`**, which on its *first* call `dlopen`s `libgcc_s.so` to find `_Unwind_Backtrace` (§93.3 measures that first call at ~360 µs; it also takes the loader lock). `_Unwind_Backtrace` itself walks `.eh_frame` via `dl_iterate_phdr`, which takes the loader's lock too — safe unless the fault was *in* the loader, which a crash at startup or during a hot-reload `dlopen` (Ch. 88) can be. The frame-pointer walk (§93.4.1) needs nothing but the stack.

The faults a trading process meets, with what `si_code` says about each:

```
   signal   si_code                   typical cause in a Part XIII system                                  si_addr
   ──────── ───────────────────────── ───────────────────────────────────────────────────────────────── ─────────────
   SIGSEGV  SEGV_MAPERR               stale pointer / use-after-unmap (Ch. 29), null deref, stack        the address
            SEGV_ACCERR               write to PROT_NONE guard page (Ch. 5 §5.4.5), to .rodata, to a     the address
                                      MAP_PRIVATE|PROT_READ shm segment (Ch. 89 §89.4.2's reader)
   SIGBUS   BUS_ADRERR                hugetlbfs: touching a page past the reserved huge pages (Ch. 29),  the address
                                      MAP_SHARED file truncated under the mapping (Ch. 29, 94),
                                      DAX/NVDIMM media error
            BUS_ADRALN                misaligned access with AC set, or a misaligned AVX-512 aligned      the address
                                      load/store (`vmovaps` on an unaligned pointer — Ch. 32)
   SIGFPE   FPE_INTDIV                integer divide by zero; INT64_MIN / -1 (Ch. 30 — FP never traps     the insn
                                      by default; this is the *integer* unit)
   SIGILL   ILL_ILLOPN                `-march=native` binary on the wrong CPU (Ch. 24 §24.5); `ud2` from   the insn
                                      __builtin_trap / std::unreachable reached in a non-release build
   SIGABRT  SI_TKILL                  abort(): std::terminate (noexcept violation, uncaught exception —    —
                                      Ch. 21), assert, _GLIBCXX_ASSERTIONS (Ch. 5 §5.4.5), glibc's own
                                      checks (double free, "malloc(): corrupted top size")
   SIGTRAP  TRAP_BRKPT / TRAP_HWBKPT  int3 / debug register — a breakpoint fired with no debugger         the insn
                                      attached (Ch. 5 §5.4.5's sidecar, std::breakpoint — Ch. 21 §21.4.4)
```

The `ucontext_t` gives the registers at the fault (`uc_mcontext.gregs[REG_RIP]`, `REG_RSP`, `REG_RBP`, …, and `REG_ERR` — the page-fault error code: bit 1 set means a write, bit 0 means protection violation vs not-present). Those plus `si_addr` are, nine times out of ten, enough to name the bug before the core is opened: `REG_RIP` inside `apply()`, `si_addr` equal to a book pointer that was `munmap`ed, `REG_ERR = 6` (user write, not present) is a use-after-unmap, and the handler can print it in under a microsecond.

### 93.2.3 The order of business: safety, evidence, dump

A dying strategy has three obligations and the order is not negotiable:

1. **Safety first — the kill-switch handoff.** One `write(2)` of eight bytes to a pre-opened `eventfd` (or pipe) that the gateway's housekeeping thread blocks on. The gateway begins cancelling this shard's orders *now*, ~1–2 µs after the fault, not after the dump; the supervisor's `SIGCHLD` is the backstop, not the trigger. This is Ch. 89 §89.4.4's kill-switch with its trigger moved from the end of the dump to the start of the handler.
2. **Evidence — the crash record.** Signal, code, address, thread, TSC, the registers, the frame chain, and the flight recorder (Ch. 98) — all `write`-n as pre-formatted bytes to a pre-opened descriptor. This is the 4 KB that says what happened; the core is the 40 MB that lets you check.
3. **The dump — small, elsewhere, bounded.** Return from the handler with `SIG_DFL` restored; the kernel writes what `coredump_filter` and `MADV_DONTDUMP` left in, through `core_pattern` to a handler on a housekeeping core.

Everything in the handler is a syscall on an already-open descriptor or a load from memory. No step may wait on another thread, allocate, or take a lock. If the handler itself faults — the frame chain was corrupted, the flight-recorder pointer was the thing that got overwritten — `SA_NODEFER | SA_RESETHAND` ensure the second fault terminates the process with a plain core instead of looping; the kill-switch write, being first, has already happened.

### 93.2.4 Why dumps stall the box, and what controls their size

A core is written by the dying process's own context, in the kernel, at memory speed into the page cache and then to disk. For a process that maps a 32 GB order book in 1 GiB huge pages and 8 GB of rings, the default policy — `coredump_filter = 0x33`: anonymous private, anonymous shared, ELF headers, private huge pages — dumps all of it. Four effects reach the survivors: **memory bandwidth** (reading 40 GB at ~10 GB/s saturates a socket's DRAM channels for seconds — Ch. 16, 50 — and evicts the live processes' LLC lines), **the storage path** (40 GB of writes through the page cache stall the capture writer of Ch. 94 if they share a device, and writeback steals the housekeeping cores), **the page cache itself** (40 GB of dirty pages evict everything else and trigger reclaim — Ch. 26 §26.5), and **time**: the supervisor cannot restart the shard until the process exits, which is after the dump. The controls: `MADV_DONTDUMP` on the segments that are reproducible from the capture (the book — Ch. 94 §94.4.5 rebuilds it) or useless in a dump (NIC rings, telemetry histograms), `coredump_filter` bits per process, `RLIMIT_CORE` for file dumps (ignored for piped dumps — the kernel sets the limit to infinity when `core_pattern` starts with `|`), the pipe handler's own bounds (`timeout`, `nice`, `ionice`, `taskset`, a separate disk), and systemd-coredump's `ProcessSizeMax`/`ExternalSizeMax`. §93.3 puts numbers on the difference and §93.4.4 writes the configuration.

## 93.3 Measure it: a signal's cost, a stack capture's cost, a dump versus its size

*Toolchain and machine: Appendix L.3. The listings below were built with GCC 13.3 (`-O2 -std=c++20 -march=native -pthread`, `-fno-omit-frame-pointer -g` for the backtrace test) and run on a 4-vCPU Intel Xeon VM (TSC 2.70 GHz, virtio disk) under Linux 6.18. Signal and unwinding costs are CPU-bound and transfer to the reference machine within the VM's TSC-read overhead; the dump timings depend on the disk and are labelled.*

**1. What one signal costs the thread it lands on.** `raise()` returns after the handler has run, so its round trip is the full cost: kernel entry, `setup_rt_frame` (the `XSAVE` of the thread's FP/vector state into the sigframe — ~2.5 KB with AVX-512 enabled), the handler, `rt_sigreturn` and the `XRSTOR`:

```
   measurement (sigcost.cpp, 200,000 iterations each)                          cycles       ≈ time @ 2.7 GHz
   ───────────────────────────────────────────────────────────────────────── ─────────── ──────────────────
   raise(SIGUSR1) round trip, SA_SIGINFO handler on the thread's own stack      2,937       1.09 µs
   same, SA_ONSTACK (handler on the sigaltstack)                                3,199       1.18 µs   (+9 %)
   same, with AVX-512 state dirty before the raise                              2,896       1.07 µs   (no change: the
                                                                                                        sigframe is full-size either way)
   pthread_sigmask (rt_sigprocmask) — the cost of masking                         241       89 ns
   2,000 pthread_kill(SIGUSR1) at the hot thread                               handler ran on the hot thread 1,970 times
   2,000 pthread_kill, hot thread masked, housekeeping thread in sigtimedwait  handler ran on the hot thread 0 times; 1,998 funnelled
```

A signal delivery is a **context-switch-class event** (Ch. 46 §46.3: a null syscall is 50–100 ns, a switch 1–3 µs): ~1.1 µs of direct cost, plus the sigframe's 2.5 KB written and read through L1, plus whatever the handler touches. The alternate stack adds 9 % (a different, cold stack page). The AVX-512 result is the surprise worth knowing: the kernel allocates and `XSAVE`s the *full* enabled state area regardless of what is dirty — the `xsave` init optimisation skips writing clean components but the frame is the same size and the `XRSTOR` is the same cost — so a hot thread that never touches a `zmm` register still pays for them on every signal if AVX-512 is enabled in `XCR0`. The last two rows are Ch. 46 §46.4.3's technique and this chapter's reason for a *per-thread* story: on the funnelled run the hot thread saw none of the two thousand signals. (The hot thread's own max-gap measurement on this VM is dominated by hypervisor steal — 0.1–2.8 ms with or without signals — and is not reported; on the reference machine's isolated core each delivery is a ~1.1 µs gap plus the cold-restart penalty of Ch. 46 §46.2.)

**2. What a stack capture costs — and the first-call trap.** At two depths, three ways of capturing the return addresses of the current stack (the code is Ch. 24 §24.4.4's `btcost.cpp`, which also measures the production-binary side):

```
   depth (frames)   frame-pointer walk      _Unwind_Backtrace (.eh_frame)    glibc backtrace()
   ──────────────── ─────────────────────── ──────────────────────────────── ───────────────────────────────
   13               23 cycles  (2/frame)    2,957 cycles  (227/frame)        3,056 cycles  (235/frame)
   37              152 cycles  (4/frame)    7,338 cycles  (198/frame)        7,308 cycles  (198/frame)
   FIRST call       —                       4,782 cycles                     984,452 cycles = 365 µs  (dlopen of libgcc_s.so)
```

The frame-pointer walk is **fifty to a hundred times cheaper** than the DWARF-CFI unwinders (a dependent load chain of two loads per frame versus a table search and an expression interpreter per frame — Ch. 24 §24.4.4) and needs no library and no lock. `backtrace()`'s first call is the number to remember: a third of a millisecond, a `dlopen`, and the loader lock — in a signal handler that is a hang waiting to happen, and in a handler invoked *because* the loader's state was corrupted it is a second fault. If you must use it (you want the unwinder's correctness across frames without frame pointers), call it once at startup to pay the `dlopen` there (§93.4.2); if the production binary keeps frame pointers (Ch. 24 §24.4.4 measured the cost at 0–1 % on the book-update kernel), walk them.

**3. A dump versus its size, and what the survivors see.** A child maps *N* GiB, touches every page, optionally `madvise(MADV_DONTDUMP)`s all but the first 64 MiB, and `abort()`s with `RLIMIT_CORE` unlimited; the parent times `fork`→`waitpid` while a busy-poll "survivor" thread on another core records its longest gap:

```cpp
// dumpcost.cpp — dump duration vs dumped bytes, and the survivor's worst gap while the kernel writes the core.
// Build: g++ -O2 -std=c++20 -pthread dumpcost.cpp -o dumpcost      Run: ./dumpcost <GiB> <dontdump 0|1>   (core_pattern = core)
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <x86intrin.h>
static inline std::uint64_t now() { unsigned a; return __rdtscp(&a); }
int main(int argc, char** argv) {
    std::size_t gib = std::atoi(argv[1]); bool dontdump = std::atoi(argv[2]);
    pid_t pid = fork();
    if (pid == 0) {
        rlimit rl{RLIM_INFINITY, RLIM_INFINITY}; setrlimit(RLIMIT_CORE, &rl);
        std::size_t bytes = gib << 30;
        char* p = (char*)mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
        for (std::size_t i = 0; i < bytes; i += 4096) p[i] = char(i);                 // the "book": resident and dirty
        if (dontdump) madvise(p + (64 << 20), bytes - (64 << 20), MADV_DONTDUMP);       // keep 64 MiB of it in the core, drop the rest
        abort();
    }
    std::atomic<bool> stop{false}; std::atomic<std::uint64_t> maxgap{0};
    std::thread survivor([&] { std::uint64_t prev = now(), mg = 0;                      // a busy-poll thread on another core
        while (!stop.load(std::memory_order_relaxed)) { auto t = now(); if (t - prev > mg) mg = t - prev; prev = t; } maxgap = mg; });
    timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a); int st; waitpid(pid, &st, 0); clock_gettime(CLOCK_MONOTONIC, &b);
    stop = true; survivor.join(); struct stat sb{}; stat("core", &sb);
    double ms = (b.tv_sec - a.tv_sec) * 1e3 + (b.tv_nsec - a.tv_nsec) / 1e6;
    std::printf("%zu GiB resident, MADV_DONTDUMP=%d: core=%.2f GiB in %.0f ms; survivor max gap %.1f us\n",
                gib, dontdump, sb.st_size / double(1 << 30), ms, maxgap.load() / 2700.0);
    unlink("core");
}
```

```
   resident   MADV_DONTDUMP on all but 64 MiB   core size    fork→exit    survivor's max gap during the dump
   ────────── ────────────────────────────────── ──────────── ──────────── ──────────────────────────────────
   2 GiB      no                                 2.00 GiB     13,146 ms    4,963 µs
   2 GiB      yes                                0.06 GiB        525 ms      102 µs
   4 GiB      no                                 4.00 GiB     20,003 ms    8,039 µs
   4 GiB      yes                                0.06 GiB      1,467 ms      427 µs
```

This VM's virtio disk writes at ~0.15–0.20 GiB/s, so the absolute durations are pessimistic by an order of magnitude against the reference machine's NVMe (~2 GiB/s — representative: a 40 GB process dumps in ~20 s there, not ~4 min); the *ratios* are the point. Excluding the book cut the dump **25× to 14×** in duration and **33× to 67×** in size, and the survivor's worst gap — a thread on a different core doing nothing but reading the TSC — **shrank 50× to 19×**: 5–8 ms stalls while the kernel streamed gigabytes through the memory system and the page cache, versus 0.1–0.4 ms with the dump kept small. (The remaining 0.4–1.5 s with `MADV_DONTDUMP` is the kernel freeing 2–4 GiB of pages at exit and the hypervisor's share of a 4-vCPU box — on the reference machine a 60 MiB core is a ~30 ms event.) The 64 MiB that *was* dumped held the stacks, the registers and the flight recorder — the post-mortem needed nothing else.

## 93.4 Techniques

### 93.4.1 The async-signal-safe fault handler

```cpp
// crashhandler.cpp — pre-opened fd, pre-formatted text, kill-switch first, frame-pointer walk, flight-recorder dump,
// then the default action for the core.   Build: g++ -O2 -g -std=c++20 -fno-omit-frame-pointer -pthread crashhandler.cpp -o crashhandler
#include <csignal>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <initializer_list>
#include <sys/eventfd.h>
#include <ucontext.h>
#include <unistd.h>
#include <x86intrin.h>

namespace crash {
static int   g_fd = -1;                 // pre-opened crash log (O_APPEND): no open() in the handler
static int   g_killswitch_fd = -1;      // pre-opened eventfd/pipe the gateway's housekeeping thread blocks on (Ch. 89 §89.4.4)
static const void* g_ring = nullptr;    // the flight-recorder ring (Ch. 98), dumped raw
static std::size_t g_ring_bytes = 0;
alignas(16) static thread_local char t_altstack[64 * 1024];   // PER THREAD: the handler runs here even after a stack overflow

// --- AS-safe helpers: no malloc, no stdio, no locale, no locks; write(2) and the stack only.
static void put(const char* s)        { ssize_t r = ::write(g_fd, s, std::strlen(s)); (void)r; }
static void put_hex(std::uint64_t v)  { char b[19] = "0x"; for (int i = 15; i >= 0; --i) { b[2 + i] = "0123456789abcdef"[v & 15]; v >>= 4; } b[18] = 0; put(b); }
static void put_dec(long v)           { char b[24], o[24]; int n = 0, m = 0; bool neg = v < 0; unsigned long u = neg ? -(unsigned long)v : v;
                                        do { b[n++] = char('0' + u % 10); u /= 10; } while (u); if (neg) b[n++] = '-'; while (n) o[m++] = b[--n]; o[m] = 0; put(o); }
static void put_frames(void** fp, void* ip) {     // walk the FAULTING frame's chain (ucontext rbp), not the handler's own
    put(" frame0 "); put_hex((std::uint64_t)ip); put("\n");
    for (int n = 1; fp && n < 32; ++n) {
        void** next = (void**)fp[0]; put(" frame"); put_dec(n); put(" "); put_hex((std::uint64_t)fp[1]); put("\n");
        if (next <= fp) break;                    // monotonic check: a corrupted chain ends the walk instead of faulting again
        fp = next;
    }
}
static void handler(int sig, siginfo_t* si, void* uc_) {
    if (g_killswitch_fd >= 0) { std::uint64_t one = 1; ssize_t r = ::write(g_killswitch_fd, &one, 8); (void)r; }   // 1. SAFETY: cancel our orders NOW
    auto* uc = static_cast<ucontext_t*>(uc_); auto& g = uc->uc_mcontext.gregs;                                       // 2. EVIDENCE
    put("FATAL signal "); put_dec(sig); put(" code "); put_dec(si->si_code); put(" addr "); put_hex((std::uint64_t)si->si_addr);
    put(" tid "); put_dec((long)::gettid()); put(" tsc "); put_hex(__rdtsc()); put("\n");
    put(" rip "); put_hex(g[REG_RIP]); put(" rsp "); put_hex(g[REG_RSP]); put(" rbp "); put_hex(g[REG_RBP]);
    put(" rax "); put_hex(g[REG_RAX]); put(" rdi "); put_hex(g[REG_RDI]); put(" err "); put_hex(g[REG_ERR]); put("\n");
    put_frames((void**)g[REG_RBP], (void*)g[REG_RIP]);
    if (g_ring) { put(" flight-recorder "); put_dec((long)g_ring_bytes); put(" bytes follow\n"); ssize_t r = ::write(g_fd, g_ring, g_ring_bytes); (void)r; }
    ::fsync(g_fd);
    // 3. DUMP: SA_RESETHAND restored SIG_DFL; returning re-executes the faulting insn, which re-faults, and the default action writes the core
}
static void install_thread() { stack_t ss{}; ss.ss_sp = t_altstack; ss.ss_size = sizeof t_altstack; ::sigaltstack(&ss, nullptr); }
static void install(const char* path, int killswitch_fd, const void* ring, std::size_t ring_bytes) {
    g_fd = ::open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644); g_killswitch_fd = killswitch_fd; g_ring = ring; g_ring_bytes = ring_bytes;
    install_thread();                                                     // every other thread calls install_thread() in its start routine
    struct sigaction sa{}; sa.sa_sigaction = handler; sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_NODEFER | SA_RESETHAND;    // ONSTACK: survive stack overflow; NODEFER+RESETHAND: a fault IN the handler terminates
    for (int s : {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT, SIGTRAP}) ::sigaction(s, &sa, nullptr);
}
}  // namespace crash
```

Run against a deliberate use-after-`munmap` of the book (`apply(book, 2)` after `munmap(book)`), the handler produced, in one write burst and before the kernel began the dump:

```
   FATAL signal 11 code 1 addr 0x00007f2a016bc020 tid 19440 tsc 0x00002532b94785b8
    rip 0x000055b12991192b rsp 0x00007ffef00919a8 rbp 0x00007ffef0091ab0 rax 0x0000000000000000 rdi 0x00007f2a016bc000 err 0x0000000000000006
    frame0 0x000055b12991192b      ← apply()+0xb: `addq $0x1,(%rdi,%rsi,1)` — the faulting store (objdump / addr2line against the build-ID-matched binary)
    frame1 0x00007f2a0142a1ca      ← __libc_start_call_main: main's return address — on_tick() and apply() left NO frames (see below)
    frame2 0x00007f2a0142a28b
    frame3 0x000055b129911385
    flight-recorder 32768 bytes follow
```

Read it the way you would at 09:30: `code 1` is `SEGV_MAPERR` (no mapping), `addr` is `rdi + 0x20` — the book pointer plus `2 × sizeof(Level)` — and `err 0x6` is a user-mode *write* to a *not-present* page: a use-after-unmap, named without opening the core. The frame chain also teaches the chapter's one honest caveat about frame-pointer walks: `apply` is a leaf (GCC's `-momit-leaf-frame-pointer` default leaves leaves frameless even under `-fno-omit-frame-pointer`) and `on_tick` tail-called it (`jmp apply`), so *neither* has a frame — `frame0` (the `rip`) is the only record of `apply`, and `on_tick` is gone entirely. That is why `frame0` is printed from `REG_RIP` and not from the chain, why the handler records the registers (the arguments of a leaf are still in `rdi`/`rsi`), and why Ch. 24 §24.4.4 discusses `-mno-omit-leaf-frame-pointer` and `-fno-optimize-sibling-calls` as the price of complete crash stacks.

### 93.4.2 Pre-warming, and choosing the stack-capture mechanism

The table in §93.3 says which unwinder to use and when:

- **Frame pointers kept, walk them (the default here).** 2–4 cycles per frame, no library, no lock, no first-call cost; the *only* AS-safe choice that works when the fault is inside the loader or the allocator. Costs: `-fno-omit-frame-pointer` in the production build (0–1 % on the kernels Ch. 24 §24.4.4 measured), and incomplete chains through leaves and tail calls unless you also pay for `-mno-omit-leaf-frame-pointer` (a few more bytes per leaf) — or accept `frame0` from `rip` as the leaf's record.
- **`_Unwind_Backtrace` / libunwind (`unw_backtrace`), pre-warmed.** Correct through frameless functions, ~200 cycles per frame, needs `.eh_frame` (present in every binary that has not been built `-fno-asynchronous-unwind-tables`, exceptions or not — Ch. 21 §21.4.3). `_Unwind_Backtrace` takes the loader lock via `dl_iterate_phdr`; libunwind built `UNW_LOCAL_ONLY` caches the `.eh_frame` lookup and documents its local unwinder as usable from signal handlers *if* its cache has been primed. Either way: **call it once at startup** (and after every `dlopen` of Ch. 88's strategy reload) so the first-call `dlopen`/cache fill (365 µs / 4.8k cycles in §93.3) and the lock acquisition have already happened.
- **`backtrace()` from glibc — only pre-warmed, never cold.** It is `_Unwind_Backtrace` with a `dlopen` of `libgcc_s` on the first call; glibc's own manual says to call it once before installing the handler. With that done it is equivalent to the row above and slightly more convenient (`backtrace_symbols_fd` writes to an fd without allocating — the `backtrace_symbols` variant mallocs).
- **`std::stacktrace::current()` (C++23) — not in the handler.** It allocates (Ch. 21 §21.4.4 measures ~4,100 cycles for capture and ~40,000 for symbolisation at 12 frames); it is the right tool for the *terminate handler* and for assertion failures on a cold path, with a `pmr` resource, and the wrong one inside a `SIGSEGV` handler.

Whatever walks the stack, **do not symbolise in the handler**: write raw addresses and resolve offline against the build-ID-matched binary (§93.4.5). To make raw addresses resolvable under ASLR, write the load base once at startup — `dl_iterate_phdr` at install time, or copy `/proc/self/maps` into the crash log's header — so the post-mortem can subtract it.

### 93.4.3 The kill-switch handoff and the flight recorder

The handler's first line is the most important one in the chapter: eight bytes to an `eventfd` the gateway created and passed to the strategy at spawn (an fd inherited across `fork`/`exec`, or sent over a Unix socket with `SCM_RIGHTS` — Ch. 57). On the gateway side a housekeeping thread (Ch. 48, pinned off the hot cores) blocks in `read()` on it and, on wake, marks the shard `disarmed` in the telemetry segment's readiness slot (Appendix M.1 — the order path's `may_send()` now refuses) and enqueues mass-cancels for every order tagged with that shard (Ch. 89 §89.4.4). Elapsed, fault to the first cancel on the wire: the signal delivery (~1 µs), the `write` (~0.3 µs), the gateway thread's wake (a futex wake plus a cross-core IPI and C-state exit — Ch. 46 §46.3: 3–10 µs), and the cancel encode and send (Ch. 60, 67) — **under 20 µs**, against the tens of seconds a `SIGCHLD`-driven design takes when the dump is large. The eventfd is *also* the supervisor's early warning: it can `poll()` the same fd (or a second one) and begin the warm restart's page pre-touching (Ch. 53) while the dump is still being written, because the restart does not need the dead process's exit — only its segments (Ch. 89 §89.4.1), which the new instance maps read-only until the supervisor confirms the old one is gone.

The flight recorder is Ch. 98's always-on, per-thread ring of 32-byte binary records (the last ~65k things the thread did, with TSC stamps). On a fault it is the most valuable evidence in the process and the cheapest to save: one `write(2)` of the ring's bytes (2 MiB — ~1 ms on the reference machine's NVMe, acceptable *after* the kill-switch and the registers). A multi-threaded process has one ring per hot thread; the handler knows only the faulting thread's (`thread_local`), so write that one first and let the core carry the others — or, better, place the rings in a `MAP_SHARED` segment the sidecar of §93.4.4 can read after the fact, which also keeps them out of the core (`MADV_DONTDUMP`) and the write out of the handler.

### 93.4.4 Dumps that do not stall the box: `coredump_filter`, `MADV_DONTDUMP`, the pipe handler, systemd-coredump and minidumps

Four layers, from the process outward:

- **Mark what must not be dumped, from the process.** `madvise(addr, len, MADV_DONTDUMP)` on the hugepage-backed book (reproducible from the capture — Ch. 94 §94.4.5), the shm data-plane rings (Ch. 89 §89.4.1; the *other* processes still have them), the NIC's DMA rings (Ch. 70 — meaningless in a core), the telemetry segment (Ch. 86 — scraped elsewhere). Keep the stacks, the heap, the flight recorder and a small header of each segment (`MADV_DODUMP` on the first page re-includes it). This is per range and survives `fork`; it is the fine-grained tool and the one that produced §93.3's 33–67× reduction. For the coarse setting, `/proc/self/coredump_filter` (inherited across `fork`/`exec`) takes a hex mask: bit 0 anonymous private, 1 anonymous shared, 2 file-backed private, 3 file-backed shared, 4 ELF headers (always include: the loader needs them), 5 private huge pages, 6 shared huge pages, 7/8 DAX. `echo 0x11 > /proc/self/coredump_filter` dumps only private anonymous memory plus headers — stacks and heap, no shared segments, no huge pages — and is the right default for a Part XIII process whose big state is all hugepage-backed shared memory.
- **Bound the dump, from the kernel.** `RLIMIT_CORE` caps a *file* dump (and `0` disables it — the default on most distributions, which is why "we have no core" is the first finding of many post-mortems). It is **ignored for piped dumps**: `fs/coredump.c` sets the limit to infinity when `core_pattern` begins with `|`, so a pipe handler must bound itself. `core_pipe_limit` (`/proc/sys/kernel/core_pipe_limit`) caps how many pipe handlers run concurrently — set it to 1–2 so a crash-loop (Ch. 89 §89.5) cannot spawn a dozen dumpers; `PR_SET_DUMPABLE` and `fs.suid_dumpable` decide whether a process that changed credentials or holds capabilities (Ch. 87 §87.4.5) is dumpable at all — a process that dropped privileges after `CAP_NET_RAW` for kernel bypass is *not* dumpable unless it calls `prctl(PR_SET_DUMPABLE, 1)` after the drop.
- **Pipe the dump to a bounded handler on a housekeeping core.** `kernel.core_pattern = |/usr/local/sbin/coredump-handler %P %u %g %s %t %h %e` — the kernel `fork`s the handler with the core on its stdin and the specifiers as arguments (`%P` global PID, `%s` signal, `%t` time, `%e` comm, `%E` path with `!` for `/`, `%h` hostname; `%c` is the core-size limit, which is unbounded for pipes). The handler is a few lines, and every line is a bound:

  ```bash
  #!/bin/sh
  # /usr/local/sbin/coredump-handler PID UID GID SIG TIME HOST COMM — reads the core from stdin; everything it does is bounded.
  exec taskset -c 0-1 nice -n 19 ionice -c 3 timeout 60 \
    zstd -T2 -3 -q -o "/var/crash/$7.$1.$4.$5.core.zst" -        # a NON-capture disk; zstd at ~1 GB/s per thread keeps up with NVMe
  ```

  `taskset` keeps it on the housekeeping cores (Ch. 48; never a core that `isolcpus` reserved — the dump handler is exactly the stray process Ch. 99 watches for), `nice`/`ionice` make it yield to the survivors' writers, `timeout` bounds the worst case (a truncated core beats a stalled box), the destination is a device the capture writer (Ch. 94) does not use. With `MADV_DONTDUMP` done properly the stream is tens of megabytes and the whole thing is a ~100 ms event.
- **systemd-coredump, and minidumps.** On a systemd host the default `core_pattern` is already a pipe to `systemd-coredump`, which stores to `/var/lib/systemd/coredump/`, compresses, and indexes for `coredumpctl` (`coredumpctl list`, `coredumpctl gdb <PID>`, `coredumpctl dump --output`) — convenient, but its bounds are in `/etc/systemd/coredump.conf` (`ProcessSizeMax=`, `ExternalSizeMax=`, `MaxUse=`, `Compress=`), its defaults truncate a process over 2 GB (so the `MADV_DONTDUMP` work is what makes it usable), and it runs wherever systemd puts it (`CPUAffinity=` in its unit, or the housekeeping `cpuset` of Appendix C.4). The alternative to a core at all is a **minidump** — Breakpad/Crashpad's format: each thread's registers and the top of its stack, the module list with build IDs, and nothing else, a few hundred KB written by an *out-of-process* handler (Crashpad's `crashpad_handler` reads the dying process with `ptrace` after a signal, so the in-process code is only the eventfd-style wake). Minidumps symbolise with `minidump_stackwalk` against archived symbols and are what large fleets collect by default. When is a full core still worth it? When the question is *corruption*: a lock-free structure in an impossible state (Ch. 37), an arena whose free list points into the book (Ch. 27), a seqlock reader that saw a torn value (Ch. 38) — the evidence is in the heap, not the stacks, and a minidump does not have it. The design that serves both: a small full core (`MADV_DONTDUMP` everything reproducible) *and* the handler's own record.

### 93.4.5 Post-mortem: joining the core, the crash record, the trace record and the capture

A crash in a Part XIII system leaves four artefacts and the post-mortem reads them together:

1. **The crash record** (§93.4.1) names the signal, the address, the registers, the raw frames and carries the flight recorder. Resolve the frames against the **build-ID-matched** binary and its archived debug info (Ch. 5 §5.4.1: `readelf -n app | grep 'Build ID'`, then the `.debug` file with that ID from the archive or `debuginfod`): `addr2line -e app.debug -f -C -i -p 0x<frame − load base>`. The `tsc` field joins it to everything else on the host (Ch. 98 §98.4.1's timeline).
2. **The core**, opened with the same binary: `gdb -q app.debug -c core -ex 'thread apply all bt' -ex 'info registers' -ex 'p *(Level*)$rdi' -batch` for the state at the fault, or `eu-stack --core core -e app --debuginfo-path /var/debug` for every thread's stack without gdb's startup cost. `coredumpctl gdb <PID>` does the lookup for a systemd-stored core.
3. **The trace record** (Ch. 85) of the message that was being processed — the flight recorder's last record carries its `corr`, and the gateway's log has the header — tells you *which* tick, decoded at what time, with which stage stamps, was in flight.
4. **The capture** (Ch. 94) replays that tick into the deterministic core (Ch. 89 §89.2.2) under gdb or `rr` (Ch. 5 §5.4.2): the core shows the end state, the replay shows how the state got there, and Ch. 89 §89.4.6's digest stream says at which event the production process first diverged from what the replay computes.

The `std::terminate` path (an uncaught exception, a `noexcept` violation, `std::thread` destroyed joinable — Ch. 21) deserves one hook: `std::set_terminate` to a function that writes `__cxa_current_exception_type()->name()` (a pointer into static RTTI data — AS-safe to read) and, if the exception derives from `std::exception`, its `what()` *only if* you know the type's `what()` does not allocate (most do not; `std::runtime_error`'s returns a pointer into the object), then `abort()`s — so a terminate is a `SIGABRT` with a one-line reason in the crash log instead of `terminate called after throwing an instance of 'std::runtime_error'` on a `stderr` nobody captured.

## 93.5 Pitfalls & anti-patterns

- **`printf`, `malloc` or a mutex in the handler.** Deadlocks on the lock the interrupted code held, or corrupts the heap — and the symptom is a hung process that never dumps and never dies, with its orders still in the book. Pre-formatted bytes, pre-opened fd, `write(2)` only (§93.2.2).
- **`backtrace()` for the first time in the handler.** A `dlopen` under the loader lock, 365 µs when it works and a hang when the fault was in the loader. Pre-warm it at startup or walk frame pointers (§93.4.2).
- **No `sigaltstack` — or one per process instead of per thread.** A stack overflow (a runaway recursion, an oversized `alloca`, a guard page hit — Ch. 26 §26.5) delivers `SIGSEGV` to a thread with no stack left; without `SA_ONSTACK` and a per-thread alternate stack the handler faults immediately and you get a core with no record. `sigaltstack` is per thread; `install_thread()` in every thread's start routine.
- **Missing `SA_RESETHAND`/`SA_NODEFER`.** A fault inside the handler (a corrupted frame chain) re-enters the handler forever (`SA_NODEFER` off blocks the signal, so the second fault *kills* the process without a core instead — also wrong, but differently). Both flags, always.
- **Catching `SIGSEGV` and continuing.** Returning from a fault handler without fixing the cause re-executes the instruction; `siglongjmp`ing out of it "to keep trading" leaves locks held, invariants broken and the book in an unknown state — the one thing worse than a crash is a process that keeps sending orders from corrupted state. The only legitimate `SIGSEGV` recovery is the deliberate guard-page probe of Ch. 5 §5.4.5, and that is a test tool.
- **Treating `SIGBUS` from hugetlbfs as a memory error.** It is a *reservation* error: the mapping succeeded but the huge page was not there when touched (Ch. 29 §29.5). Pre-fault every huge page at startup (`MAP_POPULATE`, `mlock`) and check `HugePages_Free` in the readiness gate (Appendix M.2) — a `SIGBUS` at 09:30 is a configuration failure that should have failed the gate at 08:30.
- **Dumping the book.** 40 GB of reproducible state into the core: a 20 s–4 min dump, a saturated memory system, a full page cache, a stalled capture writer and a `SIGCHLD` that arrives after the market has moved. `MADV_DONTDUMP` everything the capture can rebuild (§93.4.4).
- **The core on the capture disk — or on the hot path's NUMA node.** Shared fate through the storage path (Ch. 89 §89.5): the dump's writeback stalls Ch. 94's writer, and a dumper running on the wrong core stalls a hot thread. Separate device, housekeeping `taskset`, `ionice`.
- **`RLIMIT_CORE` as the bound on a piped dump.** It is ignored. Bound the pipe handler itself (`timeout`, `core_pipe_limit`, `ProcessSizeMax` — §93.4.4).
- **Kill-switch triggered by `SIGCHLD`.** It fires after the dump. Trigger it from the handler's first line (§93.4.3); `SIGCHLD` is the backstop.
- **Not dumpable after dropping privileges.** `PR_SET_DUMPABLE` is cleared by a credential change; a process that dropped capabilities after setting up kernel bypass (Ch. 70, 87) silently produces no core. `prctl(PR_SET_DUMPABLE, 1)` after the drop, and check it in the gate.
- **Raw addresses with no load base.** Under ASLR/PIE a frame address is meaningless without the mapping; record `/proc/self/maps` (or `dl_iterate_phdr`'s bases) at startup into the crash log's header (§93.4.2).
- **Stripped binary, no archive.** Ch. 5 §5.5's pitfall, repeated because the crash is where it bites: no build-ID-keyed `.debug` means `?? ()` for every frame. Archive at release time; verify the archive in the readiness gate's provenance check (Appendix M.2).

## 93.6 Exercises & checklist

**Exercises**

1. Build `crashhandler.cpp` and crash it three ways — the use-after-unmap shown, a stack overflow (`[[gnu::noinline]] void f(int n) { char b[4096]; b[0] = n; f(n + 1); }`), and a `std::terminate` from a `noexcept` function that throws. Confirm each produces a crash record *and* a core; then remove `SA_ONSTACK` and repeat the overflow. Explain what you see.
2. Rebuild with `-mno-omit-leaf-frame-pointer -fno-optimize-sibling-calls` and compare the frame chain for the use-after-unmap with §93.4.1's. Measure the two flags' cost on Ch. 24 §24.4.4's `fpcost.cpp`.
3. Insert a `std::printf` into the handler and crash the process from inside `malloc` (allocate in a loop on one thread, `pthread_kill(SIGSEGV)` it from another). Observe the hang; attach `gdb -p` to the *hung* process and read the two lock owners.
4. Run `dumpcost.cpp` with 8 GiB on your box, with and without `MADV_DONTDUMP`, while `perf stat -e LLC-load-misses -C <survivor core>` watches a pinned survivor. Report the survivor's LLC misses and max gap in both runs; then move the core to a different disk than the survivor's writer and repeat.
5. Write the `core_pattern` pipe handler of §93.4.4 (on a test box), set `core_pipe_limit=1`, and crash four processes simultaneously. How many cores do you get, and how long is the box busy? Then replace it with `systemd-coredump` with `ProcessSizeMax=256M` and compare.
6. Wire the eventfd handoff into Ch. 89's supervisor and gateway. Measure fault-to-first-cancel-on-the-wire with the Ch. 85 ledger (the cancel's `t_hw_tx` minus the crash record's `tsc`, converted via Ch. 66 §66.4.1), with and without a 4 GiB book in the core.
7. Pre-warm `backtrace()` at startup and measure the handler's total time (first `write` to `fsync`) with `rdtsc` stamps written into the record itself. How much of it is the flight-recorder write?

**Checklist**

- [ ] `sigaction` on `SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT/SIGTRAP` with `SA_SIGINFO | SA_ONSTACK | SA_NODEFER | SA_RESETHAND`; `sigaltstack` installed in *every* thread.
- [ ] Handler: kill-switch `write` first; then signal/code/addr/tid/tsc, registers, raw frames, flight recorder; `fsync`; return. No malloc, stdio, locks, `backtrace()` cold, symbolisation.
- [ ] Unwinder pre-warmed at startup (and after every `dlopen`) if anything but a frame-pointer walk is used; load base recorded for ASLR.
- [ ] `MADV_DONTDUMP` on book, data-plane rings, NIC rings, telemetry; `coredump_filter` set; `PR_SET_DUMPABLE` verified after privilege drops; `RLIMIT_CORE` non-zero.
- [ ] `core_pattern` pipes to a bounded handler (`taskset` housekeeping, `nice`/`ionice`, `timeout`, separate disk) or to systemd-coredump with explicit size limits and CPU affinity; `core_pipe_limit` set.
- [ ] Debug info archived per build ID; `readelf -n` build ID checked by the readiness gate; `addr2line`/`eu-stack`/`coredumpctl gdb` recipes in the runbook (Appendix I.10).
- [ ] `std::set_terminate` hook writes the exception type and aborts.
- [ ] Measured on your box: signal round trip, handler duration, dump duration and the survivors' max gap with the production `MADV_DONTDUMP` policy in place.

## 93.7 References

1. Linux man pages: `signal-safety(7)` (the async-signal-safe function list), `sigaction(2)`, `sigaltstack(2)`, `sigreturn(2)`, `core(5)` (`core_pattern` specifiers, pipe semantics, `core_pipe_limit`, `coredump_filter` bits), `madvise(2)` (`MADV_DONTDUMP`/`MADV_DODUMP`), `prctl(2)` (`PR_SET_DUMPABLE`), `proc(5)`, `getrlimit(2)` (`RLIMIT_CORE`), `eventfd(2)`, `backtrace(3)` (the first-call `dlopen` note).
2. Linux kernel, `fs/coredump.c` (the dump path: `do_coredump`, the `ispipe` limit handling), `arch/x86/kernel/signal.c` and `fpu/signal.c` (`setup_rt_frame`, the XSAVE sigframe), `Documentation/admin-guide/sysctl/kernel.rst` (`core_pattern`, `core_pipe_limit`, `core_uses_pid`).
3. Intel Corporation. *Intel® 64 and IA-32 Architectures Software Developer's Manual*, Vol. 1 Ch. 13 "Managing State Using the XSAVE Feature Set" (XSAVE area layout, the init and modified optimizations).
4. GNU C Library Manual, §34 "Signal Handling" (async-signal safety, `sigaltstack`), and the `backtrace` documentation; libgcc `unwind.h` (`_Unwind_Backtrace`, `_Unwind_GetIP`).
5. nongnu.org libunwind documentation, `libunwind(3)` — "Local unwinding" and the notes on signal-handler use with `UNW_LOCAL_ONLY`.
6. systemd: `systemd-coredump(8)`, `coredump.conf(5)`, `coredumpctl(1)`.
7. Google Breakpad and Chromium Crashpad project documentation — the minidump format, out-of-process handlers, `minidump_stackwalk`.
8. elfutils: `eu-stack(1)`, `eu-unstrip(1)`; `debuginfod(8)`.
9. ISO/IEC 14882:2020 §[except.terminate], §[support.exception] (`std::set_terminate`, `std::terminate`); Itanium C++ ABI, `__cxa_current_exception_type`.

## 93.8 Additional Reading

- Kerrisk, M. *The Linux Programming Interface*, No Starch Press 2010 — Ch. 21–24 (signals: fundamentals, handlers, advanced features — `sigaltstack`, `SA_SIGINFO`, the realtime semantics) and Ch. 39 (resource limits).
- Drepper, U. "Signal handling in glibc" and the glibc wiki page *Async-signal-safe functions*.
- Gregg, B. *Systems Performance*, 2nd ed. — Ch. 13 "perf" and the discussion of frame pointers vs DWARF for stack walking in production.
- Poimboeuf, J. "SFrame-based stack unwinding", LWN.net 2023–2025 — the kernel's path to unwinding user stacks without frame pointers or DWARF (Ch. 24 §24.4.4).
- Chromium project, *Crashpad Overview Design* — why the crash handler lives out of process.
- Love, R. *Linux System Programming*, 2nd ed., O'Reilly 2013 — Ch. 10 "Signals".

---

*Next: Ch. 94 — Capture, Persistence & Replay Storage, where the core's end state meets the recording of how it got there: lossless line capture with nanosecond timestamps, append-only journals and compact binary formats, getting the bytes to disk off the hot path, and the replay that turns one production crash — or one spike — into something you can run again.*
