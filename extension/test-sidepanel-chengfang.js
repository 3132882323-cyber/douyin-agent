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
assert.match(html, /经营测算与诊断/);
assert.match(html, /测算，不是平台预测/);
assert.match(html, /商品乘方资格[\s\S]*增长瓶颈[\s\S]*7 天影子观察/);
assert.match(html, /src="chengfang-planner\.js"/);
assert.match(js, /chengfangLocalPlanningV1/);
assert.match(html, /首次使用 · 经营建档/);
assert.match(html, /今日唯一动作/);
assert.match(html, /7 天影子观察/);
assert.match(html, /L0 · 只读诊断[\s\S]*L3 · 受控执行/);
assert.match(html, /重置本机建档/);
assert.match(html, /3 分钟使用教程/);
assert.match(js, /validateBoundaries/);
assert.match(js, /appendDailyShadow/);
assert.doesNotMatch(html, /平台费用率（%）[^<]*<input[^>]+value=/);
assert.doesNotMatch(html, /id="chengfang-[^"]*"[^>]*>[^<]*(自动执行|立即调整预算)/);

console.log("sidepanel chengfang visibility tests passed");
