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

BURSTS = "<<incr, len = 256>>"


def load_cluster(cfg_path):
    """The configuration and its validated `cluster` section, with the schema defaults filled in."""
    cfg = JsonRef.replace_refs(hjson.loads(pathlib.Path(cfg_path).read_text()))
    return cfg, SnitchCluster(cfg["cluster"], PMACfg()).cfg


def cluster_bases(cfg, cluster):
    """Cluster bases as Occamy assigns them; one generated crossbar pair per base."""
    nr_clusters = cfg.get("nr_s1_quadrant", 1) * cfg.get("s1_quadrant", {}).get("nr_clusters", 1)
    return [cluster["cluster_base_addr"] + i * cluster.get("cluster_base_offset", 0)
            for i in range(nr_clusters)]


class ClusterXbars:
    """The cluster's two crossbars, with the parameters the cluster wrapper sets."""

    def __init__(self, cluster):
        self.addr_width = cluster["addr_width"]
        self.nr_hives = len(cluster["hives"])
        self.tcdm_size = cluster["tcdm"]["size"] * 1024
        self.periph_size = cluster["cluster_periph_size"] * 1024
        self.zero_mem_size = cluster["zero_mem_size"] * 1024
        self.alias_base = cluster["alias_region_base"] if cluster["alias_region_enable"] else None
        self.alias_region_base = cluster["alias_region_base"]
        self.regions = [""] + (["_alias"] if self.alias_base is not None else [])
        # Managers in `snitch_pkg` order; `soc_in` and `soc_out` link the cluster to the outside.
        # The narrow crossbar has no rule for the zero memory, and the wide one none for the
        # peripherals. Every narrow port may carry atomics, as through the handwritten `axi_xbar`,
        # which has `ATOPs` off on the wide side.
        self.sides = {
            "narrow": self._side(cluster, "data_width", "id_width_in", "user_width",
                                 "narrow_xbar_latency", "narrow_trans",
                                 ["core", "soc_in", "ptw"], ["tcdm", "periph"], atops=True),
            "wide": self._side(cluster, "dma_data_width", "dma_id_width_in", "dma_user_width",
                               "wide_xbar_latency", "wide_trans",
                               ["dma", "soc_in"] + [f"icache_{i}" for i in range(self.nr_hives)],
                               ["tcdm", "zero_mem"], atops=False),
        }
        if min(s["id_width_in"] for s in self.sides.values()) < 1:
            sys.exit("PULP cannot express a 0-bit ID.")

    @staticmethod
    def _side(cluster, data, id_in, user, latency, trans, mgrs, subs, atops):
        id_width_in = cluster[id_in]
        return {
            "data_width": cluster[data],
            "id_width_in": id_width_in,
            "user_width": cluster[user],
            "latency": cluster["timing"][latency],
            "trans": cluster[trans],
            "mgrs": mgrs,
            "subs": subs,
            "atops": atops,
            # The lowering sizes each port's ID as clog2(outstanding_*_ids): the managers' as the
            # cluster's, the subordinates' widened by the crossbar's clog2(#managers)
            "mgr_ids": 2 ** id_width_in,
            "sub_ids": 2 ** (id_width_in + clog2(len(mgrs))),
        }

    def check_bases(self, bases):
        if len(set(bases)) != len(bases):
            sys.exit("Several clusters need a `cluster_base_offset`.")
        for base in bases + ([self.alias_base] if self.alias_base is not None else []):
            if base % self.tcdm_size:
                sys.exit(f"Region base {base:#x} is not aligned to the TCDM size.")

    def windows(self, base):
        """Windows of one cluster and its alias, laid out as in `snitch_cluster.sv`."""
        windows = dict()
        for region, start in zip(self.regions, [base, self.alias_base]):
            windows[f"tcdm{region}"] = (start, start + self.tcdm_size - 1)
            start += self.tcdm_size
            windows[f"periph{region}"] = (start, start + self.periph_size - 1)
            start += self.periph_size
            windows[f"zero_mem{region}"] = (start, start + self.zero_mem_size - 1)
        return windows

    def sub_windows(self, side):
        """Names of the windows each of a side's own subordinates serves."""
        return {sub: [f"{sub}{r}" for r in self.regions] for sub in self.sides[side]["subs"]}


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


def with_soc_out(windows, sub_windows, addr_width):
    """Give `soc_out` every address the other subordinates leave, as `axi_xbar`'s default port."""
    windows, soc = dict(windows), []
    nxt = 0
    for lo, last in sorted(windows[w] for ws in sub_windows.values() for w in ws):
        if lo < nxt:
            sys.exit(f"Overlapping cluster windows at {lo:#x}.")
        if lo > nxt:
            soc.append((nxt, lo - 1))
        nxt = last + 1
    if nxt < 2 ** addr_width:
        soc.append((nxt, 2 ** addr_width - 1))
    for i, w in enumerate(soc):
        windows[f"soc_out_{i}"] = w
    return windows, dict(sub_windows, soc_out=[f"soc_out_{i}" for i in range(len(soc))])


