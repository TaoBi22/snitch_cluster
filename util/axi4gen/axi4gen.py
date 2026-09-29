#!/usr/bin/env python3
# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Generate the Snitch cluster crossbars with the CIRCT AXI4 dialect."""

import argparse
import pathlib
import shutil
import subprocess
import sys

import hjson
from jsonref import JsonRef
from mako.template import Template

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from clustergen.cluster import PMACfg, SnitchCluster, clog2  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cfg", "-c", type=pathlib.Path, required=True,
                    help="Configuration with a `cluster` section")
parser.add_argument("--out", "-o", type=pathlib.Path, required=True, help="Generated SV file")
parser.add_argument("--build-dir", type=pathlib.Path, required=True,
                    help="Directory for the MLIR and the per-module SV")
parser.add_argument("--circt-opt", default="circt-opt")
args = parser.parse_args()

CFG = JsonRef.replace_refs(hjson.loads(args.cfg.read_text()))
# Validating fills in the schema defaults, e.g. the ID widths
CLUSTER = SnitchCluster(CFG["cluster"], PMACfg()).cfg

# =============================================================================
# Parameters, as the cluster wrapper sets them
# =============================================================================

ADDR_WIDTH = CLUSTER["addr_width"]
NARROW_DATA_WIDTH = CLUSTER["data_width"]
WIDE_DATA_WIDTH = CLUSTER["dma_data_width"]
NARROW_ID_WIDTH_IN = CLUSTER["id_width_in"]
WIDE_ID_WIDTH_IN = CLUSTER["dma_id_width_in"]
NARROW_USER_WIDTH = CLUSTER["user_width"]
WIDE_USER_WIDTH = CLUSTER["dma_user_width"]
NARROW_LATENCY = CLUSTER["timing"]["narrow_xbar_latency"]
WIDE_LATENCY = CLUSTER["timing"]["wide_xbar_latency"]
NARROW_TRANS = CLUSTER["narrow_trans"]
WIDE_TRANS = CLUSTER["wide_trans"]
NR_HIVES = len(CLUSTER["hives"])

TCDM_SIZE = CLUSTER["tcdm"]["size"] * 1024
PERIPH_SIZE = CLUSTER["cluster_periph_size"] * 1024
ZERO_MEM_SIZE = CLUSTER["zero_mem_size"] * 1024
ALIAS_BASE = CLUSTER["alias_region_base"] if CLUSTER["alias_region_enable"] else None

# Cluster bases as Occamy assigns them; one generated crossbar pair per base
NR_CLUSTERS = CFG.get("nr_s1_quadrant", 1) * CFG.get("s1_quadrant", {}).get("nr_clusters", 1)
CLUSTER_BASES = [CLUSTER["cluster_base_addr"] + i * CLUSTER.get("cluster_base_offset", 0)
                 for i in range(NR_CLUSTERS)]

if len(set(CLUSTER_BASES)) != len(CLUSTER_BASES):
    sys.exit("Several clusters need a `cluster_base_offset`.")
for base in CLUSTER_BASES + ([ALIAS_BASE] if ALIAS_BASE is not None else []):
    if base % TCDM_SIZE:
        sys.exit(f"Region base {base:#x} is not aligned to the TCDM size.")
if min(NARROW_ID_WIDTH_IN, WIDE_ID_WIDTH_IN) < 1:
    sys.exit("PULP cannot express a 0-bit ID.")

# Managers in `snitch_pkg` order
NARROW_MGRS = ["core", "soc_in", "ptw"]
WIDE_MGRS = ["dma", "soc_in"] + [f"icache_{i}" for i in range(NR_HIVES)]

# The lowering sizes each port's ID as clog2(outstanding): the managers' as the cluster's, the
# subordinates' widened by the crossbar's clog2(#managers)
NARROW_MGR_OUTSTANDING = 2 ** NARROW_ID_WIDTH_IN
NARROW_SUB_OUTSTANDING = 2 ** (NARROW_ID_WIDTH_IN + clog2(len(NARROW_MGRS)))
WIDE_MGR_OUTSTANDING = 2 ** WIDE_ID_WIDTH_IN
WIDE_SUB_OUTSTANDING = 2 ** (WIDE_ID_WIDTH_IN + clog2(len(WIDE_MGRS)))

# =============================================================================
# Address map
# =============================================================================

