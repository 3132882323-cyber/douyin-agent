#!/bin/zsh

# Crash-safe directory replacement for the macOS installers. Every prepared
# tree lives beside its target, is validated before the first active path is
# moved, and leaves one previous tree available for recovery.

typeset -g DIAN_INSTALL_LOCK_DIR=""
typeset -g DIAN_INSTALL_LOCK_OWNER=""
typeset -g DIAN_INSTALL_RECOVERY_LEASE_DIR=""
typeset -g DIAN_INSTALL_RECOVERY_LEASE_OWNER=""
typeset -g DIAN_INSTALL_RECOVERY_OUTCOME=""

dian_durable_sync() {
  emulate -L zsh
  /bin/sync
}

dian_process_fingerprint() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 && "$1" == <-> ]] || return 64
  local started digest
  started="$(/bin/ps -p "$1" -o lstart= 2>/dev/null)" || return 1
  [[ -n "$started" ]] || return 1
  digest="$(print -rn -- "$started" | /usr/bin/shasum -a 256 2>/dev/null)" || return 1
  print -r -- "${digest%% *}"
}

dian_version_is_safe() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  print -r -- "$1" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$'
}

dian_lock_receipt_is_live() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local owner="$1"
  local owner_pid="${owner%%:*}"
  [[ -n "$owner" && "$owner" == *:* && "$owner_pid" == <-> ]] || return 1
  /bin/kill -0 "$owner_pid" 2>/dev/null || return 1
  local owner_tail="${owner#*:}"
  [[ "$owner_tail" == *:* ]] || return 1
  local expected_fingerprint="${owner##*:}"
  local current_fingerprint
  current_fingerprint="$(dian_process_fingerprint "$owner_pid" 2>/dev/null)" || return 1
  [[ -n "$current_fingerprint" && "$current_fingerprint" == "$expected_fingerprint" ]]
}

dian_wait_for_recovery_lease() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local root_abs="$1"
  local deadline="$2"
  local lease_dir="$root_abs/.install-recovery.lock"
  local owner owner_pid owner_pid_alive lock_mtime now stale
  while [[ -e "$lease_dir" || -L "$lease_dir" ]]; do
    [[ -d "$lease_dir" && ! -L "$lease_dir" && "${lease_dir:h:A}" == "$root_abs" ]] || return 64
    owner=""
    if [[ -f "$lease_dir/owner" && ! -L "$lease_dir/owner" ]]; then
      owner="$(/bin/cat "$lease_dir/owner" 2>/dev/null || true)"
    fi
    owner_pid="${owner%%:*}"
    owner_pid_alive=0
    if [[ "$owner_pid" == <-> ]] && /bin/kill -0 "$owner_pid" 2>/dev/null; then
      owner_pid_alive=1
    fi
    if [[ "$lease_dir" == "$DIAN_INSTALL_RECOVERY_LEASE_DIR" &&
          -n "$DIAN_INSTALL_RECOVERY_LEASE_OWNER" &&
          "$owner" == "$DIAN_INSTALL_RECOVERY_LEASE_OWNER" ]]; then
      return 0
    fi
    if ! dian_lock_receipt_is_live "$owner"; then
      lock_mtime="$(/usr/bin/stat -f %m "$lease_dir" 2>/dev/null || print 0)"
      now="$(/bin/date +%s)"
      if [[ ( "$owner_pid" == <-> && "$owner_pid_alive" == "0" ) ||
            ( "$owner_pid" != <-> && "$lock_mtime" == <-> && "$now" == <-> && $(( now - lock_mtime )) -gt 30 ) ]]; then
        stale="$root_abs/.install-recovery.stale.$$.$RANDOM"
        if /bin/mv "$lease_dir" "$stale" 2>/dev/null; then
          /bin/rm -rf "$stale" || return 66
          dian_durable_sync || return 66
          continue
        fi
      fi
    fi
    (( SECONDS < deadline )) || return 75
    /bin/sleep 1
  done
  return 0
}

dian_tree_has_no_symlinks() {
  emulate -L zsh
  setopt localoptions nounset pipefail

  [[ "$#" -eq 1 ]] || return 64
  local tree="$1"
  [[ -d "$tree" && ! -L "$tree" ]] || return 1
  local first_link
  first_link="$(/usr/bin/find "${tree:A}" -type l -print -quit 2>/dev/null)" || return 1
  [[ -z "$first_link" ]]
}

dian_install_bootstrap_update() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local root_abs="${1:A}"
  local source_tools="${2:A}"
  local bootstrap_dir="$root_abs/bootstrap"
  [[ "$root_abs" != "/" && "$root_abs" != "${HOME:A}" &&
     "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" &&
     -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 64
  [[ -d "$source_tools" && ! -L "$source_tools" ]] || return 65
  dian_tree_has_no_symlinks "$source_tools" || return 65
  local name source_file target_file temporary
  for name in atomic_directory_update.sh recovery_bootstrap.sh; do
    source_file="$source_tools/$name"
    [[ -f "$source_file" && ! -L "$source_file" ]] || return 65
  done
  if [[ -e "$bootstrap_dir" || -L "$bootstrap_dir" ]]; then
    [[ -d "$bootstrap_dir" && ! -L "$bootstrap_dir" && "${bootstrap_dir:A}" == "$bootstrap_dir" ]] || return 64
  else
    /bin/mkdir -m 700 "$bootstrap_dir" || return 66
  fi
  /bin/chmod 700 "$bootstrap_dir" || return 66

  # Replace the policy/helper first and the tiny launcher second.  Each file
  # rename is atomic and durable; old and new bootstrap launchers are required
  # to remain compatible with transaction schema 1.
  for name in atomic_directory_update.sh recovery_bootstrap.sh; do
    target_file="$bootstrap_dir/$name"
    if [[ -e "$target_file" || -L "$target_file" ]]; then
      [[ -f "$target_file" && ! -L "$target_file" && "${target_file:A}" == "$target_file" ]] || return 64
    fi
    temporary="$(/usr/bin/mktemp "$bootstrap_dir/.${name}.tmp.XXXXXX")" || return 66
    [[ -f "$temporary" && ! -L "$temporary" && "${temporary:h:A}" == "$bootstrap_dir" ]] || return 64
    /bin/cp -p "$source_tools/$name" "$temporary" || {
      /bin/rm -f "$temporary" 2>/dev/null || true
      return 66
    }
    /bin/chmod 755 "$temporary" || {
      /bin/rm -f "$temporary" 2>/dev/null || true
      return 66
    }
    /bin/mv -f "$temporary" "$target_file" || {
      /bin/rm -f "$temporary" 2>/dev/null || true
      return 66
    }
    dian_durable_sync || return 66
  done
  dian_tree_has_no_symlinks "$bootstrap_dir" || return 65
  [[ -x "$bootstrap_dir/atomic_directory_update.sh" &&
     -x "$bootstrap_dir/recovery_bootstrap.sh" ]] || return 65
}

dian_install_prepare_stable_plist_migration() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 3 ]] || return 64
  local root_abs="${1:A}"
  local plist_path="$2"
  local staged_plist="$3"
  local expected_plist="${HOME:A}/Library/LaunchAgents/com.dianagent.agent.plist"
  local bootstrap_dir="$root_abs/bootstrap"
  local snapshot="$bootstrap_dir/launchagent.pre-transaction.plist"
  [[ "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" &&
     -n "$DIAN_INSTALL_LOCK_OWNER" && "${plist_path:A}" == "$expected_plist" ]] || return 64
  [[ -d "$bootstrap_dir" && ! -L "$bootstrap_dir" &&
     -f "$plist_path" && ! -L "$plist_path" &&
     -f "$staged_plist" && ! -L "$staged_plist" &&
     "${staged_plist:h:A}" == "${plist_path:h:A}" ]] || return 64
  /usr/bin/plutil -lint "$plist_path" >/dev/null || return 65
  /usr/bin/plutil -lint "$staged_plist" >/dev/null || return 65
  if [[ -e "$snapshot" || -L "$snapshot" ]]; then
    [[ -f "$snapshot" && ! -L "$snapshot" && "${snapshot:A}" == "$snapshot" ]] || return 64
    /usr/bin/plutil -lint "$snapshot" >/dev/null || return 65
  else
    local snapshot_temp
    snapshot_temp="$(/usr/bin/mktemp "$bootstrap_dir/.launchagent.pre-transaction.plist.tmp.XXXXXX")" || return 66
    [[ -f "$snapshot_temp" && ! -L "$snapshot_temp" && "${snapshot_temp:h:A}" == "$bootstrap_dir" ]] || return 64
    /bin/cp -p "$plist_path" "$snapshot_temp" || {
      /bin/rm -f "$snapshot_temp" 2>/dev/null || true
      return 66
    }
    /bin/mv -f "$snapshot_temp" "$snapshot" || {
      /bin/rm -f "$snapshot_temp" 2>/dev/null || true
      return 66
    }
    dian_durable_sync || return 66
  fi
  /bin/mv -f "$staged_plist" "$plist_path" || return 66
  dian_durable_sync || return 66
}

