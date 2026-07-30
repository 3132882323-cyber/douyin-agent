# 店策 Agent 迭代说明（`zjt_dev`）

> 分支：`zjt_dev`  
> 用途：记录安全与数据质量相关改造，供后续迭代对照。  
> 最近提交：见下方变更记录。

---

## 迭代总览

| 阶段 | 目标 | 状态 |
| --- | --- | --- |
| **P0** | 截断/低质数据不可执行化 + Bridge 写入鉴权 + 工作台补采优先 | ✅ 已完成 |
| **P1** | 选择器/页面类型回归、关键页 fixture、健康漂移告警、可配置深度页数 | ✅ 已完成 |
| **P2** | 拆分 `http_receiver.py` / `sidepanel.js`；网页与官方 API 对账 | ✅ 已完成 |
| **P3** | 执行面按闸门扩展；OAuth 回调可配置；文案三态对齐 | ✅ 本轮完成（暂停已接入） |

---

## P0：数据质量闸门与本地写入鉴权

详见历史提交 `136d238`。核心：`SNAPSHOT_TRUNCATED`、Bridge Bearer、工作台补采。

---

## P1：抗改版与可回归

详见 `8d5754f`：`maxDeepScanPages`、fixture 契约、连续质量骤降告警、CI。

---

## P2：可维护性与双通道可信度（已完成）

- [x] 拆分 `http_receiver.py`：`state` / `storage` / `insights` / `actions` / `reports`；HTTP Handler 仍留 facade  
- [x] 拆分 `sidepanel.js`：`sidepanel-scan` / `sidepanel-plans` / `sidepanel-tasks`  
- [x] 网页快照 vs 官方 API 计划预算/消耗对账；偏差超阈值降置信度  

| 模块 | 说明 |
| --- | --- |
| `bridge/state.py` | 共享 `DATA_DIR` / 锁 / 缓存 |
| `bridge/storage.py` | 快照与账号目录 I/O |
| `bridge/insights.py` | 诊断、计划建议、货架/直播、体检单 |
| `bridge/actions.py` | 审计、影子核验、preflight、执行回读 |
| `bridge/reports.py` | 日报模板、生成、定时调度 |
| `bridge/reconcile.py` | 官方 plans 对账 |
| `extension/sidepanel-*.js` | 巡查体检 / 计划库存 / 任务队列 |

---

## P3：能力边界与运维加固（已完成）

- [x] 产品文案三态对齐：`observe` / `shadow` / `supervised` → 观察模式 / 影子模式 / **受监督执行**（`execution_modes.py`）  
- [x] OAuth 公网回调可配置：环境变量 `DIAN_AGENT_OAUTH_CALLBACK_URL`  
- [x] Webhook / Token 等密钥文件权限提示：`/health.secret_files`（不回传内容）  
- [x] 执行面扩展：单计划 **暂停** 走同一闸门（确认 → preflight → 口令 → 探针 → 提交 → 回读）；批量/放量继续默认关闭  

### 单计划暂停说明

| 项 | 说明 |
| --- | --- |
| 提案 | `stop_loss` 且能读到投放中状态 → `operation_type=pause_plan` |
| 口令 | `确认暂停计划{计划名称}` |
| 预检 | 账号 / 计划 ID / 质量 / 截断 / **投放状态一致** / 单计划范围 |
| 执行器 | `content-qianchuan` 唯一点击启停控件，不做批量 |
| 验收 | 回读状态含「暂停」 |
| 仍关闭 | 放量、批量启停、出价、改排期 |

### 升级注意

1. 设置页「受控执行」文案已统一为「受监督执行」，语义不变  
2. 若自建 OAuth 回调域名，启动 Companion 前设置 `DIAN_AGENT_OAUTH_CALLBACK_URL`  
3. `/health` 新增 `secret_files` 数组，仅含权限过宽提醒  
4. 无成交硬止损：读到「投放中」等状态 → 生成暂停；**读不到状态 → 文案与动作均为降预算 30%**（勿再写「建议暂停」却执行降预算）  

