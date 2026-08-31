# 店策 Agent 分发与更新设计

## 产品边界

店策 Agent 的核心链路不依赖任何大模型：浏览器采集脱敏经营数据，本地 SQLite 保存历史，本地规则引擎生成诊断、任务和验收标准。AI 只用于可选的自然语言解释、内容创意和跨模块问答，不能改变预算护栏、授权范围或执行结果。

4.0 将两个结论严格分开：`product_operational` 表示本机数据、规则和受监督执行链路可以工作；`public_distribution_ready` 表示产品同时具备生产 Ed25519 信任锚、当前平台完整代码签名与发行公证，以及与内嵌官方扩展 ID、请求 Origin、安装来源和目标版本一致的浏览器商店发布证据。Windows 使用 Authenticode，macOS 使用 Developer ID 与 Gatekeeper。前者为真不能替代后者。

本文件描述本地版分发。抖店服务市场 SaaS 使用独立发行模式、官方 OAuth/Open API 与云端多租户安全边界，不复用本地浏览器采集入口，详见 [MARKETPLACE_DEPLOYMENT.md](MARKETPLACE_DEPLOYMENT.md)。

## 数据分层

Windows 发布版默认使用 `%LOCALAPPDATA%\DianAgent`，macOS 使用 `~/Library/Application Support/DianAgent`：

```text
DianAgent/
├── app/                 已签名的 Agent 程序
├── versions/            已验证的离线升级版本
├── extension-current/   浏览器稳定加载路径
├── data/                用户私有 shop.db 与兼容 JSON（升级永不覆盖）
├── knowledge/           已验证的规则知识包与回滚副本
├── backup/              数据库迁移与更新前备份
├── config/              更新通道、用户阈值和匿名反馈授权
└── logs/                本地运行日志
```

源码开发模式继续使用 `bridge/data`，避免影响现有开发和测试。设置 `DIAN_AGENT_DATA_DIR` 可显式选择数据目录。

## 更新通道

- `stable`：默认通道，只接收完成灰度验证的版本。
- `beta`：提前体验新规则，仍需签名和兼容性检查。
- `internal`：内部验证，不面向普通用户。

更新清单只允许 HTTPS。知识包下载后依次检查版本、最低 Agent 版本、过期时间、SHA-256 和 Ed25519 签名，再原子切换；任何一步失败都保留当前版本。远程包不能包含或执行 Python、JavaScript、WASM、命令或模板表达式。

浏览器扩展遵守 Manifest V3：远程服务只能提供数据和声明式规则，不能下载并执行代码。采集代码变化必须发布新的扩展版本。

## 发布构建

发布机器安装 Python 3.10+，并从审计后的锁定清单安装构建依赖；构建脚本会拒绝版本不一致的 PyInstaller：

```powershell
python -m pip install -r bridge/requirements-build.txt
python -m pip_audit -r bridge/requirements-build.txt --progress-spinner off
```

然后优先运行完整发布构建：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_release.ps1
```

脚本会同时构建独立 Agent、现代浏览器扩展和兼容扩展，并生成 `dist/release/DianAgent-v*-windows.zip` 与 SHA-256 文件。发布包会把独立程序放在安装脚本实际读取的 `app/DianAgent.exe` 位置，因此终端用户无需 Python。

公开构建不会直接从开发工作树编译。`tools/prepare_public_source.py` 会先扫描完整工作树，只允许排除登记在发布边界中的本地乘方商业模块；任何额外私有目录、凭据、密钥、许可文件或未知私有导入都会让构建失败。随后 `tools/build_release_core.ps1` 仅从临时净化源码编译，并对 staging 目录和最终 ZIP 再次执行 fail-closed 检查。公开包根目录的 `distribution.json` 与 `dist/agent/PUBLIC_BUILD.json` 必须同时显示：

- `build_flavor: public_community`
- `commercial_modules_included: false`
- `redistributable: true`
- `source_boundary: verified_sanitized_source`

内部商业版只能在持有本地私有模块的开发机上显式运行：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_private_local.ps1
```

