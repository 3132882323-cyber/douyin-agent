"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const api = require("./shelf-tools-core.js");
// All IDs, values and URLs below are synthetic parser/export fixtures. No URL is opened.
const origin = "https://haohuo.jinritemai.com";
const longId = "00012345678901234567890123456789";
const product = origin + "/views/product/detail?product_id=" + longId;

assert.deepEqual(Object.keys(api).sort(), ["catalogHealth", "matchesCatalogHealth", "normalizeCatalogRows", "normalizeLink", "parseLinks", "titleChecks", "toCsv", "toLinksCsv"]);
assert.ok(Object.isFrozen(api));
const context = vm.createContext({ URL, Set, Map, Object });
vm.runInContext(fs.readFileSync(path.join(__dirname, "shelf-tools-core.js"), "utf8"), context);
assert.equal(typeof context.DianShelfToolsCore.normalizeLink, "function");
assert.equal(context.DianShelfToolsCore.normalizeLink(product).product_id, longId);

for (const host of ["fxg.jinritemai.com", "haohuo.jinritemai.com", "haohuo.snssdk.com", "www.douyin.com", "douyin.com", "v.douyin.com"]) {
  assert.equal(api.normalizeLink("https://" + host + "/existing-route").ok, true, host);
}
assert.equal(api.normalizeLink("https://WWW.DOUYIN.COM:443/existing").url, "https://www.douyin.com/existing");
const ordinary = api.normalizeLink(product + "&sku_id=0007&campaign=autumn&utm_source=share&spm=anything#details");
assert.equal(ordinary.ok, true);
assert.equal(ordinary.product_id, longId);
assert.equal(ordinary.sku_id, "0007");
assert.equal(ordinary.kind, "product_link");
assert.equal(ordinary.url, product + "&sku_id=0007&campaign=autumn#details");
assert.equal(api.normalizeLink(origin + "/product?variant=red%20blue&quantity=1%2B2").url,
  origin + "/product?variant=red%20blue&quantity=1%2B2", "Unknown safe params retain their exact encoding");
assert.equal(api.normalizeLink(origin + "/product?barcode=123&province_code=31&estate=abc&author=shop").ok, true,
  "Do not mistake unrelated semantic parameters for an exact code/state/auth key");
assert.equal(api.normalizeLink("/real?product_id=123", origin + "/page").url, origin + "/real?product_id=123");
assert.equal(api.normalizeLink("123", origin).ok, false, "Never manufacture links from IDs");
assert.equal(api.normalizeLink("/real", "https://evil.example/").ok, false);
assert.equal(api.normalizeLink(origin + "/product/123456789").product_id, "", "No path ID guesses");
assert.equal(api.normalizeLink(origin + "/product?id=123456789").product_id, "", "No generic ID guesses");
const syntheticDetail = origin + "/views/product/detail?id=1234567890123456789";
assert.equal(api.normalizeLink(syntheticDetail).product_id, "1234567890123456789");
assert.equal(api.normalizeLink(syntheticDetail).kind, "product_link");
assert.equal(api.normalizeLink(origin + "/views/product/detail?id=" + longId).product_id, longId);
assert.equal(api.normalizeLink(syntheticDetail + "&id=1234567890123456789").product_id, "1234567890123456789");
for (const suffix of ["&id=2", "&product_id=2", "&productId=2", "#product_id=2"]) {
  const conflictingDetail = api.normalizeLink(syntheticDetail + suffix);
  assert.equal(conflictingDetail.ok, true);
  assert.equal(conflictingDetail.product_id, "", "Conflicting detail ID " + suffix);
  assert.match(conflictingDetail.warning, /冲突/);
}
for (const nonDetail of ["https://haohuo.snssdk.com/views/product/detail?id=123",
  "https://www.douyin.com/views/product/detail?id=123", origin + "/views/product/detail/?id=123",
  origin + "/views/product/Detail?id=123", origin + "/views/product/details?id=123",
  origin + "/views/product/%64etail?id=123", origin + "/#/views/product/detail?id=123",
  origin + "/views/product/detail#id=123"]) {
  assert.equal(api.normalizeLink(nonDetail).product_id, "", "Exact format only: " + nonDetail);
}
for (const key of ["product_id", "productId", "commodity_id", "goods_id"]) assert.equal(api.normalizeLink(origin + "/p?" + key + "=" + longId).product_id, longId);
assert.equal(api.normalizeLink(origin + "/p?skuId=0003").sku_id, "0003");
for (const suffix of ["&product_id=2", "&productId=2", "#/product?commodity_id=2"]) {
  const conflict = api.normalizeLink(product + suffix);
  assert.equal(conflict.ok, true);
  assert.equal(conflict.product_id, "");
  assert.match(conflict.warning, /冲突/);
}
assert.equal(api.normalizeLink(product + "&goods_id=" + longId).product_id, longId);
assert.match(api.normalizeLink(origin + "/p?product_id=").warning, /不明确/);
const hashRoute = api.normalizeLink(origin + "/#/product?product_id=" + longId + "&skuId=09&utm_campaign=test");
assert.equal(hashRoute.product_id, longId);
assert.equal(hashRoute.sku_id, "09");
assert.equal(hashRoute.url, origin + "/#/product?product_id=" + longId + "&skuId=09");
assert.equal(api.normalizeLink(product + "#utm_source=share&spm=a").url, product);
assert.equal(api.normalizeLink(product + "#section-2").url, product + "#section-2");
assert.equal(api.normalizeLink(product + "#/product?utm_source=a").url, product + "#/product");
const short = api.normalizeLink("https://v.douyin.com/AbC123/?product_id=" + longId);
assert.equal(short.kind, "short_link");
assert.equal(short.product_id, "");
assert.equal(short.sku_id, "");
assert.match(short.warning, /未展开/);

