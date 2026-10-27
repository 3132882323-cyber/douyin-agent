"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const core = require("./shelf-detail-core.js");
const linkCore = require("./shelf-tools-core.js");

// All rows, IDs, shop context and media paths are synthetic. URLs are parser
// fixtures only: this test never opens them or accesses a browser/shop session.
const PRODUCT_ID = "1234567890123456789";
const SKU_ID = "SYNTHETIC_SKU_01";
const PRODUCT_URL = "https://haohuo.jinritemai.com/views/product/detail?id=" + PRODUCT_ID;
const SOURCE_URL = "https://fxg.jinritemai.com/ffa/g/list";
const IMAGE_URL = "https://p3.pstatp.com/product/synthetic-main.jpg";
const captured = "2026-10-01T00:00:00.000Z";
const image = (url = IMAGE_URL, extra = {}) => ({ url, alt: "合成商品主图", kind: "row_image", source: "visible_dom_img", ...extra });
const row = (extra = {}) => ({ product_id: PRODUCT_ID, sku_id: SKU_ID, product_name: "合成测试商品", sku_text: "红色 / M", merchant_code: "SYNTHETIC-001", price_text: "12.80", stock_text: "0", ...extra });
const snapshot = (rows = [row()], extra = {}) => ({ scope: "current_visible_rows", context_key: "synthetic:current-store:1", source_url: SOURCE_URL, captured_at: captured, rows, ...extra });
const build = (rows, extra) => core.buildProductDetail(snapshot(rows, extra), PRODUCT_ID);
const warning = (detail, pattern) => detail.warnings.some((message) => pattern.test(message));
let tests = 0;
function test(name, run) { run(); tests++; process.stdout.write("ok - " + name + "\n"); }

test("CommonJS and browser exports are frozen and need no network or DOM", () => {
  assert(Object.isFrozen(core));
  assert.deepEqual(Object.keys(core).sort(), ["buildProductDetail", "normalizeAssetUrl", "normalizeVideoAssetUrl", "toAssetsCsv", "toCopyText", "toDetailCsv"].sort());
  let externalCalls = 0;
  const unavailable = () => { externalCalls++; throw new Error("No external operation is allowed"); };
  const context = vm.createContext({ URL, DianShelfToolsCore: linkCore, fetch: unavailable, Image: unavailable });
  vm.runInContext(fs.readFileSync(require.resolve("./shelf-detail-core.js"), "utf8"), context);
  assert.equal(typeof context.DianShelfDetailCore.buildProductDetail, "function");
  const result = context.DianShelfDetailCore.buildProductDetail(snapshot(), PRODUCT_ID);
  assert.equal(result.ok, true);
  assert.equal(result.product_id, PRODUCT_ID);
  assert.equal(externalCalls, 0);
});

test("empty data and non-exact product identifiers fail without guessing", () => {
  for (const input of [undefined, {}, snapshot([]), snapshot([row({ product_id: "SYNTHETIC_OTHER" })])]) {
    const result = core.buildProductDetail(input, PRODUCT_ID);
    assert.equal(result.ok, false);
    assert.deepEqual(result.rows, []);
    assert.deepEqual(result.images, []);
  }
  for (const id of [undefined, 123, Number(PRODUCT_ID), "", " " + PRODUCT_ID, PRODUCT_ID + "/1"]) {
    assert.equal(core.buildProductDetail(snapshot(), id).ok, false);
  }
});

test("scope context and maximum row count remain explicit", () => {
  for (const extra of [{ scope: "all_products" }, { context_key: "" }, { context_key: "\u200b" }, { rows: Array(201).fill(row()) }]) {
    assert.equal(core.buildProductDetail(snapshot(undefined, extra), PRODUCT_ID).ok, false);
  }
  assert.equal(build(Array.from({ length: 200 }, (_, i) => row({ sku_id: "SYNTHETIC_SKU_" + i }))).rows.length, 200);
});

test("exact long IDs are preserved and other products are excluded without mutation", () => {
  const input = snapshot([row(), row({ product_id: "SYNTHETIC_OTHER", product_name: "不属于所选商品" })]);
  const before = JSON.stringify(input), detail = core.buildProductDetail(input, PRODUCT_ID);
  assert.equal(detail.ok, true);
  assert.equal(detail.rows.length, 1);
  assert.equal(detail.rows[0].product_id, PRODUCT_ID);
  assert.equal(detail.rows[0].sku_id, SKU_ID);
  assert.equal(JSON.stringify(input), before);
  assert.notEqual(detail.rows[0], input.rows[0]);
  assert(warning(detail, /当前可见行/));
});

