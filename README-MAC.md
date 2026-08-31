# 店策 Agent macOS 安装说明

适用范围：macOS 13 或更高版本，Chrome 或 Edge。当前不支持 Safari。

## 先看安装包类型

- `macos-source`：当前可直接发给朋友的源码内测包，仅支持 Apple Silicon（arm64）；首次安装需要 Apple Silicon 版 Python 3.10+ 和网络，安装器会创建隔离环境，不改系统 Python。
- `macos-arm64`：在真实 Apple Silicon Mac 上构建的原生包，终端用户不需要 Python。

这里的“原生包”是 `ZIP + .command` 安装器和独立 Agent 可执行文件，不是已签名、公证的 `.app` 或 `.pkg`；“无需 Python”也不等于可以公开商用发布。

M1、M2、M3、M4 等 Apple 芯片应下载文件名包含 `macos-arm64` 的包。原生安装器会再次核对架构，不匹配时会停止。v4.14.9 固定使用已修复已知漏洞的 `cryptography 50.0.0`；其[官方变更记录](https://cryptography.io/en/stable/changelog/)说明 49.0.0 起已移除 macOS x86_64 支持，因此 Intel Mac 构建和安装会明确停止，不会静默降级到已知有漏洞的旧依赖。

## 安装

1. 从发包人以外的可信渠道取得 SHA-256，在“终端”运行 `shasum -a 256 压缩包文件名` 并核对一致。
2. 解压 ZIP，双击 `install_dian_agent.command`。
3. 打开扩展管理页：Chrome 输入 `chrome://extensions`；Edge 输入 `edge://extensions`。安装器会优先尝试打开已安装的 Chrome 或 Edge。
4. 开启“开发者模式”，点击“加载已解压的扩展程序”，选择：
   `~/Library/Application Support/DianAgent/extension-current`
   如果目录选择器中找不到它，按 `⌘⇧G`，粘贴上面的完整路径后回车。
5. 把“店策 Agent”固定到工具栏，点击后确认本地 Agent 已连接。

## 生成无需 Python 的原生包

仓库已提供手动 GitHub Actions 工作流 `Build macOS preview packages`，会在 Apple Silicon runner 上运行完整测试并生成 arm64 ZIP。也可以在 Apple Silicon Mac 上运行 `./tools/macos/build_release.sh`。构建出的内测包仍未经过你的 Apple Developer ID 签名和公证，不能当作正式商用安装包。

程序安装在当前用户目录，不需要管理员密码。Agent 由 macOS LaunchAgent 登录启动并在异常退出后恢复；店铺数据保存在 `~/Library/Application Support/DianAgent/data`。千川 App Secret 与 Token 保存在当前用户的 macOS 登录钥匙串，不写入普通数据文件。

## 升级、修复与卸载

- 当前没有正式在线自动更新。升级时下载新版 Mac 包并再次运行安装器，然后在扩展管理页点一次“重新加载”；店铺数据默认保留。
- Agent 未启动或扩展提示认证配置损坏：双击 `repair_dian_agent.command`。健康配置不会轮换凭据；只有明确检测到损坏时，脚本才会把原认证与信任记录保存在 `~/Library/Application Support/DianAgent/config/repair-backup` 后重建，并在“扩展配对 + 受保护状态接口”都验证成功后报告恢复。
- 卸载程序：双击 `uninstall_dian_agent.command`。默认保留店铺数据、配置、备份、日志及 macOS 钥匙串凭证。
- 如果已经删除解压包，在 Finder 选择“前往 → 前往文件夹”，输入 `~/Library/Application Support/DianAgent/tools/macos`，可找到修复与卸载脚本。
- 若需一并清除千川凭证，卸载后打开“钥匙串访问”，搜索并人工删除账户为 `DianAgent`、名称为 `com.dianagent.oceanengine.app-secret` 和 `com.dianagent.oceanengine.tokens` 的项目。

## 当前发布边界

`macos-source` 可在 Windows 生成，但 v4.14.9 仅允许 Apple Silicon Mac 安装。安装器已用持久事务统一保护 Agent、扩展、维护工具、版本指针和 LaunchAgent；独立于可切换工具目录的稳定恢复启动层会在断电重启后先回滚未完成的 `prepared/switching` 事务，再启动原版本。`build_release.sh` 会先生成并复核不含本地乘方商业模块的公开源码树，只从该树编译，并在构包前强制运行 Darwin/launchd 行为测试。Windows 只能验证静态合同；发给朋友前仍必须在真实 Apple Silicon Mac 取得全部行为测试通过日志，并完成安装、强制中断恢复、重启和浏览器连接验收。无需 Python 的 `macos-arm64` 原生包必须在 Apple Silicon Mac 构建。

当前内测包没有 Apple Developer ID 签名和公证，macOS 可能显示开发者验证提示。安装前先核对随包提供的 SHA-256；确认发包人和哈希无误后，可在“系统设置 → 隐私与安全”中使用系统提供的“仍要打开”。请只通过可信来源发送，不能通过关闭 Gatekeeper 或执行去隔离命令绕过系统保护。

真实生产投放写入仍保持关闭。当前版本提供数据采集、运营诊断、官方只读同步、乘方影子评估与本机模拟执行，不会自动修改真实千川计划。