for (const unsafe of [null, {}, 123, "", "123456789", "javascript:alert(1)", "http://www.douyin.com/p",
  "https://evil.example/p", "https://www.douyin.com.evil.example/p", "https://evilwww.douyin.com/p",
  "https://www.douyin.com./p", "https://www.douyin.com@evil.example/p", "https://user:pass@www.douyin.com/p",
  "https://@www.douyin.com/p", "https://www.douyin.com:444/p", "https://127.0.0.1/p", "https://[::1]/p",
  "https://%77ww.douyin.com/p", "https://ｗｗｗ.douyin.com/p", "https:\\\\www.douyin.com/p",
  "https://www.douyin.com\n.evil.example/p", "https://www.douyin.com/\tproduct", origin + "/p%0Athing",
  origin + "/p%252Fthing", origin + "/p%2Fthing", origin + "/p%5cthing", origin + "/p%zz",
  origin + "/p%ED%A0%80", origin + "/private/../product", origin + "/%70rivate/product",
  origin + "/order;anything", origin + "/order%3banything", origin + "/product;jsessionid=secret",
  origin + "/p?x=%2526token%253Dsecret", origin + "/p?x=a;token=secret", origin + "/p?x=%00",
  origin + "/p?%26token=secret", origin + "/p?x=https%3A%2F%2Fevil.example", origin + "/p?x=%2F%2Fevil.example",
  origin + "/p#https://evil.example", origin + "/p#https://evil.example?variant=1", origin + "/p#token=secret", origin + "/p#/orders/123",
  origin + "/p\u202eevil", origin + "/p?x=%E2%80%8B", origin + "/p?x=%EF%BB%BF"]) {
  const result = api.normalizeLink(unsafe);
  assert.equal(result.ok, false, String(unsafe));
  assert.equal(result.url, "");
  assert.equal(result.product_id, "");
  assert.ok(result.warning);
}
for (const route of ["private", "order", "orders", "pay", "payment", "login", "auth", "session", "oauth2", "checkout", "cashier", "account", "wallet", "refund"]) {
  assert.equal(api.normalizeLink(origin + "/" + route + "/123").ok, false, route);
  assert.equal(api.normalizeLink(origin + "/#/" + route + "/123").ok, false, "hash " + route);
}
for (const key of ["token", "access_token", "CODE", "state", "password", "signature", "signAture", "auth",
  "session_id", "shop_id", "shopId", "store_key", "seller_id", "merchant_id", "user_id", "account_id",
  "redirect_uri", "return_url", "nestedurl", "targetUrl", "callback", "__proto__", "constructor", "prototype",
  "%74oken", "shop%5Fid", "api-key", "csrf_token", "ｔｏｋｅｎ"]) {
  assert.equal(api.normalizeLink(origin + "/p?" + key + "=secret").ok, false, key);
  assert.equal(api.normalizeLink(origin + "/p#/detail?" + key + "=secret").ok, false, "hash " + key);
}

const parsed = api.parseLinks("商品分享「" + product + "&sku_id=1&utm_source=a」\n" + product + "&sku_id=1\n"
  + product + "&sku_id=2\nhttps://evil.example/product\nhttps://v.douyin.com/A12/。");
