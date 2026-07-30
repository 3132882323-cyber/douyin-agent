#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "==> bridge unit tests"
(
  cd "$ROOT/bridge"
  python -m unittest discover -s . -p "test_*.py"
)

echo "==> extension node tests"
(
  cd "$ROOT/extension"
  node test-content-common.js
  node test-scan-policy.js
  node test-content-qianchuan.js
  node test-content-qianchuan-executor.js
  node test-page-contracts.js
)

echo "all tests passed"
