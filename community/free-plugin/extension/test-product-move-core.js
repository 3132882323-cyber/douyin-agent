"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const core = require("./product-move-core.js");
const detailCore = require("./shelf-detail-core.js");
const linkCore = require("./shelf-tools-core.js");
// All products, store keys, IDs, media and receipt values are synthetic fixtures.
// Official/CDN URLs exercise local validation only; this test never opens them.
let tests = 0;
function test(name, run) { run(); tests++; process.stdout.write("ok - " + name + "\n"); }
const asset = (name = "main") => ({ url: "https://p3.pstatp.com/product/" + name + ".jpg" });
const pkg = (extra = {}) => ({ schema_version: 1,
  source: { platform: "doudian", source_kind: "doudian_edit_product", product_id: "00012345678901234567890", shop_name: "合成来源店铺", store_key: "synthetic-source-store", context_key: "synthetic:source:shop:epoch:1", source_url: "https://fxg.jinritemai.com/ffa/g/create?product_id=00012345678901234567890&entrance=edit", captured_at: "2026-10-01T00:00:00.000Z" },
  title: "A[1] A[1] 纯棉上衣", category: { source_id: "000cat", label: "服装" }, brand: { label: "原品牌" },
  attributes: [{ name: "材质", value: "棉" }],
  skus: [{ sku_id: "000sku", specifications: [{ name: "颜色", value: "蓝" }], price_raw: "10.00", stock_raw: "0", merchant_code_raw: "000-code", status_raw: "售卖中" }],
  media: { main_images: [asset()], detail_images: [asset("detail")], sku_images: [], videos: [] },
  shipping: { template_id: "000ship", template_name: "运费模板" }, coverage: { sku_complete: true, media_complete: true }, warnings: [], ...extra });
const config = (extra = {}) => ({ target_platform: "doudian", target_context_key: "synthetic:target:shop:epoch:1", intent: "preview", category_mapping: [{ source_id: "000cat", target_id: "001cat", target_label: "目标服装" }], shipping_mapping: [{ source_id: "000ship", target_id: "001ship", target_label: "目标运费" }], price_policy: { mode: "preserve_raw" }, stock_policy: { mode: "preserve_raw" }, ...extra });
const preview = (product = pkg(), cfg = config()) => core.buildPreview(product, cfg);
const missing = (result, code) => result.missing.some((entry) => entry.code === code);
const conflict = (result, code) => result.conflicts.some((entry) => entry.code === code);
const formula = (factor = "1.1", offset = "5", decimals = "2") => ({ mode: "formula", factor, offset, decimals });
const twoAxes = () => pkg({
  skus: [
    { sku_id: "b", specifications: [{ name: "尺码", value: "M" }, { name: "颜色", value: "加绒红色" }], price_raw: "100.00", stock_raw: "2", merchant_code_raw: "code-b", status_raw: "售卖中" },
    { sku_id: "a", specifications: [{ name: "尺码", value: "L" }, { name: "颜色", value: "蓝色" }], price_raw: "200.00", stock_raw: "3", merchant_code_raw: "code-a", status_raw: "售卖中" }
  ],
  media: { ...pkg().media, sku_images: [{ ...asset("red"), sku_id: "b" }, { ...asset("blue"), sku_id: "a" }] }
});

