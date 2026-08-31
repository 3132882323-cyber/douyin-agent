# 浏览器扩展免手动重载 P0 方案

## 结论

普通 Windows / Chrome 用户不能由本地安装器静默安装或刷新“已解压扩展”。`extension-current` 只是稳定磁盘路径，不是 Chrome 的更新通道；继续增加 `chrome.runtime.reload()`、定时器或修复按钮，只能降低偶发故障，不能消除用户手动进入 `chrome://extensions` 的依赖。

面向普通用户唯一可持续的 P0 路径是：

1. Chrome 使用 Chrome Web Store，Edge 使用 Edge Add-ons。
2. 浏览器商店负责安装后的自动更新，本地安装器不再复制或替换消费者扩展。
3. Agent 与扩展通过协议版本协商兼容，不再要求两个产品版本完全相同。
4. 首次安装仍需用户在商店点击一次“添加”；后续升级不再要求“重新加载”。企业客户可由管理员策略静默安装，普通消费者没有合法的静默绕过方式。

Chrome 官方将 unpacked 定义为开发用途，并说明 Windows / macOS 的正式分发应使用 Chrome Web Store；自托管只适用于受管理的企业环境：[Chrome 扩展分发](https://developer.chrome.com/docs/extensions/how-to/distribute)。Chrome 企业策略可以静默强装，但 Windows 外部自托管扩展要求设备加入 AD、Azure AD 或 Chrome Enterprise Core：[ExtensionInstallForcelist](https://chromeenterprise.google/policies/extension-install-forcelist/)。Edge 也把 sideload 定位为本地测试，正式用户通过 Edge Add-ons 安装：[Edge sideload](https://learn.microsoft.com/en-us/microsoft-edge/extensions-chromium/getting-started/extension-sideloading)。

Native Messaging 只能让“已经安装的扩展”与本机程序通信，需要预先注册 host 和精确 `allowed_origins`；它不能安装或更新扩展，因此不是本问题的 P0 解法：[Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)。

## 当前阻塞证据

- `tools/install_release.ps1` 把源码复制到 `extension-current`，打开 `chrome://extensions`，并在超时后要求用户点击 Reload。
- `tools/build_release_core.ps1` 把 unpacked 目录直接装进公开 Windows 包。
- `bridge/activation_status.py` 将 Agent 版本同时当成 required extension version，磁盘版本和运行版本都要求完全相等。
- `bridge/promotion_readiness.py` 的 Chrome / Edge 官方扩展 ID 仍为空。
- 当前 manifest 的固定开发 ID 是 `obpbbgjamjfkambmhidbjnaoiehfndcj`，但没有与已发布商店条目对应的证据。

这些不是欢迎页 UI 问题，而是发行所有权和版本合同问题。

## P0 目标架构

### 1. 两种包彻底分离

| 通道 | 扩展来源 | 更新责任方 | 面向用户 |
|---|---|---|---|
| `internal-unpacked` | `extension-current` | 开发者/内部安装器 | 仅开发与内测 |
| `consumer-store` | Chrome Web Store / Edge Add-ons | 浏览器商店 | 普通用户 |
| `enterprise-managed` | 商店或企业自托管 CRX | 企业策略 | 已受管设备 |

公开安装包不再包含可加载的 `extension-current`。它只包含 Agent、商店条目 URL、官方扩展 ID 和连接诊断。内部包继续保留 unpacked，但必须明确显示“开发通道，升级可能需要 Reload”。

### 2. 版本改为协议协商

建议新增独立协议，例如：

```text
Agent product version: 4.15.0
Agent bridge protocol: current=2, supported=1..2
Extension product version: 4.14.8
Extension bridge protocol: current=2, supported=2..2
```

只有协议区间没有交集时才阻断业务。产品版本仅用于诊断和灰度，不再决定认证是否可用。

- 扩展请求携带 `X-Dian-Agent-Protocol`。
- `/activation/status` 返回 Agent 支持的协议区间和浏览器报告协议；不再返回“Agent 产品版本就是扩展必需版本”的合同。
- 本机会话绑定 `install_id + extension_id + protocol`，产品版本只记录审计字段。
- 每次破坏性升级至少保留 N / N-1 协议兼容窗口。
- 发布顺序固定为：先发布兼容新旧协议的 Agent，再发布扩展；删除旧协议必须在商店覆盖率达到门槛后进行。

### 3. 安装器职责收缩

消费者安装器只做：

1. 安装、签名验证并启动 Agent。
2. 检查官方扩展 ID 是否已通过认证报告。
3. 未安装时打开对应商店 listing；显示“一次安装”，不打开 `chrome://extensions` 和磁盘文件夹。
4. Agent 健康但扩展未装时返回 `agent_ready_extension_pending`，不能把已提交的 Agent 安装抛成失败。
5. 扩展上线后由认证报告自动把状态变为 `ready`。

### 4. 现有 unpacked 用户迁移

1. Agent 先发布过渡版，同时接受旧开发 ID 和新官方商店 ID；新装只信任官方 ID。
2. 迁移前把必要偏好写回本地 Agent；店铺历史和经营数据库本来就在 Agent，不依赖扩展 `chrome.storage`。
3. 引导用户安装商店版，等新官方 ID 完成认证报告后，再提示移除旧开发版。
4. 若商店 ID 与旧 ID 不同，两个扩展不能共享 `chrome.storage`，必须通过 Agent 恢复偏好；若相同，也必须实测 Chrome 对 unpacked → store 的替换流程，不能假定无缝覆盖。
5. 灰度结束后撤销旧开发 ID 的信任，保留可审计的截止版本/日期。

## 企业策略与 Native Messaging 边界

- `ExtensionInstallForcelist` / `ExtensionSettings` 可为受管理客户提供真正静默安装；必须作为企业 SKU，由管理员部署，不能让普通安装器擅自写策略冒充组织管理。
- Windows 普通用户可通过注册表发现 Chrome Web Store 扩展，但 Chrome 仍可能要求用户确认启用；它不能替代商店首次同意。
- Native Messaging 可在 P1 替换或补充 localhost 通信、按需拉起 Agent；host manifest 必须只允许官方扩展 ID。它不会解决扩展安装或升级。

## 发布门禁

仓库新增：

- `extension/distribution.channels.json`：把当前开发通道、消费者通道、协议和商店证据显式分开。
- `tools/check_extension_distribution.py`：消费者发行 fail-closed 检查。
- `bridge/test_extension_distribution_gate.py`：覆盖开发 ID 稳定、当前消费者状态真实阻断，以及完整商店就绪样例。

当前检查命令：

```powershell
python tools/check_extension_distribution.py --profile internal
python tools/check_extension_distribution.py --profile consumer --store chrome_web_store
```

第一条应通过；第二条现在必须失败，并列出所有缺失条件。只有以下条件全部满足才允许消费者构建/宣传“免手动重载”：

- 商店条目状态为 published，listing URL、update URL、官方 ID 完整一致。
- 商店包的公钥派生 ID与官方 ID 一致，Agent 编译信任锚包含该 ID。
- 公开安装器不复制 unpacked 扩展、不打开开发者扩展页、不出现 Reload 指引。
- 公开构建不嵌入 unpacked 扩展目录。
- Agent / 扩展实现显式协议头和兼容区间，不再做产品版本完全相等判断。
- Chrome 商店自动升级实测通过：浏览器开启、关闭、Service Worker 休眠、Agent 先升级、扩展先升级五种顺序均无需人工 Reload。

## P0 验收指标

- 新用户首次只需要一次商店安装确认；后续 10 次版本升级人工 Reload 次数为 0。
- Agent 与扩展错峰 24 小时仍可完成连接、巡店和只读诊断。
- 不兼容时只冻结扩展业务，不破坏 Agent 安装、不丢本地数据。
- 官方 ID 之外的扩展认证 100% 拒绝。
- 消费者发行门禁通过前，产品界面和文档不得写“自动安装扩展”或“免手动更新”。