它生成 `dist/private-local/DianAgent-v*-internal`，不会生成可传播 ZIP，并带有 `INTERNAL_COMMERCIAL_BUILD.json`。该标记会被公开发布检查器主动拒绝。内部包的运行时状态必须显示 `build_flavor=private_commercial`、`redistributable=false` 和 `commercial_runtime_loaded=true`；需要安装到本机时显式添加 `-Install`。仓库源码直接加载被忽略商业模块时只会标记为 `developer_checkout`，同样不可分发。不要把 `dist/private-local`、开发工作树构建的 EXE 或内部截图当作公开发行证据。

macOS 不能从 Windows 交叉构建可信 Mach-O。v4.14.9 应在 Apple Silicon Mac 上运行：

```zsh
./tools/macos/build_release.sh
```

脚本会先强制运行 `MacOSInstallTransactionBehaviorTests`，覆盖跨树回滚、8 个故障点、旧锁回执、软链接与断电恢复；任一 Darwin/launchd 用例失败都会在构包前停止。通过后生成 `DianAgent-v*-macos-arm64.zip`，包含原生 Agent、Chrome / Edge 扩展、LaunchAgent 安装、修复和卸载入口。Windows 只能运行 `tools/build_macos_source_release.ps1` 生成需要 Apple Silicon 版 Python 3.10+ 的源码内测包，不得把它宣传为无依赖原生包。当前安全依赖已移除 macOS x86_64 支持，Intel 构建会失败关闭，不能降级依赖绕过。正式公开分发还必须在 Mac 构建机保存 10/10 行为测试凭证，并完成 Developer ID 签名、公证和 Gatekeeper 验收。

发布包同时包含无需 Python 的 `DianAgentUpdater.exe`。`tools/build_release_bundle.ps1` 生成离线升级包，默认只允许从首个受支持协议版本 `3.7.0` 或更高版本升级；更新器会同时识别初装的 `app/<version> + current-version.txt` 和后续的 `versions/<version> + current.json`。它在解压前校验 Ed25519 发布签名，然后校验兼容范围、路径及每个文件的 SHA-256，通过 `current.json` 激活版本目录，扩展使用 `extension-current` 稳定目录。

升级会先持久化旧指针和旧扩展，再使用 Release 介质自身的新启动器检查真实 8765 服务；成功后通过独立维护工具日志替换安装目录的启动器、看门狗、卸载器和更新器，最后清理回滚事务。Windows 计划任务与登录快捷方式指向安装根下不参与 `tools` 改名的稳定 bootstrap；bootstrap 同时恢复完整安装日志和维护工具日志。维护工具日志的事务 ID 必须与其 stage/backup 直接子目录后缀完全一致，伪造、跨目录或多义状态全部失败关闭并保留现场。新版自检、真实启动或维护工具更新失败时，会恢复上一版指针和扩展并严格检查旧版重启结果。

若断电，`.offline-upgrade-rollback` 会保留且不能被下一次升级覆盖。正常启动会先尝试当前指针：本机轻量 `/health/live` 的状态和版本都精确一致时才确认；若目标版本尚未写入事务状态，也必须由同一份健康证据补全后才能确认。快照、Schema 和磁盘详情只在 `/system/status` 中按需读取，不能阻塞升级存活探针。只有完成一次真实启动等待仍不健康时，才自动回滚并重新启动旧版。旧版恢复健康时启动脚本返回专用代码 `4`，升级批处理据此显示“已回滚”，不会显示升级成功；损坏或互相矛盾的事务状态始终 fail-closed，保留现场供人工处理。

`DianAgentUpdater.exe cleanup` 默认是 dry-run。它保护当前版本、待确认事务的前后版本、最近 2 个版本以及初装 `app/<version>`，默认只把超过 168 小时的孤立版本、staging、已完成事务和扩展/工具临时目录列入候选。使用 `--apply` 才实际删除；Windows 文件锁和无法确认安全边界的路径会跳过，并记录到 `logs/offline-upgrade-maintenance.jsonl`。启动器每天最多执行一次默认 dry-run；正式运维确认预览结果后可执行：

```powershell
& "$env:LOCALAPPDATA\DianAgent\tools\DianAgentUpdater.exe" cleanup `
  --install-root "$env:LOCALAPPDATA\DianAgent" `
  --keep-recent 2 --min-age-hours 168 --apply
```

