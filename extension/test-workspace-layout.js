const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(html, /class="workspace-shell"/);
assert.match(html, /id="workspace-sidebar"/);
assert.match(html, /class="workspace-topbar"/);
assert.match(html, /class="workspace-page-heading"/);
assert.match(html, /id="role-nav" class="workspace-nav"/);
assert.match(html, /data-workspace-target="oceanengine-oauth-card"/);
assert.match(html, /data-workspace-target="promotion-plan-center"/);
assert.match(html, /id="promotion-mode-workbench"/);
assert.match(html, /data-nav-tree="system"/);
assert.doesNotMatch(html, /data-nav-tree="promotion"/);
assert.doesNotMatch(html, /data-nav-tree="(?:full-domain|chengfang|suixintui)"/);
assert.match(html, /data-workspace-key="live-business"[^>]*data-workspace-target="live-plan-management-section"/);
assert.match(html, /class="workspace-nav-children live-workspace-children"/);
assert.match(html, /id="promotion-plan-open-bulk"/);
assert.match(html, /id="promotion-plan-batch-shortcut"/);
assert.equal((html.match(/>直播与投放<\/strong>/g) || []).length, 1);
assert.match(html, /data-workspace-target="report-center-card"/);
assert.match(html, /id="strategy-settings-card"/);
assert.match(html, /id="report-center-card"/);
assert.match(html, /id="scan-card"/);
assert.match(html, /id="content-workbench"/);
assert.match(html, /id="live-plan-management-section"/);
assert.match(html, /id="product-plan-management-section"/);
assert.match(html, /id="batch-plan-preparation"/);
assert.match(html, /id="promotion-plan-center"/);
assert.match(html, /id="promotion-plan-table-body"/);
assert.match(html, /id="workspace-user-path"/);
assert.match(html, /id="qianchuan-sync-dock-close"[^>]*aria-label="关闭千川快速同步悬浮窗"/);
assert.match(html, /id="qianchuan-sync-dock-collapse"[^>]*aria-expanded="true"/);
assert.match(html, /id="qianchuan-sync-dock-restore"[^>]*hidden/);
assert.match(html, /id="qianchuan-sync-dock-announcer"[^>]*role="status"/);
assert.match(html, /id="journey-command-center"/);
assert.match(html, /id="journey-primary-action"/);
assert.match(html, /src="journey-orchestrator\.js"/);
assert.match(html, /id="promotion-plan-advanced-filters"/);
assert.match(html, /src="promotion-plan-center\.js"/);

const roleButtons = [...html.matchAll(/data-role="([^"]+)"/g)].map((match) => match[1]);
assert.deepEqual(roleButtons, ["货架商品", "直播投放", "内容"]);

const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
assert.deepEqual(duplicateIds, [], `duplicate ids: ${duplicateIds.join(", ")}`);