BURSTS = "<<incr, len = 256>>"
REGIONS = [""] + (["_alias"] if ALIAS_BASE is not None else [])


def render_window(w):
    base, last = w
    return f"<base = {base:#x}, last = {last:#x}, burst_specs = {BURSTS}>"


def window_set(sub, windows, sub_windows):
    return ", ".join(render_window(windows[w]) for w in sub_windows[sub])


def accesses(connectivity, windows, sub_windows):
    # A manager reaching several windows of a subordinate declares an access to each
    return "\n".join(
        f"  axi4.dummies.accesses %{mgr}_mgr_access -> %{sub}_access "
        f"with {render_window(windows[w])}"
        for mgr, subs in connectivity.items()
        for sub in subs
        for w in sub_windows[sub]
    )


def cluster_windows(base):
    """Windows of one cluster and its alias, laid out as in `snitch_cluster.sv`."""
    windows = dict()
    for region, start in zip(REGIONS, [base, ALIAS_BASE]):
        windows[f"tcdm{region}"] = (start, start + TCDM_SIZE - 1)
        start += TCDM_SIZE
        windows[f"periph{region}"] = (start, start + PERIPH_SIZE - 1)
        start += PERIPH_SIZE
        windows[f"zero_mem{region}"] = (start, start + ZERO_MEM_SIZE - 1)
    return windows


def with_soc_out(windows, sub_windows):
    """Give `soc_out` every address the other subordinates leave, as `axi_xbar`'s default port."""
    windows, soc = dict(windows), []
    nxt = 0
    for lo, last in sorted(windows[w] for ws in sub_windows.values() for w in ws):
        if lo < nxt:
            sys.exit(f"Overlapping cluster windows at {lo:#x}.")
        if lo > nxt:
            soc.append((nxt, lo - 1))
        nxt = last + 1
    if nxt < 2 ** ADDR_WIDTH:
        soc.append((nxt, 2 ** ADDR_WIDTH - 1))
    for i, w in enumerate(soc):
        windows[f"soc_out_{i}"] = w
    return windows, dict(sub_windows, soc_out=[f"soc_out_{i}" for i in range(len(soc))])


# The narrow crossbar has no rule for the zero memory, and the wide one none for the peripherals
NARROW_SUB_WINDOWS = {
    "tcdm": [f"tcdm{r}" for r in REGIONS],
    "periph": [f"periph{r}" for r in REGIONS],
}
WIDE_SUB_WINDOWS = {
    "tcdm": [f"tcdm{r}" for r in REGIONS],
    "zero_mem": [f"zero_mem{r}" for r in REGIONS],
}

# =============================================================================
# Crossbars
# =============================================================================


def ports(data_width, outstanding, atops):
    return (f"addr_width = {ADDR_WIDTH}, data_width = {data_width}, "
            f"outstanding_writes = {outstanding}, outstanding_reads = {outstanding}"
            + (" {pulp.atops}" if atops else ""))


def pulp_config(latency, trans):
    return (f'PULP_CONFIG_LatencyMode = "axi_pkg::{latency}", '
            f"PULP_CONFIG_MaxSlvTrans = {trans} : i32, PULP_CONFIG_MaxMstTrans = {trans} : i32, "
            "PULP_CONFIG_FallThrough = false")


def narrow_xbar(k, base):
    windows, sub_windows = with_soc_out(cluster_windows(base), NARROW_SUB_WINDOWS)
    # The handwritten crossbar connects every manager to every subordinate
    connectivity = {mgr: list(sub_windows) for mgr in NARROW_MGRS}
    # Every narrow port may carry atomics, as through the handwritten `axi_xbar`
    mgr = ports(NARROW_DATA_WIDTH, NARROW_MGR_OUTSTANDING, atops=True)
    sub = ports(NARROW_DATA_WIDTH, NARROW_SUB_OUTSTANDING, atops=True)
    return f"""// Narrow crossbar of the cluster at {base:#x}

hw.module @snitch_cluster_narrow_xbar_{k}(in %clk : !seq.clock, in %rst_ni : i1) {{
  %core_mgr, %core_mgr_access = axi4.dummies.ext_manager "core" %clk, %rst_ni {mgr}
  %soc_in_mgr, %soc_in_mgr_access = axi4.dummies.ext_manager "soc_in" %clk, %rst_ni {mgr}
  %ptw_mgr, %ptw_mgr_access = axi4.dummies.ext_manager "ptw" %clk, %rst_ni {mgr}

  %xbar = axi4.dummies.xbar %clk, %rst_ni mgrs %core_mgr, %soc_in_mgr, %ptw_mgr
    addr_width = {ADDR_WIDTH}, data_width = {NARROW_DATA_WIDTH}
    {{{pulp_config(NARROW_LATENCY, NARROW_TRANS)}}}

  %tcdm_access = axi4.dummies.ext_subordinate "tcdm" %clk, %rst_ni, %xbar
    windows <{window_set('tcdm', windows, sub_windows)}>
    {sub}
  %periph_access = axi4.dummies.ext_subordinate "periph" %clk, %rst_ni, %xbar
    windows <{window_set('periph', windows, sub_windows)}>
    {sub}
  %soc_out_access = axi4.dummies.ext_subordinate "soc_out" %clk, %rst_ni, %xbar
    windows <{window_set('soc_out', windows, sub_windows)}>
    {sub}

{accesses(connectivity, windows, sub_windows)}
}}
"""


