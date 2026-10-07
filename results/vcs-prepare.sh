#!/usr/bin/env bash
# Generate, filter and run the VCS analysis script (replaces the work-vcs/compile.sh rule),
# as in Occamy's vcs-prepare.sh: default vlogan timescale + drop dependency testbenches.
set -euo pipefail
cd "$(dirname "$0")/target/snitch_cluster"
source ../../env.sh
rm -rf work-vcs && mkdir -p work-vcs
bender script vcs -t snitch_cluster -t rtl -t snitch_cluster_sim -t test -t simulation -t vcs \
  --vlog-arg="-assert svaext -assert disable_cover -full64 -kdb -timescale=1ns/1ps" \
  --vcom-arg="-full64 -kdb" > work-vcs/compile.sh
../../vcs-filter-compile.py work-vcs/compile.sh 2> work-vcs/filter.log
tail -1 work-vcs/filter.log
chmod +x work-vcs/compile.sh
$CAP work-vcs/compile.sh > work-vcs/compile.log 2>&1
