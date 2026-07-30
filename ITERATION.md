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
| **P3** | 执行面按闸门扩展；OAuth 回调可配置；文案三态对齐 | ✅ 本轮首刀 |

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

## P3：能力边界与运维加固（本轮首刀）

- [x] 产品文案三态对齐：`observe` / `shadow` / `supervised` → 观察模式 / 影子模式 / **受监督执行**（`execution_modes.py`）  
- [x] OAuth 公网回调可配置：环境变量 `DIAN_AGENT_OAUTH_CALLBACK_URL`  
- [x] Webhook / Token 等密钥文件权限提示：`/health.secret_files`（不回传内容）  
- [ ] 执行面扩展：单计划暂停仍走同一闸门；批量/放量继续默认关闭（下一批）  

### 升级注意

1. 设置页「受控执行」文案已统一为「受监督执行」，语义不变  
2. 若自建 OAuth 回调域名，启动 Companion 前设置 `DIAN_AGENT_OAUTH_CALLBACK_URL`  
3. `/health` 新增 `secret_files` 数组，仅含权限过宽提醒  

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
