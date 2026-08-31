const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = __dirname;
const background = fs.readFileSync(path.join(root, "background.js"), "utf8");
const popupHtml = fs.readFileSync(path.join(root, "popup.html"), "utf8");
const popupJs = fs.readFileSync(path.join(root, "popup.js"), "utf8");
const welcomeHtml = fs.readFileSync(path.join(root, "welcome.html"), "utf8");
const welcomeJs = fs.readFileSync(path.join(root, "welcome.js"), "utf8");
const sidepanelHtml = fs.readFileSync(path.join(root, "sidepanel.html"), "utf8");
const sidepanelJs = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");
const sidepanelCss = fs.readFileSync(path.join(root, "sidepanel.css"), "utf8");

assert.match(popupHtml, /60 秒自动投放体验[\s\S]*id="demo-button"/);
assert.match(welcomeHtml, /60 秒自动投放体验[\s\S]*id="try-auto-delivery"/);
assert.match(popupJs, /openWorkbench\("chengfang-demo"\)/);
assert.match(popupJs, /type: "open-workbench", route/);
assert.match(welcomeJs, /openWorkbench\("chengfang-demo"\)/);

assert.match(background, /function workbenchUrlForRoute\(route = ""\)/);
assert.match(background, /route === "chengfang-demo"/);
assert.match(background, /message\.type === "open-workbench" \|\| message\.type === "platform-assistant-open-workbench"/);
assert.match(sidepanelJs, /const CHENGFANG_DEMO_ENTRY_ROUTE = "chengfang-demo"/);
assert.match(sidepanelJs, /currentRole = "直播投放"/);
assert.match(sidepanelJs, /setPromotionView\("chengfang"\)/);
assert.match(sidepanelJs, /id="chengfang-one-click-demo"|getElementById\("chengfang-one-click-demo"\)/);
assert.match(sidepanelJs, /scrollIntoView\(\{ behavior: "smooth", block: "center" \}\)/);
assert.match(sidepanelJs, /chengfang-one-click-demo-run/);
assert.match(sidepanelCss, /\.chengfang-one-click-demo\.activation-entry-focus/);

assert.match(popupJs, /Promise\.allSettled\(\[/);
assert.match(popupJs, /renderApiUnavailable/);
assert.match(popupJs, /本地 Agent、网页同步和安全演示仍可使用/);
assert.doesNotMatch(popupJs, /const \[status, sync\] = await Promise\.all\(\[/);
assert.match(welcomeJs, /Promise\.all\(\[fetchStores\(\), hasOpenDoudianTab\(\)\]\)/);
assert.match(welcomeJs, /function welcomeStoreState\(/);
assert.match(welcomeJs, /尚未打开抖店，请先登录并保持页面打开/);
assert.match(welcomeJs, /抖店已连接，可以开始巡店/);
assert.match(welcomeHtml, /打开工作台开始巡店[\s\S]*开始快速巡店[\s\S]*四个核心页面/);
assert.doesNotMatch(welcomeHtml, /一键同步/);
assert.doesNotMatch(welcomeJs, /storeStatus\.textContent = `已识别 \$\{stores/);
assert.doesNotMatch(welcomeJs, /bridgeAuthClient\.clearSession\(/, "welcome must not multiply bridge-auth's bounded 401 retry");
assert.match(welcomeJs, /function recoverInvalidatedExtensionContext\(/);
assert.match(welcomeJs, /认证会话已失效，自动刷新失败/);

assert.match(sidepanelHtml, /id="connection-guide"[^>]*technical-compatibility-only[^>]*hidden[^>]*aria-hidden="true"/);
assert.doesNotMatch(sidepanelHtml, /data-workspace-target="connection-guide"/);
assert.doesNotMatch(sidepanelHtml, /data-workspace-key="store-connection"/);
assert.match(sidepanelHtml, /打开抖店后自动确认并巡店，不需要识别、绑定或填写账号/);

assert.match(sidepanelHtml, /id="unlinked-account-evidence"[\s\S]*匿名尾号、证据来源、置信度和最后发现时间/);
assert.match(sidepanelJs, /function qianchuanAccountEvidence/);
assert.match(sidepanelJs, /匿名账户 \$\{tail\} · \$\{source\} · 置信度\$\{confidence\}/);
assert.match(sidepanelJs, /已选账户证据：匿名尾号/);
assert.match(sidepanelJs, /account\.evidence_source/);
assert.match(sidepanelJs, /account\.last_seen/);
assert.match(sidepanelJs, /renderSelectedQianchuanAccountEvidence/);

console.log("activation entry tests passed");