dian_install_restore_stable_plist_migration() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local root_abs="${1:A}"
  local plist_path="$2"
  local snapshot="$root_abs/bootstrap/launchagent.pre-transaction.plist"
  local expected_plist="${HOME:A}/Library/LaunchAgents/com.dianagent.agent.plist"
  [[ "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" &&
     -n "$DIAN_INSTALL_LOCK_OWNER" && "${plist_path:A}" == "$expected_plist" ]] || return 64
  [[ ! -e "$snapshot" && ! -L "$snapshot" ]] && return 0
  [[ -f "$snapshot" && ! -L "$snapshot" ]] || return 64
  /usr/bin/plutil -lint "$snapshot" >/dev/null || return 65
  local restore
  restore="$(/usr/bin/mktemp "${plist_path:h:A}/.com.dianagent.agent.plist.restore.XXXXXX")" || return 66
  [[ -f "$restore" && ! -L "$restore" && "${restore:h:A}" == "${plist_path:h:A}" ]] || return 64
  /bin/cp -p "$snapshot" "$restore" || return 66
  /bin/mv -f "$restore" "$plist_path" || return 66
  dian_durable_sync || return 66
  /bin/rm -f "$snapshot" || return 66
  dian_durable_sync || return 66
}

dian_install_lock_acquire() {
  emulate -L zsh
  setopt localoptions nounset pipefail

  [[ "$#" -eq 2 ]] || return 64
  local install_root="$1"
  local timeout_seconds="$2"
  [[ "$timeout_seconds" == <-> && "$timeout_seconds" -ge 1 && "$timeout_seconds" -le 300 ]] || return 64
  /bin/mkdir -p "$install_root" || return 66
  [[ -d "$install_root" && ! -L "$install_root" ]] || return 64
  /bin/chmod 700 "$install_root" || return 66
  local root_abs="${install_root:A}"
  [[ "$root_abs" != "/" && "$root_abs" != "${HOME:A}" ]] || return 64
  local lock_dir="$root_abs/.install-operation.lock"
  [[ "${lock_dir:h:A}" == "$root_abs" && ! -L "$lock_dir" ]] || return 64
  local deadline=$(( SECONDS + timeout_seconds ))
  local owner token owner_pid owner_fingerprint owner_live owner_pid_alive lock_mtime now stale
  token="$$-${RANDOM}-${RANDOM}"

  while true; do
    dian_wait_for_recovery_lease "$root_abs" "$deadline" || return
    if /bin/mkdir -m 700 "$lock_dir" 2>/dev/null; then
      break
    fi
    [[ -d "$lock_dir" && ! -L "$lock_dir" ]] || return 64
    owner=""
    if [[ -f "$lock_dir/owner" && ! -L "$lock_dir/owner" ]]; then
      owner="$(/bin/cat "$lock_dir/owner" 2>/dev/null || true)"
    fi
    owner_pid="${owner%%:*}"
    owner_live=0
    owner_pid_alive=0
    if [[ "$owner_pid" == <-> ]] && /bin/kill -0 "$owner_pid" 2>/dev/null; then
      owner_pid_alive=1
    fi
    dian_lock_receipt_is_live "$owner" && owner_live=1
    if [[ "$owner_pid" == <-> && "$owner_live" == "0" && "$owner_pid_alive" == "0" ]]; then
      stale="$root_abs/.install-operation.stale.$$.$RANDOM"
      if /bin/mv "$lock_dir" "$stale" 2>/dev/null; then
        /bin/rm -rf "$stale" || return 66
        continue
      fi
    elif [[ "$owner_pid" != <-> ]]; then
      lock_mtime="$(/usr/bin/stat -f %m "$lock_dir" 2>/dev/null || print 0)"
      now="$(/bin/date +%s)"
      if [[ "$lock_mtime" == <-> && "$now" == <-> && $(( now - lock_mtime )) -gt 30 ]]; then
        stale="$root_abs/.install-operation.stale.$$.$RANDOM"
        if /bin/mv "$lock_dir" "$stale" 2>/dev/null; then
          /bin/rm -rf "$stale" || return 66
          continue
        fi
      fi
    fi
    (( SECONDS < deadline )) || return 75
    /bin/sleep 1
  done

  owner_fingerprint="$(dian_process_fingerprint "$$")" || {
    /bin/rmdir "$lock_dir" 2>/dev/null || true
    return 66
  }
  print -r -- "$$:$token:$owner_fingerprint" > "$lock_dir/owner" || {
    /bin/rmdir "$lock_dir" 2>/dev/null || true
    return 66
  }
  dian_durable_sync || {
    /bin/rm -rf "$lock_dir" 2>/dev/null || true
    return 66
  }
  typeset -g DIAN_INSTALL_LOCK_DIR="$lock_dir"
  typeset -g DIAN_INSTALL_LOCK_OWNER="$$:$token:$owner_fingerprint"
  return 0
}

dian_install_recovery_lease_acquire() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local root_abs="${1:A}"
  local lease_dir="$root_abs/.install-recovery.lock"
  [[ "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" && -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 75
  [[ "${lease_dir:h:A}" == "$root_abs" && ! -e "$lease_dir" && ! -L "$lease_dir" ]] || return 75
  /bin/mkdir -m 700 "$lease_dir" || return 66
  local fingerprint token owner
  fingerprint="$(dian_process_fingerprint "$$")" || {
    /bin/rmdir "$lease_dir" 2>/dev/null || true
    return 66
  }
  token="recovery-$$-${RANDOM}-${RANDOM}"
  owner="$$:$token:$fingerprint"
  print -r -- "$owner" > "$lease_dir/owner" || {
    /bin/rm -rf "$lease_dir" 2>/dev/null || true
    return 66
  }
  dian_durable_sync || {
    /bin/rm -rf "$lease_dir" 2>/dev/null || true
    return 66
  }
  typeset -g DIAN_INSTALL_RECOVERY_LEASE_DIR="$lease_dir"
  typeset -g DIAN_INSTALL_RECOVERY_LEASE_OWNER="$owner"
  return 0
}

dian_install_recovery_lease_release() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ -n "$DIAN_INSTALL_RECOVERY_LEASE_DIR" && -n "$DIAN_INSTALL_RECOVERY_LEASE_OWNER" ]] || return 0
  local lease_dir="$DIAN_INSTALL_RECOVERY_LEASE_DIR"
  local owner=""
  if [[ -d "$lease_dir" && ! -L "$lease_dir" && -f "$lease_dir/owner" && ! -L "$lease_dir/owner" ]]; then
    owner="$(/bin/cat "$lease_dir/owner" 2>/dev/null || true)"
    if [[ "$owner" == "$DIAN_INSTALL_RECOVERY_LEASE_OWNER" ]]; then
      /bin/rm -rf "$lease_dir" || return 66
      dian_durable_sync || return 66
    fi
  fi
  typeset -g DIAN_INSTALL_RECOVERY_LEASE_DIR=""
  typeset -g DIAN_INSTALL_RECOVERY_LEASE_OWNER=""
  return 0
}

