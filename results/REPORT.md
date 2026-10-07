# VCS regression: baseline vs. generated snitch_cluster crossbars

Run on `turing` on 2026-09-29/30.

## Summary

**No differences.** All 53 active tests in `sw/run.yaml` pass in both runs. Every test has the same pass status and the same `simulation time [ns]` (total 297,200 ns in both). No time-0 assertion from the generated crossbars fired. There are no compile or elaboration warnings or errors about the generated crossbar files or modules.

To get VCS to build at all, both runs needed some environment and flow workarounds. They are listed under "Deviations from the handover". Each one was applied the same way to both runs, and none touches the snitch_cluster RTL or the patches.

| | baseline | generated |
|---|---|---|
| Branch / commit | `baseline` @ `da57b043` | `axi4-xbars` @ `5a712b91` (patches 0001–0003 on `da57b04`, `git am` applied cleanly) |
| Tests passed | 53 / 53 | 53 / 53 |
| Sum of sim time | 297,200 ns | 297,200 ns |
| Tests with differing status or sim time | – | **0** |
| Report | `baseline_sim_report.csv` | `generated_sim_report.csv` |

## 1. CSVs

- `baseline_sim_report.csv` and `generated_sim_report.csv` are `run.py`'s reports, copied unchanged.
  - At this commit `run.py` writes the report to `<run-dir>/report.csv`, i.e. `runs/vcs/report.csv`. It does not write `sim_report.csv` as the README says (`util/snitch_cluster.py` passes `report_path=Path(args.run_dir) / 'report.csv'`).
- **53 tests, not 57.** `sw/run.yaml` has 57 `elf:` lines, but 4 are commented out: `fp64_conversions_scalar`, `interrupt`, `conv2d` ("Fails with wrong results") and `fusedconv` ("Fails with wrong results").

## 2. Per-test comparison

`comparison.csv` puts both runs side by side for every test.

**No test differs in status or in `simulation time [ns]`.** CPU time differs, as expected: the baseline ran 4 sims in parallel and the generated run ran 2. Neither setting affects simulated time.

On top of the CSV comparison, I diffed every `Error:`, `ASSERT`, `Fatal` and `Warning-` line of each test's `sim.txt` between the runs, timestamps included. All 53 tests are identical (see section 3 for the pre-existing errors).

## 3. Elaboration and time-0 assertions (generated run)

Where I searched:
- `logs/generated/generated_make_vcs.log` (elaboration)
- `logs/generated/vlogan_compile.log` (analysis)
- `logs/generated/generated_make_sw.log`
- all 53 `sim.txt` files in `logs/generated/sim_logs.tgz`

| Pattern | Hits |
|---|---|
| `Generated` (`GeneratedTCDMSize`, …, `ASSERT_INIT` in `snitch_cluster_{narrow,wide}_xbar`) | 0 |
| `SlvBits`, `MstBits` | 0 |
| `No generated snitch_cluster` (the `$fatal` in `gen_unsupported`) | 0 |
| `ClusterBaseAddrParam` (also `ClusterBaseAddrAlign`) | 0 |
| `ASSERT FAILED` (the `ASSERT_RPT` prefix) / `Fatal` | 0 |

**Why "0 hits" means the checks passed, not that they were missing:**
- `INC_ASSERT` is defined under VCS here: `assertions.svh` defines it unless `VERILATOR`, `SYNTHESIS` or `XSIM` is set, and the flow sets none of them. So the `ASSERT_INIT`s are compiled in.
- The elaborated database (`bin/snitch_cluster.vcs.daidir`) contains `snitch_cluster_narrow_xbar_0`, `snitch_cluster_wide_xbar_0`, `axi_xbar_3u3d_a48_d64_i2_o4_atop` and `axi_xbar_3u3d_a48_d512_i1_o3`.
- `gen_unsupported` does not appear in the elaborated database. So the `BaseAddr` case picked `48'h10000000`, which matches the wrapper's `ClusterBaseAddr = 48'h10000000` and `cluster_base_addr_i = 48'h10000000`.
- The pre-lowered `snitch_cluster_axi4_xbars.sv` was used as-is. Its sha256 is `a5ad2d52…6afc046`, the same before and after the build. With `-o $PWD/generated/snitch_cluster_axi4_xbars.sv`, make never ran `axi4gen.py`/`circt-opt` (checked with `make -n` and in the build log).
  - Without `-o`, make *would* run `axi4gen.py`. `cfg/lru.hjson` depends on `FORCE`, so everything generated from the config always looks out of date.

