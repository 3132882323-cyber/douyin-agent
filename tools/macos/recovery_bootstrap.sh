#!/bin/zsh
set -eu

# This launcher intentionally lives outside the transactionally replaced
# tools/macos tree.  It can restore an interrupted prepared/switching update
# after a power loss even when the active tools path was between two renames.
INSTALL_ROOT="${DIAN_AGENT_INSTALL_ROOT:-$HOME/Library/Application Support/DianAgent}"
EXPECTED_ROOT="$HOME/Library/Application Support/DianAgent"
[[ "$INSTALL_ROOT" == "$EXPECTED_ROOT" && -d "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" ]] || {
  print -u2 "Dian Agent recovery bootstrap has an unsafe installation root."
  exit 1
}
INSTALL_ROOT="${INSTALL_ROOT:A}"
HELPER="$INSTALL_ROOT/bootstrap/atomic_directory_update.sh"
LAUNCHER="$INSTALL_ROOT/tools/macos/launch_agent.sh"
PLIST_PATH="$HOME/Library/LaunchAgents/com.dianagent.agent.plist"
DOMAIN="gui/$UID"
LABEL="com.dianagent.agent"
[[ -f "$HELPER" && -x "$HELPER" && ! -L "$HELPER" ]] || {
  print -u2 "Dian Agent stable recovery helper is missing or unsafe."
  exit 75
}
source "$HELPER"

dian_bootstrap_receipt_is_live() {
  local directory="$1"
  [[ -d "$directory" && ! -L "$directory" &&
     -f "$directory/owner" && ! -L "$directory/owner" ]] || return 1
  local owner="$(/bin/cat "$directory/owner" 2>/dev/null || true)"
  dian_lock_receipt_is_live "$owner"
}

# Install and repair deliberately start the pending runtime while holding a
# signed primary lock or recovery lease.  The transaction-aware launcher is
# the only component allowed to interpret that authorization.
if dian_bootstrap_receipt_is_live "$INSTALL_ROOT/.install-operation.lock" ||
   dian_bootstrap_receipt_is_live "$INSTALL_ROOT/.install-recovery.lock"; then
  [[ -f "$LAUNCHER" && -x "$LAUNCHER" && ! -L "$LAUNCHER" ]] || exit 75
  exec "$LAUNCHER"
fi

dian_install_lock_acquire "$INSTALL_ROOT" 30 || exit 75
trap 'dian_install_lock_release >/dev/null 2>&1 || true' EXIT

TRANSACTION_DIR="$INSTALL_ROOT/.install-transaction"
if [[ -d "$TRANSACTION_DIR" && ! -L "$TRANSACTION_DIR" ]]; then
  PHASE="$(dian_transaction_read_field "$TRANSACTION_DIR" phase 2>/dev/null || true)"
  case "$PHASE" in
    prepared|switching|verified) ;;
    # Recovery in these phases may need to unload/reload this very launchd job.
    # Keep the journal intact for the interactive installer/repair command
    # rather than pretending an in-job rollback can prove process identity.
    launching|healthy|rolling_back|rollback_ready) exit 75 ;;
    *) exit 75 ;;
  esac
fi

dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL" || exit 75
dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH" || exit 75
dian_install_lock_release || exit 75
trap - EXIT

if [[ ! -f "$INSTALL_ROOT/current-version.txt" || -L "$INSTALL_ROOT/current-version.txt" ]]; then
  # A fresh install may lose power after publishing the LaunchAgent but before
  # committing its first runtime pointer.  The filesystem is already rolled
  # back; unload this now-orphaned job so KeepAlive cannot spin forever.
  /bin/launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  exit 75
fi
[[ -f "$LAUNCHER" && -x "$LAUNCHER" && ! -L "$LAUNCHER" ]] || exit 75
exec "$LAUNCHER"
