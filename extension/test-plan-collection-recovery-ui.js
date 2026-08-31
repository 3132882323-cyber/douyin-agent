const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const promotionPlanPolicy = require("./promotion-plan-center.js");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

[
  "promotion-plan-collection-receipt",
  "promotion-plan-coverage-state-label",
  "promotion-plan-coverage-title",
  "promotion-plan-coverage-detail",
  "promotion-plan-coverage-blocker-label",
  "promotion-plan-coverage-warning",
  "promotion-plan-coverage-action",
].forEach((id) => {
  assert.equal((html.match(new RegExp(`id=["']${id}["']`, "g")) || []).length, 1, `${id} must exist exactly once`);
});
assert.match(html, /id="promotion-plan-coverage-action"[^>]*data-recovery-kind="sync_page"/);
assert.match(css, /\.promotion-plan-coverage-next button\s*\{/);
assert.match(css, /\.promotion-plan-coverage-next button:focus-visible\s*\{/);
assert.match(css, /\.promotion-plan-coverage-evidence\s*\{/);
assert.match(css, /#promotion-plan-center\[data-empty="true"\][\s\S]*?\.promotion-plan-toolbar\s*\{\s*display:\s*none/);

function extractFunction(name, nextName) {
  const start = script.indexOf(`function ${name}`);
  const end = script.indexOf(`\nfunction ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `${name} source must be extractable`);
  return vm.runInNewContext(`(${script.slice(start, end).trim()})`, {
    DianPromotionPlanCenter: promotionPlanPolicy,
  });
}

const recovery = extractFunction("derivePromotionPlanCollectionRecovery", "renderPromotionPlanCollectionReceipt");
const warning = (...codes) => ({ coverage_warnings: codes.map((code) => ({ code })) });

assert.equal(recovery(warning("PLATFORM_TOTAL_CONFLICT", "PAGINATION_TRUNCATED")).kind, "review_account");
assert.equal(recovery(warning("MULTIPLE_TOTAL_SCOPES", "STABLE_PLAN_ID_INCOMPLETE")).kind, "narrow_scope");
assert.equal(recovery(warning("COLLECTED_ROWS_EXCEED_PLATFORM_TOTAL")).label, "选择单一账户与模式");
assert.equal(recovery(warning("PAGINATION_TRUNCATED", "STABLE_PLAN_ID_INCOMPLETE")).label, "继续翻页并读取");
assert.equal(recovery(warning("LOCAL_LIST_TRUNCATED")).label, "缩小模式范围");
assert.equal(recovery(warning("STABLE_PLAN_ID_INCOMPLETE")).label, "补齐稳定计划 ID");
assert.equal(recovery(warning("PROMOTION_MODE_INCOMPLETE")).kind, "narrow_mode");
assert.equal(recovery(warning("PLATFORM_TOTAL_UNOBSERVED")).kind, "sync_page");
assert.equal(recovery({ safe_to_claim_complete: true, collected_rows: 3 }).kind, "focus_account");
assert.equal(
  recovery(
    { safe_to_claim_complete: true, platform_total: 3, collected_rows: 3 },
    {
      exact_scope: true,
      source: "scoped",
      scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" },
      scope_rows: [{ read_only_diagnosis_ready: true, supervised_draft_ready: true, collection_gate_state: "ready" }],
    },
  ).kind,
  "review_plans",
);
assert.equal(
  recovery(
    { safe_to_claim_complete: true, platform_total: 2, collected_rows: 2 },
    {
      exact_scope: true,
      source: "scoped",
      scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "product" },
      scope_rows: [
        { read_only_diagnosis_ready: false, collection_gate_state: "complete_but_stale", stale: true },
        { read_only_diagnosis_ready: false, collection_gate_state: "complete_but_stale", stale: true },
      ],
    },
  ).label,
  "打开并刷新当前商品计划",
  "complete coverage with only stale rows must refresh instead of opening diagnosis",
);
assert.equal(
  recovery(
    { safe_to_claim_complete: true, platform_total: 1, collected_rows: 1 },
    {
      exact_scope: true,
      source: "scoped",
      scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" },
      scope_rows: [{
        read_only_diagnosis_ready: false,
        collection_gate_state: "plan_id_missing",
        automation_next_action: { code: "read_plan_id", label: "补读计划 ID", detail: "缺少稳定计划 ID" },
      }],
    },
  ).kind,
  "sync_stable_ids",
  "a missing live plan id must route to the live plan reader instead of inventing an id",
);
assert.equal(
  recovery(
    { safe_to_claim_complete: true, platform_total: 1, collected_rows: 1 },
    {
      exact_scope: true,
      source: "scoped",
      scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" },
      scope_rows: [{
        read_only_diagnosis_ready: false,
        collection_gate_state: "identity_mismatch",
        automation_next_action: { code: "reselect_verified_scope", label: "重新核对计划身份" },
      }],
    },
  ).kind,
  "reselect_verified_scope",
  "identity mismatch must stop collection until the account scope is reselected",
);
assert.equal(
  recovery(
    { status: "confirmed_empty", safe_to_claim_complete: true, platform_total: 0 },
    { exact_scope: true, source: "scoped", scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "product" } },
  ).kind,
  "switch_plan_type",
);
assert.equal(
  recovery(
    { status: "complete", safe_to_claim_complete: true, collected_rows: 0 },
    { exact_scope: true, source: "scoped", scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" } },
  ).warning_code,
  "COMPLETE_RECEIPT_WITHOUT_ROWS",
  "a complete flag without rows or an explicit confirmed-empty status must fail closed",
);
assert.equal(
  recovery(
    { collected_rows: 2, platform_total: 5, coverage_warnings: [{ code: "COLLECTED_ROWS_BELOW_PLATFORM_TOTAL" }] },
    { exact_scope: true, source: "scoped", scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" } },
  ).blocker,
  "平台显示 5 条，目前只读取 2 条。",
);
assert.equal(
  recovery(
    { safe_to_claim_complete: true, collected_rows: 1 },
    {
      exact_scope: true,
      source: "scoped",
      scope: { account: "acct-a", promotion_mode: "chengfang", plan_type: "live" },
      unlinked_account: { key: "account_v1_02a6cc" },
    },
  ).kind,
  "review_unlinked_account",
  "an unlinked recently observed account must fail closed before plan recovery",
);
assert.equal(
  recovery({ safe_to_claim_complete: true }, { source: "scope_missing" }).warning_code,
  "SCOPED_RECEIPT_MISSING",
  "a global complete receipt must not certify a missing exact scope",
);
Object.values([
  recovery(warning("PLATFORM_TOTAL_CONFLICT")),
  recovery(warning("PAGINATION_TRUNCATED")),
  recovery(warning("STABLE_PLAN_ID_INCOMPLETE")),
]).forEach((item) => assert.doesNotMatch(item.instruction, /自动执行|自动提交/));

assert.match(script, /DianPromotionPlanCenter\.selectCollectionReceipt\(currentPromotionPlanConsole, currentPromotionPlanFilters\)/);
assert.match(script, /renderPromotionPlanCollectionReceipt\(receiptSelection\.receipt, receiptSelection\)/);
assert.match(script, /id="promotion-plan-coverage-action"|getElementById\("promotion-plan-coverage-action"\)/);
assert.match(script, /promotion-plan-coverage-action"\)\.addEventListener\("click"/);

const handlerStart = script.indexOf("async function runPromotionPlanCollectionRecovery");
const handlerEnd = script.indexOf("\nfunction renderPromotionPlanConsole", handlerStart);
const handlerSource = script.slice(handlerStart, handlerEnd);
assert.match(handlerSource, /runPromotionPlanScopeRecovery\(currentPromotionPlanRecovery\?\.contract \|\| \{\}\)/);
assert.match(handlerSource, /navigateToWorkspaceElement/);
assert.match(handlerSource, /review_unlinked_account/);
assert.match(handlerSource, /review_plans/);
assert.match(handlerSource, /ACCOUNT_SCOPE_MISMATCH/);
assert.match(handlerSource, /ACCOUNT_MISMATCH/);
assert.match(handlerSource, /forcedSelection\.unlinked_account/);
assert.doesNotMatch(handlerSource, /bridgeFetch\(|method:\s*["']POST|chrome\.tabs\.create|automatic_submit|platform_write_enabled/);

assert.match(script, /normalizedPlanType === "live"[\s\S]*?\["qianchuan_live"\]/);
assert.match(script, /normalizedPlanType === "product"[\s\S]*?\["campaigns", "qianchuan_campaigns"\]/);
assert.match(script, /type:\s*"start-full-scan"[\s\S]*?page_ids:\s*\[\.\.\.contract\.page_ids\]/);
assert.match(script, /recovery_purpose:\s*contract\.purpose/);
assert.match(script, /expected_page_types:\s*\[\.\.\.contract\.expected_page_types\]/);
assert.match(script, /bridgeFetch\(contract\.verification_endpoint\)[\s\S]*?renderPromotionPlanConsole\(payload\)/);
assert.match(script, /platform_write_enabled !== false \|\| contract\.automatic_submit !== false/);
assert.doesNotMatch(script.slice(
  script.indexOf("async function runPromotionPlanScopeRecovery"),
  script.indexOf("async function runPromotionPlanCollectionRecovery"),
), /plan_id\s*:|run-authorized-execution|actions\/execution|method:\s*["']POST/);
assert.match(script, /deriveUnlinkedAccountGuard\([\s\S]*?currentQianchuanCatalog/);
assert.match(script, /scope_rows = view\.all_rows\.filter/);
assert.match(script, /row\.supervised_draft_ready === true/);
assert.match(script, /syncError\.code = String\(response\?\.code \|\| response\?\.error_code \|\| ""\)/);

console.log("plan collection recovery UI tests passed");
