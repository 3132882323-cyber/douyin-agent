# 开源能力复核与采用边界

更新日期：2026-08-22

本文记录店策 Agent 对外部开源项目的只读复核结果、可借鉴方向、许可证义务和明确禁区。它不是第三方项目的官方接口文档，也不构成法律意见。任何涉及巨量千川的真实接入，仍须以巨量引擎/巨量千川当期官方文档、应用权限和真实账户验收结果为准。

## 结论摘要

| 项目 | 许可证 | 本轮结论 | 采用方式 |
| --- | --- | --- | --- |
| [bububa/oceanengine](https://github.com/bububa/oceanengine) | [Apache-2.0](https://github.com/bububa/oceanengine/blob/master/LICENSE) | 有条件采纳 | 只借鉴已公开的千川接口能力和字段契约；优先独立实现 Python 只读适配，不直接搬入 Go SDK |
| [CriarBrand/qianchuanSDK](https://github.com/CriarBrand/qianchuanSDK) | [GPL-3.0](https://github.com/CriarBrand/qianchuanSDK/blob/main/LICENSE) | 仅作需求交叉验证，不采纳代码 | 上游明确提示旧接口尚在恢复维护；只把多账户报表、素材生命周期和兼容性矩阵作为产品问题，不复制 GPL 实现或旧接口常量 |
| [CacheControl/json-rules-engine](https://github.com/CacheControl/json-rules-engine) | [ISC](https://github.com/CacheControl/json-rules-engine/blob/master/LICENSE) | 采纳设计思想，暂不引入依赖 | 借鉴 JSON 条件树、事实、事件、优先级和可解释执行；沿用本项目自己的受限规则协议 |
| [apify/crawlee](https://github.com/apify/crawlee) | [Apache-2.0](https://github.com/apify/crawlee/blob/master/LICENSE.md) | 采纳可靠性思想，暂不整体引入 | 借鉴有限重试、会话健康、持久队列和生命周期记录；不采用绕过访问控制的能力 |
| [TonyWang-hub/mcp-cn-commerce](https://github.com/TonyWang-hub/mcp-cn-commerce) | [MIT](https://github.com/TonyWang-hub/mcp-cn-commerce/blob/main/LICENSE) | 采纳统一数据模型思想，不复制接口常量 | 借鉴只读连接器边界、实体规范化、来源标记和缺失值语义；千川/抖店路径与字段仍须以官方当期文档和真实账户核验 |
| [wu-shaobing/go_oceanengine_vue](https://github.com/wu-shaobing/go_oceanengine_vue) | README 宣称 MIT，但仓库根目录未找到 LICENSE | 不采纳代码 | README 功能清单不能替代许可证和可运行性验收；不复制其账户、报表、RBAC、页面或部署实现 |
| [cheyunzhuan/Douyin-Ecommerce-Insight](https://github.com/cheyunzhuan/Douyin-Ecommerce-Insight)、[wuxingzhu/douyin-live-ecommerce-analysis](https://github.com/wuxingzhu/douyin-live-ecommerce-analysis)、[caoyuan094-dotcom/douyin-ops-agent](https://github.com/caoyuan094-dotcom/douyin-ops-agent) | 无明确可复用许可证或商业使用受限 | 不采纳代码 | 只能交叉验证流量归因、直播漏斗、选品/合规等需求方向，不复制代码、数据、规则、页面或文案 |
| 无明确许可证的 GitHub 项目、演示仓库或商业网站 | 未知或未授权 | 不采纳代码 | 不复制代码、素材、文案、页面或闭源业务逻辑；只允许形成不依赖具体表达的产品问题清单 |

本轮没有把上述项目代码复制进店策 Agent。只有未来真正引入第三方依赖或衍生代码时，才进入依赖登记、许可证保留、NOTICE 检查、版本锁定和安全审计流程。

## 本轮补充：mcp-cn-commerce 与单品经营链

`mcp-cn-commerce` 的公开实现提供了一个有价值但必须谨慎使用的架构参照：平台连接器保持只读，把订单、商品、退款、评价、店铺等对象归一成稳定字段，并明确数据来源。它的测试主要依赖模拟响应，仓库中的接口路径、状态映射和字段不能证明仍符合 2026 年的抖店/千川生产契约，所以本项目没有复制其 endpoint、认证实现或字段常量。

本项目独立实现并采用以下通用原则：

- **先有实体，再做分析**：同一商品必须通过平台商品 ID、SKU ID 或商家编码关联；商品名只用于显示。
- **来源和时间随指标保存**：每项判断保留平台、页面类型、采集时间、质量分和表格行证据。
- **缺失不是 0**：库存、ROI、退款率或订单缺失时保持 `null/--`，不得制造止损或放量信号。
- **只读连接器与动作执行分离**：采集/归一化不能直接写平台；真实操作仍经过官方权限、人工授权、额度、冷却、审计和回读。
- **本机稳定匿名键**：浏览器读取到的商品、SKU、素材和直播身份在 localhost Bridge 中用安装级 HMAC 转换，原始商品身份不写入 JSON/SQLite。

基于这些原则，本轮新增“抖音电商单品经营链”：把货架、库存、直播、内容、千川和售后按同一商品汇总，只输出一个核心判断、一个下一步和一个验收条件。缺少稳定 ID、跨渠道映射、库存、退款或保本线时，系统只生成补数/人工复核，不允许进入自动放量。

## 一、bububa/oceanengine：千川只读能力

### 项目定位与可信度边界

`bububa/oceanengine` 是第三方维护的巨量引擎/千川 Go SDK，并非巨量引擎官方仓库。它可用于发现接口方向、请求字段和返回字段，但不能代替官方文档，也不能仅凭仓库存在就推断当前应用已经拥有相应权限。

仓库采用 Apache-2.0。若未来复制或修改其代码，必须保留许可证和适用的版权/归属声明，对修改文件作显著说明；若上游包含 NOTICE，还须按许可证要求随分发物保留。当前建议是根据官方接口契约独立实现本项目的 Python 适配层，避免引入不必要的 Go 运行时和跨语言依赖。

### 可借鉴能力与采用结论

| 能力 | 开源证据 | 价值 | 采纳结论 | 产品与安全边界 |
| --- | --- | --- | --- | --- |
| 计划学习期状态 | [API 调用](https://github.com/bububa/oceanengine/blob/master/marketing-api/api/qianchuan/ad/learning_status_get.go)、[请求与响应模型](https://github.com/bububa/oceanengine/blob/master/marketing-api/model/qianchuan/ad/learning_status_get.go) | 区分学习中、学习完成、学习失败和无学习状态，避免用户在学习期内频繁调计划 | **一期采纳，只读** | 每批最多 50 个计划 ID；只作用于经典千川计划；状态只显示和参与解释，不直接触发预算或启停 |
| 低效计划列表 | [API 调用及轮询警告](https://github.com/bububa/oceanengine/blob/master/marketing-api/api/qianchuan/ad/lq_ad_get.go)、[请求与响应模型](https://github.com/bububa/oceanengine/blob/master/marketing-api/model/qianchuan/ad/lq_ad_get.go) | 用平台标记补充本地 ROI/消耗诊断，帮助运营优先排查 | **一期采纳，只读、手动触发** | 只表示“本次是否命中平台低效列表”，不等于优质/劣质定论；接口失败必须显示“暂不可读”，不得默认为非低效；设置冷却，不做高频轮询 |
| 支付 ROI 目标建议 | [API 调用](https://github.com/bububa/oceanengine/blob/master/marketing-api/api/qianchuan/ad/suggest_roi_goal.go)、[字段模型](https://github.com/bububa/oceanengine/blob/master/marketing-api/model/qianchuan/ad/suggest_roi_goal.go) | 给出平台建议 ROI 及可能的上下界，可与店铺盈亏平衡 ROI 对照 | **二期条件采纳，影子建议** | 只能作为“平台参考值”；必须同时展示本地盈亏平衡线、请求场景、数据时间和来源；不得直接覆盖用户目标或自动提交 |
| 建议预算 | [API 调用](https://github.com/bububa/oceanengine/blob/master/marketing-api/api/qianchuan/ad/suggest_budget.go)、[字段模型](https://github.com/bububa/oceanengine/blob/master/marketing-api/model/qianchuan/ad/suggest_budget.go) | 为直播时段和预算设置提供区间参考 | **二期条件采纳，先核验单位** | SDK 字段注释中的金额单位必须与官方文档、真实响应交叉验证；只展示区间，不把建议上限当作自动扩量额度 |
| 效果预估 | [API 调用](https://github.com/bububa/oceanengine/blob/master/marketing-api/api/qianchuan/ad/estimate_effect.go)、[字段模型](https://github.com/bububa/oceanengine/blob/master/marketing-api/model/qianchuan/ad/estimate_effect.go) | 在用户提交前提供成本或 ROI 的区间预演 | **二期条件采纳，仅适用场景** | 当前开源注释限定了具体直播投放场景；必须标明“预估，不是承诺”，场景不匹配、权限不足或字段不完整时禁用 |

### 已发现的上游风险

- [学习状态枚举](https://github.com/bububa/oceanengine/blob/master/marketing-api/enum/qianchuan/learing_status.go) 中，`LEARNED` 常量被误写成了 `LEARNING`。本项目不得照抄该枚举，应保存接口原始值，并独立映射 `LEARNED` 为“学习完成”。
- SDK 可能滞后于平台接口。所有路径、参数、枚举、权限、限流和金额单位都必须先通过官方文档及沙箱/测试账户验证。
- 经典千川计划 ID、全域推广父计划 ID、乘方任务 ID 不能按计划名称猜测或合并。不同身份必须保留独立来源类型和可核验 ID。
- 平台“建议”是平台侧参考，不代表商家利润最优。商品毛利、退款率、库存和日亏损上限仍由本地经营边界优先约束。

### 一期建议的数据契约

学习期与低效结果只进入计划健康诊断，不进入乘方控制证据：

```json
{
  "plan_source_family": "standard_ad",
  "learning_status_raw": "LEARNING",
  "learning_phase": "learning",
  "learning_status_label": "学习中",
  "low_efficiency_state": "flagged",
  "low_efficiency_label": "平台标记低效",
  "plan_health_source": "official_api",
  "plan_health_captured_at_ms": 0,
  "diagnostic_only": true
}
```

约束：

- `low_efficiency_state` 使用 `flagged`、`not_flagged_currently`、`unavailable`、`not_applicable` 四态，禁止用一个布尔值掩盖接口失败。
- 未识别的学习状态保存原始值并映射为 `unknown`，不生成自动动作。
- 不覆盖计划原有投放 `status`。
- 不写入乘方 `decision_records`，不改变 `production_identifier_complete` 或 `platform_write_enabled`。
- 低效列表只在用户手动同步时读取，并设置账户级冷却；单个补充接口失败不阻断原有计划、报表和素材同步。

## 二、json-rules-engine：规则表达思想

项目 README 将其定位为 JSON 表达的规则引擎，支持递归 `ALL`/`ANY` 条件、优先级、事实缓存和事件输出，并强调不使用 `eval()`。这些思想适合店策 Agent 将“为什么建议调整”从散落代码提升为可版本化、可审计、可回放的规则协议。

### 采纳的思想

- **事实与规则分离**：店铺数据、计划数据和经营边界作为事实；规则只描述条件和输出，不直接读取页面或发起平台操作。
- **显式布尔树**：使用受限的 `all`、`any`、`not` 组合，避免把复杂逻辑藏在字符串表达式里。
- **优先级和冲突处理**：止损、授权、库存等硬保护优先于扩量建议；冲突时默认保持或阻断。
- **事件不是执行**：规则命中只生成带原因码的候选事件，之后仍要经过证据、作用域、人工授权和回读门禁。
- **可回放**：规则版本、事实快照、命中路径和输出事件一起保存，确保运营可以复盘同一结论。
- **无动态执行**：不允许 `eval`、任意脚本、网络函数或用户注入运算符。

### 暂不采纳的部分

- 暂不直接增加 `json-rules-engine` 运行时依赖，避免同时维护两套执行语义。
- 不照搬示例规则、事件名称或 JavaScript 实现。
- 不允许知识包通过 JSON 获得文件、网络、Cookie、Token 或平台写入权限。

下一步先定义本项目自己的受限规则 Schema、类型校验、允许的运算符、字段白名单、版本号和失败关闭策略。若未来确需引入依赖，ISC 要求在所有副本中保留版权和许可声明。

## 三、Crawlee：重试与会话可靠性思想

Crawlee README 公开了持久请求队列、会话管理、可配置错误处理与重试、生命周期钩子等能力；其[会话管理指南](https://github.com/apify/crawlee/blob/master/docs/guides/session_management.mdx)进一步描述了会话池和异常会话处理。这些机制适合用来提升本地 Agent 的稳定性，但不代表店策 Agent 需要整体迁移到 Crawlee。

### 采纳的思想

- **有限重试**：只对网络超时、临时页面未就绪等可恢复错误重试；使用指数退避、抖动和严格次数上限。
- **失败分类**：授权失效、验证码、账号不匹配、权限不足属于硬失败，立即交还用户处理，不盲目重试。
- **会话健康**：把“登录有效、店铺一致、千川账户一致、最近成功时间”作为会话状态；异常会话退休后由用户重新登录或确认。
- **任务持久化**：每个巡店/同步任务保存 `queued → running → succeeded/failed` 状态，Agent 重启后可恢复或明确结束，避免重复执行。
- **幂等和检查点**：同一账户、页面、时间窗的任务具有稳定键；每一页保存检查点，防止重启后重复入库。
- **生命周期记录**：记录重试次数、最后错误、恢复动作、页面和账号作用域，为用户提供可理解的故障提示。
- **资源上限**：限制同时打开页面数、单任务时长、队列长度和本地存储体积，优先保证用户当前操作流畅。

### 明确不采纳的部分

- 不使用代理轮换、指纹伪装或其他手段绕过平台访问控制、风控或验证码。
- 不读取用户未主动授权的页面，不导出 Cookie、Token 或原始 DOM。
- 不以“自动恢复”为由跨店铺、跨账号或绕过人工确认。
- 暂不整体引入 Crawlee；先在现有本地 Agent 中实现最小的任务状态机、重试预算和会话健康模型。

若未来复制、修改或分发 Crawlee 代码，必须遵守 Apache-2.0 的许可证、版权/归属和 NOTICE 义务。

## 四、qianchuanSDK：素材生命周期与兼容性问题

`CriarBrand/qianchuanSDK` 是 GPL-3.0 的非官方历史 SDK。维护者在 README 中明确说明，现有代码主要基于早期接口，OAuth、账户、计划、素材和报表仍在逐项核对新版平台契约。因此本项目不复制其代码、数据结构或接口常量，也不把它作为生产接入依据。

它对产品规划有两个可独立验证的价值：

- **素材生命周期不是一张排行榜**：多账户素材需要按“未测试 → 测试中 → 潜力 → 胜出 → 衰退 → 停测候选”保存时间序列；单个时点的 ROI 或平台标签只能作为线索。
- **接口兼容性必须可见**：每项官方能力要记录文档版本、最近沙箱验证时间、当前应用权限、当前账户是否可用和最后错误，不能让用户从“页面上有按钮”推断已经可写。

本项目当前的素材治理已经覆盖保护、停测、复测和单变量测试，但“衰退”仍需同一素材跨日数据才能成立。下一阶段只能在至少两个可比较时间窗、指标口径一致且样本量达标时给出衰退提示；它仍只生成内容任务，不自动删除或停投素材。

## 五、未许可、演示项目与商业网站

### 默认规则

- GitHub 仓库没有明确许可证时，默认不具备复制、修改或再分发授权；不得复制其代码、文档、图片、图标、数据文件或测试样本。
- 只有演示页面、截图或宣传文案，不能证明背后的算法、数据或交互实现可复用。
- 商业网站公开可见的导航和流程只能帮助识别用户问题，不能用于复制页面、文案、视觉资产、接口、源代码、模型参数或闭源经营逻辑。
- 不通过反编译、抓取源码映射、绕过登录或接口鉴权等方式获取非公开实现。

### 方向借鉴不等于复制

**方向借鉴 ≠ 复制网站/闭源逻辑。**

可以借鉴的是抽象问题，例如“计划需要统一检索”“批量动作需要预检”“执行后必须回读”“失败需要可恢复”。落地时必须使用本项目自己的信息架构、字段协议、文案、视觉系统、规则实现和安全门禁。

不得借鉴的是能够识别原作品表达或非公开实现的内容，包括但不限于：

- 相同或近似的页面结构、组件组合、文案和视觉资产；
- 通过浏览器资源、接口调用或混淆文件还原对方代码；
- 复制商业策略阈值、内部账户管理逻辑或闭源自动投放算法；
- 伪装成对方产品，或使用对方商标、名称和宣传材料造成关联误解。

## 六、下一步采用顺序与门禁

### Phase 0：官方核验

- 核对巨量千川当期官方接口文档、应用权限、授权范围、限流、字段单位和适用场景。
- 使用专用测试账户验证响应，不用生产店铺直接试错。
- 建立接口能力开关；未验权时保持 `unavailable`，不伪造模拟成功。

### Phase 1：只读计划健康

- 接入学习期状态和低效计划列表，只显示在“计划健康”或计划列表徽标中。
- 学习状态按 50 个 ID 分批；低效列表仅手动读取并配置冷却。
- 部分失败保留已成功结果，未知项显式显示“暂不可读”。
- 保持现有同步页数和乘方决策记录不变，加入无写请求回归测试。
- 本轮已把官方 API `plans` 快照纳入计划中心只读展示；未知投放模式、过期快照或身份不完整的计划仍不得绑定或执行。

### Phase 2：平台建议对照

- ROI 建议、预算建议和效果预估先进入影子模式。
- 同屏展示平台建议、本地盈亏平衡线、库存/退款/亏损边界、适用场景、单位、时间和置信说明。
- 只生成候选，不自动提交；任何真实调整仍需官方写权限、精确身份、人工授权、单次上限、审计和回读。

### Phase 3：规则协议和可靠性

- 定义签名、版本化、可回滚的受限规则 Schema；事实、条件、候选事件与执行器彻底分离。
- 引入任务幂等键、检查点、有限重试、硬失败分类、会话健康和可见恢复入口。
- 不引入反检测、绕过风控或后台静默控制。

### 依赖与发布门禁

未来每次真正采用第三方代码或包，至少完成：

1. 固定仓库、版本/提交和用途。
2. 核验许可证、版权声明、NOTICE 和传递义务。
3. 登记到第三方依赖清单或 SBOM。
4. 完成依赖漏洞、维护活跃度和供应链风险检查。
5. 对所有修改保留可追踪记录，不把第三方来源包装成自主原创。
6. 通过离线、无 AI、无写权限、授权失效、部分接口失败和 Agent 重启测试。

只有上述门禁全部通过，能力才能从“研究参考”升级为“可发布功能”。
