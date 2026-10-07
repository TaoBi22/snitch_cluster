source /home/bea/snitch-axi4-xbars/sc/.venv/bin/activate
export PATH=/home/bea/snitch-axi4-xbars/sc/tools:$PATH
export LLVM_BINROOT=/home/bea/tools/pulp-llvm/riscv32-pulp-llvm-centos7-0.12.0/bin
export COMMON_BENDER_FLAGS="-t snitch_cluster"
# Bender 0.32.1 pulls dependency testbenches (some with `timescale) into compile.sh; give vlogan a default.
# Command-line override replaces common.mk's VLOGAN_FLAGS, so repeat its defaults.
export VCS_MAKE_FLAGS='VLOGAN_FLAGS=-assert svaext -assert disable_cover -full64 -kdb -timescale=1ns/1ps'
# Shared machine: 24 CPUs, lowest priority (user request 2026-09-29)
CAP='taskset -c 0-23 nice -n 19'
export PATH=$HOME/tools/dtc/bin:$PATH  # needed by libfesvr configure
# VCS elaboration: 24 jobs, and accept `logic x = 0;` initializers on always_ff vars (ICPD_INIT in snitch_cc.sv),
# as Occamy does. This commit's Makefile has no VCS_FLAGS hook, but VCS uses ?=, so set it from the environment.
export VCS='vcs -j24 -ignore initializer_driver_checks'
# Only 5 shared VCSRuntime_Net licenses: run 2 sims at a time (user request 2026-09-30) and queue for a license instead of failing
# (a sim that fails to get one leaves IPC-based verify.py scripts blocked forever on their FIFO).
export SNPSLMD_QUEUE=true
RUN_JOBS=2
