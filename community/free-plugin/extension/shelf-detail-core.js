(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./shelf-tools-core.js") : root.DianShelfToolsCore);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianShelfDetailCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (linkCore) {
  "use strict";

  // Pure, original, current-visible-row detail model. No fetch, image loading,
  // storage, DOM access, inferred identities, or platform writes.
  const MAX_ROWS = 200, MAX_IMAGES = 8, MAX_TOTAL_IMAGES = 100, MAX_TEXT = 4000, MAX_URL = 8192;
  const CDNS = ["pstatp.com", "byteimg.com", "douyinpic.com", "ecombdimg.com"];
  const VIDEO_CDNS = ["douyinvod.com", "bytevod.com", "toutiaovod.com", "bytevcloud.com", "ibytedtos.com"];
  const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/;
  const URL_CONTROL = /[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\\]/;
  const SENSITIVE = /token|password|passwd|secret|signature|credential|session|authorization|auth|cookie|csrf|xsrf|jwt|ticket|nonce|apikey|accesskey|privatekey|shop|store|merchant|seller|account|buyer|user|openid|unionid|phone|email|address|order|redirect|callback|(?:url|uri)$|^(?:code|state|sign|link|target|destination|referer|referrer)$/i;
  const PRIVATE_PATH = /(?:^|[\/._-])(?:private|orders?|pay|payment|checkout|cashier|login|logout|auth|oauth|sessions?|tokens?|password|accounts?|wallet|refund|avatar|profile)(?:$|[\/._-])/i;
  const ATTRIBUTE_NAMES = Object.freeze({ "品牌": "品牌", "商品品牌": "品牌", "类目": "类目", "商品类目": "类目", "类目名称": "类目", "商品状态": "商品状态" });
  const ROW_TEXT_FIELDS = ["product_name", "sku_text", "merchant_code", "price_text", "stock_text"];

  function own(object, key) {
    if (!object || typeof object !== "object") return undefined;
    try { const descriptor = Object.getOwnPropertyDescriptor(object, key); return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined; }
    catch (_) { return undefined; }
  }
  function has(object, key) {
    if (!object || typeof object !== "object") return false;
    try { return !!Object.getOwnPropertyDescriptor(object, key); } catch (_) { return true; }
  }
  function validId(value) { return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value); }
  function safeText(value, limit = MAX_TEXT) { return typeof value === "string" && value.length <= limit && !CONTROL.test(value) ? value : ""; }
  function addWarning(warnings, message) { if (!warnings.includes(message)) warnings.push(message); }
  function failUrl(error) { return { ok: false, url: "", error }; }
  function decode(value) {
    if (/%(?![0-9a-f]{2})/i.test(value)) throw new Error("encoding");
    const text = decodeURIComponent(value);
    if (URL_CONTROL.test(text) || /%[0-9a-f]{2}/i.test(text)) throw new Error("encoding");
    return text;
  }
  function normalizeAssetUrl(raw, kind = "image") {
    const video=kind==="video";if(kind!=="image"&&!video)return failUrl("素材类型无效。");
    if (typeof raw !== "string" || !raw || raw.length > MAX_URL || URL_CONTROL.test(raw) || raw.includes("#")) return failUrl("图片地址为空、过长或含不安全字符。");
    const authority = /^https:\/\/([a-z0-9.-]+)(?=[/?]|$)/i.exec(raw);
    if (!authority) return failUrl("仅接受无身份信息和显式端口的 HTTPS 图片地址。");
    try {
      const parsed = new URL(raw);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || parsed.hash
        || !(video?[...CDNS,...VIDEO_CDNS]:CDNS).some((host) => parsed.hostname === host || parsed.hostname.endsWith("." + host))) return failUrl("素材域名不在受信 CDN 范围内。");
      const path = decode(parsed.pathname);
      if(video&&/\.(?:m3u8|mpd|html?|js)(?:$|[\/.~!])/i.test(path))return failUrl("不接受分段播放列表或网页脚本地址。");
      if (PRIVATE_PATH.test(path) || /\.svgz?(?:$|[\/.~!])/i.test(path)) return failUrl("不接受私人路径、头像或 SVG 图片。");
      for (const pair of parsed.search.slice(1).split("&")) {
        if (!pair) continue;
        if (pair.includes(";")) return failUrl("图片参数格式不明确。");
        const equal = pair.indexOf("=");
        const key = decode(equal < 0 ? pair : pair.slice(0, equal)).normalize("NFKC").replace(/[\s._-]/g, "");
        const value = decode(equal < 0 ? "" : pair.slice(equal + 1));
        if (!key || SENSITIVE.test(key) || /(?:https?:|\/\/|javascript:|data:|blob:|file:)/i.test(value)
          || /\bsvgz?\b/i.test(value) || PRIVATE_PATH.test(value)) return failUrl("图片参数含敏感信息或不安全内容。");
      }
      if (parsed.href.length > MAX_URL) return failUrl("图片地址编码后过长。");
      return { ok: true, url: parsed.href };
    } catch (_) { return failUrl("图片地址或编码无效。"); }
  }

  function timestamp(value) {
    if (typeof value !== "number" && typeof value !== "string") return "";
    if (typeof value === "number" && (!Number.isFinite(value) || value < 0)) return "";
    if (typeof value === "string" && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)) return "";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "";
    const normalized = date.toISOString();
    return typeof value !== "string" || normalized.slice(0, 19) === value.slice(0, 19) ? normalized : "";
  }
  function sourceLink(value, warnings) {
    if (!value) { addWarning(warnings, "未提供可核验的来源页面地址。"); return ""; }
    const parsed = typeof linkCore?.normalizeLink === "function" ? linkCore.normalizeLink(value) : null;
    if (!parsed?.ok) { addWarning(warnings, "来源页面地址未通过安全校验，未保留该地址。"); return ""; }
    return parsed.url;
  }
  function normalizeAttributes(raw, warnings) {
    if (raw === undefined) return [];
    if (!Array.isArray(raw) || raw.length > 12) { addWarning(warnings, "部分商品属性格式无效或超过上限，未采用。"); return []; }
    const attributes = [], seen = new Set();
    for (let i = 0; i < raw.length; i++) {
      const entry = own(raw, String(i)), name = own(entry, "name"), value = own(entry, "value");
      const canonical = typeof name === "string" && Object.hasOwn(ATTRIBUTE_NAMES, name) ? ATTRIBUTE_NAMES[name] : "";
      if (!canonical || !safeText(value, 500).trim()) { addWarning(warnings, "仅保留明确读取的品牌、类目和商品状态属性。"); continue; }
      const key = JSON.stringify([canonical, value]);
      if (!seen.has(key)) { seen.add(key); attributes.push({ name: canonical, value }); }
    }
    return attributes;
  }
  function normalizeImages(raw, warnings) {
    if (raw === undefined) return [];
    if (!Array.isArray(raw) || raw.length > MAX_IMAGES) { addWarning(warnings, "单行图片超过 8 张或格式无效，未采用该行图片。"); return []; }
    const images = [], seen = new Set();
    for (let i = 0; i < raw.length; i++) {
      const entry = own(raw, String(i));
      if (own(entry, "kind") !== "row_image" || own(entry, "source") !== "visible_dom_img") { addWarning(warnings, "忽略非可见商品行来源的图片。"); continue; }
      const parsed = normalizeAssetUrl(own(entry, "url"));
      if (!parsed.ok) { addWarning(warnings, "部分图片地址未通过安全校验，未采用。"); continue; }
      if (!seen.has(parsed.url)) { seen.add(parsed.url); images.push({ url: parsed.url, alt: safeText(own(entry, "alt"), 200), kind: "row_image", source: "visible_dom_img" }); }
    }
    return images;
  }
  function provenance(row) {
    if (!has(row, "bulk_source_issues")) return [];
    const flags = own(row, "bulk_source_issues");
    if (!Array.isArray(flags) || flags.length > 12) return ["源数据校验标记格式无效"];
    const result = [];
    for (let i = 0; i < flags.length; i++) {
      const value = own(flags, String(i));
      result.push(typeof value === "string" && value.trim() && value.length <= 240 && !CONTROL.test(value) ? value : "源数据校验标记格式无效");
    }
    return result;
  }
  function linkIdentityConflict(row, productId, skuId) {
    const links = own(row, "links");
    if (!linkCore?.normalizeLink) return false;
    if (Array.isArray(links) && links.length > 20) return true;
    const candidates = [own(row, "url")];
    if (Array.isArray(links)) for (let i = 0; i < links.length; i++) {
      const entry = own(links, String(i));
      candidates.push(typeof entry === "string" ? entry : own(entry, "url"));
    }
    for (const candidate of candidates) {
      const parsed = linkCore.normalizeLink(candidate);
      if (!parsed.ok || parsed.kind === "short_link") continue;
      if (parsed.warning || (parsed.product_id && parsed.product_id !== productId) || (parsed.sku_id && skuId && parsed.sku_id !== skuId)) return true;
    }
    return false;
  }
  function rowLinks(row) {
    const result = [], seen = new Set();
    const add = (raw, label, source) => {
      const parsed = linkCore?.normalizeLink?.(raw);
      if (result.length < 3 && parsed?.ok && !parsed.warning && !seen.has(parsed.url)) {
        seen.add(parsed.url); result.push({ url: parsed.url, label: safeText(label, 120), source: source === "visible_dom_href" ? source : "" });
      }
    };
    const links = own(row, "links");
    if (Array.isArray(links) && links.length <= 20) for (let i = 0; i < links.length; i++) {
      const entry = own(links, String(i));
      add(typeof entry === "string" ? entry : own(entry, "url"), own(entry, "label"), own(entry, "source"));
    }
    add(own(row, "url"), "", "");
    return result;
  }

  function buildProductDetail(snapshot, productId) {
    const detail = { ok: false, product_id: validId(productId) ? productId : "", product_name: "", captured_at: "", context_key: "", source_url: "", rows: [], images: [], attributes: [], warnings: [] };
    const fail = (error) => ({ ...detail, ok: false, error, rows: [], images: [], attributes: [] });
    if (!validId(productId)) return fail("商品 ID 必须是完整、精确的文本，不能按名称或数值猜测。");
    const rawRows = own(snapshot, "rows"), context = own(snapshot, "context_key");
    if (own(snapshot, "scope") !== "current_visible_rows" || !Array.isArray(rawRows) || rawRows.length > MAX_ROWS
      || typeof context !== "string" || !safeText(context, 2000).trim()) return fail("快照范围、上下文或行数无效；仅支持当前可见的最多 200 行。");
    detail.context_key = context;
    detail.captured_at = timestamp(own(snapshot, "captured_at"));
    if (!detail.captured_at) addWarning(detail.warnings, "读取时间未知；本资料不能证明数据实时有效。");
    detail.source_url = sourceLink(own(snapshot, "source_url"), detail.warnings);
    const sourceWarnings = own(snapshot, "warnings");
    if (Array.isArray(sourceWarnings)) {
      for (let i = 0; i < Math.min(sourceWarnings.length, 20); i++) {
        const message = safeText(own(sourceWarnings, String(i)), 500);
        if (message) addWarning(detail.warnings, "采集提示：" + message);
      }
      if (sourceWarnings.length > 20) addWarning(detail.warnings, "采集提示超过 20 条，仅展示前 20 条有效提示。");
    }
    const names = new Set(), skus = new Map(), imageUrls = new Set(), attributeKeys = new Set(), attributeValues = new Map();
    for (let i = 0; i < rawRows.length; i++) {
      const raw = own(rawRows, String(i));
      if (own(raw, "product_id") !== productId) continue;
      const sourceIssues = provenance(raw);
      if (sourceIssues.some((issue) => /(?:^|\b)(?:product_id|sku_id)(?:\b|$)/.test(issue))) return fail("商品或 SKU 身份存在源数据损失，请重新读取精确文本 ID。");
      const sourceSku = own(raw, "sku_id");
      const skuId = sourceSku === undefined || sourceSku === "" ? "" : validId(sourceSku) ? sourceSku : null;
      if (skuId === null) return fail("商品记录含非精确文本 SKU ID，未猜测或修复身份。");
      if (linkIdentityConflict(raw, productId, skuId)) return fail("商品行与已有链接的商品或 SKU 身份冲突，未生成详情。");
      const row = { product_id: productId, sku_id: skuId, captured_at: detail.captured_at };
      for (const field of ROW_TEXT_FIELDS) {
        const value = own(raw, field);
        row[field] = safeText(value);
        if ((value !== undefined || has(raw, field)) && value !== "" && !row[field]) {
          addWarning(detail.warnings, "部分 " + field + " 字段未完整读取，保留为未知。");
          sourceIssues.push(field + " 原文无效、过长或不可读取，详情展示未保留原值");
        }
      }
      if (has(raw, "reserved_stock_text")) {
        const reserved = own(raw, "reserved_stock_text");
        row.reserved_stock_text = typeof reserved === "string" && reserved.length <= MAX_TEXT ? reserved : "无效预留库存";
        if (row.reserved_stock_text !== reserved) sourceIssues.push("reserved_stock_text 原文无效或不可读取");
      }
      if (sourceIssues.length) {
        row.bulk_source_issues = [...new Set(sourceIssues)];
        addWarning(detail.warnings, "部分记录存在源数据损失标记；仅供查看，后续批量修改必须保持阻止。");
      }
      row.links = rowLinks(raw);
      row.url = row.links[0]?.url || "";
      row.images = normalizeImages(own(raw, "images"), detail.warnings);
      row.attributes = normalizeAttributes(own(raw, "attributes"), detail.warnings);
      if (!skuId) addWarning(detail.warnings, "部分行没有 SKU ID；仍按原行展示，不合并或推测规格身份。");
      if (!row.price_text || !row.stock_text) addWarning(detail.warnings, "部分价格或库存未知；空值未转换为 0。");
      if (row.product_name.trim()) names.add(row.product_name);
      const fingerprint = JSON.stringify(ROW_TEXT_FIELDS.map((field) => row[field]).concat([row.attributes]));
      if (skuId && skus.has(skuId)) addWarning(detail.warnings, skus.get(skuId) === fingerprint
        ? "同一 SKU 有重复可见行；已保留原行，未偷偷合并。" : "同一 SKU 的可见数据存在冲突；已保留全部原行，请核对来源。");
      else if (skuId) skus.set(skuId, fingerprint);
      detail.rows.push(row);
      for (const image of row.images) if (!imageUrls.has(image.url)) {
        imageUrls.add(image.url);
        if (detail.images.length < MAX_TOTAL_IMAGES) detail.images.push(image);
        else addWarning(detail.warnings, "商品图片清单达到 100 张上限，其余可见图片未列入资料清单或下载范围。");
      }
      for (const attribute of row.attributes) {
        const key = JSON.stringify([attribute.name, attribute.value]);
        if (!attributeKeys.has(key)) { attributeKeys.add(key); detail.attributes.push(attribute); }
        if (attributeValues.has(attribute.name) && attributeValues.get(attribute.name) !== attribute.value) addWarning(detail.warnings, "同一商品的 " + attribute.name + " 属性存在不同值；已分别保留，不作推断。");
        else attributeValues.set(attribute.name, attribute.value);
      }
    }
    if (!detail.rows.length) return fail("当前快照没有该精确商品 ID 的可见记录。");
    if (names.size === 1) detail.product_name = [...names][0];
    else if (names.size > 1) addWarning(detail.warnings, "同一商品 ID 对应不同标题，汇总标题留空，原行标题保留。");
    else addWarning(detail.warnings, "商品标题未读取，未按 SKU 或链接猜测。");
    addWarning(detail.warnings, "仅含读取时当前可见行；不是完整商品详情、实时库存或平台写入结果。");
    detail.ok = true;
    return detail;
  }

  function exportRows(detail) {
    const rows = own(detail, "rows"), productId = own(detail, "product_id");
    if (own(detail, "ok") !== true || !validId(productId) || !Array.isArray(rows) || rows.length > MAX_ROWS) throw new TypeError("只能导出有效的当前商品详情。");
    const result = [];
    for (let i = 0; i < rows.length; i++) {
      const row = own(rows, String(i));
      if (own(row, "product_id") !== productId) throw new TypeError("所选 SKU 不属于当前商品，未导出。");
      result.push(row);
    }
    return result;
  }
  function cell(value, protect = false) {
    let text = safeText(value, MAX_URL);
    if (typeof value === "string" && value.length > MAX_URL) throw new RangeError("导出字段过长，未截断。");
    if (text && (protect || /^[\t\r\n]/.test(text) || /^\s*[=+@-]/u.test(text.normalize("NFKC")))) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }
  function csv(headers, records, protectedColumns = []) {
    return "\uFEFF" + [headers.map((value) => cell(value)).join(","), ...records.map((record) => record.map((value, i) => cell(value, protectedColumns.includes(i))).join(","))].join("\r\n") + "\r\n";
  }
  function toDetailCsv(detail) {
    const rows = exportRows(detail), warnings = [];
    const source = sourceLink(own(detail, "source_url"), warnings), captured = timestamp(own(detail, "captured_at"));
    return csv(["商品ID", "SKU ID", "商品名称", "规格", "商家编码", "价格（页面原文）", "库存（页面原文）", "读取时间", "来源页面", "资料范围"], rows.map((row) => [own(row, "product_id"), own(row, "sku_id"), own(row, "product_name"), own(row, "sku_text"), own(row, "merchant_code"), own(row, "price_text"), own(row, "stock_text"), captured, source, "当前可见行·未提交平台"]), [0, 1, 4]);
  }
  function toAssetsCsv(detail) {
    exportRows(detail);
    const raw = own(detail, "images");
    if (!Array.isArray(raw) || raw.length > MAX_TOTAL_IMAGES) throw new TypeError("图片资料列表无效或超过 100 张上限。");
    const records = [], seen = new Set();
    for (let i = 0; i < raw.length; i++) {
      const image = own(raw, String(i));
      if (own(image, "kind") !== "row_image" || own(image, "source") !== "visible_dom_img") continue;
      const url = normalizeAssetUrl(own(image, "url"));
      if (!url.ok || seen.has(url.url)) continue;
      seen.add(url.url);
      records.push([own(detail, "product_id"), url.url, safeText(own(image, "alt"), 200), "row_image", "visible_dom_img", timestamp(own(detail, "captured_at")), "仅资源清单·未下载"]);
    }
    return csv(["商品ID", "图片地址", "图片说明", "资源类型", "读取来源", "读取时间", "状态"], records, [0]);
  }
  function toCopyText(detail) {
    const rows = exportRows(detail);
    const clean = (value) => safeText(value).replace(/[\t\r\n]+/g, " ");
    const lines = ["商品资料（当前可见行；未提交平台）", "商品ID：" + clean(own(detail, "product_id"))];
    for (const row of rows) lines.push(["SKU：" + (clean(own(row, "sku_id")) || "未读取"), "商品：" + (clean(own(row, "product_name")) || "未读取"), "规格：" + (clean(own(row, "sku_text")) || "未读取"), "编码：" + (clean(own(row, "merchant_code")) || "未读取"), "价格：" + (clean(own(row, "price_text")) || "未读取"), "库存：" + (clean(own(row, "stock_text")) || "未读取")].join(" | "));
    lines.push("读取时间：" + (timestamp(own(detail, "captured_at")) || "未知"));
    return lines.join("\n");
  }
  return Object.freeze({ buildProductDetail, normalizeAssetUrl, normalizeVideoAssetUrl:raw=>normalizeAssetUrl(raw,"video"), toDetailCsv, toAssetsCsv, toCopyText });
});
