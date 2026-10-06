# 免费插件模块 · 2026-10-06

本目录只包含自有免费插件的独立逻辑及合成测试，沿用仓库根目录的 [MIT 许可证](../../LICENSE)。它不是完整插件安装包，也不更改现有发行版本或默认权限。

## 本次公开范围

| 模块 | 能力与边界 |
| --- | --- |
| `product-package-queue.js` | 显式批量读取、本机保存、暂停和停止；超时不等于底层请求已取消。未知保存可只读核对同版本，不重新保存或宣称本次请求成功。需要调用方提供读取、本机保存与来源检查适配器。 |
| `category-required-attribute-check-core.js` | 检查同商品已保存资料中的必填属性，区分已观察、空、未报告、不可读、冲突和条件未知。不自动填写，不替代平台完整校验。 |
| `complex-sku-native-dom-reader.js` | 只读识别已观察的草稿二维 2×2 虚拟合并表布局。明确库存是增减语义，不当成绝对库存；临时行键不作为 SKU 身份。仅确认当前呈现组合，不代表任意规格、全店完整或写入权限。 |

没有包含投放、商业策略、收费服务、私有后台操作链路、账号凭据、真实店铺/订单数据、截图、备份、竞品程序或内部拆解报告。浏览器模块在适用页面中读取 DOM，不携带 Cookie 或密钥；调用方仍须实施权限、身份和来源核验。

## 测试

使用支持 CommonJS、TextEncoder 和现代 JavaScript 的 Node.js，运行：

```sh
node community/free-plugin/extension/test-product-package-queue.js
node community/free-plugin/extension/test-product-package-queue-recovery.js
node community/free-plugin/extension/test-category-required-attribute-check-core.js
node community/free-plugin/extension/test-complex-sku-native-dom-reader.js
```

可选浏览器测试需要本机已有 Chrome，以及测试环境安装的 Playwright；不作为模块运行依赖：

```sh
node community/free-plugin/tools/smoke_complex_sku_native_dom_reader.cjs
```

浏览器测试拦截导航并返回合成页面，不连接真实商家。测试通过不等于已安装、已接通用户界面或所有参考插件功能已经完成。原生编辑页可能自动保存输入为草稿；“未点击保存”不证明没有持久化。本次公开的 DOM 模块不填写、保存或发布。

## 集成约束

不要把只读输出或调用方传入的布尔值当作执行授权。业务适配器、页面入口和平台写入需要独立验证，本目录不提供这些权限。保留未知结果、过期、停止后续与晚到回执边界，避免通过销毁队列重建任务绕过未结束请求保护。

上传清单见 [PUBLIC_FILES.json](PUBLIC_FILES.json)，后续新增文件应先确认属于免费公开范围。
