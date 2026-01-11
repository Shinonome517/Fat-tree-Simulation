#!/usr/bin/env bash
set -Eeuo pipefail

run() {
  local name="$1"; shift
  local log="$1"; shift

  echo "==== $(date '+%F %T') START ${name} ====" | tee -a "$log"
  "$@" >>"$log" 2>&1 </dev/null
  local rc=$?
  echo "==== $(date '+%F %T') END   ${name} rc=${rc} ====" | tee -a "$log"
  return "$rc"
}

run analyze1 analyze1.log python3 analysis/analyze_whitebox.py --latest-n 20 --log-dir 0111-lambda80--4-64 --output-dir 0111-lambda80--4-64 --verbose
run analyze2 analyze2.log python3 analysis/analyze_whitebox.py --latest-n 20 --log-dir 0111-lambda320--1-16 --output-dir 0111-lambda320--1-16 --verbose
run analyze3 analyze3.log python3 analysis/analyze_whitebox.py --latest-n 20 --log-dir 0111-lambda1088--1-4 --output-dir 0111-lambda1088--1-4 --verbose