dian_install_lock_release() {
  emulate -L zsh
  setopt localoptions nounset pipefail

  [[ -n "$DIAN_INSTALL_LOCK_DIR" && -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 0
  local lock_dir="$DIAN_INSTALL_LOCK_DIR"
  local owner=""
  if [[ -d "$lock_dir" && ! -L "$lock_dir" && -f "$lock_dir/owner" && ! -L "$lock_dir/owner" ]]; then
    owner="$(/bin/cat "$lock_dir/owner" 2>/dev/null || true)"
    if [[ "$owner" == "$DIAN_INSTALL_LOCK_OWNER" ]]; then
      /bin/rm -rf "$lock_dir" || return 66
    fi
  fi
  typeset -g DIAN_INSTALL_LOCK_DIR=""
  typeset -g DIAN_INSTALL_LOCK_OWNER=""
  return 0
}

dian_atomic_directory_commit() {
  emulate -L zsh
  setopt localoptions nounset pipefail

  if [[ "$#" -ne 4 ]]; then
    print -u2 "atomic directory commit requires install root, stage, target and validator"
    return 64
  fi
  local install_root="$1"
  local prepared="$2"
  local target="$3"
  local validator="$4"
  local root_abs="${install_root:A}"
  local home_abs="${HOME:A}"
  local target_parent="${target:h:A}"
  local target_name="${target:t}"
  local expected_target="$target_parent/$target_name"
  local resolved_target="${target:A}"
  local resolved_prepared="${prepared:A}"

  [[ -n "$target_name" && "$target_name" != "." && "$target_name" != ".." ]] || return 64
  [[ "$root_abs" != "/" && "$root_abs" != "$home_abs" ]] || return 64
  [[ "$target_parent" == "$root_abs" || "$target_parent" == "$root_abs/"* ]] || return 64
  [[ "$resolved_target" == "$expected_target" ]] || return 64
  [[ "${prepared:h:A}" == "$target_parent" && "$resolved_prepared" != "$expected_target" ]] || return 64
  [[ -d "$resolved_prepared" && ! -L "$prepared" ]] || return 65
  dian_tree_has_no_symlinks "$resolved_prepared" || return 65
  typeset -f "$validator" >/dev/null 2>&1 || return 64
  "$validator" "$resolved_prepared" || return 65

  local previous="$target_parent/.${target_name}.previous"
  local failed="$target_parent/.${target_name}.transaction-rollback"
  local scratch
  for scratch in "$previous" "$failed"; do
    [[ "${scratch:h:A}" == "$target_parent" ]] || return 64
    if [[ -e "$scratch" || -L "$scratch" ]]; then
      [[ "${scratch:A}" == "$scratch" && ! -L "$scratch" ]] || return 64
    fi
  done
  [[ ! -e "$failed" && ! -L "$failed" ]] || return 75

  # Cross-tree transaction setup owns orphan .previous normalization. Atomic
  # commit must never silently change the baseline captured by its journal.
  if [[ ! -e "$target" && ! -L "$target" && ( -e "$previous" || -L "$previous" ) ]]; then
    return 75
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    [[ "${target:A}" == "$expected_target" && ! -L "$target" ]] || return 64
    dian_tree_has_no_symlinks "$target" || return 65
    if [[ -e "$previous" || -L "$previous" ]]; then
      /bin/rm -rf "$previous" || return 66
    fi
    /bin/mv "$target" "$previous" || return 66
  fi

  if ! /bin/mv "$resolved_prepared" "$target"; then
    if [[ ! -e "$target" && -d "$previous" ]]; then
      /bin/mv "$previous" "$target" || true
    fi
    if [[ -d "$resolved_prepared" && ! -e "$failed" && ! -L "$failed" ]]; then
      /bin/mv "$resolved_prepared" "$failed" 2>/dev/null || true
    fi
    return 66
  fi
  if ! dian_tree_has_no_symlinks "$target" || ! "$validator" "$target"; then
    /bin/mv "$target" "$failed" || true
    if [[ -d "$previous" ]]; then
      /bin/mv "$previous" "$target" || true
    fi
    return 65
  fi
  return 0
}

dian_atomic_directory_update() {
  emulate -L zsh
  setopt localoptions nounset pipefail

  if [[ "$#" -ne 4 ]]; then
    print -u2 "atomic directory update requires install root, source, target and validator"
    return 64
  fi
  local install_root="$1"
  local source="$2"
  local target="$3"
  local validator="$4"
  local root_abs="${install_root:A}"
  local target_parent="${target:h:A}"
  local target_name="${target:t}"
  local stage="$target_parent/.${target_name}.stage.$$"

  [[ -d "$source" && ! -L "$source" ]] || return 65
  dian_tree_has_no_symlinks "$source" || return 65
  [[ "$root_abs" != "/" && "$root_abs" != "${HOME:A}" ]] || return 64
  [[ "$target_parent" == "$root_abs" || "$target_parent" == "$root_abs/"* ]] || return 64
  [[ "${stage:h:A}" == "$target_parent" ]] || return 64
  if [[ -e "$stage" || -L "$stage" ]]; then
    [[ "${stage:A}" == "$stage" && ! -L "$stage" ]] || return 64
    /bin/rm -rf "$stage" || return 66
  fi
  /bin/mkdir -m 700 "$stage" || return 66
  if ! /usr/bin/rsync -a --delete "${source:A}/" "$stage/"; then
    /bin/rm -rf "$stage"
    return 66
  fi
  if ! "$validator" "$stage"; then
    /bin/rm -rf "$stage"
    return 65
  fi
  if ! dian_tree_has_no_symlinks "$stage"; then
    /bin/rm -rf "$stage"
    return 65
  fi
  if ! dian_atomic_directory_commit "$install_root" "$stage" "$target" "$validator"; then
    if [[ -e "$stage" && "${stage:A}" == "$stage" && ! -L "$stage" ]]; then
      /bin/rm -rf "$stage"
    fi
    return 66
  fi
  return 0
}

dian_transaction_field_path() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local transaction_dir="$1"
  local field="$2"
  case "$field" in
    schema|version|phase|app_state|tools_state|extension_state|pointer_state|plist_state|app_had_target|tools_had_target|extension_had_target|pointer_had_file|plist_had_file|old_job_loaded|old_job_running|old_version|launch_owner|authorized_version) ;;
    *) return 64 ;;
  esac
  [[ -d "$transaction_dir" && ! -L "$transaction_dir" ]] || return 64
  local field_path="$transaction_dir/$field"
  [[ "${field_path:h:A}" == "${transaction_dir:A}" ]] || return 64
  if [[ -e "$field_path" || -L "$field_path" ]]; then
    [[ -f "$field_path" && ! -L "$field_path" ]] || return 64
  fi
  print -r -- "$field_path"
}