---

## 已知遗留问题（历史问题，非本轮引入）

> 审查于 2026-07-30。下列项在改暂停/拆分之前已存在，**本轮仅记录，未改行为**。后续可单独立项。

| ID | 问题 | 影响 | 建议方向 |
| --- | --- | --- | --- |
| LEGACY-1 | 已确认方案启动 preflight 时仍走完整 `validate_action_draft`，含 `ACTION_EXPIRED`（采集时间 +10 分钟） | 确认后稍慢再执行会报草稿过期，与「确认后再重读」不一致 | 对 `state=confirmed` 跳过草稿 TTL，新鲜度交给 preflight 的 `fresh_reread` |
| LEGACY-2 | `runAuthorizedExecution` 不像 `collectFromTab` 那样在 `sendMessage` 失败后重注入 content script | 扩展更新后未刷新的千川页，执行探针直接失败 | probe/submit 复用采集重注入逻辑 |
| LEGACY-3 | `consume_execution_authorization` 在锁外做配额检查 | 极端并发可能突破日次数/冷却 | 锁内复核 `assess_execution_quota` |
| LEGACY-4 | 默认 OAuth 回调指向第三方公网域 | 未设环境变量时授权码经外部域名 | 生产强制 `DIAN_AGENT_OAUTH_CALLBACK_URL`；文档已提示 |
| LEGACY-5 | `http_receiver.set_data_dir` 不同步清空 `_analysis_cache`；facade `DATA_DIR` 与 `state.DATA_DIR` 双源 | 测试/热切换偶发脏缓存 | `set_data_dir` 调 `_invalidate_cache()`；统一只读 `state.DATA_DIR` |
| LEGACY-6 | `runAuthorizedExecution` 在 submit 成功前就 `consume` 授权 | 点击/网络失败后授权已废，需整段重来 | 成功后再 consume，或失败自动恢复会话 |
| LEGACY-7 | 官方对账仅按计划名归一化，同名后者覆盖 | 重名计划可能错配预算/消耗 | 优先 plan_id；重名标 `ambiguous` |
| LEGACY-8 | `build_plan_recommendations` 对 doudian 嵌入千川表未按选中账号过滤 | 多店并存时可能串号建议 | `selected_account` 非空时过滤 `entry.account_key` |
| LEGACY-9 | `list_snapshots` 读全局目录，业务读账号分区 | 侧栏显示有数据但 readback 可能缺失 | 目录列举与 load 路径对齐 |

---

## 原则（后续改动请遵守）

1. **不完整数据不能变成今日必做或可确认资金方案**  
2. **确认 ≠ 执行**；执行继续走 preflight / 口令 / 探针 / 回读  
3. **密钥与 Webhook 永不提交 Git，也不经 `/health` 回传**  
4. **改选择器或闸门必须带测试**，避免 silent 回归  

---

## 变更记录

| 日期 | 提交 | 说明 |
| --- | --- | --- |
| 2026-07-30 | `136d238` | P0：截断阻断、Bridge Bearer、工作台补采 |
| 2026-07-30 | `8d5754f` | P1：可配置深度页数、fixture 契约、连续质量骤降告警、CI |
| 2026-07-30 | `071bfef` | P2 首刀：storage 拆分、网页/API 预算对账降置信度 |
| 2026-07-30 | `1e9b6fb` | P2 续：抽出 `insights.py` + `sidepanel-scan.js` |
| 2026-07-30 | `4b6424f` | P2 收尾 + P3 首刀：actions/reports 拆分、文案三态、OAuth 回调可配 |
| 2026-07-30 | `e52613a` | P3：单计划暂停接入受监督闸门与页面执行器 |
| 2026-07-30 | `c2d9d5a` | 完善本次暂停引入项；记录 LEGACY-1～5 历史问题 |
| 2026-07-30 | （待提交） | 暂停验收与影子核验统一成功态；效果复查仅预算；补充 LEGACY-6～9 |