**Pre-existing runtime errors, identical in both runs.** Four tests print SVA `$error`s from `common_cells/src/stream_xbar.sv:184/195`, in `i_tcdm_interconnect.gen_xbar.i_stream_xbar.gen_handshake_assertions`:

| Test | `Error:` lines |
|---|---|
| `axpy` | 37 |
| `gemm` | 1152 |
| `openmp_double_buffering` | 2596 |
| `non_null_exitcode` | 1 (`tb_bin.sv:51`, the intended non-zero exit) |

- These are `data_i`/`data_o` "unstable while `valid && !ready`" checks in the **TCDM** interconnect, not the AXI crossbars.
- They appear in the baseline with the same counts and timestamps, and all these tests still pass.
- Not caused by this change, but maybe worth a separate look upstream.

## 4. Compile and elaboration warnings or errors about the generated crossbars

**None.** No warning or error in the analysis or elaboration logs mentions `snitch_cluster_axi4_xbars.sv`, `snitch_cluster_narrow_xbar`, `snitch_cluster_wide_xbar` or `axi_xbar_3u3d_*`.

Both runs produce exactly the same warnings, and only these:
- 65 × `Warning-[LNX_OS_VERUN]` in analysis
- 1 × `Warning-[LNX_OS_VERUN]` in elaboration

VCS W-2024.09 does not officially support AlmaLinux 9.8.

## 5. Tool versions

| Tool | Version |
|---|---|
| VCS | `W-2024.09-1_Full64` (compiler and runtime), `/usr/synopsys/vcs/W-2024.09-1` |
| Bender | `bender 0.32.1` (not 0.27.1 as in `iis-setup.sh`) |
| LLVM for `make sw` | pulp-llvm 0.12.0, `clang version 12.0.1 (… d2f0eff9be1f58bb186499e2055eb6888ce88dcc)`, `llvm-config` 12.0.1-0, at `~/tools/pulp-llvm/riscv32-pulp-llvm-centos7-0.12.0/bin` |
| Host C/C++ (testbench, libfesvr) | gcc 11.5.0 (Red Hat 11.5.0-14) |
| Python (venv) | 3.9.25, with `numpy` 1.26.4 and `torch` 2.1.0 |
| spike-dasm | snitch-v0.1.0 release tarball |

## Deviations from the handover

All of these were applied the same way to both runs. `env.sh` and `vcs-prepare.sh` in this directory reproduce the setup.

1. **`iis-setup.sh` can't run here**, because it points at ETH paths (`/usr/local/anaconda3-2022.05`, `bender-0.27.1`, `/usr/pack/...`, SEPP). I did its steps by hand:
   - venv with system Python 3.9 + `python-requirements.txt`
   - `bender vendor init`
   - spike-dasm into `tools/`
   - `LLVM_BINROOT` set to the local pulp-llvm 0.12.0, the same version the script names
2. **Bender lock.** Bender 0.32.1 accepted `Bender.lock` as-is. `bender checkout` left it byte-identical, and I did not run `bender update`.
   - Side note: the lock labels `axi` as 0.39.4, but its revision `587355b7` describes as `v0.39.3-40-g587355b7`.
   - Also, Occamy's own `Bender.local` on this machine overrides `axi` to 0.39.2 and `common_cells` to 1.31.1. So Occamy's local builds don't use these pins.