离线清单的签名覆盖产品、版本、分发类型、兼容范围以及全部文件的路径、大小和 SHA-256；只排除 `signature` 字段本身。生产模式默认拒绝未签名包、测试签名包和未知公钥。仓库当前故意不含生产私钥，也尚未配置生产公钥，因此生产离线包的构建与安装均为 fail-closed。正式发布负责人必须在线下或 HSM 中生成 Ed25519 生产密钥，把私钥保存在仓库外，只把公钥加入 `bridge/offline_upgrade.py` 的 `PRODUCTION_OFFLINE_PUBLIC_KEYS`，然后使用：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_release_bundle.ps1 `
  -SigningPrivateKeyPath D:\secure\dian-agent-offline-ed25519.pem `
  -SigningKeyId dian-agent-offline-2026-01
```

生产私钥路径如果位于仓库内，构建会直接失败；脚本不会把私钥复制进产物或输出到日志。仓库内公开的 RFC 8032 测试公私钥只用于自动化测试。开发人员必须显式运行下面的命令才能生成测试包，文件名和 manifest 都会标记 `development_test`，不可公开分发：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_release_bundle.ps1 -DevelopmentTestSigning
```

测试签名包也只有在更新器显式提供 `--allow-test-keys` 时才会通过；普通 `inspect/install` 永远不接受。Ed25519 证明发布清单来自受信密钥，但公开推广前仍需给 Agent、Updater 和安装器增加 Windows Authenticode，并通过可信 HTTPS 渠道发布。

`dist/agent/DianAgent.exe` 只是中间构建产物，不能单独当作完整安装包发布。正式公开分发前还必须完成 Windows 代码签名、安装器签名和恶意软件误报测试；当前 ZIP 属于便携内测包，用户移动或删除解压目录会导致自动启动失效。

## 隐私与匿名反馈

匿名改进计划默认关闭，必须由用户主动打开。允许字段仅限受控行业 slug、规则编号、指标区间、是否采纳和效果方向；行业只接受内置枚举，不接受自由文本、手机号或店铺标识。店名、账号、订单、商品标题、素材原文、截图、Cookie、Token、Webhook 和原始页面内容一律不得上传。

4.0 只实现有上限的本地匿名反馈队列，不配置自动上传目标，也不进行后台发送。未明确同意时接口拒绝入队；用户可在工作台查看条数并单独清空队列，清理动作不会触碰店铺快照、经营记忆或 SQLite 数据库。

## 行业知识包

行业知识包与程序版本分离更新。工作台显示当前行业、版本、来源和可回滚版本；本地导入同样必须通过 SHA-256、有效期、最低 Agent 版本与 Ed25519 签名校验，失败时保持当前知识包不变。仓库未配置知识包生产公钥时，导入按钮会按失败关闭策略禁用，不能用无签名 JSON 替换经营规则。

## 4.0 公开发行验收

以下三项必须全部具备可核验证据，才允许 `public_distribution_ready=true`：

1. 更新器内嵌与生产私钥匹配的 Ed25519 公钥，且生产私钥从未进入仓库或构建日志。
2. 当前平台发布链已完成验证：Windows 的 Agent、Updater、安装/升级入口和维护工具通过 Authenticode 与 SmartScreen 抽检；macOS 原生 Agent 通过 Developer ID 签名、Apple 公证与 Gatekeeper 验收。只签主程序但缺少完整发行证据不算通过。
3. 扩展已在目标浏览器商店正式发布，运行时请求 Origin 或受保护请求中的显式扩展 ID 与内嵌官方扩展 ID 匹配，安装来源属于该商店且版本与 4.14.9 完全一致；显式扩展 ID 仍必须同时通过短期令牌主体与版本校验，开发者模式加载和仅靠环境变量声明只能作为内测来源。

当前仓库仅达到“产品本地可运行、发行条件透明可查”。便携包仍由 `.bat` 入口启动，仓库也未内嵌正式商店扩展 ID；在签名安装器/升级器替代不可验证入口且全部证据补齐前，构建产物只能作为内部验收包，不得宣传为正式公开发行版。

## 灰度与回滚

推荐发布顺序为内部用户、5%、20%、50%、100%。服务端可停止继续发放某个知识包，但不能绕过客户端签名、过期和兼容性校验。客户端始终保留最近一个已验证版本，并在切换失败时自动回滚。