test("CommonJS and UMD expose a frozen pure API", () => {
  assert(Object.isFrozen(core)); assert(Object.isFrozen(core.LIMITS)); assert(Object.isFrozen(core.STATES));
  for (const name of ["normalizePackage", "normalizeConfig", "buildPreview", "dedupKey", "createTask", "normalizeTask", "transitionTask", "normalizeResult", "createBatch"]) assert.equal(typeof core[name], "function");
  const context = vm.createContext({ URL, DianShelfDetailCore: detailCore, DianShelfToolsCore: linkCore });
  vm.runInContext(fs.readFileSync(require.resolve("./product-move-core.js"), "utf8"), context);
  assert.equal(typeof context.DianProductMoveCore.buildPreview, "function");
  assert.equal(vm.runInContext('DianProductMoveCore.normalizePackage({schema_version:1,source:{platform:"doudian",product_id:"0001"}}).ok', context), true);
});
test("normalization preserves exact IDs and raw numeric zero without mutating input", () => {
  const input = pkg({ skus: [{ sku_id: "000sku", price_raw: 0, stock_raw: 0, merchant_code_raw: "00001" }] });
  const before = JSON.stringify(input), result = core.normalizePackage(input);
  assert.equal(result.ok, true); assert.equal(result.product.source.product_id, input.source.product_id);
  assert.equal(result.product.skus[0].price_raw, "0"); assert.equal(result.product.skus[0].stock_raw, "0"); assert.equal(result.product.skus[0].merchant_code_raw, "00001");
  assert.equal(JSON.stringify(input), before); assert.notEqual(result.product, input); assert.notEqual(result.product.skus, input.skus);
});
test("numeric or rounded product, SKU, and mapping IDs are never converted", () => {
  for (const id of [0, 1, 9007199254740993, " 001", "001 ", "x/1", ""]) {
    assert.equal(core.normalizePackage(pkg({ source: { ...pkg().source, product_id: id } })).ok, false);
    assert.equal(core.normalizePackage(pkg({ skus: [{ sku_id: id }] })).ok, false);
  }
  assert.equal(core.normalizeConfig(config({ category_mapping: [{ source_id: "000cat", target_id: 1 }] })).ok, false);
});
test("missing optional data stays absent and differs from business readiness", () => {
  const input = { schema_version: 1, source: { platform: "doudian", product_id: "0001" } };
  const result = core.normalizePackage(input);
  assert.equal(result.ok, true); assert.deepEqual(result.product, input);
  const local = preview(input, { target_platform: "doudian" });
  assert.equal(local.structural_ok, true); assert.equal(local.business_ready, false);
  for (const key of ["title", "skus", "media", "brand"]) assert.equal(Object.hasOwn(local.target_fields, key), false);
  assert(missing(local, "title_unknown")); assert(missing(local, "skus_unknown")); assert(missing(local, "main_images_unknown"));
});
test("full declared package produces only a local preview, never a platform draft", () => {
  const result = preview();
  assert.equal(result.kind, "local_adaptation_preview"); assert.equal(result.business_ready, true);
  assert.equal(result.target_fields.skus[0].stock_value, "0");
  assert.equal(result.coverage.sku, "DECLARED_COMPLETE");
  for (const key of ["connector_supported", "platform_draft_created", "platform_publish_created", "execution_authorized", "complete"]) assert.equal(result[key], false);
  assert.equal(Object.hasOwn(result, "platform_draft_id"), false); assert.equal(Object.hasOwn(result, "draft_id"), false);
  assert.equal(core.normalizeResult(result).ok, true);
});
test("false or absent coverage is UNKNOWN even when visible SKUs and media exist", () => {
  for (const coverage of [{ sku_complete: false, media_complete: false }, {}]) {
    const result = preview(pkg({ coverage }));
    assert.equal(result.business_ready, false); assert.deepEqual(result.coverage, { sku: "UNKNOWN", media: "UNKNOWN" });
    assert.equal(result.missing.filter((entry) => entry.code === "coverage_unknown").length, 2);
  }
  const input = pkg(); delete input.coverage;
  assert.equal(preview(input).coverage.sku, "UNKNOWN");
});
test("missing SKU, main/detail media, category and shipping are independently reported", () => {
  const input = pkg({ skus: [], media: { main_images: [], detail_images: [], sku_images: [], videos: [] }, category: {}, shipping: {} });
  const result = preview(input);
  assert.equal(result.structural_ok, true); assert.equal(result.business_ready, false);
  assert(missing(result, "skus_unknown")); assert(missing(result, "main_images_unknown")); assert(missing(result, "detail_images_unknown"));
  assert(result.missing.some((entry) => entry.path === "category")); assert(result.missing.some((entry) => entry.path === "shipping"));
});
test("coverage strings, invalid schema, source platforms and unknown fields fail closed", () => {
  for (const extra of [{ schema_version: "1" }, { schema_version: 2 }, { coverage: { sku_complete: "true" } }, { invented: true }]) assert.equal(core.normalizePackage(pkg(extra)).ok, false);
  assert.equal(core.normalizePackage(pkg({ source: { ...pkg().source, platform: "other" } })).ok, false);
  assert.equal(core.normalizeConfig(config({ execution_authorized: true })).ok, false);
});
test("unsafe asset URLs are rejected with the existing asset validator", () => {
  for (const url of ["http://p3.pstatp.com/a.jpg", "https://user@p3.pstatp.com/a.jpg", "https://p3.pstatp.com/a.jpg?token=secret", "https://p3.pstatp.com/a.jpg?auth=x", "https://p3.pstatp.com/a.svg", "https://evil.invalid/a.jpg", "https://p3.pstatp.com/private/a.jpg"]) {
    const input = pkg({ media: { main_images: [{ url }] } });
    assert.equal(core.normalizePackage(input).ok, false, url); assert.equal(preview(input).structural_ok, false);
  }
});