test("missing or invalid capture time and source links stay unknown", () => {
  for (const captured_at of [undefined, "2026-02-30T00:00:00Z", "not-a-time"]) {
    const detail = build(undefined, { captured_at, source_url: "https://untrusted.example/product" });
    assert.equal(detail.ok, true);
    assert.equal(detail.captured_at, "");
    assert.equal(detail.source_url, "");
    assert(warning(detail, /读取时间未知/));
    assert(warning(detail, /安全校验/));
  }
  const absent = build(undefined, { source_url: undefined });
  assert(warning(absent, /未提供/));
  assert.equal(build().captured_at, captured);
});

test("missing prices stock and SKU identifiers are never invented or merged", () => {
  const detail = build([row({ sku_id: "", price_text: "", stock_text: "" }), row({ sku_id: "", price_text: undefined, stock_text: undefined })]);
  assert.equal(detail.ok, true);
  assert.equal(detail.rows.length, 2);
  for (const entry of detail.rows) {
    assert.equal(entry.sku_id, "");
    assert.equal(entry.price_text, "");
    assert.equal(entry.stock_text, "");
  }
  assert(warning(detail, /没有 SKU ID/));
  assert(warning(detail, /空值未转换为 0/));
  assert.equal(build().rows[0].stock_text, "0");
});

test("invalid SKU identity and source identity loss block the whole detail", () => {
  for (const extra of [{ sku_id: 1 }, { sku_id: "bad/id" }, { bulk_source_issues: ["product_id 精度丢失"] }, { bulk_source_issues: ["sku_id 原值损失"] }]) {
    const detail = build([row(), row(extra)]);
    assert.equal(detail.ok, false);
    assert.deepEqual(detail.rows, []);
    assert.deepEqual(detail.images, []);
  }
  const damaged = build([row({ bulk_source_issues: ["stock_text 原值未完整读取"] })]);
  assert.equal(damaged.ok, true);
  assert.deepEqual(damaged.rows[0].bulk_source_issues, ["stock_text 原值未完整读取"]);
  assert(warning(damaged, /后续批量修改必须保持阻止/));
});

test("conflicting titles and duplicate SKU rows remain visible for review", () => {
  const duplicate = build([row(), row()]);
  assert.equal(duplicate.rows.length, 2);
  assert(warning(duplicate, /重复可见行/));
  const conflict = build([row(), row({ product_name: "另一合成标题", price_text: "15.00" })]);
  assert.equal(conflict.product_name, "");
  assert.equal(conflict.rows[1].product_name, "另一合成标题");
  assert(warning(conflict, /可见数据存在冲突/));
  assert(warning(conflict, /汇总标题留空/));
});

test("safe existing row links are deduplicated and tracking is removed", () => {
  const detail = build([row({ links: [
    { url: PRODUCT_URL + "&utm_source=synthetic", label: "合成详情", source: "visible_dom_href" },
    PRODUCT_URL,
    { url: PRODUCT_URL + "&sku_id=" + SKU_ID, source: "unverified" }
  ] })]);
  assert.equal(detail.ok, true);
  assert.equal(detail.rows[0].links.length, 2);
  assert.equal(detail.rows[0].url, PRODUCT_URL);
  assert.equal(detail.rows[0].links[0].source, "visible_dom_href");
  assert.equal(detail.rows[0].links[1].source, "");
  assert(!JSON.stringify(detail.rows[0].links).includes("utm_source"));
});

test("cross-domain unsafe short and missing links are not promoted into detail URLs", () => {
  for (const url of [undefined, "https://untrusted.example/product", "https://haohuo.jinritemai.com.evil.example/product", "https://v.douyin.com/SyntheticOnly/", PRODUCT_URL + "&token=synthetic-rejected"]) {
    const detail = build([row({ url })]);
    assert.equal(detail.ok, true);
    assert.equal(detail.rows[0].url, "");
    assert.deepEqual(detail.rows[0].links, []);
  }
});

