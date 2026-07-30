# 店策 Agent 迭代说明（`zjt_dev`）

> 分支：`zjt_dev`  
> 用途：记录安全与数据质量相关改造，供后续迭代对照。  
> 最近提交：`136d238` — harden P0 data quality gates and local bridge write auth.

---

## 迭代总览

| 阶段 | 目标 | 状态 |
| --- | --- | --- |
| **P0** | 截断/低质数据不可执行化 + Bridge 写入鉴权 + 工作台补采优先 | ✅ 已完成 |
| **P1** | 选择器/页面类型回归、关键页 fixture、健康漂移告警、可配置深度页数 | ✅ 已完成 |
| **P2** | 拆分 `http_receiver.py` / `sidepanel.js`；网页与官方 API 对账 | ✅ 本轮完成（insights + 体检 UI） |
| **P3** | 执行面按闸门扩展（单计划暂停等）；OAuth 回调可配置 | ⏳ 待开始 |

---

## P0：数据质量闸门与本地写入鉴权（本轮）

### 为什么改

此前主要风险不是「乱写千川」，而是：

1. 列表只采前 5 页时仍可能生成可确认的止损/预算方案 → 决策基于不完整数据  
2. Bridge POST 仅靠固定头 `X-Dian-Agent: 1|2` → 同机进程易伪造写入  
3. 体检单对低质量页缺少明确「补采」入口 → 运营容易忽略脏数据

### 改了什么

#### 1. 截断 / 低质阻断可执行结论

| 能力 | 说明 |
| --- | --- |
| 新阻断码 | `SNAPSHOT_TRUNCATED`：列表分页被截断时禁止确认资金动作 |
| 草稿门禁 | `build_action_draft` 在截断或质量分 &lt; 70 时 `can_confirm=false` |
| 置信度 | 截断快照强制 `confidence=medium`，无法进入 high 可执行路径 |
| 执行前检查 | 增加「计划列表未截断」检查项 |
| 体检单 | `needs_review` 含：质量分 &lt; 70 **或** `pagination_truncated` |

#### 2. Bridge Token 写入鉴权

| 项 | 说明 |
| --- | --- |
| Token 文件 | `bridge/data/bridge_token.txt`（目录已 gitignore） |
| 启动 | `ensure_bridge_token()`，不存在则生成 |
| 写接口 | POST 必须同时带 `X-Dian-Agent` + `Authorization: Bearer <token>` |
| 领取 | `GET /auth/bootstrap`（仅本机 loopback + 扩展头） |
| Health | 返回 `auth_required: true`，**不回传** token |
| 扩展 | `background` / `sidepanel` / `popup` 统一注入 Bearer；403 时自动重新 bootstrap |

#### 3. 工作台补采优先

- 失败页：「只重试这一页」  
- 低质量 / 截断页：「补采」  
- 批量：「补采失败/低质量页」（含截断）  
- 自动化准备度遇数据类阻断 → 引导「补采当前千川页」，并滚动到体检单区域  

### 涉及文件

```text
bridge/action_protocol.py
bridge/http_receiver.py
bridge/test_action_protocol.py
bridge/test_http_receiver.py
extension/background.js
extension/popup.js
extension/sidepanel.js
```

### 行为变化（升级注意）

1. **必须重启本地 Agent**，生成 / 加载 `bridge_token`  
2. **必须重新加载扩展**，通过 bootstrap 领取并缓存 token  
3. 旧脚本若只带 `X-Dian-Agent` 写 Bridge，将收到 `403 missing_or_invalid_bridge_token`  
4. 列表截断时，止损/降预算方案会锁定，需先补采再确认  

### 验证方式

```bash
cd bridge
python -m unittest discover -s . -p "test_*.py"
```

本轮新增/加强的断言包括：

- 截断快照阻断确认（`SNAPSHOT_TRUNCATED`）  
- POST 缺 token / 错 token → 403；正确 Bearer → 200  
- `/auth/bootstrap` 可领取；`/health` 不含 token  
- 体检单截断页计入 `needs_review`  

