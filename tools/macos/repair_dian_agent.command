#!/bin/zsh
set -eu

INSTALL_ROOT="$HOME/Library/Application Support/DianAgent"
PLIST_PATH="$HOME/Library/LaunchAgents/com.dianagent.agent.plist"
LABEL="com.dianagent.agent"
DOMAIN="gui/$UID"
SCRIPT_DIR="${0:A:h}"
VERSION_FILE="$INSTALL_ROOT/current-version.txt"
MANIFEST_PATH="$INSTALL_ROOT/extension-current/manifest.json"
VERIFY_HELPER="$INSTALL_ROOT/tools/macos/verify_local_api.sh"
ATOMIC_UPDATE_HELPER="$INSTALL_ROOT/tools/macos/atomic_directory_update.sh"
typeset -g REPAIR_JOB_STARTED=0
typeset -g REPAIR_CLEANUP_FAILED=0
REPAIR_PENDING="$INSTALL_ROOT/.repair-pending"
REPAIR_PENDING_TEMP=""
if [[ ! -f "$ATOMIC_UPDATE_HELPER" || -L "$ATOMIC_UPDATE_HELPER" ]]; then
  for candidate in "$SCRIPT_DIR/atomic_directory_update.sh" "$SCRIPT_DIR/tools/macos/atomic_directory_update.sh"; do
    if [[ -f "$candidate" && ! -L "$candidate" ]]; then
      ATOMIC_UPDATE_HELPER="$candidate"
      break
    fi
  done
fi

fail() {
  if typeset -f repair_cleanup >/dev/null 2>&1; then
    repair_cleanup
  fi
  if [[ "${REPAIR_CLEANUP_FAILED:-0}" == "1" || -n "${DIAN_INSTALL_LOCK_DIR:-}" || -n "${DIAN_INSTALL_RECOVERY_LEASE_DIR:-}" ]]; then
    print -u2 "Repair lock cleanup is incomplete; exiting immediately so the next repair can recover the stale receipt."
    exit 1
  fi
  print -u2 "修复失败：$1"
  print -u2 "未报告恢复成功；店铺数据没有被删除。"
  read "?按回车键关闭。"
  exit 1
}

[[ -d "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" && "${INSTALL_ROOT:A}" != "${HOME:A}" ]] || fail "The installation root is missing or unsafe."
[[ "${PLIST_PATH:A}" == "${HOME:A}/Library/LaunchAgents/com.dianagent.agent.plist" ]] || fail "The LaunchAgent path is outside the trusted user location."
[[ -f "$ATOMIC_UPDATE_HELPER" && ! -L "$ATOMIC_UPDATE_HELPER" ]] || fail "The crash-recovery helper is missing or unsafe."
source "$ATOMIC_UPDATE_HELPER"
dian_install_lock_acquire "$INSTALL_ROOT" 30 || fail "Another install or repair is already running, or the installation lock is unsafe."
repair_cleanup() {
  if [[ -n "${REPAIR_PENDING_TEMP:-}" && -f "$REPAIR_PENDING_TEMP" && ! -L "$REPAIR_PENDING_TEMP" && "${REPAIR_PENDING_TEMP:h:A}" == "${INSTALL_ROOT:A}" ]]; then
    /bin/rm -f "$REPAIR_PENDING_TEMP" >/dev/null 2>&1 || true
  fi
  if [[ -n "${DIAN_INSTALL_RECOVERY_LEASE_DIR:-}" && -z "${DIAN_INSTALL_LOCK_DIR:-}" ]]; then
    dian_install_lock_acquire "$INSTALL_ROOT" 30 >/dev/null 2>&1 || true
  fi
  if [[ -n "${DIAN_INSTALL_LOCK_DIR:-}" ]]; then
    if [[ "${REPAIR_JOB_STARTED:-0}" == "1" ]]; then
      /bin/launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
      if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        typeset -g REPAIR_CLEANUP_FAILED=1
        print -u2 "The unverified repaired Agent could not be unloaded; repair remains failed."
      else
        typeset -g REPAIR_JOB_STARTED=0
      fi
    fi
    dian_install_recovery_lease_release >/dev/null 2>&1 || true
  fi
  dian_install_lock_release >/dev/null 2>&1 || true
}
trap 'repair_cleanup' EXIT
dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL" || \
  fail "The interrupted installation could not be recovered; its journal and backups were retained."
dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH" || \
  fail "Unsafe or unremovable orphan installation staging paths were found."

if [[ ! -f "$PLIST_PATH" || -L "$PLIST_PATH" || ! -f "$VERSION_FILE" || -L "$VERSION_FILE" ||
      ! -x "$INSTALL_ROOT/tools/macos/launch_agent.sh" || -L "$INSTALL_ROOT/tools/macos/launch_agent.sh" ||
      ! -f "$MANIFEST_PATH" || -L "$MANIFEST_PATH" || ! -f "$VERIFY_HELPER" || -L "$VERIFY_HELPER" ||
      ! -f "$ATOMIC_UPDATE_HELPER" || -L "$ATOMIC_UPDATE_HELPER" ]]; then
  fail "未找到完整安装，请重新运行 install_dian_agent.command。"
fi
dian_validate_installed_tools_tree "$INSTALL_ROOT/tools/macos" || fail "The installed maintenance-tool tree is unsafe."
dian_validate_installed_extension_tree "$INSTALL_ROOT/extension-current" || fail "The installed extension tree is unsafe."

source "$VERIFY_HELPER"