# =============================================================================
# Crossbars
# =============================================================================


def per_id(trans):
    """Requests per ID a crossbar input admits; the lowering sets `MaxMstTrans` to this plus one."""
    return trans - 1


def ports(addr_width, data_width, ids, trans, atops):
    # The cluster has no per-endpoint counts per ID, so endpoints match the crossbar.
    return (f"addr_width = {addr_width}, data_width = {data_width}, "
            f"outstanding_write_ids = {ids}, outstanding_read_ids = {ids}, "
            f"concurrent_writes_per_id = {per_id(trans)}, "
            f"concurrent_reads_per_id = {per_id(trans)}" + (" {pulp.atops}" if atops else ""))


def pulp_config(latency, trans):
    return (f'PULP_CONFIG_LatencyMode = "axi_pkg::{latency}", '
            f"PULP_CONFIG_MaxSlvTrans = {trans} : i32, PULP_CONFIG_FallThrough = false")


def xbar_module(xbars, side, k, base):
    """One side's crossbar of the cluster at `base`, as its own network."""
    s = xbars.sides[side]
    windows, sub_windows = with_soc_out(xbars.windows(base), xbars.sub_windows(side),
                                        xbars.addr_width)
    # The handwritten crossbar connects every manager to every subordinate
    connectivity = {mgr: list(sub_windows) for mgr in s["mgrs"]}
    mgr = ports(xbars.addr_width, s["data_width"], s["mgr_ids"], s["trans"], s["atops"])
    sub = ports(xbars.addr_width, s["data_width"], s["sub_ids"], s["trans"], s["atops"])
    mgrs = "\n".join(f'  %{m}_mgr, %{m}_mgr_access = axi4.dummies.ext_manager "{m}" '
                     f"%clk, %rst_ni {mgr}" for m in s["mgrs"])
    subs = "\n".join(f'  %{t}_access = axi4.dummies.ext_subordinate "{t}" %clk, %rst_ni, %xbar\n'
                     f"    windows <{window_set(t, windows, sub_windows)}>\n    {sub}"
                     for t in sub_windows)
    return f"""// {side.capitalize()} crossbar of the cluster at {base:#x}

hw.module @snitch_cluster_{side}_xbar_{k}(in %clk : !seq.clock, in %rst_ni : i1) {{
{mgrs}

  %xbar = axi4.dummies.xbar %clk, %rst_ni mgrs {', '.join(f'%{m}_mgr' for m in s["mgrs"])}
    addr_width = {xbars.addr_width}, data_width = {s["data_width"]},
    upstream_concurrent_per_id = {per_id(s["trans"])}
    {{{pulp_config(s["latency"], s["trans"])}}}

{subs}

{accesses(connectivity, windows, sub_windows)}
}}
"""


# =============================================================================
# Lowering to SV
# =============================================================================


def lower(name, mlir, user_width, build_dir, circt_opt):
    """Lower one crossbar's MLIR to SV, one file per module."""
    build = build_dir / name
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    (build / f"{name}.mlir").write_text(mlir)
    subprocess.run([
        circt_opt, f"--lower-axi4-dummies-to-axi=user-width={user_width}",
        "--lower-axi4-to-hw=pulp-mapping=true req-resp-ports=true",
        build / f"{name}.mlir", "-o", build / f"{name}.hw.mlir"
    ], check=True)
    subprocess.run([
        circt_opt, "--test-apply-lowering-options=options=locationInfoStyle=none",
        "--lower-seq-to-sv", f"--export-split-verilog=dir-name={build / 'sv'}",
        build / f"{name}.hw.mlir", "-o", "/dev/null"
    ], check=True)
    files = (build / "sv" / "filelist.f").read_text().split()
    # Each crossbar has one width per side, so a converter means a misderived ID width
    if any("converter" in f for f in files):
        sys.exit(f"Unexpected converter in `{name}`.")
    return {f: (build / "sv" / f).read_text() for f in files}


# =============================================================================
# Selectors, instantiated by `snitch_cluster.sv` in place of `axi_xbar`
# =============================================================================


