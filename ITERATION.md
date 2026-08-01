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
| 执行器 | `content-qianchuan` 唯一点击启停控件；若有二次确认弹窗则点唯一确认 |
| 验收 | 与 Bridge 同一套状态归一：已暂停/暂停中/独立「暂停」；排除控件文案与可暂停；不认 toast |
| 仍关闭 | 放量、批量启停、出价、改排期 |

### 升级注意

1. 设置页「受控执行」文案已统一为「受监督执行」，语义不变  
2. 若自建 OAuth 回调域名，启动 Companion 前设置 `DIAN_AGENT_OAUTH_CALLBACK_URL`  
3. `/health.secret_files` 含权限过宽提醒，以及默认 OAuth 回调告警  
4. 无成交硬止损：读到「投放中」等状态 → 生成暂停；**读不到状态 → 文案与动作均为降预算 30%**（勿再写「建议暂停」却执行降预算）  
5. 执行失败且**未改动平台**时，授权可通过 `/actions/preflight/restore` 恢复；已点击平台控件则不恢复  

---

## 历史遗留（LEGACY）处理进度

> 初审于 2026-07-30；下列项原为暂停前既有债务，本轮已逐项收敛。

| ID | 状态 | 处理说明 |
| --- | --- | --- |
| LEGACY-1 | ✅ | `confirmed` 校验跳过 `ACTION_EXPIRED`；新鲜度交给 preflight 重读 |
| LEGACY-2 | ✅ | probe/采集失败可重注入；**submit 禁止** reinject 重放（防暂停开关二次点击） |
| LEGACY-3 | ✅ | `consume_execution_authorization` 在锁内复核配额 |
| LEGACY-4 | ⚠️ | `/health.secret_files` 增加默认第三方回调告警；仍需生产自设 `DIAN_AGENT_OAUTH_CALLBACK_URL` |
| LEGACY-5 | ✅ | `set_data_dir` 清空 `_analysis_cache` |
| LEGACY-6 | ✅ | 平台未改动失败时 `/actions/preflight/restore` 恢复授权；已点击则不恢复；恢复最多 2 次 |
| LEGACY-7 | ✅ | 对账优先 `plan_id`；同名标 `ambiguous_plan_name` |
| LEGACY-8 | ✅ | 选中账号非空时按 `entry.account_key` 过滤建议 |
| LEGACY-9 | ✅ | `list_snapshots` 优先账号分区，并合并根目录独有页（如官方 plans） |

### 执行提交安全补充（LEGACY 收敛后复查）

- submit **禁止** reinject 后原样重放（避免暂停开关二次点击）
- 传输层失败按「可能已改动平台」处理，**不** restore
- 暂停确认弹窗须含「暂停/停用」文案，避免点到无关「确认」
- 暂停成功：content 与 Bridge 共用状态归一（已暂停/暂停中/独立「暂停」）；优先读投放状态列
- 状态列与整行均剥离按钮/开关文案（防状态列内嵌「暂停」按钮假成功）
- 须离开授权投放态；不认 toast；可暂停/取消暂停永不成功
- 确认弹窗：每层最多点 2 次；新弹窗重置；弹窗残留不挡行状态成功
- 零消耗跳过仅已暂停类，不把「未投放」误跳过优化建议

### ITERATION 全量复查补丁（P0～P3）

- 列表页手动/自动采集：存在下一页时标记 `pagination_truncated`，避免覆盖截断闸门
- `SNAPSHOT_TRUNCATED` 引导改为深度补采 `qianchuan_campaigns`，不再只 sync 当前页
- `list_snapshots` 增加 `storage_account_key`；建议/指标加载走 `load_catalog_snapshot`
- LEGACY-8：仅过滤原生 `qianchuan` 源，不误伤抖店嵌入千川页
- 对账：官方索引已有 plan_id 时，错误 ID 不得静默按名称匹配
- 执行与预检共用 active 千川标签页；verify 失败再延迟补采一次
- 表格采集保留空单元格，防状态列错位；预算口令整数格式对齐；popup Bearer 403 可刷新；撤销动作同步取消 preflight 会话

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
| 2026-07-30 | `9b4a62d` | 暂停验收与影子核验统一成功态；效果复查仅预算；补充 LEGACY-6～9 |
| 2026-07-30 | `c596224` | 投放状态归一化：拒绝「未暂停」子串误判 |
| 2026-07-30 | `8f98cb9` | 否定状态/错列字段/暂停控件与 toast 收紧 |
| 2026-07-30 | `01f98ae` | 收敛 LEGACY-1～9；暂停二次确认弹窗；授权失败可恢复 |
| 2026-07-30 | `3f472f8` | submit 禁止重放；传输失败不 restore；弹窗/快照合并收紧 |
| 2026-07-30 | `1d77c04` | consume 校验 confirmed；根快照按账号过滤；确认暂停按钮 |
| 2026-08-01 | `39fc8bf` | 修复本次回归：弹窗误点、官方 plans 根目录回退、暂停成功态对齐 |
| 2026-08-01 | `6169885` | 去掉裸暂停行判定；弹窗确认不唯一则失败；官方 plans 优先 official_api |
| 2026-08-01 | b6a1eba | 消息未送达可 restore；暂停成功增加开关关闭信号 |
| 2026-08-01 | 3935a29 | 开关仅认本次点击控件；port closed 不 restore |
| 2026-08-01 | `b5564df` | 暂停成功只认行内已暂停/暂停中；对齐 Bridge；弹窗残留不挡成功 |
| 2026-08-01 | `74654d9` | 排除按钮 loading 暂停中假成功；未投放不再被零消耗跳过 |
| 2026-08-01 | `3d7389b` | 暂停成功判定一次收口：状态归一对齐、状态列优先、弹窗按层重试 |
| 2026-08-01 | `809579c` | 状态列内嵌暂停按钮剥离后再判定成功 |
| 2026-08-01 | `c47cb53` | ITERATION 全量复查：截断/快照/对账/执行标签页/空单元格/口令等回归 |