dian_transaction_write_field() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 3 ]] || return 64
  local transaction_dir="$1"
  local field_path
  field_path="$(dian_transaction_field_path "$transaction_dir" "$2")" || return 64
  local temporary
  temporary="$(/usr/bin/mktemp "$transaction_dir/.${2}.tmp.XXXXXX")" || return 66
  [[ "${temporary:h:A}" == "${transaction_dir:A}" && -f "$temporary" && ! -L "$temporary" ]] || return 64
  print -r -- "$3" > "$temporary" || {
    /bin/rm -f "$temporary" 2>/dev/null || true
    return 66
  }
  /bin/mv -f "$temporary" "$field_path" || {
    /bin/rm -f "$temporary" 2>/dev/null || true
    return 66
  }
  dian_durable_sync || return 66
}

dian_transaction_read_field() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local field_path
  field_path="$(dian_transaction_field_path "$1" "$2")" || return 64
  [[ -f "$field_path" && ! -L "$field_path" ]] || return 65
  /bin/cat "$field_path"
}

dian_validate_any_runtime_tree() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local tree="$1"
  dian_tree_has_no_symlinks "$tree" || return 1
  [[ -x "$tree/DianAgent" || ( -x "$tree/venv/bin/python" && -f "$tree/bridge/http_receiver.py" ) ]]
}

dian_validate_installed_tools_tree() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local tree="$1"
  dian_tree_has_no_symlinks "$tree" || return 1
  [[ -f "$tree/launch_agent.sh" && -x "$tree/launch_agent.sh" &&
     -f "$tree/verify_local_api.sh" && -x "$tree/verify_local_api.sh" &&
     -f "$tree/atomic_directory_update.sh" && -x "$tree/atomic_directory_update.sh" &&
     -f "$tree/repair_dian_agent.command" && -x "$tree/repair_dian_agent.command" ]]
}

dian_validate_installed_extension_tree() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local tree="$1"
  dian_tree_has_no_symlinks "$tree" || return 1
  [[ -f "$tree/manifest.json" && ! -L "$tree/manifest.json" ]]
}

dian_install_cleanup_orphan_stages() {
  emulate -L zsh
  setopt localoptions nounset pipefail nullglob
  [[ "$#" -eq 2 ]] || return 64
  local root_abs="${1:A}"
  local plist_path="$2"
  local expected_plist_parent="${HOME:A}/Library/LaunchAgents"
  local plist_parent="${plist_path:h:A}"
  [[ "$root_abs" != "/" && "$root_abs" != "${HOME:A}" &&
     "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" &&
     -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 64
  [[ "$plist_parent" == "$expected_plist_parent" &&
     "${plist_path:t}" == "com.dianagent.agent.plist" ]] || return 64

  local container
  for container in "$root_abs/app" "$root_abs/tools" "$root_abs/bootstrap"; do
    if [[ -e "$container" || -L "$container" ]]; then
      [[ -d "$container" && ! -L "$container" ]] || return 64
    fi
  done
  if [[ -e "$expected_plist_parent" || -L "$expected_plist_parent" ]]; then
    [[ -d "$expected_plist_parent" && ! -L "$expected_plist_parent" ]] || return 64
  fi

  local -a directory_candidates file_candidates
  directory_candidates=(
    "$root_abs"/.install-transaction.stage.*(N)
    "$root_abs"/.install-operation.stale.*(N)
    "$root_abs"/.install-recovery.stale.*(N)
    "$root_abs"/.extension-current.stage.*(N)
    "$root_abs"/app/.stage-*(N)
    "$root_abs"/tools/.macos.stage.*(N)
  )
  file_candidates=(
    "$root_abs"/.current-version.tmp.*(N)
    "$root_abs"/.current-version.restore.*(N)
    "$root_abs"/.repair-pending.tmp.*(N)
    "$root_abs"/bootstrap/.atomic_directory_update.sh.tmp.*(N)
    "$root_abs"/bootstrap/.recovery_bootstrap.sh.tmp.*(N)
    "$root_abs"/bootstrap/.launchagent.pre-transaction.plist.tmp.*(N)
  )
  if [[ -d "$expected_plist_parent" && ! -L "$expected_plist_parent" ]]; then
    file_candidates+=(
      "$expected_plist_parent"/.com.dianagent.agent.plist.tmp.*(N)
      "$expected_plist_parent"/.com.dianagent.agent.plist.restore.*(N)
    )
  fi

  # Preflight every candidate before deleting the first one. A symlink, special
  # file or escaped parent is evidence to retain and report, never a cleanup
  # instruction to follow.
  local candidate parent
  for candidate in "${directory_candidates[@]}"; do
    parent="${candidate:h:A}"
    [[ "$parent" == "$root_abs" || "$parent" == "$root_abs/app" ||
       "$parent" == "$root_abs/tools" ]] || return 64
    [[ -d "$candidate" && ! -L "$candidate" && "${candidate:A}" == "$candidate" ]] || return 64
  done
  for candidate in "${file_candidates[@]}"; do
    parent="${candidate:h:A}"
    [[ "$parent" == "$root_abs" || "$parent" == "$root_abs/bootstrap" ||
       "$parent" == "$expected_plist_parent" ]] || return 64
    [[ -f "$candidate" && ! -L "$candidate" && "${candidate:A}" == "$candidate" ]] || return 64
  done

  local removed=0
  for candidate in "${directory_candidates[@]}"; do
    /bin/rm -rf "$candidate" || return 66
    removed=1
  done
  for candidate in "${file_candidates[@]}"; do
    /bin/rm -f "$candidate" || return 66
    removed=1
  done
  [[ "$removed" == "0" ]] || dian_durable_sync
}

dian_install_transaction_cleanup_tombstone() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local root_abs="${1:A}"
  local tombstone="$root_abs/.install-transaction.finished"
  [[ "${tombstone:h:A}" == "$root_abs" ]] || return 64
  if [[ -e "$tombstone" || -L "$tombstone" ]]; then
    [[ -d "$tombstone" && ! -L "$tombstone" ]] || return 64
    /bin/rm -rf "$tombstone" || return 66
    dian_durable_sync || return 66
  fi
}

dian_prepare_transaction_baseline() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 3 ]] || return 64
  local root_abs="${1:A}"
  local target="$2"
  local validator="$3"
  local parent="${target:h:A}"
  local expected="$parent/${target:t}"
  local previous="$parent/.${target:t}.previous"
  local failed="$parent/.${target:t}.transaction-rollback"
  [[ "$parent" == "$root_abs" || "$parent" == "$root_abs/"* ]] || return 64
  [[ "${target:A}" == "$expected" && "${previous:h:A}" == "$parent" && "${failed:h:A}" == "$parent" ]] || return 64
  [[ ! -e "$failed" && ! -L "$failed" ]] || return 75
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -d "$target" && ! -L "$target" ]] || return 64
    dian_tree_has_no_symlinks "$target" || return 65
    "$validator" "$target" || return 65
  fi
  if [[ -e "$previous" || -L "$previous" ]]; then
    [[ -d "$previous" && ! -L "$previous" ]] || return 64
    dian_tree_has_no_symlinks "$previous" || return 65
    "$validator" "$previous" || return 65
    if [[ ! -e "$target" && ! -L "$target" ]]; then
      /bin/mv "$previous" "$target" || return 66
      dian_durable_sync || return 66
    else
      # Two structurally valid trees without a journal are ambiguous: this can
      # be either an old successful install or a crash before health proof.
      # Preserve both byte-for-byte and require explicit operator recovery.
      return 75
    fi
  fi
}

