# 免费插件模块 · 2026-10-27

本目录只包含自有免费插件的独立逻辑及合成测试，沿用仓库根目录的 [MIT 许可证](../../LICENSE)。它不是完整插件安装包，也不更改现有发行版本或默认权限。

## 本次公开范围

| 模块 | 能力与边界 |
| --- | --- |
| `product-package-queue.js` | 显式批量读取、本机保存、暂停和停止；超时不等于底层请求已取消。未知保存可只读核对同版本，不重新保存或宣称本次请求成功。支持显式的已保存草稿资料来源（`doudian_draft_product`），来源与店铺上下文仍须核验。需要调用方提供读取、本机保存与来源检查适配器。 |
| `category-required-attribute-check-core.js` | 检查同商品已保存资料中的必填属性，区分已观察、空、未报告、不可读、冲突和条件未知。不自动填写，不替代平台完整校验。 |
| `complex-sku-native-dom-reader.js` | 只读识别已观察的草稿二维 2×2 虚拟合并表布局。明确库存是增减语义，不当成绝对库存；临时行键不作为 SKU 身份。仅确认当前呈现组合，不代表任意规格、全店完整或写入权限。 |
| `shelf-tools-core.js` | 解析、检查和去重已有官方链接文本，整理商品行、CSV 字符串和标题/资料检查结果。不展开短链接，不按商品编号拼接链接，不获取商品页面或授权商家操作。 |
| `shelf-detail-core.js` | 根据调用方传入的当前可见行，整理精确商品编号对应的资料、已有图片地址及允许的属性；返回资料 CSV、图片清单 CSV 或复制文本。最多接收 200 行，汇总最多 100 张图片，不读取页面、不加载图片，也不推断完整商品详情。 |
| `product-move-core.js` | 校验商品资料、显式类目/运费映射及规则配置，生成价格、库存和规格处理的本地预览，提供本地任务状态与批次去重。支持编辑商品、商品列表和显式已保存草稿资料来源，不提供平台连接器、保存或发布。 |

没有包含投放、商业策略、收费服务、私有后台操作链路、账号凭据、真实店铺/订单数据、截图、备份、竞品程序或内部拆解报告。仅 `complex-sku-native-dom-reader.js` 在适用页面中只读检查 DOM；本次新增的三个核心模块只处理传入数据，不联网或持久化。调用方仍须实施权限、身份和来源核验。

## 本地输出与商家操作

- 本地预览不是商家操作。`title_replace` 及价格、库存、规格规则会改变预览结果，不修改传入对象或平台商品。三个新增核心模块不填写平台表单、保存草稿或上传发布商品；队列的本机资料保存由调用方提供的适配器处理。
- `business_ready`、`localpreviewready` 与 `DECLARED_COMPLETE` 只表示按传入资料、映射及完整性声明通过本地检查，不证明资料完整、实时、平台校验通过或具有执行权限。
- CSV、图片清单和复制文本仅以字符串返回；相应核心模块不下载图片，也不创建或保存文件。需要由调用方另行提供文件保存或下载入口。
- 输出不会自动脱敏。传入的商品编号、商家编码、店名和上下文等内容可能被保留；调用方必须在输入、展示和导出前自行确认数据范围。链接中的敏感参数检查不是通用的秘密清洗器。
- 测试中的商品、店铺上下文、编号、素材路径和回执均为合成样例；官方与 CDN 地址只用于本地格式校验，测试不请求这些地址。

## 测试

使用支持 CommonJS、TextEncoder 和现代 JavaScript 的 Node.js，运行：

```sh
node community/free-plugin/extension/test-product-package-queue.js
node community/free-plugin/extension/test-product-package-queue-recovery.js
node community/free-plugin/extension/test-category-required-attribute-check-core.js
node community/free-plugin/extension/test-complex-sku-native-dom-reader.js
node community/free-plugin/extension/test-shelf-tools-core.js
node community/free-plugin/extension/test-shelf-detail-public.js
node community/free-plugin/extension/test-product-move-core.js
node community/free-plugin/extension/test-product-move-price-rule.js
```

可选浏览器测试需要本机已有 Chrome，以及测试环境安装的 Playwright；不作为模块运行依赖：

```sh
node community/free-plugin/tools/smoke_complex_sku_native_dom_reader.cjs
```

浏览器测试拦截导航并返回合成页面，不连接真实商家。测试通过不等于已安装、已接通用户界面、实店操作通过或所有参考插件功能已经完成。原生编辑页可能自动保存输入为草稿；“未点击保存”不证明没有持久化。本次公开的 DOM 模块不填写、保存或发布。

## 集成约束

不要把只读输出或调用方传入的布尔值当作执行授权。业务适配器、页面入口和平台写入需要独立验证，本目录不提供这些权限。保留未知结果、过期、停止后续与晚到回执边界，避免通过销毁队列重建任务绕过未结束请求保护。

上传清单见 [PUBLIC_FILES.json](PUBLIC_FILES.json)，后续新增文件应先确认属于免费公开范围。
