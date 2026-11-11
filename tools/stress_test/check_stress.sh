#!/bin/sh
# DD2 Project 1: does this ngspice keep BSIM4 mobility stress?
# Runs stress_test.cir: two identical NMOS that differ only in SA and SB, with
# mobility stress and no other stress effect in the model card. A stock ngspice
# (42 to 47) gives the same drain current twice; the fixed build gives about 3.5% more
# current on the device with 0.26 um stress distances.
# Exit status 0 means the fixed build; 1 means a stock build; 2 means the test could not run.
NGSPICE=${NGSPICE:-ngspice}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$("$NGSPICE" -b "$HERE/stress_test.cir" 2>&1) || { echo "stress test: ngspice failed to run"; echo "$OUT"; exit 2; }
RATIO=$(echo "$OUT" | awk '/^RATIO/ {print $2}')
[ -n "$RATIO" ] || { echo "stress test: no result"; echo "$OUT"; exit 2; }
BIN=$(command -v "$NGSPICE")
VER=$("$NGSPICE" -v 2>&1 | grep -m1 -o 'ngspice-[0-9.]*')
SHA=$(sha256sum "$BIN" | cut -d' ' -f1)
echo "ngspice: $VER  $BIN  sha256 $SHA"
echo "current ratio, stressed / unstressed: $RATIO"
if awk -v r="$RATIO" 'BEGIN { d = r - 1; if (d < 0) d = -d; exit !(d > 0.01) }'; then
  echo "PASS: mobility stress is applied (fixed build)"; exit 0
else
  echo "FAIL: mobility stress is discarded (stock build); use the course image"; exit 1
fi