3. **Git submodules** (`sw/deps/riscv-opcodes`, `sw/deps/printf`) needed `git submodule update --init`, because the README's clone isn't `--recursive`.
4. **`numpy<2`.** pip resolved numpy 2.0.2, which torch 2.1 can't use ("Numpy is not available"), so the DNN data generators failed. I pinned numpy 1.26.4.
5. **Wrapper before the VCS script.** On a clean tree, `bender script vcs` fails (`E31 … snitch_cluster_wrapper.sv doesn't exist`): `VCS_SOURCES` is expanded when make parses the Makefile, and `work-vcs/compile.sh` has no prerequisites. I ran `make $PWD/generated/snitch_cluster_wrapper.sv` first.
6. **`COMMON_BENDER_FLAGS="-t snitch_cluster"`**, as the handover says.
7. **VCS analysis (`vcs-prepare.sh`, modelled on Occamy's).** Bender 0.32.1 puts the dependencies' `test/` testbenches into `compile.sh`. That caused two problems, fixed as follows:
   - `` `timescale `` mismatches (`ITSFM`): I added `-timescale=1ns/1ps` to vlogan. Elaboration already uses `-override_timescale=1ns/1ps`, so simulation semantics don't change.
   - A `$unit` class clash (`CRE`, `icache_request`): Occamy's `vcs-filter-compile.py` drops dependency `*_tb.sv` / `tb_*.sv` files. It dropped the same 56 files, plus 4 then-empty vlogan calls, in both runs.
   - I fixed one bug in the local copy. Its "keep `*_pkg.sv`" check didn't strip the trailing ` \`, so it also dropped `tb_axi_pkg.sv` and broke `axi_riscv_atomics/test/golden_memory.sv` (`SV-EEM-SRE`). It's a one-line diff against `~/repos/occamy/target/sim/vcs-filter-compile.py`.
8. **Dependency RTL patch (`axi-vcs.patch`, approved by you).** VCS W-2024.09 rejects `axi_xbar_unmuxed_intf` (`SV-UMDAI`, multi-dimensional array of interfaces). I applied Occamy's `target/sim/axi-vcs.patch` to the pinned `axi` checkout in `.bender/`.
   - It wraps that interface module in `` `ifndef TARGET_VCS `` and adds `axi_pkg::iomsb` for the zero-entry `IdMap` in `axi_id_serialize`.
   - Nothing in the cluster or in the generated crossbars instantiates either module.
9. **Elaboration flags via `VCS=`** (this commit's Makefile has no `VCS_FLAGS` hook):
   - `-ignore initializer_driver_checks`, for `ICPD_INIT` on `logic [63:0] cycle = 0;` in `snitch_cc.sv`
   - `-j24`
10. **Shared-machine limits.**
    - Builds: `taskset -c 0-23 nice -n 19`.
    - Sims: the license server has only 5 `VCSRuntime_Net` licenses. So I used `run.py -j 4` for the baseline, `-j 2` for the generated run (your request), and `SNPSLMD_QUEUE=true` so a sim waits for a license instead of failing.
    - The first baseline attempt at `-j 24` ran out of licenses. `simple` and `printf_simple` "failed" with "Failed to obtain license", and `gemm`'s IPC `verify.py` blocked forever on its FIFO once its simulator died. That attempt was discarded and the baseline rerun from scratch (`logs/baseline/baseline_run_license_failure.log`).

## Files

| Path | Contents |
|---|---|
| `baseline_sim_report.csv`, `generated_sim_report.csv` | `run.py` reports |
| `comparison.csv` | per-test side-by-side comparison |
| `env.sh`, `vcs-prepare.sh`, `vcs-filter-compile.py`, `axi-vcs.patch` | environment and flow workarounds |
| `logs/{baseline,generated}/` | `make sw`, vlogan analysis, elaboration and `run.py` logs, filter log, and every test's `sim.txt` (`sim_logs.tgz`) |