test("product or SKU conflicts in existing links fail atomically", () => {
  for (const url of ["https://haohuo.jinritemai.com/views/product/detail?id=SYNTHETIC_OTHER", PRODUCT_URL + "&sku_id=SYNTHETIC_OTHER_SKU", PRODUCT_URL + "&product_id=SYNTHETIC_OTHER"]) {
    const detail = build([row({ images: [image()] }), row({ url })]);
    assert.equal(detail.ok, false);
    assert.deepEqual(detail.rows, []);
    assert.deepEqual(detail.images, []);
  }
});

test("image URLs accept only HTTPS trusted CDN hosts and unambiguous public paths", () => {
  for (const host of ["pstatp.com", "p3.pstatp.com", "byteimg.com", "douyinpic.com", "ecombdimg.com"]) {
    assert.equal(core.normalizeAssetUrl("https://" + host + "/product/synthetic.jpg").ok, true);
  }
  for (const url of ["http://p3.pstatp.com/product/a.jpg", "https://p3.pstatp.com.evil.example/a.jpg", "https://untrusted.example/a.jpg", "https://user:password@p3.pstatp.com/a.jpg", "https://p3.pstatp.com:444/a.jpg", IMAGE_URL + "#fragment", IMAGE_URL + "?token=synthetic-rejected", IMAGE_URL + "?redirect=https%3A%2F%2Funtrusted.example", "https://p3.pstatp.com/private/a.jpg", "https://p3.pstatp.com/avatar/a.jpg", "https://p3.pstatp.com/product/a.svg", "https://p3.pstatp.com/product/a%252ejpg", "https://p3.pstatp.com/product/a%0Ajpg"]) {
    const result = core.normalizeAssetUrl(url);
    assert.equal(result.ok, false, url);
    assert.equal(result.url, "");
  }
});

test("video validation is separate and does not accept playlists or scripts", () => {
  const video = "https://v1.douyinvod.com/product/synthetic.mp4";
  assert.equal(core.normalizeVideoAssetUrl(video).ok, true);
  assert.equal(core.normalizeAssetUrl(video).ok, false);
  for (const suffix of ["m3u8", "mpd", "html", "js"]) {
    assert.equal(core.normalizeVideoAssetUrl("https://v1.douyinvod.com/product/synthetic." + suffix).ok, false);
  }
  assert.equal(core.normalizeVideoAssetUrl(video + "?signature=synthetic-rejected").ok, false);
});

test("images require visible row provenance and are deduplicated across rows", () => {
  const detail = build([row({ images: [image(), image(), image(IMAGE_URL, { kind: "avatar" }), image(IMAGE_URL, { source: "network_response" })] }), row({ sku_id: "SYNTHETIC_SKU_02", images: [image()] })]);
  assert.equal(detail.rows[0].images.length, 1);
  assert.equal(detail.rows[1].images.length, 1);
  assert.equal(detail.images.length, 1);
  assert(warning(detail, /非可见商品行来源/));
  assert.equal(build([row({ images: Array.from({ length: 9 }, () => image()) })]).images.length, 0);
});

test("aggregate image lists stay bounded without claiming all images were retained", () => {
  const rows = Array.from({ length: 13 }, (_, i) => row({ sku_id: "SYNTHETIC_SKU_" + i, images: Array.from({ length: 8 }, (_, j) => image("https://p3.pstatp.com/product/synthetic-" + i + "-" + j + ".jpg")) }));
  const detail = build(rows);
  assert.equal(detail.ok, true);
  assert.equal(detail.images.length, 100);
  assert(warning(detail, /100 张上限/));
  assert.equal(core.toAssetsCsv(detail).split("\r\n").length, 102);
});

test("only explicit allowed attributes are retained and conflicts are not resolved", () => {
  const detail = build([row({ attributes: [{ name: "商品品牌", value: "合成品牌甲" }, { name: "品牌", value: "合成品牌甲" }, { name: "地址", value: "合成但不允许" }] }), row({ sku_id: "SYNTHETIC_SKU_02", attributes: [{ name: "品牌", value: "合成品牌乙" }, { name: "类目", value: "合成类目" }] })]);
  assert.equal(detail.attributes.length, 3);
  assert.deepEqual(detail.attributes[0], { name: "品牌", value: "合成品牌甲" });
  assert(warning(detail, /仅保留明确读取/));
  assert(warning(detail, /品牌 属性存在不同值/));
  assert.equal(build([row({ attributes: Array(13).fill({ name: "品牌", value: "合成品牌" }) })]).attributes.length, 0);
});

