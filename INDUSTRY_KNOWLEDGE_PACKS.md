# 行业知识包指南

行业知识包让同一台店策 Agent 为不同店铺使用不同的经营判断，同时保留通用安全规则。它不是 AI 提示词，也不是可执行插件：包内只能包含经过白名单校验的确定性规则数据。

## 用户怎么用

1. 在工作台选择当前店铺。
2. 打开“版本与更新 → 当前店铺行业判断”。
3. 选择“通用电商、服饰鞋包、美妆个护或食品生鲜”。
4. 查看版本、规则数、能力范围和信任状态，点击“应用到当前店铺”。
5. 重新巡检一次；命中的任务证据会标明规则来自通用层还是行业层。

不同店铺的绑定保存在本机 `knowledge/bindings.json`，只记录不可逆 `store_key`、包 ID、版本、哈希和操作时间，不保存店名。未选择店铺时不能绑定。

## 组合规则

```text
通用电商基础包（始终保留）
        ↓ 按 rule_id 确定性叠加
当前店铺行业包（最多一个）
        ↓
本店有效规则集
```

- 行业包默认只能新增规则。只有通用包明确标记 `overridable: true` 的普通规则才允许被同 `rule_id` 调整。
- 所有未开放覆盖的通用规则（不只 `system.*`）都不能被行业包覆盖、关闭，行业包也不能借用相同 `dedupe_key` 把它们隐藏。
- 包过期、损坏、签名失效、Agent 版本不兼容或组合校验失败时，该店回退通用包；其他店铺不受影响。
- 知识包只生成诊断和建议。预算、启停等资金动作继续走独立授权、执行前检查、额度、冷却和回读流程。

## 包格式

当前兼容 schema v1，并在原有签名字段上增加行业清单字段：

```json
{
  "schema_version": 1,
  "pack_id": "partner.apparel",
  "industry": "apparel",
  "display_name": "服饰鞋包经营知识包",
  "publisher": "合作方名称",
  "channel": "stable",
  "min_agent_version": "4.6.0",
  "max_agent_version": "5.0.0",
  "pack_version": "2026.08.22.1",
  "published_at": "2026-08-22T00:00:00+08:00",
  "expires_at": "2027-08-22T00:00:00+08:00",
  "capabilities": ["尺码库存", "素材衰减"],
  "required_metrics": ["inventory.available", "roi"],
  "rules": [],
  "sha256": "64 位小写十六进制",
  "signature": "Ed25519 签名"
}
```

`pack_id`、`industry` 和店铺键只允许安全 ASCII slug；同一 `pack_id + pack_version` 不允许出现两个不同哈希。自定义文件上限 2 MB，导入后只安装、不自动启用。

当前行业规则可使用的稳定事实字段只有 `spend`、`roi`、`data_age_minutes`、`inventory.available`、`sales.last_24h`；可使用的设置只有 `min_spend`、`roi_target`、`inventory_warning_line`、`max_data_age_minutes`。`required_metrics` 必须覆盖规则实际引用的全部事实字段，未知字段、错拼字段、嵌套表达式中的未知字段和错拼设置都会被判为不兼容，避免出现“显示已生效但永远不命中”。

现阶段库存和订单事实是全店汇总级，因此内置行业规则只会提示“进入商品/计划页进一步核对”，不会把汇总值冒充为具体 SKU、尺码、赠品、批次或保质期结论。

## 本机目录与接口

- 已安装包：`knowledge/packs/<pack_id>/<version>-<sha12>.json`
- 每店绑定：`knowledge/bindings.json`
- 查询目录：`GET /rules/packs`
- 验签安装：`POST /rules/packs/import`（仅当前已配对扩展）
- 应用到本店：`POST /rules/packs/bind`（仅当前已配对扩展）

自定义导入必须配置可解码且运行时可验证的 Ed25519 公钥；Agent 会真实探测密钥和验签能力，而不是只判断环境变量是否非空。没有生产信任锚时，界面会保持导入禁用，但三个内置行业包仍可直接选择。正式商业发行应把 `key_id → 生产公钥` 信任环嵌入签名安装包，并保留密钥轮换与撤销机制。

行业包安装和绑定还要求浏览器扩展身份已被批准。正式版使用内嵌的官方商店 ID；离线安装和源码调试都必须先由安装脚本根据当前 `manifest.json` 写入 `config/trusted_extension_ids.json`。未知扩展不能通过自报 ID 或环境变量完成首次配对。

## GitHub 方案取舍

实现调研只采用宽松许可项目的成熟边界，没有直接引入重型服务或来源不明的行业数据：

- [Open Policy Agent bundles](https://github.com/open-policy-agent/opa/blob/main/docs/docs/management-bundles/index.md)（Apache-2.0）：借鉴命名 bundle、签名 key、持久化后离线恢复、状态可见和多来源隔离。
- [Home Assistant integration manifest](https://github.com/home-assistant/core/blob/dev/homeassistant/loader.py)（Apache-2.0）：借鉴 `domain / version / dependencies / requirements / quality` 形式的清单身份与兼容性表达。
- [RAGFlow knowledge-base service](https://github.com/infiniflow/ragflow/blob/main/api/db/services/knowledgebase_service.py)（Apache-2.0）：借鉴知识库按租户归属、访问隔离和不可用状态检查。
- [Backstage catalog descriptor](https://github.com/backstage/backstage/blob/master/docs/features/software-catalog/descriptor-format.md)（Apache-2.0）：借鉴稳定 `apiVersion/kind/metadata/spec` 信封思路，为后续 schema v2 做准备。

没有直接复用 Open WebUI，因为其仓库许可证不是标准宽松许可证标识；也没有引入 Dify、RAGFlow 的运行时或向量数据库，因为本功能必须在不连接 AI、不中转店铺数据时仍能工作。