assert.equal(parsed.rows.length, 3);
assert.equal(parsed.rows[0].sku_id, "1");
assert.equal(parsed.rows[1].sku_id, "2");
assert.equal(parsed.duplicate_count, 1);
assert.equal(parsed.rejected_count, 1);
assert.equal(parsed.truncated, false);
assert.ok(parsed.warnings.length);
assert.equal(api.parseLinks("分享（" + product + "）").rows.length, 1);
assert.equal(api.parseLinks("分享 (" + product + ")").rows[0].url, product);
assert.equal(api.parseLinks("123456789").rows.length, 0);
assert.equal(api.parseLinks({}).rows.length, 0);
const capped = api.parseLinks(Array.from({ length: 205 }, (_, n) => origin + "/p?product_id=" + n).join("\n"));
assert.equal(capped.rows.length, 200);
assert.equal(capped.truncated, true);
assert.match(capped.warnings.join(" "), /200/);
const cappedText = api.parseLinks(product + "\n" + "x".repeat(100010));
assert.equal(cappedText.truncated, true);
assert.equal(cappedText.rows.length, 1);
assert.match(cappedText.warnings.join(" "), /100000/);
assert.equal(api.parseLinks(origin + "/" + "x".repeat(100010)).rows.length, 0);
assert.equal(api.normalizeLink(origin + "/" + "x".repeat(8192)).ok, false);
assert.equal(api.normalizeLink(origin + "/" + "商".repeat(1000)).ok, false, "Bound the encoded URL too, so CSV never truncates it");

let getterRead = false;
const row = { product_name: "原名", name: "别名", product_id: longId, sku_id: "0008", merchant_code: "000123",
  sku_text: "红色 / 大号", spec: "不采用", price_text: "¥12.80 起", stock_text: "—", captured_at: "2026-09-24T08:00:00Z",
  token: "secret", shop_id: "private-shop", cost: 2, raw_html: "<script>bad</script>",
  links: [{ url: product, label: "商品详情", source: "visible_dom_href", token: "secret" }, { url: "https://evil.example", label: "bad" },
    { url: product + "&utm_source=a", label: "重复" }, { url: product + "&sku_id=1", label: "SKU1" },
    { url: product + "&sku_id=2", label: "SKU2" }, { url: product + "&sku_id=3", label: "过量" }] };
Object.defineProperty(row, "price", { get() { getterRead = true; throw new Error("do not read"); } });
const inherited = Object.create({ product_name: "inherited", product_id: "123", url: product });
const accessor = {};
Object.defineProperty(accessor, "product_name", { get() { getterRead = true; throw new Error("do not read"); } });
const catalog = api.normalizeCatalogRows([row, inherited, accessor, { name: "别名", spec: "规格" },
  { product_id: 9007199254740993, sku_id: 42, url: product }, JSON.parse('{"__proto__":{"polluted":true},"product_name":"安全"}'), null]);
assert.equal(catalog.length, 6);
assert.equal(getterRead, false);
assert.deepEqual(Object.keys(catalog[0]).sort(), ["product_name", "product_id", "sku_id", "merchant_code", "sku_text", "price_text", "stock_text", "links", "url", "captured_at"].sort());
assert.equal(catalog[0].product_id, longId);
const stockHints=api.normalizeCatalogRows([{stock_text:'0',stock_hint_text:'部分SKU售罄'},{stock_text:'42',stock_hint_text:'可以自动追投'}]);
assert.equal(stockHints[0].stock_text,'0');
assert.equal(stockHints[0].stock_hint_text,'部分SKU售罄');
assert.equal(Object.hasOwn(stockHints[1],'stock_hint_text'),false);
assert(stockHints[1].bulk_source_issues.some(issue=>/库存提示/.test(issue)));
const priceHints=api.normalizeCatalogRows([{price_text:'10.00–20.00',price_hint_text:'已获优价推手曝光扶持'},{price_text:'0.00',price_hint_text:'建议改价提销量'},{price_hint_text:'已自动提报'}]);
assert.equal(priceHints[0].price_hint_text,'已获优价推手曝光扶持');
assert.equal(priceHints[1].price_text,'0.00');
assert.equal(Object.hasOwn(priceHints[2],'price_hint_text'),false);
assert(priceHints[2].bulk_source_issues.some(issue=>/价格提示/.test(issue)));
assert.equal(catalog[0].sku_text, "红色 / 大号");
assert.equal(catalog[0].links.length, 3);
assert.equal(catalog[0].url, product);
assert.deepEqual(Object.keys(catalog[0].links[0]), ["url", "label", "source"]);
assert.equal(catalog[1].product_name, "");
assert.equal(catalog[1].url, "");
assert.equal(catalog[2].product_name, "");
assert.equal(catalog[3].product_name, "别名");
assert.equal(catalog[3].sku_text, "规格");
assert.equal(catalog[4].product_id, "");
assert.equal(catalog[4].sku_id, "42");
assert.equal({}.polluted, undefined);
assert.equal(api.normalizeCatalogRows(Array.from({ length: 205 }, () => row)).length, 200);
assert.deepEqual(api.normalizeCatalogRows(null), []);
assert.equal(api.normalizeCatalogRows([{ url: origin + "/orders/1" }])[0].url, "");
assert.equal(api.normalizeCatalogRows([{ product_id: "1".repeat(129) }])[0].product_id, "", "Never silently truncate IDs");

