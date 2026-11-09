# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 68 — PCIe & the Host–Device Boundary

> **Prerequisites:** Ch. 7 (memory hierarchy — PCIe is another, slower level), Ch. 26 (mmap — MMIO/BAR mapping is mmap of device memory), Ch. 41 (interrupts vs polling — MSI-X vs poll), Ch. 57/64/65 (the NIC/bypass/RDMA devices that sit behind PCIe), Ch. 58 (DDIO — where a DMA write lands), Ch. 1 (latency — the round-trip floor).
>
> **Leads into:** Ch. 69 (GPU — PCIe round-trip is why GPUs are off the order path), Ch. 70 (MPI), Ch. 71 (FPGA — on the NIC, behind/beside PCIe), Ch. 72 (SmartNICs/DPUs). Opens **Part XI** — and the PCIe round-trip is the **floor under every offload decision** in the rest of the book.

---

## 68.1 Why it matters: the round-trip floor under every offload

Part XI is about pushing work off the CPU and onto *other* hardware — GPUs (Ch. 69), FPGAs (Ch. 71), SmartNICs (Ch. 72) — and *every one of them sits behind **PCIe***, the bus connecting the CPU to peripheral devices. The NIC you bypass (Ch. 64), the RDMA HCA (Ch. 65), the GPU, the FPGA — all are PCIe devices, and *all* communication between the CPU and these devices crosses the PCIe boundary. So before any offload chapter, you must understand the **cost of crossing that boundary**, because it's the **irreducible floor under every offload decision**: a PCIe round-trip — the CPU reading a value from a device and getting the answer back — costs **hundreds of nanoseconds to a microsecond**, and *no amount of accelerator speed can hide it*. If offloading a computation to a GPU saves 500 ns of compute but costs a 1 µs PCIe round-trip to send the data and get the result, you *lost*. The PCIe round-trip is why GPUs are off the tick-to-trade path (Ch. 69), why FPGAs are placed *inline on the NIC* rather than behind PCIe (Ch. 71), and why every "should I offload this?" question is really "is the work big enough to amortize the PCIe crossing?".

The asymmetry that makes PCIe subtle — and the single most important fact in this chapter — is that **a device-register *read* is catastrophically more expensive than a *write*.** A PCIe *memory* **write** is **posted** (configuration and legacy I/O writes, like all reads, are non-posted and cost a completion round-trip): the CPU fires it and moves on, not waiting for it to arrive (~tens of ns to *issue*) — fire-and-forget. A PCIe **read** is **non-posted**: it requires a full round-trip — the request goes to the device, the device responds, and the CPU *stalls* the whole time (~hundreds of ns to a microsecond). So a **device-register read on the hot path is a latency disaster** (§68.5), and the entire design of low-latency device interaction is built around *avoiding reads*: the CPU **writes** to the device (posts commands to descriptor rings, rings doorbells) and learns of completions by the device **writing** to *host memory* (which the CPU reads cheaply from cache — Ch. 7) rather than the CPU reading device registers. This "device writes to host memory, host polls host memory" pattern (descriptor rings, Ch. 34/54/64) is how every fast device driver works, and *why*.

For HFT this understanding is foundational to Part XI: it tells you *what* can be offloaded (big, parallel, latency-insensitive work — risk, backtesting, ML — Ch. 69–70) and what *can't* (the latency-critical tick-to-trade path, which can't afford a PCIe round-trip — so its acceleration goes *inline on the NIC's FPGA*, Ch. 71, not behind PCIe). It explains the descriptor-ring/doorbell pattern you've already seen in the NIC (Ch. 57, 64) and RDMA (Ch. 65). And it covers the mechanisms — MMIO/BAR-mapped registers, DMA, the IOMMU, posted/non-posted transactions, write-combining, MSI-X vs polling, peer-to-peer DMA (GPUDirect) — that determine device-interaction latency. This chapter explains the PCIe topology/MMIO/DMA mental model (§68.2), measures the device-register round-trip and streaming bandwidth (§68.3), details write-combining, interrupts-vs-polling, and P2P DMA (§68.4), and warns about the device-read-on-the-hot-path and IOMMU costs (§68.5). It's the floor the rest of Part XI builds on.

## 68.2 Mental model

### 68.2.1 PCIe topology, lanes/generations and bandwidth