test("SKU images retain exact observed SKU links and reject guessed or conflicting associations", () => {
  const images = [{ ...asset("sku"), sku_id: "000sku" }], input = pkg({ media: { ...pkg().media, sku_images: images } });
  const normalized = core.normalizePackage(input); assert.equal(normalized.ok, true);
  assert.deepEqual(normalized.product.media.sku_images, images);
  const result = preview(input); assert.deepEqual(result.target_fields.media.sku_images, images);
  assert.equal(core.normalizeResult(result).ok, true);
  for (const bad of [[asset("sku")], [{ ...asset("sku"), sku_id: 1 }], [{ ...asset("sku"), sku_id: "UNKNOWN" }], images.concat([{ ...asset("other"), sku_id: "000sku" }])]) {
    assert.equal(core.normalizePackage(pkg({ media: { ...pkg().media, sku_images: bad } })).ok, false);
    assert.equal(core.normalizeResult({ ...result, target_fields: { ...result.target_fields, media: { ...result.target_fields.media, sku_images: bad } } }).ok, false);
  }
  assert.equal(core.normalizePackage(pkg({ media: { main_images: images } })).ok, false);
});
test("unsafe source URLs, identity conflicts and invalid calendar dates are rejected", () => {
  for (const source_url of ["https://fxg.jinritemai.com/ffa/g/list?token=secret", "https://user@fxg.jinritemai.com/ffa/g/list", "https://haohuo.jinritemai.com/views/product/detail?id=OTHER", "https://v.douyin.com/a/"]) assert.equal(core.normalizePackage(pkg({ source: { ...pkg().source, source_url } })).ok, false);
  assert.equal(core.normalizePackage(pkg({ source: { ...pkg().source, captured_at: "2026-02-30T00:00:00Z" } })).ok, false);
});
test("native edit source URL is accepted but route-only context cannot prove shop identity", () => {
  const source_url = "https://fxg.jinritemai.com/ffa/g/create?product_id=" + pkg().source.product_id + "&entrance=edit";
  assert.equal(linkCore.normalizeLink(source_url).ok, true);
  const input = pkg({ source: { ...pkg().source, source_url, context_key: "route-only:" + pkg().source.product_id } });
  delete input.source.store_key;
  const normalized = core.normalizePackage(input);
  assert.equal(normalized.ok, true); assert.equal(normalized.product.source.source_url, source_url);
  const result = preview(input); assert.equal(result.structural_ok, true); assert.equal(result.business_ready, false); assert(missing(result, "source_context_unknown"));
  assert(missing(preview(pkg({ source: { ...pkg().source, store_key: undefined } })), "source_context_unknown"));
});
test("getter fields and getter array elements are rejected without execution", () => {
  let executed = 0;
  const input = pkg(); Object.defineProperty(input, "title", { enumerable: true, get() { executed++; throw new Error("getter must not run"); } });
  assert.equal(core.normalizePackage(input).ok, false);
  const skus = []; Object.defineProperty(skus, "0", { enumerable: true, get() { executed++; return {}; } });
  assert.equal(core.normalizePackage(pkg({ skus })).ok, false);
  const cfg = config(); Object.defineProperty(cfg, "title_replace", { get() { executed++; return {}; } });
  assert.equal(core.normalizeConfig(cfg).ok, false); assert.equal(executed, 0);
});
test("prototype poison, inherited data and cycles are rejected", () => {
  const poisoned = pkg(); Object.defineProperty(poisoned, "__proto__", { value: { published: true }, enumerable: true });
  assert.equal(core.normalizePackage(poisoned).ok, false);
  assert.equal(core.normalizePackage(Object.create(pkg())).ok, false);
  const input = pkg(); input.attributes.push(input);
  assert.equal(core.normalizePackage(input).ok, false);
  assert.equal(core.normalizeConfig(config({ title_replace: new RegExp("A") })).ok, false);
});
test("control characters and oversize strings/arrays are not silently repaired", () => {
  for (const title of ["A\u0000B", "A\nB", "A\u200bB", "A".repeat(4001)]) assert.equal(core.normalizePackage(pkg({ title })).ok, false);
  assert.equal(core.normalizePackage(pkg({ skus: Array.from({ length: 201 }, () => ({ sku_id: "1" })) })).ok, false);
  assert.equal(core.normalizePackage(pkg({ media: { main_images: Array.from({ length: 101 }, () => asset()) } })).ok, false);
  assert.equal(core.normalizePackage(pkg({ skus: new Array(2) })).ok, false);
});
test("literal title replacement preserves regex syntax as text", () => {
  const result = preview(pkg(), config({ title_replace: { from: "A[1]", to: "$&新" } }));
  assert.equal(result.target_fields.title, "$&新 $&新 纯棉上衣"); assert.equal(result.business_ready, true);
  assert.equal(core.normalizeConfig(config({ title_replace: { from: "", to: "new" } })).ok, false);
  assert.equal(core.normalizeConfig(config({ title_replace: { from: "A" } })).ok, false);
  assert.equal(core.normalizeConfig(config({ title_replace: { from: "A", to: "B", regex: true } })).ok, false);
});
test("replacement expansion does not bypass the title bound", () => {
  const result = preview(pkg({ title: "A".repeat(2000) }), config({ title_replace: { from: "A", to: "BBB" } }));
  assert.equal(result.business_ready, false); assert(conflict(result, "title_limit")); assert.equal(Object.hasOwn(result.target_fields, "title"), false);
});
test("explicit price/stock policies preserve zero and never default missing values", () => {
  const cfg = config({ price_policy: { mode: "set", value: 0 }, stock_policy: { mode: "set", value: 0 } });
  const result = preview(pkg(), cfg);
  assert.equal(result.target_fields.skus[0].price_value, "0"); assert.equal(result.target_fields.skus[0].stock_value, "0");
  assert.equal(result.target_fields.skus[0].price_raw, "10.00"); assert.equal(result.business_ready, true);
  const none = config(); delete none.price_policy; delete none.stock_policy;
  assert.equal(preview(pkg(), none).missing.filter((entry) => entry.code === "policy_missing").length, 2);
  assert.equal(Object.hasOwn(preview(pkg(), none).target_fields.skus[0], "price_value"), false);
  for (const value of ["", "-1", "1e3", "NaN", " 1", "1.0"]) assert.equal(core.normalizeConfig(config({ stock_policy: { mode: "set", value } })).ok, false);
  assert.equal(core.normalizeConfig(config({ price_policy: { mode: "preserve_raw", value: "1" } })).ok, false);
});
test("ambiguous display prices and unknown stock are preserved raw but not guessed", () => {
  const result = preview(pkg({ skus: [{ sku_id: "000sku", price_raw: "¥10–20", stock_raw: "" }] }));
  assert.equal(result.target_fields.skus[0].price_raw, "¥10–20"); assert.equal(result.target_fields.skus[0].stock_raw, "");
  assert.equal(Object.hasOwn(result.target_fields.skus[0], "price_value"), false); assert.equal(Object.hasOwn(result.target_fields.skus[0], "stock_value"), false);
  assert(conflict(result, "price_not_numeric")); assert(missing(result, "stock_unknown"));
});
test("mapping matches exact source IDs and conflicts are not arbitrarily resolved", () => {
  const cfg = config({ category_mapping: [{ source_id: "000cat", target_id: "A" }, { source_id: "000cat", target_id: "B" }] });
  const result = preview(pkg(), cfg); assert(conflict(result, "mapping_conflict")); assert.equal(Object.hasOwn(result.target_fields, "category"), false);
  assert(missing(preview(pkg(), config({ category_mapping: [{ source_id: "cat", target_id: "A" }] })), "mapping_missing"));
  assert(missing(preview(pkg({ category: { label: "服装" } })), "source_mapping_id_unknown"));
});
test("duplicate SKU identities remain visible and block readiness", () => {
  const input = pkg(); input.skus.push({ ...input.skus[0], price_raw: "20.00", specifications: [{ name: "颜色", value: "红" }] });
  const result = preview(input); assert.equal(result.target_fields.skus.length, 2); assert(conflict(result, "duplicate_sku")); assert.equal(result.business_ready, false);
});
test("unsupported targets and platform intentions never fabricate successful writes", () => {
  assert(conflict(preview(pkg(), config({ target_platform: "unsupported" })), "unsupported_local_target"));
  for (const intent of ["platform_draft", "publish"]) {
    const result = preview(pkg(), config({ intent })); assert(conflict(result, "connector_unavailable")); assert.equal(result.business_ready, false); assert.equal(result.platform_draft_created, false); assert.equal(result.execution_authorized, false);
    assert(!Object.keys(result).some((key) => /draft_id|publish_id|receipt_id/.test(key)));
  }
});
test("dedup keys are stable by normalized input plus target context and are not authorization", () => {
  const a = pkg(), cfg = config(); const reversed = Object.fromEntries(Object.entries(a).reverse());
  assert.equal(core.dedupKey(a, cfg), core.dedupKey(reversed, cfg));
  assert.notEqual(core.dedupKey(a, cfg), core.dedupKey(a, config({ target_context_key: "different-target" })));
  assert.notEqual(core.dedupKey(a, cfg), core.dedupKey(pkg({ title: "不同标题" }), cfg));
  assert.equal(core.dedupKey(pkg({ schema_version: 2 }), cfg), null);
  assert.equal(preview(a, cfg).execution_authorized, false);
});
test("local task transitions are explicit, immutable and cannot jump to publish", () => {
  const first = core.createTask(pkg(), config()); assert.equal(first.ok, true); assert.equal(first.task.state, "collecting");
  assert.equal(core.transitionTask(first.task, "preview").ok, false); assert.equal(core.transitionTask(first.task, "complete").ok, false);
  const mapped = core.transitionTask(first.task, "mapping"); assert.equal(mapped.ok, true); assert.equal(first.task.state, "collecting");
  const shown = core.transitionTask(mapped.task, "preview"); assert.equal(shown.ok, true); assert.equal(shown.task.result.kind, "local_adaptation_preview");
  const ready = core.transitionTask(shown.task, "localpreviewready"); assert.equal(ready.ok, true); assert.equal(ready.task.platform_draft_created, false);
  assert.equal(core.transitionTask(ready.task, "published").ok, false); assert.equal(core.normalizeTask(ready.task).ok, true);
});
test("partial packages cannot enter ready and cancellation is a terminal local state", () => {
  let task = core.createTask(pkg({ coverage: { sku_complete: false, media_complete: false } }), config()).task;
  task = core.transitionTask(task, "mapping").task; task = core.transitionTask(task, "preview").task;
  assert.equal(core.transitionTask(task, "localpreviewready").ok, false);
  const blocked = core.transitionTask(task, "blocked"); assert.equal(blocked.ok, true);
  const cancelled = core.transitionTask(blocked.task, "cancelled"); assert.equal(cancelled.ok, true);
  for (const state of core.STATES) assert.equal(core.transitionTask(cancelled.task, state).ok, false);
  assert.equal(core.normalizeTask({ ...cancelled.task, state: "complete" }).ok, false);
});
test("forged local keys and authorization flags are not trusted", () => {
  const task = core.createTask(pkg(), config()).task;
  assert.equal(core.normalizeTask({ ...task, local_task_key: "fake" }).ok, false);
  assert.equal(core.normalizeTask({ ...task, execution_authorized: true }).ok, false);
  assert.equal(core.normalizeTask({ ...task, platform_draft_created: true }).ok, false);
  assert.equal(core.normalizeTask({ ...task, result: { kind: "platform_draft_receipt", target_platform: "doudian", connector_id: "synthetic-connector", receipt_id: "synthetic-receipt" } }).ok, false);
  assert.equal(core.normalizeResult({ ...preview(), execution_authorized: true }).ok, false);
  assert.equal(core.normalizeResult({ ...preview(), platform_draft_id: "invented-id" }).ok, false);
  assert.equal(core.normalizeResult({ kind: "local_adaptation_preview", structural_ok: true, business_ready: true }).ok, false);
});
test("platform receipt types remain unverified and can never prove completion", () => {
  for (const kind of ["platform_draft_receipt", "platform_publish_receipt"]) {
    const receipt = { kind, target_platform: "doudian", connector_id: "synthetic-connector", receipt_id: "synthetic-receipt" };
    const result = core.normalizeResult(receipt); assert.equal(result.ok, true); assert.equal(result.result.kind, kind); assert.equal(result.result.verification, "unverified"); assert.equal(result.result.complete, false); assert.equal(result.result.platform_draft_created, false);
    for (const extra of [{ verified: true }, { verification: "verified" }, { complete: true }, { platform_draft_created: true }, { execution_authorized: true }]) assert.equal(core.normalizeResult({ ...receipt, ...extra }).ok, false);
  }
  assert.equal(core.normalizeResult({ kind: "platform_draft_receipt" }).ok, false);
});
test("result validation rechecks target media, exact IDs and readiness instead of trusting a view", () => {
  const result = preview();
  assert.notEqual(core.normalizeResult(result).result, result);
  assert.equal(core.normalizeResult({ ...result, target_fields: { ...result.target_fields, source_product_id: 123 } }).ok, false);
  assert.equal(core.normalizeResult({ ...result, target_fields: { ...result.target_fields, media: { main_images: [{ url: "https://p3.pstatp.com/a.jpg?token=secret" }] } } }).ok, false);
  assert.equal(core.normalizeResult({ ...result, target_fields: {} }).ok, false);
  assert.equal(core.normalizeResult({ ...result, coverage: { sku: "UNKNOWN", media: "DECLARED_COMPLETE" } }).ok, false);
  const partial = preview({ schema_version: 1, source: { platform: "doudian", product_id: "0001" } }, { target_platform: "doudian" });
  assert.equal(core.normalizeResult(partial).ok, true);
});
test("bounded batches deduplicate exact full inputs and are all-or-nothing on invalid data", () => {
  const input = pkg(), before = JSON.stringify(input);
  const result = core.createBatch([input, input, pkg({ title: "不同商品资料" })], config());
  assert.equal(result.ok, true); assert.equal(result.tasks.length, 2); assert.equal(result.duplicate_count, 1); assert.equal(JSON.stringify(input), before);
  assert.equal(core.createBatch(Array.from({ length: 21 }, () => input), config()).ok, false);
  const invalid = core.createBatch([input, pkg({ schema_version: 2 })], config()); assert.equal(invalid.ok, false); assert.equal(invalid.tasks.length, 0);
  assert.deepEqual(core.createBatch([], config()).tasks, []);
});
test("source kind is explicit and bounded without guessing legacy packages", () => {
  const legacyInput = pkg(); delete legacyInput.source.source_kind;
  const legacy = core.normalizePackage(legacyInput);
  assert.equal(Object.hasOwn(legacy.product.source, "source_kind"), false);
  assert.equal(preview(legacyInput).business_ready, false); assert(missing(preview(legacyInput), "source_kind_unknown"));
  const store = pkg(); store.source.source_kind = "doudian_store_product";
  store.source.source_url = "https://fxg.jinritemai.com/ffa/g/list";
  assert.equal(core.normalizePackage(store).ok, true);
  assert.equal(core.normalizePackage(store).product.source.source_kind, "doudian_store_product");
  assert.equal(preview(store).business_ready, true); assert.equal(preview(store).execution_authorized, false); assert.equal(preview(store).platform_draft_created, false);
  store.source.source_url += "?tab=draft";
  assert.equal(preview(store).business_ready, true);
  assert.equal(preview(store).execution_authorized, false, "list filters never authorize platform writes");
  const edit = pkg(); edit.source.source_kind = "doudian_edit_product";
  edit.source.source_url = `https://fxg.jinritemai.com/ffa/g/create?product_id=${edit.source.product_id}&entrance=edit`;
  assert.equal(core.normalizePackage(edit).ok, true);
  const result = core.buildPreview(edit, config());
  assert.deepEqual([result.execution_authorized, result.platform_draft_created, result.platform_publish_created], [false, false, false]);
  assert(Object.isFrozen(core.SOURCE_KINDS));
});
test("marketplace sources and ambiguous draft labels are rejected", () => {
  for (const kind of ["douyin_marketplace_product", "doudian_draft", "framework", "web_request", "", null, true, "doudian_store_product\n"]) {
    const input = pkg(); input.source.source_kind = kind;
    assert.equal(core.normalizePackage(input).ok, false, String(kind));
  }
  for (const [kind, route] of [["doudian_edit_product", "/ffa/g/list"], ["doudian_store_product", "/ffa/g/create?product_id=00012345678901234567890&entrance=edit"]]) {
    const input = pkg(); input.source.source_kind = kind; input.source.source_url = "https://fxg.jinritemai.com" + route;
    assert.equal(core.normalizePackage(input).issues.some(issue => issue.code === "source_kind_route_mismatch"), true);
  }
});
test("formula policies normalize text parameters without changing legacy policies", () => {
  const input = config({ price_policy: formula() }), before = JSON.stringify(input);
  assert.deepEqual(core.normalizeConfig(input).config.price_policy, formula());
  assert.equal(JSON.stringify(input), before);
  for (const mode of ["preserve_raw", "set"]) {
    const rule = mode === "set" ? { mode, value: "1.00" } : { mode };
    assert.deepEqual(core.normalizeConfig(config({ price_policy: rule })).config.price_policy, rule);
    for (const key of ["factor", "offset", "decimals"]) assert.equal(core.normalizeConfig(config({ price_policy: { ...rule, [key]: "1" } })).ok, false);
  }
  assert.equal(core.normalizeConfig(config({ stock_policy: formula() })).ok, false);
  assert.equal(core.normalizeConfig(config({ price_policy: { ...formula(), value: "2" } })).ok, false);
});

