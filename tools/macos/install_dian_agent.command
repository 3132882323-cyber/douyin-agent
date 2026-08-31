#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
if [[ -f "$SCRIPT_DIR/extension/manifest.json" ]]; then
  SOURCE_ROOT="$SCRIPT_DIR"
else
  SOURCE_ROOT="${SCRIPT_DIR:h:h}"
fi
INSTALL_ROOT="$HOME/Library/Application Support/DianAgent"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENT_DIR/com.dianagent.agent.plist"
PLIST_TEMP=""
VERSION_TEMP=""
REPAIR_PENDING="$INSTALL_ROOT/.repair-pending"
LABEL="com.dianagent.agent"
DOMAIN="gui/$UID"
VERIFY_HELPER="$SOURCE_ROOT/tools/macos/verify_local_api.sh"
ATOMIC_UPDATE_HELPER="$SOURCE_ROOT/tools/macos/atomic_directory_update.sh"
typeset -g DIAN_INSTALL_TRANSACTION_ACTIVE=0

dian_installer_cleanup() {
  local exit_status="$1"
  if [[ -n "${VERSION_TEMP:-}" && -f "$VERSION_TEMP" && ! -L "$VERSION_TEMP" && "${VERSION_TEMP:h:A}" == "${INSTALL_ROOT:A}" ]]; then
    /bin/rm -f "$VERSION_TEMP" >/dev/null 2>&1 || true
  fi
  if [[ -n "${PLIST_TEMP:-}" && -f "$PLIST_TEMP" && ! -L "$PLIST_TEMP" && "${PLIST_TEMP:h:A}" == "${LAUNCH_AGENT_DIR:A}" ]]; then
    /bin/rm -f "$PLIST_TEMP" >/dev/null 2>&1 || true
  fi
  if [[ "$exit_status" != "0" && "$DIAN_INSTALL_TRANSACTION_ACTIVE" == "1" ]] && \
      typeset -f dian_install_transaction_recover >/dev/null 2>&1; then
    if dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL"; then
      typeset -g DIAN_INSTALL_TRANSACTION_ACTIVE=0
      if [[ "${DIAN_INSTALL_RECOVERY_OUTCOME:-}" == "finalized_new" ]]; then
        print -u2 "The health-verified new Dian Agent remained active and its interrupted cleanup was completed."
      else
        print -u2 "The previous Dian Agent installation was restored."
      fi
    else
      print -u2 "Automatic rollback was incomplete; the persistent transaction journal and backups were retained for Repair Dian Agent."
    fi
  fi
  if [[ "$exit_status" != "0" && "$DIAN_INSTALL_TRANSACTION_ACTIVE" == "0" &&
        ! -e "$INSTALL_ROOT/.install-transaction" && ! -L "$INSTALL_ROOT/.install-transaction" ]] &&
      typeset -f dian_install_restore_stable_plist_migration >/dev/null 2>&1; then
    dian_install_restore_stable_plist_migration "$INSTALL_ROOT" "$PLIST_PATH" >/dev/null 2>&1 || true
  fi
  dian_install_lock_release >/dev/null 2>&1 || true
}

dian_install_fault() {
  [[ "${DIAN_AGENT_MACOS_INSTALL_FAULT_POINT:-}" != "$1" ]]
}