**PCIe — the tree of links connecting CPU to devices.** PCIe (Peripheral Component Interconnect Express) is a **point-to-point, serial, switched** interconnect: the CPU's root complex connects (directly or through switches) to devices, each via a **link** of some number of **lanes** (x1, x4, x8, **x16**) at some **generation** speed:

```
   CPU (root complex) ──x16 Gen4── GPU
        │             ──x16 Gen5── NIC / RDMA HCA / FPGA
        └── PCIe switch ──x8── device
                        ──x8── device
   each lane: a serial differential pair; bandwidth = lanes × per-lane-rate(generation)
```

- **Lanes (x1…x16):** more lanes = more bandwidth (parallel serial lanes). A GPU/NIC typically uses **x16** or x8.
- **Generations and per-lane bandwidth:** each PCIe generation ~doubles per-lane rate. **Gen3** ~1 GB/s/lane, **Gen4** ~2 GB/s/lane, **Gen5** ~4 GB/s/lane (usable, after encoding). So a **x16 Gen4** link is ~32 GB/s, **x16 Gen5** ~64 GB/s. This is the *streaming bandwidth* ceiling for bulk transfers (host↔device).
- **Bandwidth vs latency — both matter, differently.** Bandwidth (lanes × gen) governs *bulk* transfer (sending a big array to a GPU — Ch. 69); **latency** (the round-trip time for a small access — §68.2.3) governs *interaction* (a register read/write). An offload's cost is *transfer bandwidth* (for the data) *plus* *round-trip latency* (for the control). Both bound the offload.
- **Topology matters (NUMA — Ch. 16).** A device hangs off a *specific* CPU socket's root complex (or a switch). Accessing a device from the *other* socket crosses the inter-socket interconnect (Ch. 16) — extra latency. Place the device-using thread on the device's NUMA node (Ch. 16, 42) — the NIC's node for the feed handler (Ch. 57). `lspci`/`lstopo` show the topology.

### 68.2.2 MMIO, BAR-mapped registers, DMA and the IOMMU

**How the CPU and device communicate — two directions:**

