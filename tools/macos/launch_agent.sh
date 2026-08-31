#!/bin/zsh
set -eu

INSTALL_ROOT="${DIAN_AGENT_INSTALL_ROOT:-$HOME/Library/Application Support/DianAgent}"
if [[ ! -d "$INSTALL_ROOT" || -L "$INSTALL_ROOT" ]]; then
  print -u2 "Dian Agent installation root is missing or unsafe."
  exit 1
fi
INSTALL_ROOT="${INSTALL_ROOT:A}"
VERSION_FILE="$INSTALL_ROOT/current-version.txt"
INSTALL_LOCK="$INSTALL_ROOT/.install-operation.lock"
RECOVERY_LEASE="$INSTALL_ROOT/.install-recovery.lock"
TRANSACTION_DIR="$INSTALL_ROOT/.install-transaction"
REPAIR_PENDING="$INSTALL_ROOT/.repair-pending"

if [[ ! -f "$VERSION_FILE" || -L "$VERSION_FILE" ]]; then
  print -u2 "Dian Agent version marker is missing or unsafe."
  exit 1
fi

VERSION="$(/usr/bin/tr -d '[:space:]' < "$VERSION_FILE")"
if ! print -r -- "$VERSION" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$'; then
  print -u2 "Dian Agent version marker is invalid."
  exit 1
fi

receipt_is_live() {
  local receipt="$1"
  local receipt_pid="${receipt%%:*}"
  [[ -n "$receipt" && "$receipt" == *:* && "$receipt_pid" == <-> ]] || return 1
  /bin/kill -0 "$receipt_pid" 2>/dev/null || return 1
  local receipt_tail="${receipt#*:}"
  # Legacy pid:token receipts cannot prove that the PID still belongs to the
  # process that created the lock. Treat them as untrusted instead of granting
  # a pending runtime permission to start after PID reuse.
  [[ "$receipt_tail" == *:* ]] || return 1
  local expected_fingerprint="${receipt##*:}"
  local started="$(/bin/ps -p "$receipt_pid" -o lstart= 2>/dev/null || true)"
  local fingerprint="$(print -rn -- "$started" | /usr/bin/shasum -a 256 2>/dev/null || true)"
  fingerprint="${fingerprint%% *}"
  [[ -n "$started" && "$fingerprint" == "$expected_fingerprint" ]]
}

LOCK_OWNER=""
LOCK_IS_LIVE=0
LOCK_OWNER_PID_ALIVE=0
if [[ -e "$INSTALL_LOCK" || -L "$INSTALL_LOCK" ]]; then
  [[ -d "$INSTALL_LOCK" && ! -L "$INSTALL_LOCK" ]] || {
    print -u2 "Dian Agent installation lock is unsafe."
    exit 1
  }
  [[ -f "$INSTALL_LOCK/owner" && ! -L "$INSTALL_LOCK/owner" ]] && LOCK_OWNER="$(/bin/cat "$INSTALL_LOCK/owner" 2>/dev/null || true)"
  LOCK_OWNER_PID="${LOCK_OWNER%%:*}"
  if [[ "$LOCK_OWNER_PID" == <-> ]] && /bin/kill -0 "$LOCK_OWNER_PID" 2>/dev/null; then
    LOCK_OWNER_PID_ALIVE=1
  fi
  receipt_is_live "$LOCK_OWNER" && LOCK_IS_LIVE=1
  # An ownerless lock can exist for a few milliseconds between mkdir and its
  # receipt write. A dead receipt is ignored only when no pending journal asks
  # launchd to keep the unverified runtime stopped.
  LOCK_MTIME="$(/usr/bin/stat -f %m "$INSTALL_LOCK" 2>/dev/null || print 0)"
  NOW="$(/bin/date +%s)"
  if [[ -z "$LOCK_OWNER" && "$LOCK_MTIME" == <-> && "$NOW" == <-> && $(( NOW - LOCK_MTIME )) -le 30 ]]; then
    print -u2 "Dian Agent maintenance lock is being established."
    exit 75
  fi
fi

