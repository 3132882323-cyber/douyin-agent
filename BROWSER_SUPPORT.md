# 浏览器适配说明

店策 Agent v2.11.0 提供两个扩展包。两者使用相同的采集、分析、数据体检单和安全操作草稿代码，区别只在经营工作台的打开方式。

| 浏览器 | 推荐扩展包 | 工作台方式 | 支持级别 |
|---|---|---|---|
| Google Chrome 116+ | `dian-agent-modern` | 原生侧边栏 | 完整支持 |
| Microsoft Edge 新版 | `dian-agent-modern` | 原生侧边栏或自动降级 | 完整支持 |
| 360 极速浏览器 X | 优先 `dian-agent-modern` | 支持时侧栏，否则新标签页 | 已适配 |
| 360 安全浏览器 | `dian-agent-compatible` | 新标签页 | 兼容支持 |
| QQ 浏览器 Windows 版 | `dian-agent-compatible` | 新标签页 | 已在 21.6.6019.400 实机验证 |
| 搜狗高速浏览器 | `dian-agent-compatible` | 新标签页 | 兼容支持 |
| 联想浏览器 | `dian-agent-compatible` | 新标签页 | 兼容支持 |
| Brave、Arc、Vivaldi、Opera | 优先 `dian-agent-modern` | 支持时侧栏，否则新标签页 | 兼容支持 |

## 生成两个扩展包

在项目根目录双击 `build_browser_packages.bat`，会生成：

- `dist/dian-agent-modern`
- `dist/dian-agent-compatible`

进入浏览器的扩展管理页面，开启开发者模式，然后选择“加载已解压的扩展程序”并选中对应目录。

## 360、QQ、搜狗等双核浏览器注意事项

1. 抖店和巨量千川页面必须使用“极速模式”或 Chromium 内核，不能使用 IE 兼容模式。
2. 如果现代版提示清单不兼容，改装多浏览器兼容版。
3. 兼容版点击“打开经营副驾”后会打开一个固定的新标签页，这是正常行为。
4. 浏览器必须支持 Manifest V3、Service Worker 和 `chrome.scripting`。内核过旧时请先升级浏览器。
5. 所有版本仍只连接本机 `127.0.0.1:8765`，不会把店铺数据上传到第三方服务器。

## 支持边界

国产浏览器经常更新内核或隐藏品牌版本号，因此“兼容支持”表示已移除品牌浏览器常见的不兼容清单项，并提供运行时降级；最终仍需以用户安装的具体浏览器版本实测为准。浏览器若禁用开发者模式、企业策略禁止本地扩展，或使用 IE 兼容内核，扩展无法绕过这些限制。