def wide_xbar(k, base):
    windows, sub_windows = with_soc_out(cluster_windows(base), WIDE_SUB_WINDOWS)
    # The handwritten crossbar connects every manager to every subordinate
    connectivity = {mgr: list(sub_windows) for mgr in WIDE_MGRS}
    # The handwritten crossbar has `ATOPs` off
    mgr = ports(WIDE_DATA_WIDTH, WIDE_MGR_OUTSTANDING, atops=False)
    sub = ports(WIDE_DATA_WIDTH, WIDE_SUB_OUTSTANDING, atops=False)
    mgrs = "\n".join(f'  %{m}_mgr, %{m}_mgr_access = axi4.dummies.ext_manager "{m}" '
                     f"%clk, %rst_ni {mgr}" for m in WIDE_MGRS)
    return f"""// Wide crossbar of the cluster at {base:#x}

hw.module @snitch_cluster_wide_xbar_{k}(in %clk : !seq.clock, in %rst_ni : i1) {{
{mgrs}

  %xbar = axi4.dummies.xbar %clk, %rst_ni mgrs {', '.join(f'%{m}_mgr' for m in WIDE_MGRS)}
    addr_width = {ADDR_WIDTH}, data_width = {WIDE_DATA_WIDTH}
    {{{pulp_config(WIDE_LATENCY, WIDE_TRANS)}}}

  %tcdm_access = axi4.dummies.ext_subordinate "tcdm" %clk, %rst_ni, %xbar
    windows <{window_set('tcdm', windows, sub_windows)}>
    {sub}
  %zero_mem_access = axi4.dummies.ext_subordinate "zero_mem" %clk, %rst_ni, %xbar
    windows <{window_set('zero_mem', windows, sub_windows)}>
    {sub}
  %soc_out_access = axi4.dummies.ext_subordinate "soc_out" %clk, %rst_ni, %xbar
    windows <{window_set('soc_out', windows, sub_windows)}>
    {sub}

{accesses(connectivity, windows, sub_windows)}
}}
"""


# =============================================================================
# Lowering to SV
# =============================================================================


def lower(name, mlir, user_width):
    """Lower one crossbar's MLIR to SV, one file per module."""
    build = args.build_dir / name
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    (build / f"{name}.mlir").write_text(mlir)
    subprocess.run([
        args.circt_opt, f"--lower-axi4-dummies-to-axi=user-width={user_width}",
        "--lower-axi4-to-hw=pulp-mapping=true req-resp-ports=true",
        build / f"{name}.mlir", "-o", build / f"{name}.hw.mlir"
    ], check=True)
    subprocess.run([
        args.circt_opt, "--test-apply-lowering-options=options=locationInfoStyle=none",
        "--lower-seq-to-sv", f"--export-split-verilog=dir-name={build / 'sv'}",
        build / f"{name}.hw.mlir", "-o", "/dev/null"
    ], check=True)
    files = (build / "sv" / "filelist.f").read_text().split()
    # Each crossbar has one width per side, so a converter means a misderived ID width
    if any("converter" in f for f in files):
        sys.exit(f"Unexpected converter in `{name}`.")
    return {f: (build / "sv" / f).read_text() for f in files}


narrow = lower("snitch_cluster_narrow_xbar",
               "\n".join(narrow_xbar(k, b) for k, b in enumerate(CLUSTER_BASES)),
               NARROW_USER_WIDTH)
