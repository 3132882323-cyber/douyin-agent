const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
require("./content-common.js");

assert.equal(
  globalThis.DianAgentExtractor.maskText("电话 13812345678，邮箱 buyer@example.com"),
  "电话 138****5678，邮箱 bu***@example.com",
);
assert.equal(
  globalThis.DianAgentExtractor.maskText("身份证 110101199001011234"),
  "身份证 110101********1234",
);
assert.equal(globalThis.DianAgentExtractor.compact("  A  \n\n\n  B  "), "A \n\n B");
assert.equal(globalThis.DianAgentExtractor.maskText("订单号 123456789012345678"), "订单号 [已隐藏]");
const planToken = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier("直播计划\nID：987654321", "计划");
assert.match(planToken, /ID：pid_[a-f0-9]{32}/);
assert.equal(planToken.includes("987654321"), false);
assert.equal(globalThis.DianAgentExtractor.maskText(planToken), planToken);
const numberedPlanToken = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier("乘方计划\n编号：A1234", "乘方计划信息");
assert.match(numberedPlanToken, /编号：pid_[a-f0-9]{32}/);
assert.equal(numberedPlanToken.includes("A1234"), false, "short 编号 identities must not survive privacy masking");
assert.equal(globalThis.DianAgentExtractor.maskText(numberedPlanToken), numberedPlanToken);
assert.notEqual(
  globalThis.DianAgentExtractor.pseudonymizePlanIdentifier("987654321", "计划ID"),
  globalThis.DianAgentExtractor.pseudonymizePlanIdentifier("123456789", "计划ID"),
);
assert.equal(
  globalThis.DianAgentExtractor.pseudonymizePlanIdentifier("abcd", "计划ID"),
  "pid_88d4266fd4e6338d13b845fcf289579d",
  "plan tokens must use the deterministic 128-bit SHA-256 prefix",
);
const legacyFnvCollisionLeft = "578088328377115042";
const legacyFnvCollisionRight = "496758059572559982";
const hardenedCollisionLeft = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(legacyFnvCollisionLeft, "计划ID");
const hardenedCollisionRight = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(legacyFnvCollisionRight, "计划ID");
assert.match(hardenedCollisionLeft, /^pid_[a-f0-9]{32}$/);
assert.match(hardenedCollisionRight, /^pid_[a-f0-9]{32}$/);
assert.notEqual(hardenedCollisionLeft, hardenedCollisionRight, "known FNV-1a collision IDs must remain distinct");
assert.equal(hardenedCollisionLeft.includes(legacyFnvCollisionLeft), false, "the raw plan ID must not leak into its token");
assert.equal(hardenedCollisionRight.includes(legacyFnvCollisionRight), false, "the raw plan ID must not leak into its token");
assert.equal(globalThis.DianAgentExtractor.isSensitiveHeader("收货地址"), true);
assert.equal(globalThis.DianAgentExtractor.isSensitiveHeader("商品名称"), false);
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("商品 ID"), "douyin_product_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("SKU_ID"), "douyin_sku_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("素材ID"), "qianchuan_material_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("推广计划 ID"), "qianchuan_plan_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("广告计划编号"), "qianchuan_plan_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("推广计划 ＩＤ 可排序"), "qianchuan_plan_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("广告计划编号 排序"), "qianchuan_plan_id");
assert.equal(globalThis.DianAgentExtractor.entityKindForHeader("商品名称"), "");
for (const header of ["计划", "推广计划名称", "广告计划名称", "商品计划信息", "计划名称/ID", "计划名称（含ID）"]) {
  assert.equal(globalThis.DianAgentExtractor.isPlanNameHeader(header), true, `${header} should be a plan-name header`);
}
assert.equal(globalThis.DianAgentExtractor.isPlanNameHeader("推广计划ID"), false);
assert.equal(globalThis.DianAgentExtractor.isPlanNameHeader("计划编号"), false);
assert.equal(globalThis.DianAgentExtractor.isLocalEntityIdentifier("987654321"), true);
assert.equal(globalThis.DianAgentExtractor.isLocalEntityIdentifier("商品 A"), false);

assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("夏季防晒衣\nID：987654321", "商品信息"),
  { douyin_product_id: "987654321" },
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("商品ID：987654321\nSKU ID：sku_778899", "商品详情"),
  { douyin_product_id: "987654321" },
  "a product composite column must not guess SKU semantics outside an allowlisted SKU column",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("商品ID：987654321\n商品ID：123456789", "商品信息"),
  {},
  "conflicting IDs must remain unresolved",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("订单ID：987654321", "订单信息"),
  {},
  "order identifiers must never become product identifiers",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("8/12策\nID：1873314612646960\n1\n商品", "计划"),
  { qianchuan_plan_id: "1873314612646960" },
  "Chengfang composite plan cells must expose their bare plan ID",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("8/7-1-1\n自定义\nID:1873314612646960\n推商品", "计划名称"),
  { qianchuan_plan_id: "1873314612646960" },
  "legacy standard plan-name cells must expose their bare plan ID",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("秋季商品卡\n推广计划编号：plan_778899", "推广计划信息"),
  { qianchuan_plan_id: "plan_778899" },
  "composite plan cells labeled with 编号 must expose the stable plan identity",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("计划ID：plan_778899\n计划编号：plan_778800", "推广计划信息"),
  {},
  "mixed conflicting ID/编号 evidence must remain unresolved",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.extractLabeledEntityIdentifiers("ID：1873314612646960\nID：1873314612646961", "计划"),
  {},
  "conflicting plan IDs in one composite cell must remain unresolved",
);
assert.deepEqual(
  globalThis.DianAgentExtractor.commerceIdentityCoverage([
    { headers: ["商品信息", "库存", "商品ID"], rows: [["商品 A", "", "987654321"], ["商品 B", "10", ""]] },
  ]),
  { eligible_rows: 2, identified_rows: 1, coverage_rate: 50, by_kind: { douyin_product_id: 1 } },
);

function fakeCell(innerText) {
  return {
    innerText,
    getClientRects: () => [1],
    getAttribute: () => null,
    querySelectorAll: () => [],
  };
}
function fakeRow(values, header = false) {
  const cells = values.map(fakeCell);
  return {
    getClientRects: () => [1],
    querySelectorAll(selector) {
      return selector === "th, [role='columnheader']" ? (header ? cells : []) : cells;
    },
  };
}
function fakeTable(rows) {
  return { getClientRects: () => [1], querySelectorAll: () => rows };
}
const fixedPlanHeaders = [
  "", "计划", "投放状态",
  ...Array.from({ length: 21 }, (_, index) => `指标${index + 1}`),
  "末列转化成本",
];
assert.equal(fixedPlanHeaders.length, 25, "the production Qianchuan fixed header currently has 25 cells");
const fixedPlanSummary = ["共2条计划", ...Array.from({ length: 21 }, (_, index) => String(index + 1))];
const fixedPlanBodyRows = [
  ["", "8/12策\nID：1873314612646960\n1\n商品", "投放中", ...Array.from({ length: 21 }, (_, index) => String(index + 1)), "9.99"],
  ["", "8/2\nID：1873314612646961\n1\n商品", "已暂停", ...Array.from({ length: 21 }, (_, index) => String(index + 101)), "8.88"],
];
assert.equal(fixedPlanSummary.length, 22, "the production aggregate row currently has 22 cells");
assert.equal(fixedPlanBodyRows.every((row) => row.length === 25), true, "the split production body rows have 25 cells");
const headerTable = fakeTable([
  fakeRow(fixedPlanHeaders, true),
  // The live Qianchuan grid appends aggregate metrics to its summary row.
  // Those trailing values must not make the header fragment look like a
  // complete business table or the adjacent body table loses all headers.
  fakeRow(fixedPlanSummary),
]);
const bodyTable = fakeTable(fixedPlanBodyRows.map((row) => fakeRow(row)));
const unrelatedTable = fakeTable([
  fakeRow(["", "普通说明\nID：1873314612646999", "正常", ...Array.from({ length: 22 }, () => "-")]),
]);
globalThis.getComputedStyle = () => ({ display: "block", visibility: "visible" });
globalThis.document = { querySelectorAll: () => [headerTable, bodyTable, unrelatedTable] };
const virtualTables = globalThis.DianAgentExtractor.extractTables(true);
const virtualPlanTable = virtualTables[1];
assert.deepEqual(virtualTables[0].headers, fixedPlanHeaders);
assert.equal(virtualTables[0].rows.length, 0, "the 22-cell aggregate row must not become an identity row");
assert.deepEqual(virtualPlanTable.headers, [...fixedPlanHeaders, "计划ID"]);
assert.equal(virtualPlanTable.rows[0][24], "9.99", "the 25th production column must not be silently truncated");
assert.match(virtualPlanTable.rows[0][25], /^pid_[a-f0-9]{32}$/);
assert.notEqual(virtualPlanTable.rows[0][25], virtualPlanTable.rows[1][25]);
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage(virtualTables), {
  eligible_rows: 2,
  identified_rows: 2,
  coverage_rate: 100,
});
assert.deepEqual(virtualTables[2].headers, [], "split headers must not leak into later unrelated tables");
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage([{
  headers: ["计划信息", "投放状态"],
  rows: [["没有计划 ID 的计划", "投放中"]],
}]), {
  eligible_rows: 1,
  identified_rows: 0,
  coverage_rate: 0,
});
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage([{
  headers: ["计划信息", "投放状态"],
  rows: [["暂无符合条件的计划", ""]],
}]), {
  eligible_rows: 0,
  identified_rows: 0,
  coverage_rate: 0,
}, "an explicit empty state must not be treated as a plan missing its ID");
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage([{
  headers: ["推广计划名称", "推广计划 ID", "投放状态"],
  rows: [["秋季商品卡", "plan_778899", "投放中"]],
}]), {
  eligible_rows: 1,
  identified_rows: 1,
  coverage_rate: 100,
}, "common promoted-plan headers must enter identity coverage");