def req_resp_bits(addr_width, data_width, id_width, user_width):
    """Widths of PULP's `req_t` and `resp_t`, to check the cluster's structs against."""
    ax = id_width + addr_width + 8 + 3 + 2 + 1 + 4 + 3 + 4 + 4 + user_width
    w = data_width + data_width // 8 + 1 + user_width
    b = id_width + 2 + user_width
    r = id_width + data_width + 2 + 1 + user_width
    return (ax + 6) + w + ax + 5, b + r + 5


def selectors(xbars):
    sizes = {
        "TCDMSize": ("int unsigned", "0", xbars.tcdm_size),
        "ClusterPeriphSize": ("int unsigned", "0", xbars.periph_size // 1024),
        "ZeroMemorySize": ("int unsigned", "0", xbars.zero_mem_size // 1024),
        "AliasRegionEnable": ("bit", "1'b0", int(xbars.alias_base is not None)),
        "AliasRegionBase": (f"logic [{xbars.addr_width - 1}:0]", "'0",
                            f"{xbars.addr_width}'h{xbars.alias_region_base:x}"),
    }

    def config(s):
        return {
            "LatencyMode": ("axi_pkg::xbar_latency_e", "axi_pkg::NO_LATENCY",
                            f"axi_pkg::{s['latency']}"),
            "MaxMstTrans": ("int unsigned", "0", s["trans"]),
            "MaxSlvTrans": ("int unsigned", "0", s["trans"]),
        }

    def bits(s, ids):
        return req_resp_bits(xbars.addr_width, s["data_width"], clog2(ids),
                             s["user_width"])

    narrow, wide = xbars.sides["narrow"], xbars.sides["wide"]
    # Ports as `snitch_pkg` indexes the cluster's AXI arrays
    return [{
        "name": "snitch_cluster_narrow_xbar",
        "mgrs": {"core": "snitch_pkg::CoreReq", "soc_in": "snitch_pkg::AXISoC",
                 "ptw": "snitch_pkg::PTW"},
        "subs": {"tcdm": "snitch_pkg::TCDM", "periph": "snitch_pkg::ClusterPeripherals",
                 "soc_out": "snitch_pkg::SoC"},
        "mgr_bits": bits(narrow, narrow["mgr_ids"]),
        "sub_bits": bits(narrow, narrow["sub_ids"]),
        "checks": dict(sizes, **config(narrow)),
    }, {
        "name": "snitch_cluster_wide_xbar",
        "mgrs": dict({"dma": "snitch_pkg::SDMAMst", "soc_in": "snitch_pkg::SoCDMAIn"},
                     **{f"icache_{i}": f"snitch_pkg::ICache + {i}"
                        for i in range(xbars.nr_hives)}),
        "subs": {"tcdm": "snitch_pkg::TCDMDMA", "zero_mem": "snitch_pkg::ZeroMemory",
                 "soc_out": "snitch_pkg::SoCDMAOut"},
        "mgr_bits": bits(wide, wide["mgr_ids"]),
        "sub_bits": bits(wide, wide["sub_ids"]),
        "checks": dict(sizes, NrHives=("int unsigned", "0", xbars.nr_hives), **config(wide)),
    }]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cfg", "-c", type=pathlib.Path, required=True,
                        help="Configuration with a `cluster` section")
    parser.add_argument("--out", "-o", type=pathlib.Path, required=True,
                        help="Generated SV file")
    parser.add_argument("--build-dir", type=pathlib.Path, required=True,
                        help="Directory for the MLIR and the per-module SV")
    parser.add_argument("--circt-opt", default="circt-opt")
    args = parser.parse_args()

    cfg, cluster = load_cluster(args.cfg)
    xbars = ClusterXbars(cluster)
    bases = cluster_bases(cfg, cluster)
    xbars.check_bases(bases)

    lowered = {
        side: lower(f"snitch_cluster_{side}_xbar",
                    "\n".join(xbar_module(xbars, side, k, b) for k, b in enumerate(bases)),
                    xbars.sides[side]["user_width"], args.build_dir, args.circt_opt)
        for side in ("narrow", "wide")
    }
    # PULP wrapper names don't encode the user width, so the two runs could reuse one
    for f in lowered["narrow"].keys() & lowered["wide"].keys():
        if lowered["narrow"][f] != lowered["wide"][f]:
            sys.exit(f"Both crossbars generate a different `{f}`.")

    template = Template(filename=str(pathlib.Path(__file__).parent /
                                     "snitch_cluster_xbars.sv.tpl"))
    args.out.write_text(
        "// AUTOMATICALLY GENERATED by axi4gen.py; edit the script or configuration instead.\n\n"
        + "".join(dict(lowered["narrow"], **lowered["wide"]).values())
        + template.render(selectors=selectors(xbars), bases=bases,
                          addr_width=xbars.addr_width))


if __name__ == "__main__":
    main()
