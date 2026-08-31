(function initChengfangProfileDraft(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianChengfangProfileDraft = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createChengfangProfileDraft() {
  "use strict";

  const CONSERVATIVE_BOUNDARY_DRAFT = Object.freeze({
    single_adjustment_cap: 5,
    daily_adjustment_cap: 5,
    daily_action_cap: 1,
    cooldown_minutes: 120,
    authorization_ttl_seconds: 60,
  });

  const BUSINESS_BOUNDARY_FIELDS = Object.freeze([
    "minimum_contribution_margin",
    "daily_budget_cap",
    "daily_loss_cap",
    "refund_rate_ceiling",
    "inventory_days_floor",
  ]);

  const HYDRATABLE_EVIDENCE_FIELDS = Object.freeze([
    "data_completeness",
    "data_freshness_minutes",
    "current_total_budget",
    "actual_roi",
    "today_spend",
    "attributed_orders",
  ]);

  function isBlank(value) {
    return value === "" || value === null || value === undefined;
  }

  function buildBoundaryDraft(value = {}) {
    const current = value && typeof value === "object" ? value : {};
    const boundaries = { ...current };
    const drafted_fields = [];
    const preserved_fields = [];
    Object.entries(CONSERVATIVE_BOUNDARY_DRAFT).forEach(([field, fallback]) => {
      if (isBlank(current[field])) {
        boundaries[field] = fallback;
        drafted_fields.push(field);
      } else {
        preserved_fields.push(field);
      }
    });
    return {
      boundaries,
      drafted_fields,
      preserved_fields,
      still_required: BUSINESS_BOUNDARY_FIELDS.filter((field) => isBlank(boundaries[field])),
      requires_confirmation: drafted_fields.length > 0,
      execution_allowed: false,
    };
  }

  function evidenceFromHydration(payload = {}) {
    const hydration = payload && typeof payload === "object"
      ? payload.evidence_hydration || payload
      : {};
    const profile = hydration.profile && typeof hydration.profile === "object" ? hydration.profile : {};
    const evidence = profile.evidence && typeof profile.evidence === "object" ? profile.evidence : {};
    const snapshot = hydration.snapshot && typeof hydration.snapshot === "object" ? hydration.snapshot : {};
    const extracted = snapshot.extracted && typeof snapshot.extracted === "object" ? snapshot.extracted : {};
    const values = {};
    const hydrated_fields = [];
    const cleared_fields = [];
    HYDRATABLE_EVIDENCE_FIELDS.forEach((field) => {
      const snapshotQualityField = ["data_completeness", "data_freshness_minutes"].includes(field) && Object.keys(snapshot).length > 0;
      const snapshotHasField = Object.prototype.hasOwnProperty.call(extracted, field);
      const snapshotObservedField = snapshotHasField && !isBlank(extracted[field]);
      if ((snapshotQualityField || snapshotObservedField)
        && !isBlank(evidence[field]) && Number.isFinite(Number(evidence[field]))) {
        values[field] = Number(evidence[field]);
        hydrated_fields.push(field);
      } else if (snapshot.exact_official_record === true && snapshotHasField && isBlank(extracted[field])) {
        // An exact official record can explicitly prove that a derived metric
        // is unavailable. Clear any stale UI value so it cannot be written
        // back into the trusted profile after hydration.
        cleared_fields.push(field);
      }
    });
    return {
      status: String(hydration.status || "unavailable"),
      fresh: hydration.fresh === true,
      values,
      hydrated_fields,
      cleared_fields,
      missing_decision_fields: Array.isArray(hydration.missing_decision_fields)
        ? hydration.missing_decision_fields.map(String)
        : [],
      scope_rejections: Array.isArray(hydration.scope_rejections)
        ? hydration.scope_rejections.map(String)
        : [],
      production_identifier_verified: hydration.production_identifier_verified === true,
    };
  }

  return {
    BUSINESS_BOUNDARY_FIELDS,
    CONSERVATIVE_BOUNDARY_DRAFT,
    HYDRATABLE_EVIDENCE_FIELDS,
    buildBoundaryDraft,
    evidenceFromHydration,
  };
});