test("formula requires bounded exact parameters and precision from zero through six", () => {
  for (const key of ["factor", "offset", "decimals"]) {
    const missingRule = formula(); delete missingRule[key];
    assert.equal(core.normalizeConfig(config({ price_policy: missingRule })).ok, false);
    for (const value of [undefined, null, 1, true, "", " 1", "1 ", "1e2", "NaN", "Infinity", "+1", "1\n", "1".repeat(65)]) {
      assert.equal(core.normalizeConfig(config({ price_policy: { ...formula(), [key]: value } })).ok, false, key + ": " + String(value));
    }
  }
  for (const decimals of ["-1", "7", "01", "2.0", "0.5"]) assert.equal(core.normalizeConfig(config({ price_policy: formula("1", "0", decimals) })).ok, false);
  for (const decimals of ["0", "1", "2", "3", "4", "5", "6"]) assert.equal(core.normalizeConfig(config({ price_policy: formula("1", "-0.50", decimals) })).ok, true);
  for (const factor of ["-1", "-0", ".5", "1."]) assert.equal(core.normalizeConfig(config({ price_policy: formula(factor) })).ok, false);
  for (const offset of ["--1", "-", "-.5", "-1."]) assert.equal(core.normalizeConfig(config({ price_policy: formula("1", offset) })).ok, false);
});