dian_install_transaction_discard_journal() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local root_abs="${1:A}"
  local transaction_dir="$root_abs/.install-transaction"
  local tombstone="$root_abs/.install-transaction.finished"
  [[ -d "$transaction_dir" && ! -L "$transaction_dir" && "${tombstone:h:A}" == "$root_abs" ]] || return 64
  dian_install_transaction_cleanup_tombstone "$root_abs" || return
  /bin/mv "$transaction_dir" "$tombstone" || return 66
  dian_durable_sync || return 66
  /bin/rm -rf "$tombstone" || return 66
  dian_durable_sync || return 66
}

dian_install_transaction_begin() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 5 ]] || return 64
  local install_root="$1"
  local version="$2"
  local plist_path="$3"
  local domain="$4"
  local label="$5"
  local root_abs="${install_root:A}"
  local transaction_dir="$root_abs/.install-transaction"
  local stage="$root_abs/.install-transaction.stage.$$.$RANDOM"
  local expected_plist="${HOME:A}/Library/LaunchAgents/com.dianagent.agent.plist"
    dian_version_is_safe "$version" || return 64
  [[ "$root_abs" != "/" && "$root_abs" != "${HOME:A}" && "${plist_path:A}" == "$expected_plist" ]] || return 64
  [[ "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" && -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 75
  dian_install_transaction_cleanup_tombstone "$root_abs" || return
  [[ ! -e "$transaction_dir" && ! -L "$transaction_dir" && ! -e "$stage" && ! -L "$stage" ]] || return 75
  /bin/mkdir -m 700 "$stage" || return 66

  local app_target="$root_abs/app/$version"
  local tools_target="$root_abs/tools/macos"
  local extension_target="$root_abs/extension-current"
  local pointer_path="$root_abs/current-version.txt"
  local bootstrap_plist_snapshot="$root_abs/bootstrap/launchagent.pre-transaction.plist"
  local app_had=0 tools_had=0 extension_had=0 pointer_had=0 plist_had=0
  local initial_plist_state=prepared
  local old_loaded=0 old_running=0 old_version=""
  local baseline_status=0
  dian_prepare_transaction_baseline "$root_abs" "$app_target" dian_validate_any_runtime_tree || {
    baseline_status=$?
    /bin/rm -rf "$stage"
    return "$baseline_status"
  }
  dian_prepare_transaction_baseline "$root_abs" "$tools_target" dian_validate_installed_tools_tree || {
    baseline_status=$?
    /bin/rm -rf "$stage"
    return "$baseline_status"
  }
  dian_prepare_transaction_baseline "$root_abs" "$extension_target" dian_validate_installed_extension_tree || {
    baseline_status=$?
    /bin/rm -rf "$stage"
    return "$baseline_status"
  }
  local target
  for target in "$app_target" "$tools_target" "$extension_target"; do
    [[ ! -L "$target" ]] || { /bin/rm -rf "$stage"; return 64; }
    if [[ -e "$target" ]]; then
      [[ -d "$target" ]] || { /bin/rm -rf "$stage"; return 64; }
    fi
    local rollback_artifact="${target:h:A}/.${target:t}.transaction-rollback"
    [[ ! -e "$rollback_artifact" && ! -L "$rollback_artifact" ]] || { /bin/rm -rf "$stage"; return 75; }
  done
  [[ -d "$app_target" ]] && app_had=1
  [[ -d "$tools_target" ]] && tools_had=1
  [[ -d "$extension_target" ]] && extension_had=1
  [[ "$app_had" == "0" ]] || dian_validate_any_runtime_tree "$app_target" || { /bin/rm -rf "$stage"; return 65; }
  [[ "$tools_had" == "0" ]] || dian_validate_installed_tools_tree "$tools_target" || { /bin/rm -rf "$stage"; return 65; }
  [[ "$extension_had" == "0" ]] || dian_validate_installed_extension_tree "$extension_target" || { /bin/rm -rf "$stage"; return 65; }
  if [[ -e "$pointer_path" || -L "$pointer_path" ]]; then
    [[ -f "$pointer_path" && ! -L "$pointer_path" ]] || { /bin/rm -rf "$stage"; return 64; }
    pointer_had=1
    old_version="$(/usr/bin/tr -d '[:space:]' < "$pointer_path")"
    dian_version_is_safe "$old_version" || { /bin/rm -rf "$stage"; return 65; }
    dian_validate_any_runtime_tree "$root_abs/app/$old_version" || { /bin/rm -rf "$stage"; return 65; }
    /bin/cp -p "$pointer_path" "$stage/current-version.previous" || { /bin/rm -rf "$stage"; return 66; }
  fi
  if [[ -e "$plist_path" || -L "$plist_path" ]]; then
    [[ -f "$plist_path" && ! -L "$plist_path" ]] || { /bin/rm -rf "$stage"; return 64; }
    plist_had=1
    local plist_baseline="$plist_path"
    if [[ -e "$bootstrap_plist_snapshot" || -L "$bootstrap_plist_snapshot" ]]; then
      [[ -f "$bootstrap_plist_snapshot" && ! -L "$bootstrap_plist_snapshot" ]] || { /bin/rm -rf "$stage"; return 64; }
      /usr/bin/plutil -lint "$bootstrap_plist_snapshot" >/dev/null || { /bin/rm -rf "$stage"; return 65; }
      plist_baseline="$bootstrap_plist_snapshot"
      # The active plist was atomically migrated to the stable bootstrap before
      # this journal was published.  Record that truthful state in the initial
      # journal so a crash before the next shell statement can still roll back.
      initial_plist_state=committed
    fi
    /bin/cp -p "$plist_baseline" "$stage/launchagent.previous.plist" || { /bin/rm -rf "$stage"; return 66; }
  elif [[ -e "$bootstrap_plist_snapshot" || -L "$bootstrap_plist_snapshot" ]]; then
    /bin/rm -rf "$stage"
    return 65
  fi
  local job_receipt=""
  if job_receipt="$(/bin/launchctl print "$domain/$label" 2>/dev/null)"; then
    old_loaded=1
    if print -r -- "$job_receipt" | /usr/bin/grep -Eq 'state = running|pid = [0-9]+'; then
      old_running=1
    fi
  fi
  if [[ "$old_loaded" == "1" && ( "$plist_had" != "1" || "$pointer_had" != "1" ) ]]; then
    /bin/rm -rf "$stage"
    return 65
  fi

  local -a metadata=(
    schema 1 version "$version" phase prepared
    app_state prepared tools_state prepared extension_state prepared pointer_state prepared plist_state "$initial_plist_state"
    app_had_target "$app_had" tools_had_target "$tools_had" extension_had_target "$extension_had"
    pointer_had_file "$pointer_had" plist_had_file "$plist_had"
    old_job_loaded "$old_loaded" old_job_running "$old_running" old_version "$old_version"
  )
  local field value index=1
  while (( index <= ${#metadata} )); do
    field="${metadata[$index]}"
    value="${metadata[$(( index + 1 ))]}"
    print -r -- "$value" > "$stage/$field" || { /bin/rm -rf "$stage"; return 66; }
    (( index += 2 ))
  done
  dian_durable_sync || { /bin/rm -rf "$stage"; return 66; }
  /bin/mv "$stage" "$transaction_dir" || { /bin/rm -rf "$stage"; return 66; }
  dian_durable_sync || return 66
  return 0
}

dian_install_transaction_mark() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 3 ]] || return 64
  local transaction_dir="${1:A}/.install-transaction"
  case "$2" in app|tools|extension|pointer|plist) ;; *) return 64 ;; esac
  case "$3" in prepared|committing|committed|restored) ;; *) return 64 ;; esac
  dian_transaction_write_field "$transaction_dir" "${2}_state" "$3"
}

