const fs = require("fs");
const path = require("path");
const assert = require("assert");

const root = __dirname;
const html = fs.readFileSync(path.join(root, "sidepanel.html"), "utf8");
const js = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");
const css = fs.readFileSync(path.join(root, "sidepanel.css"), "utf8");

assert.match(html, /data-owner="直播投放"[\s\S]*data-promotion-view="chengfang">千川乘方/);
assert.match(html, /千川乘方当前仅提供只读诊断/);
assert.match(html, /不得使用旧版单计划预算、暂停或恢复功能/);
assert.match(js, /bridgeFetch\("\/qianchuan\/promotion-readiness"\)/);
assert.match(js, /field\.status !== "present"[\s\S]*return "待同步"/);
assert.match(js, /不展示猜测的利润、预算或 ROI/);
assert.match(css, /\.chengfang-notice[\s\S]*\.chengfang-status-grid/);
assert.doesNotMatch(html, /id="chengfang-[^"]*"[^>]*>[^<]*(自动执行|立即调整预算)/);

console.log("sidepanel chengfang visibility tests passed");