test("formula reproduces reference values and recalculates every SKU from its original price", () => {
  const input = twoAxes(), before = JSON.stringify(input), cfg = config({ price_policy: formula() });
  const first = preview(input, cfg), second = preview(input, cfg);
  assert.deepEqual(first.target_fields.skus.map(sku => sku.price_value), ["115.00", "225.00"]);
  assert.deepEqual(second, first);
  assert.deepEqual(preview(input, config({ price_policy: formula("1.2", "0") })).target_fields.skus.map(sku => sku.price_value), ["120.00", "240.00"]);
  assert.deepEqual(first.target_fields.skus.map(sku => sku.price_raw), ["100.00", "200.00"]);
  assert.equal(JSON.stringify(input), before);
  assert.equal(first.target_fields.title, input.title);
  assert.equal(first.business_ready, true); assert.equal(core.normalizeResult(first).ok, true);
  // An adapted target SKU is not a source package: derived values cannot be
  // silently re-imported and mistaken for original baselines.
  assert.equal(core.normalizePackage(pkg({ skus: first.target_fields.skus })).ok, false);
});

test("identity formula preserves lexical price even when only requested decimals change", () => {
  for (const price_raw of ["19.9900", "2.55", "00019.9900", "0.0000"]) {
    const input = pkg({ skus: [{ ...pkg().skus[0], price_raw }] });
    for (const rule of [formula("1", "0", "0"), formula("01.000", "-0.00", "6")]) assert.equal(preview(input, config({ price_policy: rule })).target_fields.skus[0].price_value, price_raw);
  }
});

