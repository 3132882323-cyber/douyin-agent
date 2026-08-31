const assert = require("assert");
const draft = require("./chengfang-profile-draft.js");

const safe = draft.buildBoundaryDraft({
  daily_budget_cap: 800,
  daily_loss_cap: 100,
  single_adjustment_cap: 3,
});
assert.strictEqual(safe.boundaries.daily_budget_cap, 800);
assert.strictEqual(safe.boundaries.daily_loss_cap, 100);
assert.strictEqual(safe.boundaries.single_adjustment_cap, 3, "用户已有值不能被草案覆盖");
assert.strictEqual(safe.boundaries.daily_adjustment_cap, 5);
assert.strictEqual(safe.boundaries.daily_action_cap, 1);
assert.strictEqual(safe.boundaries.cooldown_minutes, 120);
assert.strictEqual(safe.boundaries.authorization_ttl_seconds, 60);
assert.deepStrictEqual(safe.preserved_fields, ["single_adjustment_cap"]);
assert.ok(safe.still_required.includes("minimum_contribution_margin"));
assert.ok(safe.still_required.includes("refund_rate_ceiling"));
assert.strictEqual(safe.execution_allowed, false);

const hydrated = draft.evidenceFromHydration({
  evidence_hydration: {
    status: "partial",
    fresh: true,
    profile: {
      evidence: {
        data_completeness: 92,
        data_freshness_minutes: 3,
        current_total_budget: 1000,
        actual_roi: "1.25",
        today_spend: 320,
        attributed_orders: 6,
        daily_realized_loss: 99,
      },
    },
    snapshot: {
      extracted: {
        current_total_budget: 1000,
        actual_roi: "1.25",
        today_spend: 320,
        attributed_orders: 6,
      },
    },
    missing_decision_fields: [],
    production_identifier_verified: false,
  },
});
assert.strictEqual(hydrated.values.current_total_budget, 1000);
assert.strictEqual(hydrated.values.data_completeness, 92);
assert.strictEqual(hydrated.values.data_freshness_minutes, 3);
assert.strictEqual(hydrated.values.actual_roi, 1.25);
assert.strictEqual(hydrated.values.today_spend, 320);
assert.strictEqual(hydrated.values.attributed_orders, 6);
assert.strictEqual(hydrated.values.daily_realized_loss, undefined, "账本亏损不能由投放快照猜测");
assert.strictEqual(hydrated.production_identifier_verified, false);

const rejected = draft.evidenceFromHydration({
  status: "unavailable",
  profile: { evidence: { actual_roi: "not-a-number" } },
  scope_rejections: ["ACCOUNT_SCOPE_MISMATCH"],
});
assert.deepStrictEqual(rejected.values, {});
assert.deepStrictEqual(rejected.scope_rejections, ["ACCOUNT_SCOPE_MISMATCH"]);

const preservedManual = draft.evidenceFromHydration({
  status: "partial",
  fresh: true,
  profile: { evidence: { actual_roi: 9, today_spend: 200 } },
  snapshot: { extracted: { today_spend: 200 } },
});
assert.strictEqual(preservedManual.values.actual_roi, undefined, "旧手工值不能伪装成本次快照证据");
assert.strictEqual(preservedManual.values.today_spend, 200);

const exactUnavailable = draft.evidenceFromHydration({
  status: "partial",
  fresh: true,
  profile: { evidence: { actual_roi: null, today_spend: 200 } },
  snapshot: {
    exact_official_record: true,
    extracted: { actual_roi: null, today_spend: 200 },
  },
  missing_decision_fields: ["actual_roi"],
});
assert.deepStrictEqual(exactUnavailable.cleared_fields, ["actual_roi"]);
assert.strictEqual(exactUnavailable.values.actual_roi, undefined);
assert.strictEqual(exactUnavailable.values.today_spend, 200);

console.log("chengfang profile draft policy passed");