const csv = api.toCsv([{ ...row, product_name: '=HYPERLINK("https://evil.example","run")', sku_text: "line 1\nline 2, \"quote\"",
  price_text: "+SUM(1,2)", stock_text: "\t@SUM(1,2)" }, { product_name: "  -cmd|' /C calc'!A0", merchant_code: "=bad",
  price_text: "＝1+1", stock_text: "\r\n=1+1", url: origin + "/pay" }]);
assert.ok(csv.startsWith('\uFEFF"商品名称","商品ID","SKU ID","商家编码","规格","价格（页面原文）","库存（页面原文）","商品链接","读取时间"\r\n'));
assert.ok(csv.includes('"\'' + longId + '"'));
assert.ok(csv.includes('"\'0008"'));
assert.ok(csv.includes('"\'000123"'));
assert.ok(csv.includes('"\'=HYPERLINK(""https://evil.example"",""run"")"'));
assert.ok(csv.includes('"line 1\nline 2, ""quote"""'));
assert.ok(csv.includes('"\'+SUM(1,2)"'));
assert.ok(csv.includes('"\'\t@SUM(1,2)"'));
assert.ok(csv.includes('"\'  -cmd|\' /C calc\'!A0"'));
assert.ok(csv.includes('"\'＝1+1"'));
assert.ok(csv.includes('"\'\r\n=1+1"'));
assert.ok(!csv.includes('="' + longId));
assert.ok(!csv.includes("private-shop"));
assert.ok(!csv.includes(origin + "/pay"));
assert.equal(api.toCsv([]).split("\r\n").length, 2);
const longUrl = origin + "/" + "x".repeat(3000) + "?product_id=" + longId;
assert.equal(api.normalizeLink(longUrl).ok, true);
assert.ok(api.toCsv([{ url: longUrl }]).includes('"' + longUrl + '"'), "Export must preserve an accepted URL in full");
const textIds = api.toCsv([{ product_id: "1E10", sku_id: "01-02", merchant_code: "A001" }]);
assert.ok(textIds.includes('"\'1E10","\'01-02","\'A001"'), "Every nonempty ID/code must stay text in Excel");

const linkCsv = api.toLinksCsv([
  { url: syntheticDetail + "&sku_id=" + longId + "&utm_source=share", product_id: "FORGED", kind: "verified", warning: "=HYPERLINK(unsafe)" },
  { url: "https://v.douyin.com/AbC123/?product_id=888", product_id: "888", sku_id: "9", kind: "product_link", warning: "" },
  { url: origin + "/p?product_id=1E10&sku_id=01-02" },
  { url: syntheticDetail + "&product_id=2" },
  { url: origin + "/ordinary" },
  { url: "https://evil.example/p?product_id=123" },
  { url: syntheticDetail + "&sku_id=" + longId },
  { url: origin + "/private?token=secret" },
]);
assert.ok(linkCsv.startsWith('\uFEFF"链接","URL中的商品ID（未核验）","URL中的SKU ID（未核验）","链接类型","说明"\r\n'));
assert.ok(linkCsv.includes('"\'1234567890123456789","\'' + longId + '"'));
assert.ok(linkCsv.includes('"https://v.douyin.com/AbC123/?product_id=888","","","短链接（未解析）"'));
assert.ok(linkCsv.includes("短链接未展开"));
assert.ok(linkCsv.includes('"\'1E10","\'01-02"'));
assert.ok(linkCsv.includes("有冲突或格式不明确，未提取该 ID"));
assert.ok(linkCsv.includes("官方域名链接（未核验）"));
assert.equal((linkCsv.match(/仅解析 URL 文本/g) || []).length, 5, "Every exported row carries the unverified explanation");
assert.ok(!linkCsv.includes("FORGED"));
assert.ok(!linkCsv.includes("HYPERLINK"));
assert.ok(!linkCsv.includes("evil.example"));
assert.ok(!linkCsv.includes("token=secret"));
assert.ok(!linkCsv.includes("utm_source"));
assert.equal(linkCsv.split("\r\n").length, 7, "Unsafe and duplicate rows are not exported");
assert.ok(api.toLinksCsv([{ url: longUrl }]).includes('"' + longUrl + '"'));
assert.equal(api.toLinksCsv(null).split("\r\n").length, 2);
assert.equal(api.toLinksCsv([]).split("\r\n").length, 2);
assert.equal(api.toLinksCsv(Array.from({ length: 205 }, (_, n) => ({ url: origin + "/p?product_id=" + n }))).split("\r\n").length, 202);
assert.ok(!api.toLinksCsv([Object.create({ url: product })]).includes(product), "Ignore inherited URL fields");
assert.equal(typeof context.DianShelfToolsCore.toLinksCsv, "function");