### 升级操作清单

1. 拉取并签出 `zjt_dev`  
2. 重启 Companion（`bridge/start_bridge.bat` 或自启脚本）  
3. 浏览器扩展管理页 → 重新加载「店策 Agent」  
4. 打开哨兵/工作台，确认本地 Agent 已连接  
5. 若写入仍 403：清除扩展 storage，或删除 `bridge/data/bridge_token.txt` 后重启 Agent  

---

## 后续迭代（建议顺序）

### P1 — 抗改版与可回归（本轮已完成）

- [x] 为关键 `page_type` 增加脱敏 HTML fixture + 契约测试（账号字段、关键列、最少行数）  
- [x] CI 跑现有 `extension/test-*.js` 与 bridge 单测（`.github/workflows/test.yml` + `run-tests.ps1` / `run-tests.sh`）  
- [x] 健康监控：同页质量分连续骤降 → 工作台提示「疑似平台改版」  
- [x] 设置项：深度巡查最大页数（默认 5，可上调至 20，截断仍阻断可执行结论）  

#### P1 说明

| 能力 | 说明 |
| --- | --- |
| `maxDeepScanPages` | 保存在扩展 `chrome.storage.local.settings`；content 采集读取；工作台「策略阈值」可改 |
| 截断语义 | 页数上调只影响「采多深」；仍有下一页时 `pagination_truncated=true`，P0 闸门继续生效 |
| Fixture | `extension/fixtures/`：`qianchuan-campaigns` / `qianchuan-overview` / `doudian-orders` |
| 契约测试 | `extension/test-page-contracts.js` |
| 改版告警 | `check_selector_health`：连续 ≥2 次下降且每次 ≥15 分 →「疑似平台改版」；并修复行数对比误用 quality_score 的 bug |
| 本地验证 | `.\run-tests.ps1` 或 `bash run-tests.sh` |

### P2 — 可维护性与双通道可信度（本轮继续）

- [x] 拆分 `http_receiver.py`：抽出 `state.py` + `storage.py`（快照/账号目录 I/O），facade 继续 re-export  
- [x] 继续拆分：抽出 `insights.py`（诊断/计划建议/货架直播/体检单）；`actions` / `reports` / `http_api` 仍待下一批  
- [x] 拆分 `sidepanel.js`：抽出 `sidepanel-scan.js`（巡查进度 + 体检单）；任务/千川方案/设置仍待下一批  
- [x] 有 OAuth 时：网页快照 vs 官方 API 计划预算/消耗对账；偏差超阈值降置信度  

#### P2 说明

| 能力 | 说明 |
| --- | --- |
| `bridge/state.py` | 共享 `DATA_DIR` / 锁 / 缓存；测试用 `http_receiver.set_data_dir()` 同步 |
| `bridge/storage.py` | `save_data` / `load_data` / `list_snapshots` / 账号目录 / `build_store_catalog` |
| `bridge/insights.py` | trends / insights / 计划建议 / 货架直播 / ops / 体检单等；经 `http_receiver` facade re-export |
| `bridge/reconcile.py` | 官方 `plans` 与浏览器计划行对账；预算或消耗偏差 ≥20% 或 ≥50 元 → `confidence=medium` |
| `extension/sidepanel-scan.js` | `scanReceiptFromStatus` / `renderScanReceipt` / `renderFullScan`；工作台 HTML 在 `sidepanel.js` 后加载 |
| 官方计划表 | `oceanengine_data` 的 `plans` 表增加「消耗」列，便于计划级对账 |
| 无 OAuth | 对账跳过，行为与 P1 一致 |

### P3 — 能力边界与运维加固（下一优先）

- [ ] 产品文案三态对齐：`observe` / `shadow` / `supervised`  
- [ ] 执行面扩展仍走同一闸门：单计划暂停 → 批量/放量继续默认关闭  
- [ ] OAuth 公网回调域名可配置；Webhook/Token 文件权限提示  

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
| 2026-07-30 | `7476f98` | P2 续：抽出 `insights.py` + `sidepanel-scan.js` |