dian_install_transaction_phase() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local transaction_dir="${1:A}/.install-transaction"
  case "$2" in prepared|switching|launching|healthy|rolling_back|rollback_ready|verified) ;; *) return 64 ;; esac
  dian_transaction_write_field "$transaction_dir" phase "$2"
}

dian_install_transaction_authorize_launch() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 2 ]] || return 64
  local root_abs="${1:A}"
  local version="$2"
  local transaction_dir="$root_abs/.install-transaction"
  dian_version_is_safe "$version" || return 64
  [[ "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" && -n "$DIAN_INSTALL_LOCK_OWNER" ]] || return 75
  dian_transaction_write_field "$transaction_dir" launch_owner "$DIAN_INSTALL_LOCK_OWNER" || return
  dian_transaction_write_field "$transaction_dir" authorized_version "$version"
}

dian_transaction_preflight_directory() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 4 ]] || return 64
  local target="$1" had_target="$2" state="$3" validator="$4"
  [[ "$had_target" == "0" || "$had_target" == "1" ]] || return 64
  case "$state" in prepared|committing|committed|restored) ;; *) return 64 ;; esac
  typeset -f "$validator" >/dev/null 2>&1 || return 64
  local parent="${target:h:A}"
  local name="${target:t}"
  local expected="$parent/$name"
  local previous="$parent/.${name}.previous"
  local failed="$parent/.${name}.transaction-rollback"
  [[ "${target:A}" == "$expected" && "${previous:h:A}" == "$parent" && "${failed:h:A}" == "$parent" ]] || return 64
  local candidate
  for candidate in "$target" "$previous" "$failed"; do
    if [[ -e "$candidate" || -L "$candidate" ]]; then
      [[ -d "$candidate" && ! -L "$candidate" ]] || return 64
      [[ "$candidate" == "$failed" ]] || dian_tree_has_no_symlinks "$candidate" || return 65
    fi
  done
  if [[ "$state" == "prepared" ]]; then
    if [[ "$had_target" == "1" ]]; then
      [[ -d "$target" ]] && "$validator" "$target"
      return
    fi
    [[ ! -e "$target" && ! -L "$target" ]]
    return
  fi
  if [[ "$state" == "restored" ]]; then
    if [[ "$had_target" == "1" ]]; then
      [[ -d "$target" ]] && "$validator" "$target"
      return
    fi
    [[ ! -e "$target" && ! -L "$target" ]]
    return
  fi
  if [[ "$had_target" == "1" ]]; then
    if [[ -d "$previous" ]]; then
      "$validator" "$previous"
      return
    fi
    # A crash can occur after the previous tree was restored but before its
    # durable `restored` mark. The deterministic rollback tree proves that
    # this target is the restored old tree, not an unverified pending tree.
    [[ -d "$failed" && -d "$target" ]] || return 65
    "$validator" "$target"
    return
  fi
  [[ ! -e "$previous" && ! -L "$previous" ]] || return 65
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -d "$target" ]] && "$validator" "$target"
    return
  fi
  return 0
}

dian_transaction_restore_directory() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 4 ]] || return 64
  local target="$1"
  local had_target="$2"
  local state="$3"
  local validator="$4"
  [[ "$had_target" == "0" || "$had_target" == "1" ]] || return 64
  case "$state" in prepared|committing|committed|restored) ;; *) return 64 ;; esac
  dian_transaction_preflight_directory "$target" "$had_target" "$state" "$validator" || return
  [[ "$state" != "prepared" && "$state" != "restored" ]] || return 0
  local parent="${target:h:A}"
  local name="${target:t}"
  local expected="$parent/$name"
  local previous="$parent/.${name}.previous"
  local failed="$parent/.${name}.transaction-rollback"
  [[ "${target:A}" == "$expected" && "${failed:h:A}" == "$parent" ]] || return 64
  if [[ "$had_target" == "1" ]]; then
    if [[ ! -e "$previous" && ! -L "$previous" ]]; then
      [[ -d "$failed" && -d "$target" && ! -L "$target" ]] || return 65
      "$validator" "$target" || return 65
      return 0
    fi
    [[ -d "$previous" && ! -L "$previous" ]] || return 64
    dian_tree_has_no_symlinks "$previous" || return 65
    "$validator" "$previous" || return 65
  elif [[ -e "$previous" || -L "$previous" ]]; then
    return 65
  fi
  if [[ -e "$failed" || -L "$failed" ]]; then
    [[ -d "$failed" && ! -L "$failed" && "${failed:A}" == "$failed" ]] || return 64
    [[ ! -e "$target" && ! -L "$target" ]] || return 65
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -d "$target" && ! -L "$target" ]] || return 64
    dian_tree_has_no_symlinks "$target" || return 65
    /bin/mv "$target" "$failed" || return 66
  fi
  if [[ "$had_target" == "1" ]]; then
    if ! /bin/mv "$previous" "$target"; then
      [[ -d "$failed" && ! -e "$target" ]] && /bin/mv "$failed" "$target" 2>/dev/null || true
      return 66
    fi
    if ! "$validator" "$target"; then
      /bin/mv "$target" "$previous" 2>/dev/null || true
      [[ -d "$failed" ]] && /bin/mv "$failed" "$target" 2>/dev/null || true
      return 65
    fi
  fi
  return 0
}

dian_transaction_cleanup_rollback_directory() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local target="$1"
  local parent="${target:h:A}"
  local failed="$parent/.${target:t}.transaction-rollback"
  [[ "${failed:h:A}" == "$parent" ]] || return 64
  if [[ -e "$failed" || -L "$failed" ]]; then
    [[ -d "$failed" && ! -L "$failed" ]] || return 64
    /bin/rm -rf "$failed" || return 66
    dian_durable_sync || return 66
  fi
}

dian_install_transaction_finalize() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  local root_abs="${1:A}"
  local transaction_dir="$root_abs/.install-transaction"
  [[ -d "$transaction_dir" && ! -L "$transaction_dir" ]] || return 64
  local version phase
  version="$(dian_transaction_read_field "$transaction_dir" version)" || return
  phase="$(dian_transaction_read_field "$transaction_dir" phase)" || return
  dian_version_is_safe "$version" || return 65
  [[ "$phase" == "verified" ]] || return 65
  local backup
  for backup in "$root_abs/.extension-current.previous" "$root_abs/tools/.macos.previous" "$root_abs/app/.${version}.previous"; do
    [[ "${backup:h:A}" == "$root_abs" || "${backup:h:A}" == "$root_abs/tools" || "${backup:h:A}" == "$root_abs/app" ]] || return 64
    if [[ -e "$backup" || -L "$backup" ]]; then
      [[ -d "$backup" && ! -L "$backup" ]] || return 64
      /bin/rm -rf "$backup" || return 66
    fi
  done
  local bootstrap_plist_snapshot="$root_abs/bootstrap/launchagent.pre-transaction.plist"
  if [[ -e "$bootstrap_plist_snapshot" || -L "$bootstrap_plist_snapshot" ]]; then
    [[ -f "$bootstrap_plist_snapshot" && ! -L "$bootstrap_plist_snapshot" ]] || return 64
    /bin/rm -f "$bootstrap_plist_snapshot" || return 66
  fi
  dian_durable_sync || return 66
  dian_install_transaction_discard_journal "$root_abs"
}

dian_install_transaction_commit() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 1 ]] || return 64
  dian_install_transaction_phase "$1" verified || return
  dian_install_transaction_finalize "$1"
}