assert.equal(api.titleChecks("保温杯 😀").length, 5);
assert.deepEqual(api.titleChecks("保温杯 保温杯 SALE sale").duplicate_terms, ["保温杯", "sale"]);
assert.ok(api.titleChecks("").warnings.length);
assert.equal(api.titleChecks("商品标题").warnings.length, 0);
const reservedRows = api.normalizeCatalogRows([
  { product_id: "001", sku_id: "002", reserved_stock_text: "12" },
  { product_id: "003", sku_id: "004", reserved_stock_text: "0" },
  { product_id: "005", sku_id: "006" },
  { product_id: "007", sku_id: "008", reserved_stock_text: 0 },
]);
assert.equal(reservedRows[0].reserved_stock_text, "12");
assert.equal(reservedRows[1].reserved_stock_text, "0");
assert.equal(Object.hasOwn(reservedRows[2], "reserved_stock_text"), false);
assert.equal(reservedRows[3].reserved_stock_text, "无效预留库存");
for (const damaged of [
  { product_id: "00\u200b1", product_name: "标题" },
  { product_id: "001", product_name: "标题", stock_text: "1\u200b2" },
  { product_id: "001", product_name: "a".repeat(2000) + "尾" },
  { product_id: 123, product_name: "标题" },
]) {
  const normalized = api.normalizeCatalogRows([damaged])[0];
  assert(normalized.bulk_source_issues.length > 0, "Lossy edit baselines must carry blocking provenance");
  assert.deepEqual(api.normalizeCatalogRows([normalized])[0].bulk_source_issues, normalized.bulk_source_issues, "Second normalization must not erase blocking provenance");
}
assert.equal(api.normalizeCatalogRows([{ product_id: "00\u200b1" }])[0].product_id, "");
assert(api.normalizeCatalogRows([{ product_id: "001", bulk_source_issues: "bad" }])[0].bulk_source_issues.length);
assert(api.normalizeCatalogRows([{ product_id: "001", bulk_source_issues: undefined }])[0].bulk_source_issues.length);
const healthyRow = { product_id: "synthetic-product", sku_id: "synthetic-sku", merchant_code: "SYNTHETIC-001", price_text: "12.00", stock_text: "8" };
const healthBefore = JSON.stringify(healthyRow);
const healthy = api.catalogHealth(healthyRow);
assert.deepEqual(healthy.issues, []);
assert.equal(healthy.stock_state, "known");
assert.equal(healthy.execution_authorized, false);
assert.match(healthy.next_step, /来源页面和读取时间/);
assert.equal(JSON.stringify(healthyRow), healthBefore);
assert.equal(api.matchesCatalogHealth(healthyRow, "review_complete"), true);
const zeroHealth = api.catalogHealth({ ...healthyRow, stock_text: "0" });
assert.equal(zeroHealth.stock_state, "zero");
assert.match(zeroHealth.next_step, /库存与可售状态/);
assert.equal(api.matchesCatalogHealth({ ...healthyRow, stock_text: "—" }, "stock_unknown"), true);
assert.match(api.catalogHealth({ ...healthyRow, merchant_code: "" }).next_step, /价格和商家编码/);
for (const health of [healthy, zeroHealth, api.catalogHealth({})]) assert.doesNotMatch(health.next_step, /投放|利润|计划关联/);
console.log("shelf-tools-core: all tests passed");