wide = lower("snitch_cluster_wide_xbar",
             "\n".join(wide_xbar(k, b) for k, b in enumerate(CLUSTER_BASES)),
             WIDE_USER_WIDTH)
# PULP wrapper names don't encode the user width, so the two runs could reuse one
for f in narrow.keys() & wide.keys():
    if narrow[f] != wide[f]:
        sys.exit(f"Both crossbars generate a different `{f}`.")

# =============================================================================
# Selectors, instantiated by `snitch_cluster.sv` in place of `axi_xbar`
# =============================================================================


def req_resp_bits(data_width, id_width, user_width):
    """Widths of PULP's `req_t` and `resp_t`, to check the cluster's structs against."""
    ax = id_width + ADDR_WIDTH + 8 + 3 + 2 + 1 + 4 + 3 + 4 + 4 + user_width
    w = data_width + data_width // 8 + 1 + user_width
    b = id_width + 2 + user_width
    r = id_width + data_width + 2 + 1 + user_width
    return (ax + 6) + w + ax + 5, b + r + 5


SIZES = {
    "TCDMSize": ("int unsigned", "0", TCDM_SIZE),
    "ClusterPeriphSize": ("int unsigned", "0", PERIPH_SIZE // 1024),
    "ZeroMemorySize": ("int unsigned", "0", ZERO_MEM_SIZE // 1024),
    "AliasRegionEnable": ("bit", "1'b0", int(ALIAS_BASE is not None)),
    "AliasRegionBase": (f"logic [{ADDR_WIDTH - 1}:0]", "'0",
                        f"{ADDR_WIDTH}'h{CLUSTER['alias_region_base']:x}"),
}


def config(latency, trans):
    return {
        "LatencyMode": ("axi_pkg::xbar_latency_e", "axi_pkg::NO_LATENCY", f"axi_pkg::{latency}"),
        "MaxMstTrans": ("int unsigned", "0", trans),
        "MaxSlvTrans": ("int unsigned", "0", trans),
    }


# Ports as `snitch_pkg` indexes the cluster's AXI arrays
SELECTORS = [{
    "name": "snitch_cluster_narrow_xbar",
    "mgrs": {"core": "snitch_pkg::CoreReq", "soc_in": "snitch_pkg::AXISoC",
             "ptw": "snitch_pkg::PTW"},
    "subs": {"tcdm": "snitch_pkg::TCDM", "periph": "snitch_pkg::ClusterPeripherals",
             "soc_out": "snitch_pkg::SoC"},
    "mgr_bits": req_resp_bits(NARROW_DATA_WIDTH, NARROW_ID_WIDTH_IN, NARROW_USER_WIDTH),
    "sub_bits": req_resp_bits(NARROW_DATA_WIDTH, clog2(NARROW_SUB_OUTSTANDING),
                              NARROW_USER_WIDTH),
    "checks": dict(SIZES, **config(NARROW_LATENCY, NARROW_TRANS)),
}, {
    "name": "snitch_cluster_wide_xbar",
    "mgrs": dict({"dma": "snitch_pkg::SDMAMst", "soc_in": "snitch_pkg::SoCDMAIn"},
                 **{f"icache_{i}": f"snitch_pkg::ICache + {i}" for i in range(NR_HIVES)}),
    "subs": {"tcdm": "snitch_pkg::TCDMDMA", "zero_mem": "snitch_pkg::ZeroMemory",
             "soc_out": "snitch_pkg::SoCDMAOut"},
    "mgr_bits": req_resp_bits(WIDE_DATA_WIDTH, WIDE_ID_WIDTH_IN, WIDE_USER_WIDTH),
    "sub_bits": req_resp_bits(WIDE_DATA_WIDTH, clog2(WIDE_SUB_OUTSTANDING), WIDE_USER_WIDTH),
    "checks": dict(SIZES, NrHives=("int unsigned", "0", NR_HIVES),
                   **config(WIDE_LATENCY, WIDE_TRANS)),
}]

selectors = Template(filename=str(pathlib.Path(__file__).parent / "snitch_cluster_xbars.sv.tpl"))
args.out.write_text(
    "// AUTOMATICALLY GENERATED by axi4gen.py; edit the script or configuration instead.\n\n"
    + "".join(dict(narrow, **wide).values())
    + selectors.render(selectors=SELECTORS, bases=CLUSTER_BASES, addr_width=ADDR_WIDTH))
