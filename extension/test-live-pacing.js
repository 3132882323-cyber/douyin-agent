const assert = require("assert");
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(html, /id="live-pacing-card"/);
assert.match(html, /id="live-pacing-spend-rate"/);
assert.match(html, /id="live-pacing-order-rate"/);
assert.match(html, /id="live-pacing-intervals"/);
assert.match(html, /id="live-pacing-sync"/);
assert.match(html, /id="live-pacing-sync-result" role="status"/);
assert.match(html, /读取当前直播大屏/);
assert.match(html, /不自动修改预算、时长或计划状态/);
assert.match(css, /\.live-pacing-decision\.high/);
assert.match(css, /\.live-pacing-intervals/);
assert.match(css, /data-experience-mode="simple"[^}]*#live-pacing-card[^}]*display:\s*block\s*!important/);
assert.match(script, /function renderLivePacing\(pacing = \{\}\)/);
assert.match(script, /renderLivePacing\(live\.pacing \|\| \{\}\)/);
assert.match(script, /getElementById\("live-pacing-sync"\)\.addEventListener/);
assert.match(script, /resultNode\.textContent = `读取失败：/);
assert.match(script, /只读，不自动修改预算、时长或状态/);

console.log("live pacing UI tests passed");