fail() {
  print -u2 "\n安装失败：$1"
  print -u2 "没有修改店铺数据。请保留此窗口并联系发包人。"
  exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "此安装包只能在 macOS 运行。"
[[ -f "$SOURCE_ROOT/extension/manifest.json" && ! -L "$SOURCE_ROOT/extension/manifest.json" ]] || fail "安装包缺少浏览器扩展。"
[[ -f "$SOURCE_ROOT/app/DianAgent" && ! -L "$SOURCE_ROOT/app/DianAgent" ]] || fail "安装包缺少本地 Agent。"
[[ -f "$VERIFY_HELPER" && ! -L "$VERIFY_HELPER" ]] || fail "安装包缺少本地 API 验收器。"
[[ -f "$ATOMIC_UPDATE_HELPER" && ! -L "$ATOMIC_UPDATE_HELPER" ]] || fail "Atomic upgrade helper is missing or unsafe."
source "$VERIFY_HELPER"
source "$ATOMIC_UPDATE_HELPER"
dian_tree_has_no_symlinks "$SOURCE_ROOT/app" || fail "The packaged Agent tree contains an unsafe symbolic link."
dian_tree_has_no_symlinks "$SOURCE_ROOT/extension" || fail "The packaged extension tree contains an unsafe symbolic link."
dian_tree_has_no_symlinks "$SOURCE_ROOT/tools/macos" || fail "The packaged maintenance-tool tree contains an unsafe symbolic link."

VERSION="$(/usr/bin/plutil -extract version raw -o - "$SOURCE_ROOT/extension/manifest.json" 2>/dev/null || true)"
print -r -- "$VERSION" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$' || fail "版本号格式不正确。"

MACHINE="$(uname -m)"
if [[ "$(/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true)" == "1" ]]; then
  MACHINE="arm64"
fi
[[ "$MACHINE" == "arm64" ]] || fail "当前安全版本仅支持 Apple Silicon（arm64）Mac，不支持 Intel Mac。"
[[ -f "$SOURCE_ROOT/macos-architecture.txt" ]] || fail "安装包缺少架构标记。"
PACKAGE_ARCH="$(/usr/bin/tr -d '[:space:]' < "$SOURCE_ROOT/macos-architecture.txt")"
[[ "$PACKAGE_ARCH" == "arm64" ]] || fail "此安装包不是当前受支持的 Apple Silicon（arm64）版本。"
if [[ "$PACKAGE_ARCH" != "$MACHINE" ]]; then
  fail "安装包架构是 $PACKAGE_ARCH，这台 Mac 是 $MACHINE。请下载对应版本。"
fi

AGENT_ARCHS="$(/usr/bin/file "$SOURCE_ROOT/app/DianAgent")"
[[ "$AGENT_ARCHS" == *"$MACHINE"* ]] || fail "Agent 二进制与当前 Mac 架构不匹配。"

dian_install_lock_acquire "$INSTALL_ROOT" 30 || fail "Another install or repair is already running, or the installation lock is unsafe."
trap 'dian_installer_cleanup $?' EXIT
dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL" || \
  fail "An interrupted installation could not be recovered safely; its journal was retained."
dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH" || \
  fail "Unsafe or unremovable orphan installation staging paths were found."
dian_install_bootstrap_update "$INSTALL_ROOT" "$SOURCE_ROOT/tools/macos" || \
  fail "The stable recovery bootstrap could not be installed safely."
if [[ -e "$REPAIR_PENDING" || -L "$REPAIR_PENDING" ]]; then
  [[ -f "$REPAIR_PENDING" && ! -L "$REPAIR_PENDING" ]] || fail "The pending repair marker is unsafe."
fi

# Establish the fixed extension trust and 256-bit installation secret before
# mutating any active program, extension, tool or version pointer. Existing
# valid credentials and administrator-approved IDs are preserved; damaged
# state stops the install while the current version remains untouched.
TRUST_RECEIPT="$("$SOURCE_ROOT/app/DianAgent" --initialize-local-api-trust \
  "$SOURCE_ROOT/extension/manifest.json" "$INSTALL_ROOT")" || \
  fail "本地 API 信任初始化失败，已有配置可能损坏。"
RECEIPT_VERSION="$(dian_json_string_field "$TRUST_RECEIPT" "agent_version")"
[[ "$RECEIPT_VERSION" == "$VERSION" ]] || {
  TRUST_RECEIPT=""
  fail "The packaged Agent version does not match the extension/package version."
}
RECEIPT_VERSION=""

APP_ROOT="$INSTALL_ROOT/app"
APP_VERSION_DIR="$APP_ROOT/$VERSION"
APP_STAGE="$APP_ROOT/.stage-$VERSION-$$"
/bin/mkdir -p "$APP_ROOT" "$INSTALL_ROOT/tools" "$INSTALL_ROOT/data" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/config" "$INSTALL_ROOT/backup" "$LAUNCH_AGENT_DIR"
/bin/chmod 700 "$INSTALL_ROOT" "$APP_ROOT" "$INSTALL_ROOT/tools" "$INSTALL_ROOT/data" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/config" "$INSTALL_ROOT/backup" || fail "Could not secure the installation directories."
[[ "${APP_STAGE:h:A}" == "${APP_ROOT:A}" && "${APP_STAGE:A}" != "${APP_VERSION_DIR:A}" ]] || fail "Unsafe Agent upgrade staging path."
if [[ -e "$APP_STAGE" || -L "$APP_STAGE" ]]; then
  [[ "${APP_STAGE:A}" == "$APP_STAGE" && ! -L "$APP_STAGE" ]] || fail "Unsafe existing Agent upgrade stage."
  /bin/rm -rf "$APP_STAGE"
fi
/bin/mkdir -m 700 "$APP_STAGE"
/bin/cp -f "$SOURCE_ROOT/app/DianAgent" "$APP_STAGE/DianAgent"
/bin/chmod 755 "$APP_STAGE/DianAgent"

dian_validate_native_runtime_tree() {
  local tree="$1"
  [[ -d "$tree" && ! -L "$tree" && -x "$tree/DianAgent" ]] || return 1
  local arches="$(/usr/bin/file "$tree/DianAgent")"
  [[ "$arches" == *"$MACHINE"* ]]
}
dian_validate_extension_tree() {
  local tree="$1"
  [[ -d "$tree" && ! -L "$tree" && -f "$tree/manifest.json" && ! -L "$tree/manifest.json" ]] || return 1
  local staged_version="$(/usr/bin/plutil -extract version raw -o - "$tree/manifest.json" 2>/dev/null || true)"
  [[ "$staged_version" == "$VERSION" ]]
}
dian_validate_macos_tools_tree() {
  local tree="$1"
  [[ -d "$tree" && ! -L "$tree" &&
     -f "$tree/launch_agent.sh" && -x "$tree/launch_agent.sh" &&
     -f "$tree/verify_local_api.sh" && -x "$tree/verify_local_api.sh" &&
     -f "$tree/atomic_directory_update.sh" && -x "$tree/atomic_directory_update.sh" &&
     -f "$tree/repair_dian_agent.command" && -x "$tree/repair_dian_agent.command" ]]
}

dian_stage_launchagent_plist() {
  PLIST_TEMP="$(/usr/bin/mktemp "$LAUNCH_AGENT_DIR/.com.dianagent.agent.plist.tmp.XXXXXX")" || return 66
  [[ "${PLIST_TEMP:h:A}" == "${LAUNCH_AGENT_DIR:A}" && -f "$PLIST_TEMP" && ! -L "$PLIST_TEMP" ]] || return 64
  /bin/cat > "$PLIST_TEMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$INSTALL_ROOT/bootstrap/recovery_bootstrap.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key><dict>
    <key>DIAN_AGENT_INSTALL_ROOT</key><string>$INSTALL_ROOT</string>
    <key>DIAN_AGENT_DATA_DIR</key><string>$INSTALL_ROOT/data</string>
    <key>DIAN_AGENT_LOG_DIR</key><string>$INSTALL_ROOT/logs</string>
    <key>BRIDGE_PORT</key><string>8765</string>
  </dict>
  <key>StandardOutPath</key><string>$INSTALL_ROOT/logs/launch-agent.out.log</string>
  <key>StandardErrorPath</key><string>$INSTALL_ROOT/logs/launch-agent.err.log</string>
</dict></plist>
PLIST
  /usr/bin/plutil -lint "$PLIST_TEMP" >/dev/null
}

if [[ -f "$INSTALL_ROOT/current-version.txt" && ! -L "$INSTALL_ROOT/current-version.txt" &&
      -f "$PLIST_PATH" && ! -L "$PLIST_PATH" ]]; then
  dian_stage_launchagent_plist || fail "Could not stage the stable LaunchAgent migration."
  dian_install_prepare_stable_plist_migration "$INSTALL_ROOT" "$PLIST_PATH" "$PLIST_TEMP" || \
    fail "Could not preserve the exact previous LaunchAgent before bootstrap migration."
  PLIST_TEMP=""
fi

dian_install_transaction_begin "$INSTALL_ROOT" "$VERSION" "$PLIST_PATH" "$DOMAIN" "$LABEL" || fail "Could not create the durable installation transaction journal."
typeset -g DIAN_INSTALL_TRANSACTION_ACTIVE=1
dian_install_transaction_phase "$INSTALL_ROOT" switching || fail "Could not persist the installation transaction phase."
# Put the stable, non-transactional recovery bootstrap on disk before any
# active directory can enter its target-to-previous rename window.  After a
# power loss, launchd reads this plist and can restore a switching transaction.
dian_stage_launchagent_plist || fail "LaunchAgent configuration validation failed."
dian_install_transaction_mark "$INSTALL_ROOT" plist committing || fail "Could not persist the LaunchAgent switch intent."
/bin/mv -f "$PLIST_TEMP" "$PLIST_PATH" || fail "Could not activate the LaunchAgent configuration."
PLIST_TEMP=""
dian_durable_sync || fail "The LaunchAgent switch could not be persisted safely."
dian_install_transaction_mark "$INSTALL_ROOT" plist committed || fail "Could not persist the LaunchAgent switch receipt."
dian_install_fault after-plist || fail "Injected failure after LaunchAgent configuration switch."
dian_install_transaction_mark "$INSTALL_ROOT" app committing || fail "Could not persist the Agent switch intent."
dian_atomic_directory_commit "$INSTALL_ROOT" "$APP_STAGE" "$APP_VERSION_DIR" dian_validate_native_runtime_tree || fail "Agent atomic directory switch failed; the recoverable previous tree was retained."
dian_durable_sync || fail "The Agent directory switch could not be persisted safely."
dian_install_transaction_mark "$INSTALL_ROOT" app committed || fail "Could not persist the Agent switch receipt."
dian_install_fault after-app || fail "Injected failure after Agent switch."
# Keep the old extension active until the Agent and its maintenance launcher
# are fully staged.  A tools-copy failure can then never expose a new extension
# to an old active runtime.
dian_install_transaction_mark "$INSTALL_ROOT" tools committing || fail "Could not persist the maintenance-tool switch intent."
dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/tools/macos" "$INSTALL_ROOT/tools/macos" dian_validate_macos_tools_tree || fail "Maintenance-tool staging, validation or atomic switch failed."
/bin/chmod 755 "$INSTALL_ROOT/tools/macos/"*.sh "$INSTALL_ROOT/tools/macos/"*.command || fail "Installed maintenance tools could not be made executable."
dian_durable_sync || fail "The maintenance-tool switch could not be persisted safely."
dian_install_transaction_mark "$INSTALL_ROOT" tools committed || fail "Could not persist the maintenance-tool switch receipt."
dian_install_fault after-tools || fail "Injected failure after maintenance-tool switch."
dian_install_transaction_mark "$INSTALL_ROOT" extension committing || fail "Could not persist the extension switch intent."
dian_atomic_directory_update "$INSTALL_ROOT" "$SOURCE_ROOT/extension" "$INSTALL_ROOT/extension-current" dian_validate_extension_tree || fail "Extension staging, validation or atomic switch failed."
dian_durable_sync || fail "The extension switch could not be persisted safely."
dian_install_transaction_mark "$INSTALL_ROOT" extension committed || fail "Could not persist the extension switch receipt."
dian_install_fault after-extension || fail "Injected failure after extension switch."
VERSION_TEMP="$(/usr/bin/mktemp "$INSTALL_ROOT/.current-version.tmp.XXXXXX")" || fail "Could not create an exclusive version-pointer staging file."
[[ "${VERSION_TEMP:h:A}" == "${INSTALL_ROOT:A}" && -f "$VERSION_TEMP" && ! -L "$VERSION_TEMP" ]] || fail "Unsafe version-pointer staging path."
dian_install_transaction_mark "$INSTALL_ROOT" pointer committing || fail "Could not persist the version-pointer switch intent."
print -r -- "$VERSION" > "$VERSION_TEMP" || fail "Could not stage the version pointer."
/bin/mv -f "$VERSION_TEMP" "$INSTALL_ROOT/current-version.txt" || fail "Could not activate the version pointer."
VERSION_TEMP=""
dian_durable_sync || fail "The version-pointer switch could not be persisted safely."
dian_install_transaction_mark "$INSTALL_ROOT" pointer committed || fail "Could not persist the version-pointer switch receipt."
dian_install_fault after-pointer || fail "Injected failure after version-pointer switch."

dian_install_transaction_phase "$INSTALL_ROOT" launching || fail "Could not persist the LaunchAgent verification phase."
dian_install_transaction_authorize_launch "$INSTALL_ROOT" "$VERSION" || fail "Could not authorize the exact pending Agent under the installation lock."
/bin/launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "$DOMAIN" "$PLIST_PATH" || fail "无法注册登录自启动。"
dian_install_fault after-bootstrap || fail "Injected failure after LaunchAgent bootstrap."
/bin/launchctl kickstart -k "$DOMAIN/$LABEL" || fail "无法启动本地 Agent。"
dian_install_fault after-kickstart || fail "Injected failure after LaunchAgent kickstart."
if ! dian_verify_local_api "$TRUST_RECEIPT" "$VERSION" 40; then
  TRUST_RECEIPT=""
  fail "Agent 未通过本次安装身份、扩展配对和受保护接口验收。"
fi
dian_install_transaction_verify_job "$DOMAIN" "$LABEL" "$APP_VERSION_DIR/DianAgent" "$VERSION" || {
  TRUST_RECEIPT=""
  fail "LaunchAgent health came from a stale or unrelated process instead of this installation."
}
dian_install_fault after-health || fail "Injected failure after authenticated Agent health verification."
dian_install_transaction_phase "$INSTALL_ROOT" healthy || fail "Could not persist the healthy installation receipt."
if ! dian_install_transaction_commit "$INSTALL_ROOT"; then
  dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL" || fail "The Agent is healthy, but transaction cleanup could not complete."
  [[ "${DIAN_INSTALL_RECOVERY_OUTCOME:-}" == "finalized_new" || "${DIAN_INSTALL_RECOVERY_OUTCOME:-}" == "none" ]] || fail "The healthy installation could not be finalized safely."
fi
typeset -g DIAN_INSTALL_TRANSACTION_ACTIVE=0
TRUST_RECEIPT=""
if [[ -e "$REPAIR_PENDING" || -L "$REPAIR_PENDING" ]]; then
  [[ -f "$REPAIR_PENDING" && ! -L "$REPAIR_PENDING" ]] || fail "The pending repair marker became unsafe."
  /bin/rm -f "$REPAIR_PENDING" || fail "Could not clear the completed repair marker."
  dian_durable_sync || fail "Could not persist completed repair cleanup."
fi
dian_install_lock_release || fail "Could not release the completed installation lock."

if [[ -d "/Applications/Google Chrome.app" || -d "$HOME/Applications/Google Chrome.app" ]]; then
  /usr/bin/open -a "Google Chrome" "chrome://extensions" >/dev/null 2>&1 || true
elif [[ -d "/Applications/Microsoft Edge.app" || -d "$HOME/Applications/Microsoft Edge.app" ]]; then
  /usr/bin/open -a "Microsoft Edge" "edge://extensions" >/dev/null 2>&1 || true
fi
/usr/bin/open "$INSTALL_ROOT/extension-current" >/dev/null 2>&1 || true

print "\n店策 Agent $VERSION 已安装并在后台运行。"
print "下一步：打开 Chrome 的 chrome://extensions 或 Edge 的 edge://extensions。"
print "开启开发者模式，点击『加载已解压的扩展程序』。"
print "选择：$INSTALL_ROOT/extension-current"
print "数据仅保存在：$INSTALL_ROOT/data"
read "?按回车键关闭此窗口。"