test("formula uses exact BigInt decimal half-up rounding including carry and large prices", () => {
  const cases = [
    ["100", "1.2", "2", "2", "122.00"],
    ["2.675", "1", "0.0001", "2", "2.68"],
    ["1.005", "1", "-0.000001", "2", "1.00"],
    ["1.004", "1", "0.001", "2", "1.01"],
    ["9.999", "1", "0.0001", "2", "10.00"],
    ["1.49", "1", "0.01", "0", "2"],
    ["0.0000004", "1", "0.0000001", "6", "0.000001"],
    ["0.1", "0.2", "0", "2", "0.02"],
    ["1", "0.00000000000000000000000001", "0.0000005", "6", "0.000001"],
    ["9007199254740993.99", "1", "0.01", "2", "9007199254740994.00"],
    ["100", "0", "25", "2", "25.00"],
    ["100", "1", "-100", "2", "0.00"],
    ["0", "0", "0", "0", "0"]
  ];
  for (const [price_raw, factor, offset, decimals, expected] of cases) {
    const result = preview(pkg({ skus: [{ ...pkg().skus[0], price_raw }] }), config({ price_policy: formula(factor, offset, decimals) }));
    assert.equal(result.business_ready, true, JSON.stringify([price_raw, factor, offset, decimals]));
    assert.equal(result.target_fields.skus[0].price_value, expected);
  }
});

test("negative formula results are blocked before rounding and never silently zeroed", () => {
  for (const offset of ["-11", "-10.000001"]) {
    const result = preview(pkg(), config({ price_policy: formula("1", offset, "0") }));
    assert(conflict(result, "price_formula_negative")); assert.equal(result.business_ready, false);
    assert.equal(Object.hasOwn(result.target_fields.skus[0], "price_value"), false);
    assert.equal(result.target_fields.skus[0].price_raw, "10.00");
    assert.equal(core.normalizeResult(result).ok, true);
  }
});

test("formula does not guess missing display prices or accept oversized output", () => {
  for (const price_raw of [undefined, "", "¥10–20", "1e2", "-1", "NaN"]) {
    const result = preview(pkg({ skus: [{ ...pkg().skus[0], price_raw }] }), config({ price_policy: formula("0", "25") }));
    assert.equal(result.business_ready, false);
    assert.equal(Object.hasOwn(result.target_fields.skus[0], "price_value"), false);
    assert(missing(result, "price_unknown") || conflict(result, "price_not_numeric"));
  }
  const result = preview(pkg({ skus: [{ ...pkg().skus[0], price_raw: "9".repeat(64) }] }), config({ price_policy: formula("9".repeat(64), "0") }));
  assert(conflict(result, "price_formula_limit")); assert.equal(result.business_ready, false);
  assert.equal(Object.hasOwn(result.target_fields.skus[0], "price_value"), false);
  assert.equal(core.normalizeResult(result).ok, true);
});

test("specification policy fields are independently optional and whitespace words are normalized", () => {
  for (const rule of [{}, { swap_axes: true }, { swap_axes: false }, { remove_words: "加绒" }, { swap_axes: false, remove_words: "" }]) {
    assert.deepEqual(core.normalizeConfig(config({ specification_policy: rule })).config.specification_policy, rule);
  }
  const cfg = config({ specification_policy: { remove_words: "  加绒\t厚款\n[新]\r\n.*\u3000特价  " } });
  assert.equal(core.normalizeConfig(cfg).config.specification_policy.remove_words, "加绒 厚款 [新] .* 特价");
  const created = core.createTask(twoAxes(), cfg);
  assert.equal(created.ok, true); assert.equal(core.normalizeTask(created.task).ok, true);
  for (const rule of [null, [], { swap_axes: "true" }, { swap_axes: 1 }, { remove_words: 1 }, { remove_words: undefined }, { remove_words: "a\u0000b" }, { remove_words: "a\u200bb" }, { remove_words: "a".repeat(4001) }, { regex: true }]) assert.equal(core.normalizeConfig(config({ specification_policy: rule })).ok, false);
  assert.equal(core.normalizeConfig(config({ title_replace: { from: "a\nb", to: "" } })).ok, false);
});

test("specification swap preserves explicit SKU order, corresponding pictures and all raw values", () => {
  const input = twoAxes(), before = JSON.stringify(input);
  const result = preview(input, config({ specification_policy: { swap_axes: true } }));
  assert.equal(result.business_ready, true); assert.equal(core.normalizeResult(result).ok, true);
  assert.deepEqual(result.target_fields.skus.map(sku => sku.sku_id), ["b", "a"]);
  for (let i = 0; i < input.skus.length; i++) {
    assert.deepEqual(result.target_fields.skus[i].specifications, input.skus[i].specifications.slice().reverse());
    for (const field of ["price_raw", "stock_raw", "merchant_code_raw", "status_raw"]) assert.equal(result.target_fields.skus[i][field], input.skus[i][field]);
  }
  assert.deepEqual(result.target_fields.media.sku_images, input.media.sku_images);
  assert.deepEqual(result.target_fields.attributes, input.attributes);
  assert.equal(result.target_fields.title, input.title); assert.equal(JSON.stringify(input), before);
});