assert.match(css, /grid-template-columns:\s*208px minmax\(0, 1fr\)/);
assert.match(css, /\.workspace-topbar\s*\{[^}]*position:\s*sticky/s);
assert.match(css, /\.workspace-stage \.panel-shell > #oceanengine-oauth-card,[\s\S]*order:\s*80/);
assert.match(css, /@media \(max-width: 1080px\)/);
assert.match(css, /@media \(max-width: 700px\)/);
assert.match(css, /\.workspace-nav-tree\.is-expanded/);
assert.match(css, /\.workspace-nav-children button\.tool-active/);
assert.match(css, /\.live-workspace-children/);
assert.match(css, /\.batch-plan-readiness/);
assert.match(css, /\.promotion-plan-toolbar/);
assert.match(css, /\.promotion-plan-table/);
assert.match(css, /\.workspace-user-path/);
assert.match(css, /\.workspace-subfocus-hidden/);
assert.match(css, /\.journey-command-center/);
assert.match(css, /\.journey-orchestrated[^}]*#workspace-user-path/s);
assert.match(css, /\.qianchuan-sync-dock-close\s*\{/);
assert.match(css, /\.qianchuan-sync-dock\.compact \.qianchuan-sync-dock-main\s*\{/);
assert.match(css, /\.workspace-more-settings\s*\{[^}]*display:\s*block/s);
assert.doesNotMatch(css, /body\[data-experience-mode="simple"\] #qianchuan-sync-dock/);

assert.match(script, /function activateWorkspaceRole\(/);
assert.match(script, /function navigateWorkspaceTarget\(/);
assert.match(script, /function toggleWorkspaceNavTree\(/);
assert.match(script, /function setWorkspacePageContext\(/);
assert.match(script, /function renderWorkspaceUserPath\(/);
assert.match(script, /function workspacePathStage\(/);
assert.match(script, /function renderPromotionPlanConsole\(/);
assert.match(script, /selected_store_key:[\s\S]*selectedStoreKey/);
assert.match(script, /store_key: selectedStoreKey,[\s\S]*account_key: selectedQianchuanAccount/);
assert.match(script, /filterDraftsForScope\(/);
assert.match(script, /row\.binding \|\| row\.saved_binding/);
assert.match(script, /row\.freshness_label[\s\S]*row\.data_age_label/);
assert.match(script, /async function refreshPromotionPlanConsole[\s\S]*?if \(syncPage\) await syncRecentQianchuanPage\(\{[\s\S]*?expectedPageTypes:/);
assert.match(script, /function generatePromotionBatchDraft\(/);
assert.match(script, /workspace-context-label/);
assert.match(script, /workspace-primary-action/);
assert.match(script, /function renderJourneyCommand\(/);
assert.match(script, /function runJourneyPrimaryAction\(/);
assert.match(script, /QIANCHUAN_SYNC_DOCK_HIDDEN_KEY\s*=\s*"qianchuanSyncDockHiddenV1"/);
assert.match(script, /QIANCHUAN_SYNC_DOCK_COMPACT_KEY\s*=\s*"qianchuanSyncDockCompactV1"/);
assert.match(script, /function setQianchuanSyncDockHidden\(/);
assert.match(script, /function setQianchuanSyncDockCompact\(/);
assert.match(script, /qianchuan-sync-dock-close[\s\S]*setQianchuanSyncDockHidden\(true, \{ persist: true \}\)/);
assert.match(script, /qianchuan-sync-dock-restore[\s\S]*setQianchuanSyncDockHidden\(false, \{ persist: true \}\)/);
assert.match(script, /templateChecks = \{ \.\.\.stored\.templateChecks \}/);
assert.doesNotMatch(script, /todayScope[\s\S]{0,180}chrome\.storage\.local\.set\(\{ templateChecks \}\)/);
assert.match(script, /if \(previousStoreKey !== activeKey\) renderWorkbench\(\)/);

const checklistStart = script.indexOf("function templateCheckDateFromKey");
const checklistEnd = script.indexOf("\nfunction templateCheckKey", checklistStart);
assert.ok(checklistStart >= 0 && checklistEnd > checklistStart, "checklist retention helpers must be extractable");
const checklistSandbox = {};
vm.runInNewContext(script.slice(checklistStart, checklistEnd), checklistSandbox);
const retained = checklistSandbox.pruneTemplateChecksForDate({
  "store-a:2026-08-25:daily:货架商品:task-a": true,
  "store-b:2026-08-25:daily:直播投放:task-b": true,
  "store-a:2026-08-24:daily:货架商品:old-task": true,
}, "2026-08-25");
assert.deepEqual(
  Object.keys(retained).sort(),
  [
    "store-a:2026-08-25:daily:货架商品:task-a",
    "store-b:2026-08-25:daily:直播投放:task-b",
  ],
  "today's checklist must survive for every store while older dates are pruned",
);
assert.match(script, /if \(templateChecks\[key\]\) delete templateChecks\[key\];[\s\S]{0,500}pruneTemplateChecksForDate\(templateChecks\)/);

console.log("workspace layout tests passed");