VERSION="$(/usr/bin/tr -d '[:space:]' < "$VERSION_FILE")"
if ! print -r -- "$VERSION" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$'; then
  fail "安装版本标记无效，请重新运行 install_dian_agent.command。"
fi

NATIVE_AGENT="$INSTALL_ROOT/app/$VERSION/DianAgent"
SOURCE_PYTHON="$INSTALL_ROOT/app/$VERSION/venv/bin/python"
SOURCE_AGENT="$INSTALL_ROOT/app/$VERSION/bridge/http_receiver.py"
if [[ -x "$NATIVE_AGENT" ]]; then
  [[ -f "$NATIVE_AGENT" && ! -L "$NATIVE_AGENT" ]] || fail "The native Agent runtime is unsafe."
else
  [[ -f "$SOURCE_PYTHON" && ! -L "$SOURCE_PYTHON" && -f "$SOURCE_AGENT" && ! -L "$SOURCE_AGENT" ]] || fail "The source Agent runtime is unsafe."
fi
dian_validate_any_runtime_tree "$INSTALL_ROOT/app/$VERSION" || fail "The installed Agent runtime tree is unsafe."
if [[ ! -x "$NATIVE_AGENT" && ( ! -x "$SOURCE_PYTHON" || ! -f "$SOURCE_AGENT" ) ]]; then
  fail "当前版本缺少可用的 Agent 修复程序，请重新安装同版本安装包。"
fi

/usr/bin/plutil -lint "$PLIST_PATH" >/dev/null || fail "LaunchAgent 配置损坏，请重新安装。"

if [[ -e "$REPAIR_PENDING" || -L "$REPAIR_PENDING" ]]; then
  [[ -f "$REPAIR_PENDING" && ! -L "$REPAIR_PENDING" ]] || fail "The pending repair marker is unsafe."
fi
REPAIR_PENDING_TEMP="$(/usr/bin/mktemp "$INSTALL_ROOT/.repair-pending.tmp.XXXXXX")" || fail "Could not create the repair write-ahead marker."
[[ "${REPAIR_PENDING_TEMP:h:A}" == "${INSTALL_ROOT:A}" && -f "$REPAIR_PENDING_TEMP" && ! -L "$REPAIR_PENDING_TEMP" ]] || fail "The repair write-ahead marker path is unsafe."
print -r -- "schema=1 version=$VERSION" > "$REPAIR_PENDING_TEMP" || fail "Could not stage the repair write-ahead marker."
/bin/mv -f "$REPAIR_PENDING_TEMP" "$REPAIR_PENDING" || fail "Could not publish the repair write-ahead marker."
REPAIR_PENDING_TEMP=""
dian_durable_sync || fail "Could not persist the repair write-ahead marker."

# Stop the managed process before credentials can be rotated. This prevents a
# running Agent from racing the explicit repair transaction or issuing a token
# against the old installation identity.
/bin/launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  fail "The old managed Agent could not be stopped safely."
fi

TRUST_RECEIPT=""
if [[ -x "$NATIVE_AGENT" ]]; then
  TRUST_RECEIPT="$("$NATIVE_AGENT" --repair-local-api-trust "$MANIFEST_PATH" "$INSTALL_ROOT")" || \
    fail "本地 API 认证修复命令未通过，损坏配置未被静默覆盖。"
else
  TRUST_RECEIPT="$("$SOURCE_PYTHON" "$SOURCE_AGENT" --repair-local-api-trust "$MANIFEST_PATH" "$INSTALL_ROOT")" || \
    fail "本地 API 认证修复命令未通过，损坏配置未被静默覆盖。"
fi

dian_install_recovery_lease_acquire "$INSTALL_ROOT" || fail "Could not reserve the repair recovery lease."
dian_install_lock_release || fail "Could not transfer the repair lock to its recovery lease."
typeset -g REPAIR_JOB_STARTED=1
/bin/launchctl bootstrap "$DOMAIN" "$PLIST_PATH" || fail "无法重新注册登录自启动。"
/bin/launchctl kickstart -k "$DOMAIN/$LABEL" || fail "无法重新启动本地 Agent。"

dian_install_lock_acquire "$INSTALL_ROOT" 30 || fail "Could not reacquire the repair lock after Agent startup."
dian_install_recovery_lease_release || fail "Could not complete the repair lock transfer."

if ! dian_verify_local_api "$TRUST_RECEIPT" "$VERSION" 40; then
  TRUST_RECEIPT=""
  fail "服务未通过本次安装身份、扩展配对和受保护接口验收。日志位置：$INSTALL_ROOT/logs"
fi
EXPECTED_RUNTIME="$NATIVE_AGENT"
[[ -x "$EXPECTED_RUNTIME" ]] || EXPECTED_RUNTIME="$SOURCE_PYTHON"
dian_install_transaction_verify_job "$DOMAIN" "$LABEL" "$EXPECTED_RUNTIME" "$VERSION" || {
  TRUST_RECEIPT=""
  fail "LaunchAgent health came from a stale or unrelated process instead of this repaired installation."
}
typeset -g REPAIR_JOB_STARTED=0
TRUST_RECEIPT=""
/bin/rm -f "$REPAIR_PENDING" || fail "Could not clear the completed repair marker."
dian_durable_sync || fail "Could not persist completed repair cleanup."
dian_install_lock_release || fail "Could not release the verified repair lock."

print "店策 Agent 已恢复；本次安装身份、扩展配对与受保护接口均已验证。"
read "?按回车键关闭。"