test("swap rejects partial, one-axis, three-axis and inconsistent axis order atomically", () => {
  const cases = [[], undefined, [{ name: "颜色", value: "红" }], [{ name: "材质", value: "棉" }, ...twoAxes().skus[1].specifications], twoAxes().skus[1].specifications.slice().reverse(), [{ name: "型号", value: "L" }, { name: "颜色", value: "蓝" }]];
  for (const specifications of cases) {
    const input = twoAxes(); input.skus[1].specifications = specifications;
    if (specifications === undefined) delete input.skus[1].specifications;
    const result = preview(input, config({ specification_policy: { swap_axes: true } }));
    assert.equal(result.business_ready, false); assert(conflict(result, "specification_axes_mismatch"));
    assert.deepEqual(result.target_fields.skus.map(sku => sku.specifications), input.skus.map(sku => sku.specifications));
  }
  const input = twoAxes(); input.skus = []; input.media.sku_images = [];
  assert(conflict(preview(input, config({ specification_policy: { swap_axes: true } })), "specifications_unknown"));
});

test("literal word cleaning affects names and values only and can combine with swapping", () => {
  const input = twoAxes(); input.title = "加绒 [新] .* 原标题";
  for (const sku of input.skus) {
    sku.specifications[0].name = "[新]尺码";
    sku.specifications[1].value = ".*" + sku.specifications[1].value;
  }
  const before = JSON.stringify(input), result = preview(input, config({ price_policy: formula(), specification_policy: { swap_axes: true, remove_words: "加绒\t[新]\n.*" } }));
  assert.equal(result.business_ready, true);
  assert.deepEqual(result.target_fields.skus[0].specifications, [{ name: "颜色", value: "红色" }, { name: "尺码", value: "M" }]);
  assert.equal(result.target_fields.skus[0].price_value, "115.00");
  assert.equal(result.target_fields.title, input.title); assert.deepEqual(result.target_fields.media, input.media);
  assert.equal(JSON.stringify(input), before); assert.equal(core.normalizeResult(result).ok, true);
});

test("cleaning blocks empty names or values and never adopts a partial transform", () => {
  for (const part of [{ name: "加绒", value: "红" }, { name: "颜色", value: "加绒" }, { name: "颜色", value: " 加绒 " }]) {
    const input = twoAxes(); input.skus[1].specifications[1] = part;
    const result = preview(input, config({ specification_policy: { remove_words: "加绒" } }));
    assert.equal(result.business_ready, false); assert(conflict(result, "specification_empty"));
    assert.deepEqual(result.target_fields.skus[0].specifications, input.skus[0].specifications);
    assert.equal(core.normalizeResult(result).ok, true);
  }
  for (const skus of [[], undefined]) {
    const input = pkg({ skus }); if (skus === undefined) delete input.skus;
    assert(conflict(preview(input, config({ specification_policy: { remove_words: "加绒" } })), "specifications_unknown"));
  }
  const invalid = twoAxes(); invalid.skus[1].specifications[1] = { name: undefined, value: undefined };
  assert.equal(core.normalizePackage(invalid).ok, false);
  assert.equal(preview(invalid, config({ specification_policy: { remove_words: "加绒" } })).structural_ok, false);
});

test("cleaning detects renamed dimension conflicts and duplicate source dimension names", () => {
  for (const names of [["加绒颜色", "颜色"], ["颜色", "颜色"]]) {
    const input = twoAxes();
    for (const sku of input.skus) sku.specifications.forEach((part, index) => { part.name = names[index]; });
    const result = preview(input, config({ specification_policy: { swap_axes: true, remove_words: "加绒" } }));
    assert(conflict(result, "specification_name_conflict")); assert.equal(result.business_ready, false);
    assert.deepEqual(result.target_fields.skus.map(sku => sku.specifications), input.skus.map(sku => sku.specifications));
  }
});

test("cleaning detects combination collisions without merging SKUs or overwriting image links", () => {
  const input = twoAxes(); input.skus[1].specifications = [{ name: "尺码", value: "M" }, { name: "颜色", value: "红色" }];
  const result = preview(input, config({ specification_policy: { remove_words: "加绒" } }));
  assert(conflict(result, "specification_combination_collision")); assert.equal(result.business_ready, false);
  assert.equal(result.target_fields.skus.length, 2);
  assert.deepEqual(result.target_fields.skus.map(sku => sku.specifications), input.skus.map(sku => sku.specifications));
  assert.deepEqual(result.target_fields.media.sku_images, input.media.sku_images);
  assert.equal(core.normalizeResult(result).ok, true);
  input.skus[1].specifications.reverse();
  assert(conflict(preview(input, config({ specification_policy: { remove_words: "加绒" } })), "specification_combination_collision"));
});

test("combination identity uses complete named pairs without delimiter ambiguity", () => {
  const input = twoAxes();
  input.skus[0].specifications = [{ name: "颜色", value: "红|M" }, { name: "尺码", value: "L" }];
  input.skus[1].specifications = [{ name: "颜色", value: "红" }, { name: "尺码", value: "M|L" }];
  assert.equal(preview(input, config({ specification_policy: { remove_words: "加绒" } })).business_ready, true);
});

test("empty specification rules are true no-ops and legacy title replacement remains compatible", () => {
  for (const specification_policy of [{}, { swap_axes: false }, { remove_words: " \t\n " }]) {
    const result = preview(pkg(), config({ specification_policy }));
    assert.equal(result.business_ready, true); assert.deepEqual(result.target_fields, preview().target_fields);
  }
  const result = preview(twoAxes(), config({ specification_policy: { swap_axes: true }, title_replace: { from: "A[1]", to: "旧契约" } }));
  assert.equal(result.target_fields.title, "旧契约 旧契约 纯棉上衣");
});

