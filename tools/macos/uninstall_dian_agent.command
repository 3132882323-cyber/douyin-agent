#!/bin/zsh
set -eu

INSTALL_ROOT="$HOME/Library/Application Support/DianAgent"
PLIST_PATH="$HOME/Library/LaunchAgents/com.dianagent.agent.plist"
DOMAIN="gui/$UID"
LABEL="com.dianagent.agent"
SCRIPT_DIR="${0:A:h}"
ATOMIC_UPDATE_HELPER="$INSTALL_ROOT/tools/macos/atomic_directory_update.sh"
if [[ ! -f "$ATOMIC_UPDATE_HELPER" || -L "$ATOMIC_UPDATE_HELPER" ]]; then
  for candidate in "$SCRIPT_DIR/atomic_directory_update.sh" "$SCRIPT_DIR/tools/macos/atomic_directory_update.sh"; do
    if [[ -f "$candidate" && ! -L "$candidate" ]]; then
      ATOMIC_UPDATE_HELPER="$candidate"
      break
    fi
  done
fi

EXPECTED="$HOME/Library/Application Support/DianAgent"
[[ "$INSTALL_ROOT" == "$EXPECTED" ]] || { print -u2 "拒绝卸载：安装目录校验失败。"; exit 1; }
[[ -f "$ATOMIC_UPDATE_HELPER" && ! -L "$ATOMIC_UPDATE_HELPER" ]] || { print -u2 "拒绝卸载：安装锁组件缺失或不安全。"; exit 1; }
source "$ATOMIC_UPDATE_HELPER"
dian_install_lock_acquire "$INSTALL_ROOT" 30 || { print -u2 "另一个安装、修复或卸载正在进行。"; exit 1; }
trap 'dian_install_lock_release' EXIT
dian_install_transaction_recover "$INSTALL_ROOT" "$PLIST_PATH" "$DOMAIN" "$LABEL" || {
  print -u2 "拒绝卸载：中断的安装事务无法安全恢复，日志和备份已保留。"
  exit 1
}
dian_install_cleanup_orphan_stages "$INSTALL_ROOT" "$PLIST_PATH" || {
  print -u2 "Refusing uninstall: orphan installation staging paths are unsafe or could not be removed."
  exit 1
}

/bin/launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  print -u2 "拒绝卸载：本地 Agent 仍在 launchd 中运行。"
  exit 1
fi
[[ ! -e "$PLIST_PATH" && ! -L "$PLIST_PATH" ]] || /bin/rm -f "$PLIST_PATH"

for target in \
  "$INSTALL_ROOT/app" \
  "$INSTALL_ROOT/extension-current" \
  "$INSTALL_ROOT/.extension-current.previous" \
  "$INSTALL_ROOT/.extension-current.transaction-rollback" \
  "$INSTALL_ROOT/tools" \
  "$INSTALL_ROOT/bootstrap" \
  "$INSTALL_ROOT/current-version.txt" \
  "$INSTALL_ROOT/.repair-pending"; do
  [[ "${target:h:A}" == "${INSTALL_ROOT:A}" || "$target" == "$INSTALL_ROOT/tools" ]] || {
    print -u2 "拒绝卸载：程序路径超出安装目录。"
    exit 1
  }
  [[ ! -e "$target" && ! -L "$target" ]] || /bin/rm -rf "$target"
done
dian_install_transaction_cleanup_tombstone "$INSTALL_ROOT" || {
  print -u2 "卸载后的事务清理未能安全完成。"
  exit 1
}
dian_durable_sync || { print -u2 "卸载结果未能持久化到磁盘。"; exit 1; }

print "店策 Agent 程序和自启动已移除。"
print "店铺数据、配置、知识包、备份和日志仍保留在：$INSTALL_ROOT"
print "如需彻底清除，请由文件管理器确认后手动删除该目录。"
read "?按回车键关闭。"