test("own data fields are read without executing accessors or inheriting identities", () => {
  let getters = 0;
  const guarded = row();
  Object.defineProperty(guarded, "price_text", { get() { getters++; return "999"; } });
  const guardedImage = image();
  Object.defineProperty(guardedImage, "url", { get() { getters++; return IMAGE_URL; } });
  guarded.images = [guardedImage];
  const detail = build([Object.create(row()), guarded]);
  assert.equal(detail.rows.length, 1);
  assert.equal(detail.rows[0].price_text, "");
  assert.equal(detail.images.length, 0);
  assert.equal(getters, 0);
  assert(warning(detail, /未完整读取/));
});

test("invalid or overlong display text remains unknown and carries loss markers", () => {
  const detail = build([row({ product_name: "x".repeat(4001), stock_text: "1\u200b2", reserved_stock_text: 0 })]);
  assert.equal(detail.ok, true);
  assert.equal(detail.rows[0].product_name, "");
  assert.equal(detail.rows[0].stock_text, "");
  assert.equal(detail.rows[0].reserved_stock_text, "无效预留库存");
  assert(detail.rows[0].bulk_source_issues.length >= 3);
  assert(warning(detail, /商品标题未读取/));
});

test("detail CSV protects exact IDs codes formulas and quotes without extra source fields", () => {
  const detail = build([row({ product_name: '=HYPERLINK("synthetic","fixture")', sku_text: '合成, "规格"\n第二行', merchant_code: "000001", credentials: "SYNTHETIC_NOT_EXPORTED" })]);
  const csv = core.toDetailCsv(detail);
  assert(csv.startsWith('\uFEFF"商品ID","SKU ID"'));
  assert(csv.includes('"\'' + PRODUCT_ID + '","\'' + SKU_ID + '"'));
  assert(csv.includes('"\'000001"'));
  assert(csv.includes('"\'=HYPERLINK(""synthetic"",""fixture"")"'));
  assert(csv.includes('"合成, ""规格""\n第二行"'));
  assert(csv.includes("当前可见行·未提交平台"));
  assert(!csv.includes("SYNTHETIC_NOT_EXPORTED"));
});

test("exports reject invalid details and cross-product rows", () => {
  const invalid = core.buildProductDetail(snapshot([]), PRODUCT_ID);
  for (const exporter of [core.toDetailCsv, core.toAssetsCsv, core.toCopyText]) {
    assert.throws(() => exporter(invalid), TypeError);
    assert.throws(() => exporter({ ...build(), rows: [row({ product_id: "SYNTHETIC_OTHER" })] }), TypeError);
  }
});

test("asset CSV rechecks URLs and provenance and remains a list without downloading", () => {
  const detail = build([row({ images: [image()] })]);
  detail.images.push(image(), image("https://untrusted.example/a.jpg"), image(IMAGE_URL, { source: "network_response" }));
  const csv = core.toAssetsCsv(detail);
  assert.equal(csv.split("\r\n").length, 3);
  assert(csv.includes(IMAGE_URL));
  assert(csv.includes("仅资源清单·未下载"));
  assert(!csv.includes("untrusted.example"));
  assert.throws(() => core.toAssetsCsv({ ...detail, images: Array(101).fill(image()) }), TypeError);
});

test("copy output and bounded warnings retain unknown values and visible-row limits", () => {
  const detail = build([row({ sku_id: "", sku_text: "合成\n规格\tM", price_text: "", stock_text: "" })], { captured_at: undefined, warnings: Array.from({ length: 21 }, (_, i) => "合成提示 " + i) });
  const text = core.toCopyText(detail);
  assert(text.includes("SKU：未读取"));
  assert(text.includes("合成 规格 M"));
  assert(text.includes("价格：未读取 | 库存：未读取"));
  assert(text.includes("读取时间：未知"));
  assert(text.includes("未提交平台"));
  assert.equal(detail.warnings.filter((message) => message.startsWith("采集提示：")).length, 20);
  assert(warning(detail, /超过 20 条/));
});

process.stdout.write(tests + " shelf detail public tests passed.\n");
