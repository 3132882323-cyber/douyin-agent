# 抖店服务市场版改造与部署边界

## 当前结论

店策 Agent 现有 `local/internal` 版本继续作为内部验证工具；浏览器扩展、网页快照采集和 DOM 受控执行不作为抖店服务市场审核版的核心能力。

服务市场目标形态为：

```text
抖店服务市场订购
→ 商家通过抖店官方 OAuth 授权
→ HTTPS 多租户工作台
→ 抖店 Open API / 消息推送
→ 店铺级数据仓库与规则引擎
→ 今日任务、经营诊断、日报和飞书/钉钉通知
```

当前提交的是第一批安全底座，不代表已经取得软件服务商资格、应用类目或任何 Open API 权限。`GET /marketplace/readiness` 只有在全部可核验证据都存在时才会返回可提交；默认必须是 `blocked`。

## 发行模式

通过 `DIAN_AGENT_DEPLOYMENT_MODE` 显式选择：

| 模式 | 用途 | 浏览器页面采集 | DOM 执行 | 官方 Open API |
| --- | --- | --- | --- | --- |
| `local` | 普通本地开发与使用 | 开启 | 保留现有受控链路 | 可选 |
| `internal` | 内部测试 | 开启 | 保留现有受控链路 | 可选 |
| `doudian_marketplace` | 抖店服务市场 SaaS | 强制关闭 | 强制关闭 | 核心数据源 |
| `cloud` | `doudian_marketplace` 的云部署别名 | 强制关闭 | 强制关闭 | 核心数据源 |

未知值不会回退到本地模式，而会关闭浏览器采集与执行。服务市场模式下：

- `/push`、`/scan-status`、`/stores/link`、`/stores/select`、`/distribution/extension-source` 被 HTTP 层拒绝。
- DOM 执行授权、消费授权、页面探针、执行回执和回读验证接口被拒绝。
- 现有 POST 接口只识别静态 `X-Dian-Agent` 头，不能充当云端认证，因此服务市场模式拒绝全部旧写接口。
- 除 `/health` 与服务市场就绪状态外，现有本地 GET 数据接口同样被拒绝，避免在认证网关接入前暴露店铺数据。
- 云端 UI 的 CORS 只接受 `DIAN_AGENT_ALLOWED_WEB_ORIGINS` 中精确匹配的 HTTPS Origin，不反射浏览器扩展 Origin。

## OAuth 与 Open API 骨架

实现位于 `bridge/doudian_marketplace.py`：

- `ShopScope(tenant_id, shop_id)`：所有 Token 与 API 调用必须同时绑定租户和店铺。
- `OAuthStateStore`：10 分钟、一次性、店铺绑定的 OAuth state，防止回调串店。
- `TokenStore`：生产实现必须加密保存 Token；仓库只提供不落盘的 `InMemoryTokenStore` 用于单元测试。
- `RequestAuthenticator`：云端认证抽象；默认 `RejectingAuthenticator` 拒绝所有请求。
- `MarketplacePlatformAdapter`：必须由云端启动代码显式注入获批协议实现；仓库不预设授权地址、Token 地址、API 地址、签名算法或响应结构。
- `DoudianMarketplaceClient`：未注入适配器时不会联网，授权、换取 Token 和 API 调用全部 fail-closed；注入后仍会校验一次性 state 和店铺一致性。
- `ExampleDraftHmacSigner`：只用于演示确定性 canonicalization，名称和状态均明确为非平台合同，不进入任何生产调用路径。
- 适配器就绪证据必须包含 adapter ID、合同版本、构建 SHA-256，以及真实部署探针的通过时间；环境变量 `true` 不能替代运行时注入与探针。

Access Token、Refresh Token 和 App Secret 不会出现在状态响应中。真实 Secret 只能由部署密钥系统注入，不能写入仓库、日志、前端或构建产物。

## 云端还必须实现的组件

第一批代码刻意没有假装实现以下生产能力：

1. 认证网关：接入企业登录/会话/JWT，生成不可伪造的 `TenantRequestContext`。
2. 加密 Token 仓库：建议数据库只存密文，数据密钥由 KMS 托管，并记录读取审计。
3. OAuth 回调 Web 服务：部署在已备案域名的 HTTPS 地址，接入持久化 state 或短期分布式缓存。
4. 多租户数据库：所有业务表必须含 `tenant_id + shop_id` 复合范围，数据库访问层自动追加范围条件。
5. Open API 适配器：按平台实际批准的方法、字段、端点、签名版本、频控和错误码逐个接入，并在云端启动时显式注册。
6. 消息推送：验证平台签名、防重放、幂等消费、失败重试和死信审计。
7. 订购权益：校验服务市场订单、版本、到期时间和店铺授权状态。
8. 数据治理：商家导出、授权撤销、到期清理、注销删除和审计日志。

## 配置与就绪检查

可从 `.env.marketplace.example` 查看变量名称。不要直接把该文件改成真实密钥后提交。

开发环境查看：

```powershell
$env:DIAN_AGENT_DEPLOYMENT_MODE = "doudian_marketplace"
python bridge/http_receiver.py
```

然后访问：

```text
GET http://127.0.0.1:8765/marketplace/readiness
```

检查组包括：

- 企业主体、服务商资格与工具型应用类目。
- App Key/Secret、公网 HTTPS 回调与 API 合同核对。
- 当前进程实际注入的适配器合同版本、构建 SHA-256 与部署探针证据。
- 生产 Token 加密存储、租户认证网关和安全验收。
- 精确 HTTPS 工作台来源白名单。
- 隐私政策、用户协议、数据删除说明、客服支持页和 ICP 备案。

环境变量只能表达“部署证据已由发布流程核验”，不能替代平台审批。生产流水线应从受保护环境注入，并保存审批记录和验收报告。

## 首发产品范围

服务市场首发建议只提供只读经营能力：

- 店铺授权、商品、订单、库存、售后数据同步。
- 今日任务、风险提醒、货架商品诊断、直播复盘。
- 内容素材记录、经营日报、飞书/钉钉通知。
- 多店铺隔离、负责人、执行记录和复盘。

千川预算调整、计划启停、网页自动点击等能力不进入首发版。只有取得对应官方权限并完成单独安全验收后，才允许设计平台 API 写操作。

## 发布验收

必须至少满足：

- `/marketplace/readiness` 无阻塞项。
- 无浏览器扩展也能完成订购、授权、首次同步、诊断和日报。
- 跨租户、跨店铺 Token 和数据访问测试 100% 拒绝。
- OAuth state 过期、重放和店铺不一致测试 100% 拒绝。
- Secret/Token 不出现在响应、日志、异常追踪和前端包。
- 授权撤销、数据导出、数据删除和套餐到期流程验收通过。
- Open API 签名、频控、重试、幂等和平台错误码通过真实测试店铺联调。