test("formula and specification rules survive task transitions and affect local fingerprints", () => {
  const input = twoAxes(), cfg = config({ price_policy: formula(), specification_policy: { swap_axes: true, remove_words: "加绒" } });
  let task = core.createTask(input, cfg).task;
  task = core.transitionTask(task, "mapping").task; task = core.transitionTask(task, "preview").task;
  assert.equal(task.result.target_fields.skus[0].price_value, "115.00");
  assert.deepEqual(task.result.target_fields.skus[0].specifications, [{ name: "颜色", value: "红色" }, { name: "尺码", value: "M" }]);
  task = core.transitionTask(task, "localpreviewready").task;
  assert.equal(core.normalizeTask(task).ok, true);
  for (const next of [config({ ...cfg, price_policy: formula("1.2", "0") }), config({ ...cfg, specification_policy: { swap_axes: false, remove_words: "加绒" } })]) assert.notEqual(core.dedupKey(input, cfg), core.dedupKey(input, next));
  for (const intent of ["preview", "platform_draft", "publish"]) {
    const result = preview(input, { ...cfg, intent });
    for (const flag of ["connector_supported", "execution_authorized", "platform_draft_created", "platform_publish_created", "complete"]) assert.equal(result[flag], false);
    if (intent !== "preview") { assert.equal(result.business_ready, false); assert(conflict(result, "connector_unavailable")); }
  }
});

test("new policy accessors are rejected without execution", () => {
  let accessed = 0;
  for (const [key, rule, field] of [["price_policy", formula(), "factor"], ["specification_policy", { swap_axes: true }, "remove_words"]]) {
    Object.defineProperty(rule, field, { enumerable: true, get() { accessed++; throw new Error("must not run"); } });
    assert.equal(core.normalizeConfig(config({ [key]: rule })).ok, false);
  }
  assert.equal(accessed, 0);
});

test("many invalid specification cells keep blocked previews bounded and serializable", () => {
  const input = pkg({ skus: Array.from({ length: 100 }, (_, i) => ({ sku_id: String(i), price_raw: "10", stock_raw: "1", specifications: Array.from({ length: 15 }, () => ({ name: "清理", value: "清理" })) })) });
  const result = preview(input, config({ specification_policy: { remove_words: "清理" } }));
  assert.equal(result.structural_ok, true); assert.equal(result.business_ready, false);
  assert(conflict(result, "specification_empty")); assert(conflict(result, "specification_name_conflict")); assert(conflict(result, "specification_combination_collision"));
  assert(result.conflicts.length < 10); assert.equal(core.normalizeResult(result).ok, true);
});
const enumAttribute=()=>({name:"合成形态",value:"合成液体",source_metadata:{property_id:"09001",selected:[{value_id:"08001",value_name:"合成液体"}],ui_type_raw:"select",property_type_raw:3,cardinality:"unknown"}});
test("optional enum metadata survives normalization JSON import and preview",()=>{const p=pkg({attributes:[enumAttribute()]}),n=core.normalizePackage(p);assert.equal(n.ok,true);assert.deepEqual(n.product.attributes,p.attributes);assert.deepEqual(core.normalizePackage(JSON.parse(JSON.stringify(n.product))).product.attributes,p.attributes);const r=preview(p);assert.deepEqual(r.target_fields.attributes,p.attributes);assert.equal(core.normalizeResult(r).ok,true);});
test("metadata duplicate selections and display mismatch rejected",()=>{for(const mutate of [a=>a.source_metadata.selected.push({...a.source_metadata.selected[0]}),a=>a.value="错显示",a=>a.source_metadata.selected[0].value_id="",a=>a.source_metadata.cardinality="single"]){const a=enumAttribute();mutate(a);assert.equal(core.normalizePackage(pkg({attributes:[a]})).ok,false);}});
test("metadata accessors and credential additions are invalid without reads",()=>{const a=enumAttribute();let n=0;Object.defineProperty(a.source_metadata,"property_id",{get(){n++;return"09001";}});assert.equal(core.normalizePackage(pkg({attributes:[a]})).ok,false);assert.equal(n,0);const b=enumAttribute();b.source_metadata.token="secret";assert.equal(core.normalizePackage(pkg({attributes:[b]})).ok,false);});
test("specifications do not inherit attribute enum metadata capability",()=>{const p=pkg();p.skus[0].specifications[0].source_metadata=enumAttribute().source_metadata;assert.equal(core.normalizePackage(p).ok,false);});
test("duplicate source property identities cannot survive as different names",()=>{const a=enumAttribute(),b=enumAttribute();b.name="另一名称";assert.equal(core.normalizePackage(pkg({attributes:[a,b]})).ok,false);});
test("draft source subtype normalizes and roundtrips only exact current draft URL",()=>{const p=pkg();p.source.source_kind='doudian_draft_product';p.source.source_url='https://fxg.jinritemai.com/ffa/g/create?product_id='+p.source.product_id+'&entrance=draft';assert.equal(core.normalizePackage(p).ok,true);assert.equal(core.normalizePackage(JSON.parse(JSON.stringify(p))).product.source.source_kind,'doudian_draft_product');for(const suffix of ['&entrance=edit','&product_id='+p.source.product_id,'&page=2']){const q=JSON.parse(JSON.stringify(p));q.source.source_url+=suffix;assert.equal(core.normalizePackage(q).ok,false);}p.source.source_url=p.source.source_url.replace('entrance=draft','entrance=edit');assert.equal(core.normalizePackage(p).ok,false);});
process.stdout.write(tests + " product move core tests passed.\n");