- **MMIO (Memory-Mapped I/O) — the CPU accesses the device.** The device exposes registers and memory through **BARs (Base Address Registers)** that map device memory into the CPU's physical address space; the OS `mmap`s them (Ch. 26) so the driver accesses device registers as if they were memory (`*reg = value` writes a device register; `value = *reg` reads one). **A write is posted (cheap); a read is non-posted (a round-trip — §68.2.3).** This is how the CPU rings doorbells and (rarely!) reads device state.
- **DMA (Direct Memory Access) — the device accesses host memory.** The device's DMA engine reads/writes **host memory** directly (without the CPU) — this is how bulk data moves (the NIC DMA's packets into host buffers — Ch. 57, 64; the GPU DMA's data; RDMA — Ch. 65). DMA is the *device writing to host memory*, which the CPU then reads cheaply — from L3 when DDIO allocates the inbound write there (Intel — Ch. 58), otherwise a DRAM read, still far cheaper than a device-register read (§68.1). Host buffers for DMA must be **pinned by the driver at registration** (`ibv_reg_mr` — Ch. 65; `cudaHostAlloc`/`cudaHostRegister` — Ch. 69; the VFIO DMA map under kernel bypass — Ch. 64), which locks the *physical* pages; `mlock`/pre-faulting (Ch. 23, 26) only keeps pages resident and is not by itself a DMA guarantee (the kernel may still migrate an mlocked page).
- **The IOMMU (I/O Memory Management Unit).** A "TLB for devices" — it translates the addresses a device uses (I/O virtual addresses) to physical addresses, providing **isolation** (a device can only DMA to memory it's been granted — security, Ch. 75) and allowing devices to use virtual addresses. The IOMMU adds a translation step to DMA (a small latency, and IOTLB misses — like the CPU TLB, Ch. 15) — usually negligible, but it can matter (§68.5), and it's the security boundary for DMA-capable devices (a compromised device could otherwise DMA anywhere — Ch. 75). On Linux, `intel_iommu`/`amd_iommu`; VFIO (used by DPDK — Ch. 64, and device passthrough) uses it.

### 68.2.3 Descriptor rings and doorbells; posted vs non-posted transactions

**The pattern every fast device uses — and why (the heart of the chapter):**

- **Posted vs non-posted transactions (the read/write asymmetry).**
  - A **posted** transaction (a memory **write** to the device) is **fire-and-forget**: the CPU issues it and continues *without waiting* for it to complete (~tens of ns to issue). The PCIe fabric delivers it; no acknowledgment comes back to stall the CPU.
  - A **non-posted** transaction (a memory **read** from the device, or a config read) requires a **completion** — the CPU issues the request and **stalls until the device responds** (a full round-trip, ~hundreds of ns to a microsecond). **This is why a device-register read is so expensive** (§68.1, §68.5).
- **The descriptor-ring + doorbell pattern (avoid reads).** Because reads are expensive, fast devices use this pattern (you've seen it in the NIC — Ch. 57, 64, and RDMA — Ch. 65):
  ```
   1. CPU writes a DESCRIPTOR (a command: "DMA this buffer", "send this packet") into a RING in HOST memory.
   2. CPU rings the DOORBELL — a single posted WRITE to a device register ("new work in the ring"). (cheap)
   3. The DEVICE reads the descriptor (via DMA — the device's cost, not the CPU's), does the work,
      and writes a COMPLETION back into HOST memory (DMA).
   4. CPU POLLS the completion in HOST memory (a cheap cache/memory read — Ch.7), NOT a device-register read.
  ```
  The CPU only ever **writes** to the device (descriptors to host-memory rings, the doorbell — posted, cheap) and **reads from host memory** (completions — cheap). **It never reads device registers on the hot path.** This is the universal fast-device interaction model, and the reason for it is the posted/non-posted asymmetry.

The model: **everything (GPU/NIC/FPGA/RDMA) is behind PCIe; the CPU accesses devices via MMIO (BAR-mapped registers — write=posted/cheap, read=non-posted/expensive round-trip) and devices access host memory via DMA (pinned buffers, IOMMU-translated). The PCIe round-trip (a device read) is the latency floor; fast devices avoid it via the descriptor-ring + doorbell pattern (CPU writes descriptors + rings doorbell; device DMA's completions to host memory; CPU polls host memory). Offload pays off only when the work amortizes the PCIe crossing.**

## 68.3 Measure it: device-register round-trip and streaming bandwidth

Measure the two PCIe costs that govern offload: the **device-register round-trip** (an MMIO read — the latency floor, and the posted/non-posted asymmetry) and the **streaming bandwidth** (bulk DMA — the transfer ceiling). The asymmetry between a register read and write is the headline. The round-trip needs nothing but a BAR mapped from sysfs:

```cpp
// mmio_lat.cpp — the posted/non-posted asymmetry, measured: time MMIO writes vs reads of one device register.
// Build: g++ -O2 -std=c++20 mmio_lat.cpp -o mmio_lat
// Run (root, device idle and unbound or a scratch register — a NIC's or FPGA's BAR0 scratchpad; NEVER a live doorbell):
//   ./mmio_lat /sys/bus/pci/devices/0000:17:00.0/resource0 0x80     # resource0 = UC mapping; resource0_wc = WC mapping
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <algorithm>
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <x86intrin.h>

static inline std::uint64_t t() { unsigned aux; _mm_lfence(); std::uint64_t c = __rdtscp(&aux); _mm_lfence(); return c; }

int main(int argc, char** argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: %s <sysfs resourceN> <reg offset>\n", argv[0]); return 1; }
    int fd = open(argv[1], O_RDWR | O_SYNC);
    if (fd < 0) { perror("open"); return 1; }
    void* bar = mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);   // one page of BAR0, uncacheable
    if (bar == MAP_FAILED) { perror("mmap"); return 1; }
    volatile std::uint32_t* reg = reinterpret_cast<volatile std::uint32_t*>(static_cast<char*>(bar) + std::strtoul(argv[2], nullptr, 0));

    constexpr int N = 100'000; std::vector<std::uint64_t> rd(N), wr(N);
    for (int i = 0; i < N; ++i) { auto a = t(); (void)*reg;               auto b = t(); rd[i] = b - a; }   // non-posted: round-trip
    for (int i = 0; i < N; ++i) { auto a = t(); *reg = std::uint32_t(i);  auto b = t(); wr[i] = b - a; }   // posted: issue cost only
    auto pct = [](std::vector<std::uint64_t>& v, double p) { std::sort(v.begin(), v.end()); return v[std::size_t(p * (v.size() - 1))]; };
    const double ns = 1.0 / 2.9;                                            // cycles -> ns at the 2.9 GHz TSC (Appendix L.3)
    std::printf("MMIO READ  p50 %6.0f ns  p99 %6.0f ns\n", pct(rd, .50) * ns, pct(rd, .99) * ns);
    std::printf("MMIO WRITE p50 %6.0f ns  p99 %6.0f ns   (posted: the CPU does not wait for the device)\n", pct(wr, .50) * ns, pct(wr, .99) * ns);
    // A burst of N writes followed by ONE read measures the write-queue drain: (read latency - single read) / N.
    return 0;
}
```

```
   # MMIO read vs write latency: the program above (resource0 = UC; resource0_wc for a write-combining mapping, §68.4.1)
   # DMA streaming bandwidth: time a large host↔device DMA transfer (e.g. cudaMemcpy — Ch.69,
   #   or a NIC/DPDK bulk transfer), compute GB/s; compare to the link's theoretical (lanes × gen).
```

Representative results (toolchain and machine: Appendix L.3) — a Gen4 x16 device on the CPU's own root port (a 25 GbE NIC under VFIO, BAR0 scratch register; ASPM disabled), illustrative (the *asymmetry* and the *round-trip floor* are the point):

```
   operation                              latency / bandwidth      note
   MMIO WRITE (posted, fire-and-forget)   ~tens of ns to issue     CPU doesn't wait — cheap (doorbell)
   MMIO READ (non-posted, round-trip)     ~hundreds of ns - ~1 µs  CPU STALLS for the full round-trip  ← the floor
   descriptor-ring completion (poll host) ~cache/memory read       device DMA'd it to host mem — cheap (Ch.7)
   DMA streaming bandwidth (x16 Gen4)     ~25-28 GB/s (of ~32)     bulk transfer ceiling (lanes × gen)
   one-way DMA latency (small transfer)   ~hundreds of ns          setup + transfer + completion

   the asymmetry: a device-register READ is ~10-50x a WRITE → NEVER read device registers on the hot path.
```

Read it: the **MMIO read/write asymmetry is the chapter** — a posted **write** is fire-and-forget (~tens of ns to issue, the CPU moves on), while a non-posted **read** **stalls the CPU for a full PCIe round-trip** (~hundreds of ns to a microsecond) — *10-50× more*. So a device-register read on the hot path is a latency catastrophe (§68.5), and the descriptor-ring pattern (§68.2.3) exists precisely to avoid it: the device DMA's completions into *host* memory, which the CPU polls as a cheap cache/memory read (Ch. 7) — not a device read. The **streaming bandwidth** (~25-28 GB/s on x16 Gen4, ~78-88% of theoretical) is the *bulk transfer* ceiling — relevant for sending big data to a GPU (Ch. 69): a 100 MB array takes ~4 ms to transfer, which dwarfs a microsecond compute, illustrating why transfer cost dominates small offloads. The headline number for *offload decisions* is the **round-trip floor** (~hundreds of ns to µs): any offload's control path crosses PCIe at least once, so the work must be big enough to amortize it — the test applied throughout Part XI. The measurement discipline: **measure the round-trip (it's the floor — Ch. 69 measures *end-to-end including transfer*, not just kernel time), and never put a device-register read on the hot path** (§68.5).

## 68.4 Techniques

### 68.4.1 Write-combining and relaxed ordering

Optimizing the *write* path to the device (since writes are the cheap, hot-path direction):

- **Write-combining (WC) memory.** Normally, writes to MMIO are issued individually (each a small PCIe transaction). **Write-combining** (a memory type — set via MTRRs/PAT, or `ioremap_wc`) lets the CPU **coalesce** multiple small writes into larger PCIe transactions (a full cache line) before sending — far more efficient for writing a burst of data to a device (e.g. a descriptor, or a doorbell with payload). Used for the doorbell/descriptor write path. The trade: WC writes are *weakly ordered* (they can be reordered/coalesced) — so you need a fence (`sfence`) to ensure they're flushed when ordering matters (e.g. write the descriptor, fence, then ring the doorbell).
- **Relaxed ordering (PCIe).** PCIe transactions can be marked **relaxed-ordered** (RO) — allowing the fabric/device to reorder them for efficiency (vs strict ordering). For bulk DMA where order doesn't matter, RO improves throughput. A device/driver tuning knob; mind the ordering implications.
- **Batch the doorbell (Ch. 37, 64, 65).** Ring the doorbell *once* for *many* descriptors (post a batch, then one doorbell write) — amortizing the (cheap, but non-zero) doorbell write over many work items (the batching of Ch. 37/64/65). Don't ring per descriptor.
- **Order the descriptor-then-doorbell write (Ch. 30).** The descriptor (in host memory) must be fully written *before* the doorbell (the device will read the descriptor when it sees the doorbell) — a write-ordering requirement (a fence between, especially with WC/relaxed ordering — §68.2.3). The cross-device version of publication ordering (Ch. 30, 35).

### 68.4.2 MSI-X interrupts vs polling for completions

How the CPU learns a device finished — the latency choice (Ch. 41):

- **MSI-X interrupts — the device interrupts the CPU.** When work completes, the device fires an **MSI-X** interrupt (message-signaled interrupt — a posted write to a special address, more scalable than legacy INTx) → the CPU runs an interrupt handler. This frees the CPU between completions (good for *throughput*/idle efficiency), but adds **interrupt latency + a possible context switch** (Ch. 41) to each completion — the latency cost.
- **Polling — the CPU polls host memory (the low-latency way).** Instead of interrupts, the CPU **busy-polls** the completion location in *host memory* (the device DMA'd the completion there — §68.2.3) on a dedicated core (Ch. 41, 42) — learning of completion the instant it lands, with no interrupt latency/jitter. This is the descriptor-ring completion poll (§68.2.3), the busy-poll discipline of Ch. 41/64/65. The HFT default for the hot device path.
- **The trade (Ch. 41).** Polling: lowest latency, deterministic, burns a core. Interrupts: CPU-efficient when idle, higher/jittier latency. Hot device path → poll (Ch. 41, 64, 65); background/throughput device work → interrupts (or interrupt coalescing — Ch. 57). Same poll-vs-block decision as everywhere in this book, at the PCIe completion level.
- **Interrupt coalescing / steering (Ch. 57).** If interrupt-driven, coalesce (batch) interrupts for throughput (adds latency — Ch. 57) and steer the device's MSI-X vectors to the right cores (IRQ affinity — Ch. 42). But polling avoids all of this on the hot path.

### 68.4.3 Peer-to-peer DMA (GPUDirect, NIC-to-GPU/FPGA)

Devices talking *directly* — bypassing the host CPU/memory:

- **Peer-to-peer (P2P) DMA — device-to-device, no host bounce.** Normally device A → host memory → device B (two PCIe transfers + a host-memory bounce). **P2P DMA** lets device A DMA *directly* into device B's memory over PCIe — **no host CPU, no host-memory bounce** — halving the transfers and removing the CPU from the path. Requires the devices to be under a topology that supports it (same root complex / switch, P2P enabled).
- **GPUDirect (NVIDIA) — NIC↔GPU directly.** **GPUDirect RDMA** lets a NIC (RDMA — Ch. 65) DMA *directly* into/out of **GPU memory** — so network data goes straight to the GPU without bouncing through host memory (and back). For GPU-accelerated workloads fed by the network (Ch. 69–70), this removes the host round-trip — critical for keeping the GPU fed at low latency. **GPUDirect Storage** similarly connects NVMe↔GPU.
- **NIC-to-FPGA / NIC-to-GPU (Ch. 71–72).** A NIC delivering packets directly to an FPGA (Ch. 71) or GPU (Ch. 69) via P2P — the data never touches host memory/CPU. For inline acceleration (FPGA on the feed — Ch. 71) or GPU offload fed by the network, P2P is how you avoid the PCIe host-bounce. SmartNICs (Ch. 72) integrate this.
- **When P2P pays.** When data flows device→device and the host CPU doesn't need to touch it — removing the host bounce (two transfers → one, no CPU). It needs topology support (P2P-capable, ideally same switch) and driver support (GPUDirect). A key technique for the accelerator dataflows of Ch. 69–72.

## 68.5 Pitfalls & anti-patterns: a device read on the hot path; IOMMU overhead

- **A device-register read on the hot path (the cardinal PCIe sin).** A non-posted MMIO **read** stalls the CPU for a full PCIe round-trip (~hundreds of ns to µs — §68.3) — 10-50× a write. Reading a device register per message/operation is a latency catastrophe. **Never read device registers on the hot path** — use the descriptor-ring pattern (device DMA's completions to host memory; CPU polls host memory — §68.2.3, §68.4.2). The single most important PCIe rule.
- **Forgetting the PCIe round-trip in offload decisions.** Offloading work whose compute saving is *less* than the PCIe round-trip + transfer cost is a *loss* (§68.1). Always measure **end-to-end including the PCIe crossing** (Ch. 69), not just the accelerator's kernel time. The round-trip is the floor under every offload — if the work doesn't amortize it, don't offload.
- **Not pinning DMA buffers (Ch. 23).** DMA needs **pinned** (physically-stable) host buffers — un-pinned memory can't be DMA'd (the device uses physical addresses; pages could move). Pin/register at setup (Ch. 23, 26, 65). Un-pinned DMA buffers fail or require a slow bounce-buffer copy.
- **IOMMU overhead / IOTLB misses.** The IOMMU translates device addresses (§68.2.2) — adding a small latency, and **IOTLB misses** (like CPU TLB misses — Ch. 15) on scattered DMA. Usually negligible, but for very high-rate small DMA it can matter; use IOMMU huge pages / hugepage DMA buffers (Ch. 15) to reduce IOTLB pressure, or (carefully, security trade — Ch. 75) IOMMU passthrough for trusted devices. Don't disable the IOMMU casually (it's the DMA security boundary — Ch. 75).
- **Cross-NUMA device access (Ch. 16).** Accessing a device from the *other* socket's core crosses the inter-socket interconnect (Ch. 16) — extra latency. Pin the device-using thread on the **device's NUMA node** (Ch. 16, 42, 57). A device-NUMA mismatch is a Ch. 16 cliff at the PCIe level.
- **Write-combining ordering bugs (§68.4.1).** WC writes are weakly ordered/coalesced — forgetting a fence (`sfence`) between the descriptor write and the doorbell (or before a dependent read) lets the device see a doorbell before the descriptor is flushed → it reads stale/incomplete descriptor (a race — Ch. 30, at the device level). Fence appropriately.
- **Interrupt-driven completions on the hot path.** MSI-X interrupts add latency + a context switch (Ch. 41) per completion. On the hot device path, **poll** (§68.4.2). Interrupts are for background/idle-efficient device work.
- **Ignoring the bandwidth ceiling for bulk transfers.** A large host↔device transfer is bounded by the link bandwidth (lanes × gen — §68.2.1); a too-small link (x4 where you need x16, or an older generation) throttles the offload's data feed. Check the link width/speed (`lspci -vv`); a device negotiated to a lower width/gen than expected is a real, silent bottleneck.
- **ASPM — the link that went to sleep.** PCIe Active State Power Management (L0s/L1 and the L1 substates) lets an idle link drop into a low-power state; the *first* MMIO or DMA after an idle gap then pays microseconds of exit latency — the single most common PCIe-latency trap on a trading box, and invisible under continuous polling. Check `LnkCtl: ASPM Disabled` in `lspci -vv` for every device on the hot path, boot with `pcie_aspm=off` (or `/sys/module/pcie_aspm/parameters/policy = performance`), disable it in BIOS, and measure first-access latency after an idle gap vs continuous polling.
- **Assuming P2P works without topology/driver support.** P2P DMA (§68.4.3) needs the right topology (P2P-capable path) and driver support (GPUDirect); assuming it works when the devices are under different root complexes (no P2P) silently falls back to a host bounce. Verify P2P is actually happening.

## 68.6 Exercises & checklist

**Exercises**

1. **Read/write asymmetry.** With a mapped device BAR (via a driver or DPDK/VFIO — Ch. 64), time a posted MMIO **write** vs a non-posted MMIO **read** to a device register. Confirm the read is ~10-50× the write (§68.3). This is *the* PCIe lesson.
2. **Descriptor-ring poll.** Observe how a NIC driver (Ch. 57, 64) or RDMA (Ch. 65) signals completions — confirm it's the device DMA-ing a completion into *host* memory that the CPU *polls*, not a device-register read (§68.2.3). Why is this designed this way?
3. **Streaming bandwidth.** Measure host↔device DMA bandwidth (`cudaMemcpy` for a GPU — Ch. 69, or a DPDK bulk transfer) for increasing sizes; compute GB/s and compare to the link's theoretical (lanes × gen — check `lspci -vv`). Find the size where transfer dominates a microsecond compute (§68.3, ties Ch. 69).
4. **NUMA device placement (Ch. 16).** Access a device (NIC/GPU) from a core on its NUMA node vs the other socket; measure the latency difference. Confirm the cross-socket penalty (§68.5, Ch. 16). Pin to the device's node.
5. **Poll vs interrupt completion.** For a device operation, reap completions by polling host memory vs MSI-X interrupt; measure the latency difference (§68.4.2, Ch. 41). Confirm polling wins on a dedicated core.

**Checklist — PCIe & the host–device boundary**

- [ ] **No device-register reads on the hot path** — interaction uses the **descriptor-ring + doorbell** pattern (CPU writes descriptors + rings doorbell — posted/cheap; device DMA's completions to **host memory**; CPU **polls host memory**) (§68.2.3, §68.4.2).
- [ ] Offload decisions account for the **PCIe round-trip floor** + transfer cost — measured **end-to-end** (Ch. 69), not just accelerator kernel time; work must **amortize the crossing** (§68.1).
- [ ] DMA buffers are **pinned/registered** at setup (Ch. 23, 26, 65), **huge-page-backed** (Ch. 15, reduce IOTLB misses), and **NUMA-local to the device** (Ch. 16, 42); the device link is the expected **width/generation** (`lspci -vv`).
- [ ] Completions are **polled** on a dedicated core (Ch. 41, 42) on the hot device path — **not MSI-X interrupts** (which are for background/throughput work) (§68.4.2).
- [ ] The **write path** is optimized: **write-combining** + **batched doorbells**, with correct **fencing** (descriptor-before-doorbell ordering — §68.4.1, Ch. 30).
- [ ] **Peer-to-peer DMA** (GPUDirect / NIC-to-GPU/FPGA) is used where data flows device→device to avoid the **host bounce** — with verified topology/driver support (§68.4.3).
- [ ] The **IOMMU** is configured (DMA security boundary — Ch. 75) without casual disabling; IOTLB pressure managed (huge pages) where high-rate small DMA matters (§68.5).
- [ ] I recognize PCIe is the **floor under all of Part XI** — latency-critical work that can't afford the round-trip goes **inline on the NIC (FPGA — Ch. 71)**, not behind PCIe; bulk/parallel/latency-insensitive work offloads (GPU — Ch. 69).

## 68.7 References

- The PCI Express Base Specification and the various PCIe primers — topology, lanes/generations, posted/non-posted transactions, and MMIO/config space (§68.2).
- Intel/AMD platform and IOMMU (VT-d/AMD-Vi) documentation, and the Linux `Documentation/` on VFIO, DMA, and IOMMU — MMIO/DMA/IOMMU mechanics (§68.2.2; ties Ch. 64, 75).
- The NVIDIA **GPUDirect** (RDMA/Storage) documentation — peer-to-peer DMA (§68.4.3, ties Ch. 69).
- Device-driver and DPDK/RDMA documentation (descriptor rings, doorbells, write-combining, MSI-X) — the §68.2.3/§68.4 patterns in practice (ties Ch. 57, 64, 65).
- `lspci`/`lstopo`/`setpci` documentation — inspecting PCIe topology, link width/speed, and device placement (§68.2.1, §68.5).

## 68.8 Additional Reading

- The "PCIe latency" and host-device round-trip analyses (vendor whitepapers, academic measurements) — the numbers behind §68.3.
- Ch. 69 (*GPU/CUDA*) — measuring offload end-to-end including the PCIe transfer; Ch. 71 (*FPGA*) — inline-on-NIC to avoid the PCIe round-trip; Ch. 72 (*SmartNICs/DPUs*) — devices on the host boundary; Ch. 65 (*RDMA*) — registered/pinned DMA memory; Ch. 57/64 (*NIC/Bypass*) — the descriptor-ring/doorbell NIC path; Ch. 16 (*NUMA*) — device-NUMA placement; Ch. 15 (*TLB*) — the IOTLB analog.
- **Appendix E** — the PCIe round-trip number (the floor under offload); **Appendix F** — PCIe/MMIO/DMA/IOMMU glossary.

---

*Next: Ch. 69 — GPU Computing with CUDA, the first concrete accelerator: the CUDA execution and memory model, where GPUs pay off in trading (risk, options pricing, Monte-Carlo, ML) — and why the PCIe round-trip of this chapter keeps them off the tick-to-trade hot path, measured end-to-end including transfer, not just kernel time.*
