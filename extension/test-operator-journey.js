const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(script, /const QUICK_SCAN_PAGE_IDS = Object\.freeze\(DianAgentScanScopePolicy\.pageIds\("core_doudian"\)\)/);
assert.match(html, /<script src="scan-scope-policy\.js"><\/script>/);
assert.match(script, /async function runQuickScan/);
assert.match(script, /function normalizeCoreRefreshPageIds/);
assert.match(script, /scan_scope: "quick"[\s\S]*page_ids: pageIds/);
assert.doesNotMatch(script, /workspace-primary-action"\)\.addEventListener[\s\S]{0,800}scanButton\.click/);
assert.match(script, /workspace-primary-action"\)\.addEventListener[\s\S]{0,800}runQuickScan/);
assert.match(html, /id="full-scan-button">开始全店巡检/);
assert.match(html, /data-role="直播投放"[^>]*data-workspace-target="live-plan-management-section"/);
assert.match(css, /journey-orchestrated:not\(\.onboarding-complete\)\[data-experience-mode="simple"\] #journey-command-center/);
assert.match(css, /journey-orchestrated\.onboarding-complete #simple-start/);
assert.match(script, /classList\.toggle\("onboarding-complete", journey\.complete\)/);

console.log("operator journey tests passed");