RECOVERY_OWNER=""
RECOVERY_IS_LIVE=0
if [[ -e "$RECOVERY_LEASE" || -L "$RECOVERY_LEASE" ]]; then
  [[ -d "$RECOVERY_LEASE" && ! -L "$RECOVERY_LEASE" ]] || {
    print -u2 "Dian Agent recovery lease is unsafe."
    exit 1
  }
  [[ -f "$RECOVERY_LEASE/owner" && ! -L "$RECOVERY_LEASE/owner" ]] && RECOVERY_OWNER="$(/bin/cat "$RECOVERY_LEASE/owner" 2>/dev/null || true)"
  receipt_is_live "$RECOVERY_OWNER" && RECOVERY_IS_LIVE=1
fi

TRANSACTION_AUTHORIZED=0
if [[ -e "$TRANSACTION_DIR" || -L "$TRANSACTION_DIR" ]]; then
  [[ -d "$TRANSACTION_DIR" && ! -L "$TRANSACTION_DIR" ]] || {
    print -u2 "Dian Agent installation journal is unsafe."
    exit 1
  }
  SAFE_FIELDS=1
  for field in schema version phase; do
    [[ -f "$TRANSACTION_DIR/$field" && ! -L "$TRANSACTION_DIR/$field" ]] || SAFE_FIELDS=0
  done
  [[ "$SAFE_FIELDS" == "1" ]] || {
    print -u2 "Dian Agent installation journal is incomplete."
    exit 75
  }
  SCHEMA="$(/bin/cat "$TRANSACTION_DIR/schema" 2>/dev/null || true)"
  JOURNAL_VERSION="$(/bin/cat "$TRANSACTION_DIR/version" 2>/dev/null || true)"
  TRANSACTION_PHASE="$(/bin/cat "$TRANSACTION_DIR/phase" 2>/dev/null || true)"
  [[ "$SCHEMA" == "1" ]] || {
    print -u2 "Dian Agent installation journal is invalid."
    exit 75
  }
  if [[ "$TRANSACTION_PHASE" == "verified" && "$JOURNAL_VERSION" == "$VERSION" ]]; then
    for field in app_state tools_state extension_state pointer_state plist_state; do
      [[ -f "$TRANSACTION_DIR/$field" && ! -L "$TRANSACTION_DIR/$field" ]] || SAFE_FIELDS=0
    done
    if [[ "$SAFE_FIELDS" == "1" ]]; then
      VERIFIED_STATES="$(for field in app_state tools_state extension_state pointer_state plist_state; do /bin/cat "$TRANSACTION_DIR/$field"; done)"
      [[ "$VERIFIED_STATES" == $'committed\ncommitted\ncommitted\ncommitted\ncommitted' ]] && TRANSACTION_AUTHORIZED=1
    fi
  elif [[ "$LOCK_IS_LIVE" == "1" && ( "$TRANSACTION_PHASE" == "launching" || "$TRANSACTION_PHASE" == "healthy" ) ]]; then
    for field in launch_owner authorized_version app_state tools_state extension_state pointer_state plist_state; do
      [[ -f "$TRANSACTION_DIR/$field" && ! -L "$TRANSACTION_DIR/$field" ]] || SAFE_FIELDS=0
    done
    if [[ "$SAFE_FIELDS" == "1" ]]; then
      AUTHORIZED_OWNER="$(/bin/cat "$TRANSACTION_DIR/launch_owner" 2>/dev/null || true)"
      AUTHORIZED_VERSION="$(/bin/cat "$TRANSACTION_DIR/authorized_version" 2>/dev/null || true)"
      ACTIVE_STATES="$(for field in app_state tools_state extension_state pointer_state plist_state; do /bin/cat "$TRANSACTION_DIR/$field"; done)"
      if [[ "$AUTHORIZED_OWNER" == "$LOCK_OWNER" && "$AUTHORIZED_VERSION" == "$VERSION" &&
            "$JOURNAL_VERSION" == "$VERSION" && "$ACTIVE_STATES" == $'committed\ncommitted\ncommitted\ncommitted\ncommitted' ]]; then
        TRANSACTION_AUTHORIZED=1
      fi
    fi
  elif [[ "$RECOVERY_IS_LIVE" == "1" && "$TRANSACTION_PHASE" == "rollback_ready" ]]; then
    for field in launch_owner authorized_version old_version app_state tools_state extension_state pointer_state plist_state; do
      [[ -f "$TRANSACTION_DIR/$field" && ! -L "$TRANSACTION_DIR/$field" ]] || SAFE_FIELDS=0
    done
    if [[ "$SAFE_FIELDS" == "1" ]]; then
      AUTHORIZED_OWNER="$(/bin/cat "$TRANSACTION_DIR/launch_owner" 2>/dev/null || true)"
      AUTHORIZED_VERSION="$(/bin/cat "$TRANSACTION_DIR/authorized_version" 2>/dev/null || true)"
      OLD_VERSION="$(/bin/cat "$TRANSACTION_DIR/old_version" 2>/dev/null || true)"
      RESTORED_STATES="$(for field in app_state tools_state extension_state pointer_state plist_state; do /bin/cat "$TRANSACTION_DIR/$field"; done)"
      if [[ "$AUTHORIZED_OWNER" == "rollback-restored" && "$AUTHORIZED_VERSION" == "$VERSION" &&
            "$OLD_VERSION" == "$VERSION" && "$RESTORED_STATES" == $'restored\nrestored\nrestored\nrestored\nrestored' ]]; then
        TRANSACTION_AUTHORIZED=1
      fi
    fi
  fi
  [[ "$TRANSACTION_AUTHORIZED" == "1" ]] || {
    print -u2 "Dian Agent has a pending installation recovery."
    exit 75
  }
