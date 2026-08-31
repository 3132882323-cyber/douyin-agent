const assert = require("assert");
const fs = require("fs");
const path = require("path");
const governance = require("./material-governance.js");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const sidepanelJs = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const buildScript = fs.readFileSync(path.join(__dirname, "..", "build_extension.ps1"), "utf8");
assert.match(html, /id="material-governance-status"/);
assert.match(html, /id="material-governance-save"/);
assert.match(html, /<script src="material-governance\.js"><\/script>\s*<script src="promotion-plan-center\.js"><\/script>\s*<script src="promotion-bulk-actions\.js"><\/script>\s*<script src="promotion-operation-log\.js"><\/script>\s*<script src="product-capability\.js"><\/script>\s*<script src="simple-experience\.js"><\/script>\s*<script src="journey-orchestrator\.js"><\/script>\s*<script src="sidepanel\.js"><\/script>/);
assert.match(sidepanelJs, /function renderMaterialGovernance\(/);
assert.match(buildScript, /"material-governance\.js"/);

const creative = {
  data_status: "ready",
  governance_policy: {
    min_spend_for_action: 100,
    roi_target: 1.5,
    min_orders_for_scale: 3,
    data_window: "current_page_filter",
    data_window_label: "当前千川页面筛选范围",
    source_label: "本地 Agent 经营设置与当前素材快照",
  },
  videos: [
    { id: "winner-1", name: "胜出素材 A", status: "可复制放量", level: "opportunity", funnel_stage: "scalable", confidence: "high", evidence: { spend: 500, roi: 2.2, orders: 6 } },
    { id: "risk-1", name: "高风险素材 B", status: "高消耗低转化", level: "high", funnel_stage: "hook", confidence: "high", evidence: { spend: 300, roi: 0, orders: 0 } },
    { id: "hook-1", name: "钩子素材 C", status: "观察中", level: "info", funnel_stage: "hook", confidence: "high", evidence: { spend: 150, roi: 0.8, orders: 1 } },
    { id: "conversion-1", name: "承接素材 D", status: "观察中", level: "info", funnel_stage: "conversion", confidence: "high", evidence: { spend: 180, roi: 0.7, orders: 0 } },
    { id: "untested-1", name: "待测素材 E", status: "尚未测试", level: "warning", funnel_stage: "untested", confidence: "medium", evidence: { spend: 0, roi: null, orders: 0 } },
    { id: "potential-1", name: "高潜素材 F", status: "高潜素材", level: "opportunity", funnel_stage: "learning", confidence: "medium", evidence: { spend: 30, roi: null, orders: 0 } },
    { id: "observe-1", name: "观察素材 G", status: "观察中", level: "info", funnel_stage: "learning", confidence: "medium", evidence: { spend: 60, roi: 1.1, orders: 1 } },
  ],
};

const scope = { store_key: "store-001", account_key: "advertiser-ABC123" };
const preview = governance.deriveGovernance({ creative, scope });
assert.equal(preview.can_apply, true);
assert.equal(preview.affected_material_count, 7);
assert.equal(preview.protected_count, 1);
assert.equal(preview.retire_candidate_count, 1);
assert.equal(preview.retest_count, 4);
assert.equal(preview.observe_count, 1);
assert.equal(preview.package.automatic_delete_enabled, false);
assert.equal(preview.package.platform_write_enabled, false);
assert.equal(preview.package.execution_kind, "classification_preview_only");
assert.match(preview.package.config_fingerprint, /^mat-[a-f0-9]{8}$/);
assert.equal(JSON.stringify(preview.package).includes("胜出素材 A"), false, "package must not persist material names");
assert.equal(JSON.stringify(preview.package).includes("winner-1"), false, "package must not persist source material ids");
assert.equal(JSON.stringify(preview.package).includes("store-001"), false, "package must not persist raw store keys");
assert.equal(JSON.stringify(preview.package).includes("advertiser-ABC123"), false, "package must not persist raw account keys");
const risk = preview.classification.items.find((item) => item.source_id === "risk-1");
assert.equal(risk.bucket, "retire", "higher-priority stop rule must win before hook retest");

const applied = governance.deriveGovernance({ creative, scope, appliedPackage: preview.package });
assert.equal(applied.already_applied, true);
assert.equal(applied.can_apply, false);
assert.equal(applied.changed_count, 0);
assert.equal(applied.package.revision, 1);

const missingPolicy = governance.deriveGovernance({ creative: { data_status: "ready", videos: creative.videos }, scope });
assert.equal(missingPolicy.can_apply, false);
assert.ok(missingPolicy.blockers.length >= 3);
assert.equal(missingPolicy.classification.items.find((item) => item.source_id === "winner-1").bucket, "observe");

const lowConfidenceRisk = governance.deriveGovernance({
  creative: { ...creative, videos: [{ ...creative.videos[1], confidence: "medium", funnel_stage: "learning" }] },
  scope,
});
assert.equal(lowConfidenceRisk.retire_candidate_count, 0);
assert.equal(lowConfidenceRisk.observe_count, 1);

console.log("material governance tests passed");
