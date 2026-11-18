#!/usr/bin/env bash
# Builds gflbench, the Metal benchmark renderer. Needs macOS with the Xcode
# command line tools (xcode-select --install). Shaders compile when the program
# starts, so the full Xcode app is not required.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(uname)" != "Darwin" ]]; then
    echo "gflbench builds on macOS only." >&2
    exit 1
fi
swiftc -O -swift-version 5 GFLBench.swift -o gflbench
echo "Built $(pwd)/gflbench"
echo "Quick check (5 s):  bench/metal/gflbench --preset low --resolution 1280x720 --duration 5 --output /tmp/gflbench_check.csv"