elif [[ "$LOCK_IS_LIVE" == "1" || "$LOCK_OWNER_PID_ALIVE" == "1" ]]; then
  print -u2 "Dian Agent maintenance is in progress."
  exit 75
fi

if [[ -e "$REPAIR_PENDING" || -L "$REPAIR_PENDING" ]]; then
  [[ -f "$REPAIR_PENDING" && ! -L "$REPAIR_PENDING" ]] || {
    print -u2 "Dian Agent repair marker is unsafe."
    exit 1
  }
  if [[ "$RECOVERY_IS_LIVE" != "1" && "$TRANSACTION_AUTHORIZED" != "1" ]]; then
    print -u2 "Dian Agent has an unverified repair pending."
    exit 75
  fi
fi

AGENT="$INSTALL_ROOT/app/$VERSION/DianAgent"
SOURCE_PYTHON="$INSTALL_ROOT/app/$VERSION/venv/bin/python"
SOURCE_ENTRY="$INSTALL_ROOT/app/$VERSION/bridge/http_receiver.py"
APP_ROOT="$INSTALL_ROOT/app"
VERSION_ROOT="$APP_ROOT/$VERSION"

if [[ ! -d "$APP_ROOT" || -L "$APP_ROOT" || ! -d "$VERSION_ROOT" || -L "$VERSION_ROOT" ]]; then
  print -u2 "Dian Agent runtime path is missing or unsafe."
  exit 1
fi

export DIAN_AGENT_INSTALL_ROOT="$INSTALL_ROOT"
export DIAN_AGENT_DATA_DIR="$INSTALL_ROOT/data"
export DIAN_AGENT_LOG_DIR="$INSTALL_ROOT/logs"
export DIAN_AGENT_RELEASE_ROOT="$INSTALL_ROOT"
export DIAN_AGENT_AUTOSTART_SOURCE="macos_launchagent"
export BRIDGE_PORT="8765"

if [[ -f "$AGENT" && -x "$AGENT" && ! -L "$AGENT" ]]; then
  exec "$AGENT"
fi
if [[ -d "$VERSION_ROOT/venv" && ! -L "$VERSION_ROOT/venv" &&
      -d "$VERSION_ROOT/venv/bin" && ! -L "$VERSION_ROOT/venv/bin" &&
      -d "$VERSION_ROOT/bridge" && ! -L "$VERSION_ROOT/bridge" &&
      -f "$SOURCE_PYTHON" && -x "$SOURCE_PYTHON" && ! -L "$SOURCE_PYTHON" &&
      -f "$SOURCE_ENTRY" && ! -L "$SOURCE_ENTRY" ]]; then
  exec "$SOURCE_PYTHON" "$SOURCE_ENTRY"
fi
print -u2 "Dian Agent runtime is missing or not executable."
exit 1
