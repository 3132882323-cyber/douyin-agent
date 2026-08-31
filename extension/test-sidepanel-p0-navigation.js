const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(html, /data-workspace-key="controlled-execution"[\s\S]*?data-workspace-target="automation-section"[\s\S]*?<strong>执行准备<\/strong>/);
assert.match(html, /id="automation-section"/);
assert.match(script, /function navigateToWorkspaceElement\(targetOrId, options = \{\}\)/);
assert.match(script, /function revealModuleByChildId[\s\S]*?navigateToWorkspaceElement\(section/);
assert.match(script, /function revealChengfangCandidatePathTarget[\s\S]*?\["bind_scope", "resolve_binding"\][\s\S]*?navigateToWorkspaceElement\("promotion-plan-center"/);
assert.doesNotMatch(script, /navigateToWorkspaceElement\("connection-guide"/);
assert.match(script, /async function runPromotionPlanScopeRecovery[\s\S]*?if \(!selectedStoreKey\) \{[\s\S]*?await ensureCurrentStoreForScan\(\);[\s\S]*?if \(!selectedStoreKey\) \{[\s\S]*?STORE_SELECTION_UNCONFIRMED/);
assert.match(html, /id="connection-guide"[^>]*technical-compatibility-only[^>]*hidden[^>]*aria-hidden="true"/);
assert.doesNotMatch(html, /data-workspace-target="connection-guide"|data-workspace-key="store-connection"/);
assert.match(script, /autopilot-center-review[\s\S]*?navigateToWorkspaceElement\("chengfang-trial-console"/);
assert.match(script, /!isConfirmedNow && params\.state === "confirmed"[\s\S]*?navigateToWorkspaceElement\("automation-section"/);
assert.match(script, /function navigatePromotionView\(view = "overview"\)[\s\S]*?\.\.\.route\.filters[\s\S]*?renderPromotionPlanConsole\(\)[\s\S]*?navigateToWorkspaceElement\(route\.target_id/);
assert.match(script, /route\.target_id === "promotion-plan-center" \? "promotion-overview" : ""/);
assert.match(script, /\[data-promotion-view\][\s\S]*?navigatePromotionView\(button\.dataset\.promotionView \|\| "overview"\)/);
assert.doesNotMatch(script, /\[data-promotion-view\][\s\S]{0,180}?setPromotionView\(button\.dataset\.promotionView \|\| "overview"\)/);

assert.match(html, /id="agent-offline-banner"[\s\S]*?id="sidepanel-repair-agent"[\s\S]*?修复本地 Agent/);
assert.match(script, /const AGENT_WRITE_CONTROL_IDS = Object\.freeze/);
assert.match(script, /body\.classList\.toggle\("agent-offline", !online\)/);
assert.match(script, /function agentWriteGateOpen\(\)[\s\S]*?agentConnectionState === "online"/);
assert.match(script, /!agentWriteGateOpen\(\) && method !== "GET"/);
assert.match(script, /let agentConnectionState = "checking"/);
assert.match(script, /type: "repair-agent"/);
assert.doesNotMatch(script, /enable_autostart\.bat/);
assert.match(css, /body\.agent-offline \[data-agent-write\][^{]*\{[^}]*pointer-events:\s*none/s);

console.log("sidepanel P0 navigation and offline safety tests passed");