dian_install_transaction_recover() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 4 ]] || return 64
  local install_root="$1"
  local plist_path="$2"
  local domain="$3"
  local label="$4"
  local root_abs="${install_root:A}"
  typeset -g DIAN_INSTALL_RECOVERY_OUTCOME=""
  local transaction_dir="$root_abs/.install-transaction"
  local expected_plist="${HOME:A}/Library/LaunchAgents/com.dianagent.agent.plist"
  [[ "${plist_path:A}" == "$expected_plist" && "$DIAN_INSTALL_LOCK_DIR" == "$root_abs/.install-operation.lock" ]] || return 64
  dian_install_transaction_cleanup_tombstone "$root_abs" || return
  if [[ ! -e "$transaction_dir" && ! -L "$transaction_dir" ]]; then
    typeset -g DIAN_INSTALL_RECOVERY_OUTCOME="none"
    return 0
  fi
  [[ -d "$transaction_dir" && ! -L "$transaction_dir" ]] || return 64
  local schema phase version
  schema="$(dian_transaction_read_field "$transaction_dir" schema)" || return
  phase="$(dian_transaction_read_field "$transaction_dir" phase)" || return
  version="$(dian_transaction_read_field "$transaction_dir" version)" || return
  [[ "$schema" == "1" ]] && dian_version_is_safe "$version" || return 65
  case "$phase" in prepared|switching|launching|healthy|rolling_back|rollback_ready|verified) ;; *) return 65 ;; esac
  local plist_had pointer_had app_had tools_had extension_had
  local plist_state pointer_state app_state tools_state extension_state old_loaded old_running old_version job_replaced=0
  plist_had="$(dian_transaction_read_field "$transaction_dir" plist_had_file)" || return
  pointer_had="$(dian_transaction_read_field "$transaction_dir" pointer_had_file)" || return
  app_had="$(dian_transaction_read_field "$transaction_dir" app_had_target)" || return
  tools_had="$(dian_transaction_read_field "$transaction_dir" tools_had_target)" || return
  extension_had="$(dian_transaction_read_field "$transaction_dir" extension_had_target)" || return
  plist_state="$(dian_transaction_read_field "$transaction_dir" plist_state)" || return
  pointer_state="$(dian_transaction_read_field "$transaction_dir" pointer_state)" || return
  app_state="$(dian_transaction_read_field "$transaction_dir" app_state)" || return
  tools_state="$(dian_transaction_read_field "$transaction_dir" tools_state)" || return
  extension_state="$(dian_transaction_read_field "$transaction_dir" extension_state)" || return
  old_loaded="$(dian_transaction_read_field "$transaction_dir" old_job_loaded)" || return
  old_running="$(dian_transaction_read_field "$transaction_dir" old_job_running)" || return
  old_version="$(dian_transaction_read_field "$transaction_dir" old_version)" || return

  # Validate the complete write-ahead record and every rollback source before
  # changing the phase, unloading a job, or writing any active path. A damaged
  # journal is evidence to retain, never an instruction to delete live state.
  local flag state
  for flag in "$plist_had" "$pointer_had" "$app_had" "$tools_had" "$extension_had" "$old_loaded" "$old_running"; do
    [[ "$flag" == "0" || "$flag" == "1" ]] || return 65
  done
  for state in "$plist_state" "$pointer_state" "$app_state" "$tools_state" "$extension_state"; do
    case "$state" in prepared|committing|committed|restored) ;; *) return 65 ;; esac
  done
  [[ "$old_running" == "0" || "$old_loaded" == "1" ]] || return 65
  local pointer_path="$root_abs/current-version.txt"
  local pointer_snapshot="$transaction_dir/current-version.previous"
  local plist_snapshot="$transaction_dir/launchagent.previous.plist"
  local bootstrap_plist_snapshot="$root_abs/bootstrap/launchagent.pre-transaction.plist"
  if [[ "$pointer_had" == "1" ]]; then
    dian_version_is_safe "$old_version" || return 65
    [[ -f "$pointer_snapshot" && ! -L "$pointer_snapshot" ]] || return 65
    [[ "$(/usr/bin/tr -d '[:space:]' < "$pointer_snapshot")" == "$old_version" ]] || return 65
  else
    [[ -z "$old_version" && ! -e "$pointer_snapshot" && ! -L "$pointer_snapshot" ]] || return 65
  fi
  if [[ "$plist_had" == "1" ]]; then
    [[ -f "$plist_snapshot" && ! -L "$plist_snapshot" ]] || return 65
    /usr/bin/plutil -lint "$plist_snapshot" >/dev/null || return 65
    if [[ -e "$bootstrap_plist_snapshot" || -L "$bootstrap_plist_snapshot" ]]; then
      [[ -f "$bootstrap_plist_snapshot" && ! -L "$bootstrap_plist_snapshot" ]] || return 65
      /usr/bin/cmp -s "$bootstrap_plist_snapshot" "$plist_snapshot" || return 65
    fi
  else
    [[ ! -e "$plist_snapshot" && ! -L "$plist_snapshot" ]] || return 65
  fi
  if [[ "$old_loaded" == "1" ]]; then
    [[ "$pointer_had" == "1" && "$plist_had" == "1" ]] || return 65
    dian_validate_any_runtime_tree "$root_abs/app/$old_version" || {
      # Same-version upgrades keep the old runtime under .previous until the
      # component rollback below, so accept that exact validated source too.
      [[ "$old_version" == "$version" ]] || return 65
      dian_validate_any_runtime_tree "$root_abs/app/.${version}.previous" || return 65
    }
  fi
  local active_path
  for active_path in "$pointer_path" "$plist_path"; do
    if [[ -e "$active_path" || -L "$active_path" ]]; then
      [[ -f "$active_path" && ! -L "$active_path" ]] || return 64
    fi
  done
  if [[ "$pointer_state" == "prepared" || "$pointer_state" == "restored" ]]; then
    if [[ "$pointer_had" == "1" ]]; then
      [[ -f "$pointer_path" ]] && /usr/bin/cmp -s "$pointer_snapshot" "$pointer_path" || return 65
    else
      [[ ! -e "$pointer_path" && ! -L "$pointer_path" ]] || return 65
    fi
  fi
  if [[ "$plist_state" == "prepared" || "$plist_state" == "restored" ]]; then
    if [[ "$plist_had" == "1" ]]; then
      [[ -f "$plist_path" ]] && /usr/bin/cmp -s "$plist_snapshot" "$plist_path" || return 65
    else
      [[ ! -e "$plist_path" && ! -L "$plist_path" ]] || return 65
    fi
  fi
  local launch_owner="" authorized_version=""
  if [[ "$phase" == "launching" || "$phase" == "healthy" || "$phase" == "verified" ]]; then
    launch_owner="$(dian_transaction_read_field "$transaction_dir" launch_owner)" || return
    authorized_version="$(dian_transaction_read_field "$transaction_dir" authorized_version)" || return
    [[ -n "$launch_owner" && "$authorized_version" == "$version" ]] || return 65
  elif [[ "$phase" == "rollback_ready" ]]; then
    launch_owner="$(dian_transaction_read_field "$transaction_dir" launch_owner)" || return
    authorized_version="$(dian_transaction_read_field "$transaction_dir" authorized_version)" || return
    [[ "$launch_owner" == "rollback-restored" && "$authorized_version" == "$old_version" ]] || return 65
  fi

  if [[ "$phase" == "verified" ]]; then
    [[ "$plist_state" == "committed" && "$pointer_state" == "committed" &&
       "$app_state" == "committed" && "$tools_state" == "committed" && "$extension_state" == "committed" ]] || return 65
    [[ -f "$pointer_path" && "$(/usr/bin/tr -d '[:space:]' < "$pointer_path")" == "$version" ]] || return 65
    [[ -f "$plist_path" ]] && /usr/bin/plutil -lint "$plist_path" >/dev/null || return 65
    dian_validate_installed_extension_tree "$root_abs/extension-current" || return 65
    dian_validate_installed_tools_tree "$root_abs/tools/macos" || return 65
    dian_validate_any_runtime_tree "$root_abs/app/$version" || return 65
    dian_install_transaction_finalize "$root_abs" || return
    typeset -g DIAN_INSTALL_RECOVERY_OUTCOME="finalized_new"
    return
  fi
  dian_transaction_preflight_directory "$root_abs/extension-current" "$extension_had" "$extension_state" dian_validate_installed_extension_tree || return
  dian_transaction_preflight_directory "$root_abs/tools/macos" "$tools_had" "$tools_state" dian_validate_installed_tools_tree || return
  dian_transaction_preflight_directory "$root_abs/app/$version" "$app_had" "$app_state" dian_validate_any_runtime_tree || return
  if [[ "$phase" == "launching" || "$phase" == "healthy" || "$phase" == "rolling_back" || "$phase" == "rollback_ready" ]]; then
    job_replaced=1
  fi
  dian_install_transaction_phase "$root_abs" rolling_back || return
  if [[ "$job_replaced" == "1" ]]; then
    /bin/launchctl bootout "$domain" "$plist_path" >/dev/null 2>&1 || true
    if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then
      return 65
    fi
  fi

  if [[ "$plist_state" != "prepared" && "$plist_state" != "restored" ]]; then
    if [[ "$plist_had" == "1" ]]; then
      local plist_restore
      plist_restore="$(/usr/bin/mktemp "${plist_path:h:A}/.com.dianagent.agent.plist.restore.XXXXXX")" || return 66
      [[ "${plist_restore:h:A}" == "${plist_path:h:A}" && -f "$plist_restore" && ! -L "$plist_restore" ]] || return 64
      /bin/cp -p "$plist_snapshot" "$plist_restore" || return 66
      /bin/mv -f "$plist_restore" "$plist_path" || return 66
    else
      [[ ! -L "$plist_path" ]] || return 64
      [[ ! -e "$plist_path" ]] || /bin/rm -f "$plist_path" || return 66
    fi
  fi
  dian_durable_sync || return 66
  dian_install_transaction_mark "$root_abs" plist restored || return
  if [[ "$pointer_state" != "prepared" && "$pointer_state" != "restored" ]]; then
    if [[ "$pointer_had" == "1" ]]; then
      local pointer_restore
      pointer_restore="$(/usr/bin/mktemp "$root_abs/.current-version.restore.XXXXXX")" || return 66
      [[ "${pointer_restore:h:A}" == "$root_abs" && -f "$pointer_restore" && ! -L "$pointer_restore" ]] || return 64
      /bin/cp -p "$pointer_snapshot" "$pointer_restore" || return 66
      /bin/mv -f "$pointer_restore" "$pointer_path" || return 66
    else
      [[ ! -L "$pointer_path" ]] || return 64
      [[ ! -e "$pointer_path" ]] || /bin/rm -f "$pointer_path" || return 66
    fi
  fi
  dian_durable_sync || return 66
  dian_install_transaction_mark "$root_abs" pointer restored || return
  dian_transaction_restore_directory "$root_abs/extension-current" "$extension_had" "$extension_state" dian_validate_installed_extension_tree || return
  dian_install_transaction_mark "$root_abs" extension restored || return
  dian_transaction_cleanup_rollback_directory "$root_abs/extension-current" || return
  dian_transaction_restore_directory "$root_abs/tools/macos" "$tools_had" "$tools_state" dian_validate_installed_tools_tree || return
  dian_install_transaction_mark "$root_abs" tools restored || return
  dian_transaction_cleanup_rollback_directory "$root_abs/tools/macos" || return
  dian_transaction_restore_directory "$root_abs/app/$version" "$app_had" "$app_state" dian_validate_any_runtime_tree || return
  dian_install_transaction_mark "$root_abs" app restored || return
  dian_transaction_cleanup_rollback_directory "$root_abs/app/$version" || return

  if [[ -e "$bootstrap_plist_snapshot" || -L "$bootstrap_plist_snapshot" ]]; then
    [[ -f "$bootstrap_plist_snapshot" && ! -L "$bootstrap_plist_snapshot" ]] || return 64
    /bin/rm -f "$bootstrap_plist_snapshot" || return 66
    dian_durable_sync || return 66
  fi

  if [[ "$job_replaced" == "1" && "$old_loaded" == "1" ]]; then
    [[ "$plist_had" == "1" && -f "$plist_path" && ! -L "$plist_path" ]] || return 65
    dian_version_is_safe "$old_version" || return 65
    dian_transaction_write_field "$transaction_dir" launch_owner rollback-restored || return
    dian_transaction_write_field "$transaction_dir" authorized_version "$old_version" || return
    dian_install_transaction_phase "$root_abs" rollback_ready || return
    # The previous launcher may predate transaction-aware authorization.  The
    # filesystem and pointer are already fully restored. A separate recovery
    # lease blocks every normal installer while the primary lock is briefly
    # transferred so that both old and transaction-aware launchers can start.
    dian_install_recovery_lease_acquire "$root_abs" || return
    dian_install_lock_release || return 66
    local restore_launch_status=0
    /bin/launchctl bootstrap "$domain" "$plist_path" || restore_launch_status=66
    if [[ "$restore_launch_status" == "0" && "$old_running" == "1" ]]; then
      /bin/launchctl kickstart -k "$domain/$label" || restore_launch_status=66
    fi
    dian_install_lock_acquire "$root_abs" 30 || return 75
    dian_install_recovery_lease_release || return 66
    [[ "$restore_launch_status" == "0" ]] || return "$restore_launch_status"
    if [[ "$old_running" == "1" ]]; then
      local old_runtime="$root_abs/app/$old_version/DianAgent"
      if [[ ! -x "$old_runtime" ]]; then
        old_runtime="$root_abs/app/$old_version/venv/bin/python"
      fi
      dian_install_transaction_verify_job "$domain" "$label" "$old_runtime" "$old_version" || return 65
    else
      /bin/launchctl print "$domain/$label" >/dev/null 2>&1 || return 65
    fi
  elif [[ "$job_replaced" == "1" && "$old_loaded" == "0" ]]; then
    if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then return 65; fi
  fi
  dian_durable_sync || return 66
  dian_install_transaction_discard_journal "$root_abs" || return
  typeset -g DIAN_INSTALL_RECOVERY_OUTCOME="rolled_back_old"
  return 0
}

dian_install_transaction_verify_job() {
  emulate -L zsh
  setopt localoptions nounset pipefail
  [[ "$#" -eq 4 ]] || return 64
  local domain="$1" label="$2" expected_runtime="$3" expected_version="$4"
  dian_version_is_safe "$expected_version" || return 64
  [[ "$expected_runtime" == /* && -x "$expected_runtime" && ! -L "$expected_runtime" ]] || return 64
  local job pid command_line attempt=0
  while (( attempt < 20 )); do
    job="$(/bin/launchctl print "$domain/$label" 2>/dev/null || true)"
    if print -r -- "$job" | /usr/bin/grep -q 'state = running'; then
      pid="$(print -r -- "$job" | /usr/bin/sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*$/\1/p' | /usr/bin/head -n 1)"
      if [[ "$pid" == <-> ]] && /bin/kill -0 "$pid" 2>/dev/null; then
        command_line="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
        if [[ "$command_line" == "$expected_runtime" || "$command_line" == "$expected_runtime "* ]]; then
          return 0
        fi
      fi
    fi
    (( attempt += 1 ))
    /bin/sleep 1
  done
  return 1
}