function fakeDetachedVirtualTable(headers, rows) {
  const headerCells = headers.map(fakeCell);
  return {
    getClientRects: () => [1],
    getAttribute: () => null,
    closest: () => null,
    querySelectorAll(selector) {
      if (selector === "th, [role='columnheader']") return headerCells;
      return rows;
    },
  };
}
const detachedVirtualTable = fakeDetachedVirtualTable(
  ["推广计划名称/ID", "投放状态", "整体消耗"],
  [fakeRow(["秋季直播承接\nID：plan_778899", "投放中", "123.45"])],
);
globalThis.document = { querySelectorAll: () => [detachedVirtualTable] };
const detachedVirtualTables = globalThis.DianAgentExtractor.extractTables(true);
assert.deepEqual(detachedVirtualTables[0].headers, ["推广计划名称/ID", "投放状态", "整体消耗", "计划ID"]);
assert.match(detachedVirtualTables[0].rows[0][3], /^pid_[a-f0-9]{32}$/);
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage(detachedVirtualTables), {
  eligible_rows: 1,
  identified_rows: 1,
  coverage_rate: 100,
}, "detached virtual-grid headers must stay aligned with their business rows");

const numberedPlanTable = fakeDetachedVirtualTable(
  ["推广计划信息", "投放状态", "整体消耗"],
  [fakeRow(["秋季商品卡\n推广计划编号：plan_778899", "投放中", "88.00"])],
);
globalThis.document = { querySelectorAll: () => [numberedPlanTable] };
const numberedPlanTables = globalThis.DianAgentExtractor.extractTables(true);
assert.deepEqual(numberedPlanTables[0].headers, ["推广计划信息", "投放状态", "整体消耗", "计划ID"]);
assert.match(numberedPlanTables[0].rows[0][3], /^pid_[a-f0-9]{32}$/);
assert.deepEqual(globalThis.DianAgentExtractor.planIdentityCoverage(numberedPlanTables), {
  eligible_rows: 1,
  identified_rows: 1,
  coverage_rate: 100,
}, "计划编号 evidence must pass the same 100% plan-identity gate as an ID label");

assert.deepEqual(globalThis.DianAgentExtractor.tableSchemaEvidence([{
  headers: ["直播计划名称", "抖音号", "投放状态", "整体消耗"], rows: [],
}]).page_type, "qianchuan_live");
assert.deepEqual(globalThis.DianAgentExtractor.tableSchemaEvidence([{
  headers: ["视频", "素材建议", "审核状态", "视频类型"], rows: [],
}]).page_type, "materials");
assert.deepEqual(globalThis.DianAgentExtractor.tableSchemaEvidence([{
  headers: ["商品计划名称", "商品信息", "投放状态", "支付ROI"], rows: [],
}]).page_type, "campaigns");
assert.deepEqual(globalThis.DianAgentExtractor.tableSchemaEvidence([
  { headers: ["直播计划名称", "抖音号", "投放状态"], rows: [] },
  { headers: ["视频", "素材建议", "审核状态", "视频类型"], rows: [] },
]), {
  page_type: "unknown", confidence: "conflict", source: "conflicting_table_schemas", headers: [],
}, "simultaneously visible incompatible schemas must fail closed");
assert.deepEqual(
  globalThis.DianAgentExtractor.extractKnownSignals("当前筛选条件下暂无符合条件的计划"),
  ["当前筛选条件下暂无符合条件的计划"],
  "privacy mode must retain plan-specific empty-state evidence",
);
assert.notEqual(
  globalThis.DianAgentExtractor.tableHarvestSignature([{ headers: ["计划"], rows: [["A"]] }]),
  globalThis.DianAgentExtractor.tableHarvestSignature([{ headers: ["计划"], rows: [["B"]] }]),
  "pagination readiness must observe actual table changes rather than relying on a fixed delay",
);
delete globalThis.document;
delete globalThis.getComputedStyle;
const extractorSource = fs.readFileSync(path.join(__dirname, "content-common.js"), "utf8");
assert.match(extractorSource, /Preserve empty cells/);
assert.doesNotMatch(extractorSource, /\.map\(\(cell\) => compact\(cell\.innerText\)\)\s*\.filter\(Boolean\)/);

console.log("content-common tests passed");
