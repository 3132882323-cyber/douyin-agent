(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianShelfToolsCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // This module only transforms supplied text. It never resolves, opens, fetches,
  // persists or manufactures a product link, and never interprets an ID as a Number.
  const MAX_INPUT_LENGTH = 100000;
  const MAX_ROWS = 200;
  const MAX_URL_LENGTH = 8192;
  const HOSTS = new Set(["fxg.jinritemai.com", "haohuo.jinritemai.com", "haohuo.snssdk.com",
    "www.douyin.com", "douyin.com", "v.douyin.com"]);
  const CSV_HEADERS = Object.freeze(["商品名称", "商品ID", "SKU ID", "商家编码", "规格",
    "价格（页面原文）", "库存（页面原文）", "商品链接", "读取时间"]);
  const LINK_CSV_HEADERS = Object.freeze(["链接", "URL中的商品ID（未核验）", "URL中的SKU ID（未核验）", "链接类型", "说明"]);
  const PRODUCT_KEYS = new Set(["product_id", "productId", "commodity_id", "goods_id"]);
  const SKU_KEYS = new Set(["sku_id", "skuId"]);
  const INVISIBLE = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/;
  const PRIVATE_ROUTE = /(?:^|[\/._\-?=&;:])(?:private|orders?|pay|payment|payments|checkout|cashier|log[._-]?in|log[._-]?out|sign[._-]?in|sign[._-]?out|auth|authorize|authorization|oauth|oauth2|sessions?|tokens?|password|account|accounts|wallet|refund|refunds|aftersale|settlement)(?:$|[\/._\-?=&;:])/i;
  const SENSITIVE_KEY = /token|password|passwd|passphrase|secret|signature|session|authorization|oauth|credential|cookie|csrf|xsrf|jwt|ticket|nonce|apikey|accesskey|privatekey|(?:shop|store|merchant|seller|account|user)(?:id|key|identity|name)|openid|unionid|redirect|callback|(?:url|uri)$|deeplink|destination|^(?:code|state|auth|sign|xbogus|authcode|logincode|verificationcode|securitycode|authstate|proto|prototype|constructor|next|return|target|link|referer|referrer)$/i;

  function failure(warning) {
    return { ok: false, url: "", product_id: "", sku_id: "", kind: "", warning };
  }

  function ownValue(object, key) {
    if (!object || typeof object !== "object") return undefined;
    try {
      const descriptor = Object.getOwnPropertyDescriptor(object, key);
      return descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value") ? descriptor.value : undefined;
    } catch (_) { return undefined; }
  }

  function scalar(value, limit = 2000) {
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
    if (typeof value !== "string") return "";
    return value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, "").slice(0, limit);
  }

  function field(row, name, alias) {
    const value = ownValue(row, name);
    return scalar(value === undefined && alias ? ownValue(row, alias) : value);
  }

  function identifier(value) {
    // An already-rounded numeric ID cannot be recovered; do not export it as fact.
    if (typeof value === "number" && !Number.isSafeInteger(value)) return "";
    if (typeof value === "string" && INVISIBLE.test(value)) return "";
    const text = scalar(value, 129).trim();
    return /^[A-Za-z0-9_-]{1,128}$/.test(text) ? text : "";
  }

  function decodePart(value) {
    if (/%(?![0-9a-f]{2})/i.test(value)) throw new Error("malformed encoding");
    const decoded = decodeURIComponent(value);
    if (INVISIBLE.test(decoded) || /%[0-9a-f]{2}/i.test(decoded) || /\\/.test(decoded)) {
      throw new Error("ambiguous encoding");
    }
    return decoded;
  }

  function compactKey(value) {
    return value.normalize("NFKC").replace(/[\s._\-[\]]/g, "").toLowerCase();
  }

  function trackingKey(key) { return /^utm_/i.test(key) || /^spm$/i.test(key); }

  function inspectParameters(query, hasVerifiedDetailFormat = false) {
    const kept = [];
    const ids = { product: [], sku: [] };
    if (!query) return { query: "", ids };
    // A semicolon can be another parameter separator in downstream frameworks.
    if (query.includes(";")) throw new Error("ambiguous parameters");
    for (const pair of query.split("&")) {
      if (!pair) { kept.push(pair); continue; }
      const position = pair.indexOf("=");
      const key = decodePart((position < 0 ? pair : pair.slice(0, position)).replace(/\+/g, " "));
      const value = decodePart((position < 0 ? "" : pair.slice(position + 1)).replace(/\+/g, " "));
      if (!key || /[&=;#/?]/.test(key) || SENSITIVE_KEY.test(compactKey(key))) throw new Error("sensitive parameters");
      // Reject a nested destination regardless of the name used to carry it.
      if (/(?:[a-z][a-z0-9+.-]*:\/\/|(?:^|[\s=])\/\/|javascript:|data:|file:)/i.test(value)
        || /(?:^|[&;?])[^&;?=]*(?:token|password|signature|session|shop_id|store_id|code|state)[^&;?=]*=/i.test(value)
        || PRIVATE_ROUTE.test(value)) throw new Error("nested or private parameters");
      if (PRODUCT_KEYS.has(key) || (hasVerifiedDetailFormat && key === "id")) ids.product.push(value);
      if (SKU_KEYS.has(key)) ids.sku.push(value);
      if (!trackingKey(key)) kept.push(pair);
    }
    return { query: kept.join("&"), ids };
  }

  function inspectPath(path) {
    const decoded = decodePart(path);
    if (/%(?:2f|5c|3f|23)/i.test(path) || decoded.includes(";") || /(?:^|\/)\.{2}(?:\/|$)/.test(decoded)
      || PRIVATE_ROUTE.test(decoded.normalize("NFKC"))) throw new Error("private or ambiguous route");
  }

  function inspectFragment(hash) {
    if (!hash) return { hash: "", ids: { product: [], sku: [] } };
    const body = hash.slice(1);
    const question = body.indexOf("?");
    if (question >= 0) {
      const fragmentPath = body.slice(0, question);
      inspectPath(fragmentPath);
      if (/(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(decodePart(fragmentPath))) throw new Error("nested fragment");
      const inspected = inspectParameters(body.slice(question + 1));
      const route = body.slice(0, question);
      return { hash: route || inspected.query ? "#" + route + (inspected.query ? "?" + inspected.query : "") : "", ids: inspected.ids };
    }
    if (body.includes("=")) {
      const inspected = inspectParameters(body);
      return { hash: inspected.query ? "#" + inspected.query : "", ids: inspected.ids };
    }
    inspectPath(body);
    if (/(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(decodePart(body))) throw new Error("nested fragment");
    return { hash, ids: { product: [], sku: [] } };
  }

  function pickId(values, label, warnings) {
    if (!values.length) return "";
    const distinct = Array.from(new Set(values));
    if (distinct.length !== 1 || !distinct[0] || identifier(distinct[0]) !== distinct[0]) {
      warnings.push(label + " 有冲突或格式不明确，未提取该 ID。");
      return "";
    }
    return distinct[0];
  }

  function normalizeLink(raw, baseUrl) {
    if (typeof raw !== "string" || !raw.trim()) return failure("链接为空或不是文本。");
    if (raw.length > MAX_URL_LENGTH) return failure("链接过长，未处理。");
    if (INVISIBLE.test(raw) || /\\/.test(raw)) return failure("链接含控制字符或不明确的分隔符，未处理。");
    let input = raw.trim();
    if (/\s/.test(input)) return failure("链接含空白字符，未处理。");
    if (!/^https:\/\//i.test(input)) {
      if (!baseUrl || !/^(?:\/(?!\/)|\.\/|\?|#)/.test(input)) return failure("仅支持已有的官方 HTTPS 链接。");
      const base = normalizeLink(baseUrl);
      if (!base.ok) return failure("相对链接的来源地址不安全。");
      try {
        inspectPath(input.split(/[?#]/, 1)[0]);
        input = new URL(input, base.url).href;
      } catch (_) { return failure("相对链接格式不明确，未处理。"); }
    }
    const authority = /^https:\/\/([^/?#]*)([^?#]*)/i.exec(input);
    if (!authority || !/^[a-z0-9.-]+(?::443)?$/i.test(authority[1])) return failure("链接含身份信息、异常端口或不明确的域名。");
    let parsed;
    try { parsed = new URL(input); } catch (_) { return failure("链接格式无效。"); }
    if (parsed.protocol !== "https:" || !HOSTS.has(parsed.hostname) || parsed.username || parsed.password || parsed.port) {
      return failure("仅支持指定的抖音、抖店官方 HTTPS 域名。");
    }
    try {
      inspectPath(authority[2]);
      inspectPath(parsed.pathname);
      // The generic `id` is product-specific only for this observed official
      // URL format. It is not an ID rule for other hosts, paths or hash routes.
      const hasVerifiedDetailFormat = parsed.hostname === "haohuo.jinritemai.com"
        && parsed.pathname === "/views/product/detail";
      const query = inspectParameters(parsed.search.slice(1), hasVerifiedDetailFormat);
      const fragment = inspectFragment(parsed.hash);
      parsed.search = query.query ? "?" + query.query : "";
      parsed.hash = fragment.hash;
      if (parsed.href.length > MAX_URL_LENGTH) return failure("链接编码后过长，未处理。");
      if (parsed.hostname === "v.douyin.com") {
        return { ok: true, url: parsed.href, product_id: "", sku_id: "", kind: "short_link",
          warning: "短链接未展开，不能据此确认商品或 SKU；请打开后自行核对。" };
      }
      const warnings = [];
      const productId = pickId(query.ids.product.concat(fragment.ids.product), "商品 ID", warnings);
      const skuId = pickId(query.ids.sku.concat(fragment.ids.sku), "SKU ID", warnings);
      return { ok: true, url: parsed.href, product_id: productId, sku_id: skuId,
        kind: productId ? "product_link" : "official_link", warning: warnings.join(" ") };
    } catch (_) { return failure("链接含敏感参数、私人业务路径或不明确的编码，未处理。"); }
  }

  function parseLinks(text) {
    const result = { rows: [], warnings: [], rejected_count: 0, duplicate_count: 0, truncated: false };
    if (typeof text !== "string") { result.warnings.push("请粘贴链接或分享文本。"); return result; }
    let input = text;
    if (input.length > MAX_INPUT_LENGTH) {
      input = input.slice(0, MAX_INPUT_LENGTH);
      // Do not accidentally accept a URL cut in the middle by the character cap.
      const lastBoundary = Math.max(input.lastIndexOf("\n"), input.lastIndexOf(" "), input.lastIndexOf("\t"));
      input = lastBoundary < 0 ? "" : input.slice(0, lastBoundary);
      result.truncated = true;
      result.warnings.push("输入超过 100000 字符，超出部分及被截断的链接未处理。");
    }
    const matches = input.match(/(?:https?:\/\/|[a-z][a-z0-9+.-]*:\/\/)[^\s<>"'“”‘’《》]+/gi) || [];
    const seen = new Set();
    let processed = 0;
    for (let candidate of matches) {
      if (processed >= MAX_ROWS) {
        result.truncated = true;
        result.warnings.push("一次最多处理 200 条链接，其余链接未处理。");
        break;
      }
      processed++;
      // Share messages commonly wrap a URL with punctuation, which is not a URL field.
      candidate = candidate.replace(/[，。！？、；：）】〉」』,!?;]+$/g, "");
      while (candidate.endsWith(")") && (candidate.match(/\)/g) || []).length > (candidate.match(/\(/g) || []).length) candidate = candidate.slice(0, -1);
      const normalized = normalizeLink(candidate);
      if (!normalized.ok) { result.rejected_count++; continue; }
      if (seen.has(normalized.url)) { result.duplicate_count++; continue; }
      seen.add(normalized.url);
      const { url, product_id, sku_id, kind, warning } = normalized;
      result.rows.push({ url, product_id, sku_id, kind, warning });
    }
    if (result.rejected_count) result.warnings.push(result.rejected_count + " 条非官方、不安全或无效链接已忽略。");
    if (result.duplicate_count) result.warnings.push(result.duplicate_count + " 条完全相同的链接已合并（不同 SKU 链接不会合并）。");
    if (result.rows.some((row) => row.kind === "short_link")) result.warnings.push("短链接未展开，尚未确认商品身份。");
    if (result.rows.some((row) => row.warning && row.kind !== "short_link")) result.warnings.push("部分链接的 ID 冲突或不明确，请检查逐条提示。");
    if (!matches.length && !result.truncated && input.trim()) result.warnings.push("未找到可处理的完整链接；不会根据商品 ID 拼接链接。");
    return result;
  }

  function normalizeCatalogRows(rows) {
    if (!Array.isArray(rows)) return [];
    const normalized = [];
    for (let index = 0; index < Math.min(rows.length, MAX_ROWS); index++) {
      const row = ownValue(rows, String(index));
      if (!row || typeof row !== "object" || Array.isArray(row)) continue;
      const links = [];
      const seen = new Set();
      const addLink = (candidate, label, source) => {
        if (links.length >= 3) return;
        const parsed = normalizeLink(candidate);
        if (!parsed.ok || seen.has(parsed.url)) return;
        seen.add(parsed.url);
        links.push({ url: parsed.url, label: scalar(label, 120), source: source === "visible_dom_href" ? source : "" });
      };
      const suppliedLinks = ownValue(row, "links");
      if (Array.isArray(suppliedLinks)) {
        for (let n = 0; n < Math.min(suppliedLinks.length, 20) && links.length < 3; n++) {
          const link = ownValue(suppliedLinks, String(n));
          if (typeof link === "string") addLink(link, "", "");
          else addLink(ownValue(link, "url"), ownValue(link, "label"), ownValue(link, "source"));
        }
      }
      addLink(ownValue(row, "url"), "", "");
      const reserved = ownValue(row, "reserved_stock_text");
      // Optional source evidence must survive both catalog handoff and direct
      // bulk reads. Absence stays unknown; malformed evidence must not become 0.
      const reservedField = reserved === undefined ? {} : { reserved_stock_text:
        typeof reserved === "string" && reserved.length <= 2000 ? reserved : "无效预留库存" };
      const stockHint=ownValue(row,'stock_hint_text'),stockHintField=stockHint==='部分SKU售罄'?{stock_hint_text:stockHint}:{};
      const priceHint=ownValue(row,'price_hint_text'),priceHintValid=['建议改价提销量','已获优价推手曝光扶持'].includes(priceHint),priceHintField=priceHintValid?{price_hint_text:priceHint}:{};
      const normalizedRow = { product_name: field(row, "product_name", "name"),
        product_id: identifier(ownValue(row, "product_id")), sku_id: identifier(ownValue(row, "sku_id")),
        merchant_code: field(row, "merchant_code"), sku_text: field(row, "sku_text", "spec"),
        price_text: field(row, "price_text"), stock_text: field(row, "stock_text"), links,
        url: links.length ? links[0].url : "", captured_at: field(row, "captured_at"), ...reservedField,...stockHintField,...priceHintField };
      // Display/export cleanup is intentionally lossy. It cannot serve as an
      // editing baseline without carrying that loss through later handoffs.
      const sourceIssues = [];
      if(stockHint!==undefined&&stockHint!=='部分SKU售罄')sourceIssues.push('库存提示未通过已知格式校验');
      if(priceHint!==undefined&&!priceHintValid)sourceIssues.push('价格提示未通过已知格式校验');
      const priorIssues = ownValue(row, "bulk_source_issues");
      let hasPriorIssues = false;
      try { hasPriorIssues = Object.prototype.hasOwnProperty.call(row, "bulk_source_issues"); }
      catch (_) { sourceIssues.push("源数据校验标记无法读取"); }
      if (hasPriorIssues) {
        if (!Array.isArray(priorIssues) || priorIssues.length > 12) sourceIssues.push("源数据校验标记格式无效");
        else for (let n = 0; n < priorIssues.length; n++) {
          const issue = ownValue(priorIssues, String(n));
          sourceIssues.push(typeof issue === "string" && issue.trim() && issue.length <= 240 ? issue : "源数据校验标记格式无效");
        }
      }
      for (const [name, alias] of [["product_id"], ["sku_id"], ["product_name", "name"],
        ["merchant_code"], ["sku_text", "spec"], ["price_text"], ["stock_text"], ["reserved_stock_text"]]) {
        const direct = ownValue(row, name);
        const original = direct === undefined && alias ? ownValue(row, alias) : direct;
        if (original !== undefined && (typeof original !== "string" || original !== normalizedRow[name])) {
          sourceIssues.push(`${name} 原值被清理、截断或转换，请重新读取准确原文`);
        }
      }
      if (sourceIssues.length) normalizedRow.bulk_source_issues = [...new Set(sourceIssues)].slice(0, 12);
      normalized.push(normalizedRow);
    }
    return normalized;
  }

  function csvCell(value, isId) {
    // Catalog text has already been bounded. A valid source URL may be longer
    // than a display field and must not become a different, truncated URL here.
    let text = scalar(value, MAX_URL_LENGTH);
    const formulaProbe = text.normalize("NFKC");
    // All identifiers are text: not just long integers, but also 1E10 / 01-02.
    if ((isId && text) || /^[\t\r\n]/.test(text) || /^\s*[=+@-]/u.test(formulaProbe)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function toCsv(rows) {
    const lines = [CSV_HEADERS.map((header) => csvCell(header, false)).join(",")];
    for (const row of normalizeCatalogRows(rows)) {
      const values = [row.product_name, row.product_id, row.sku_id, row.merchant_code, row.sku_text,
        row.price_text, row.stock_text, row.url, row.captured_at];
      lines.push(values.map((value, index) => csvCell(value, index === 1 || index === 2 || index === 3)).join(","));
    }
    return "\uFEFF" + lines.join("\r\n") + "\r\n";
  }

  function toLinksCsv(rows) {
    const lines = [LINK_CSV_HEADERS.map((header) => csvCell(header, false)).join(",")];
    const seen = new Set();
    if (Array.isArray(rows)) {
      for (let index = 0; index < Math.min(rows.length, MAX_ROWS); index++) {
        const row = ownValue(rows, String(index));
        // Re-derive the IDs, type and warnings from the actual URL; caller fields
        // cannot promote an unverified link into a confirmed catalog record.
        const parsed = normalizeLink(typeof row === "string" ? row : ownValue(row, "url"));
        if (!parsed.ok || seen.has(parsed.url)) continue;
        seen.add(parsed.url);
        const type = parsed.kind === "short_link" ? "短链接（未解析）"
          : parsed.kind === "product_link" ? "含商品 ID 的链接（未核验）" : "官方域名链接（未核验）";
        const explanation = "仅解析 URL 文本；商品、SKU 与链接类型均未核验，未检查商品详情或可用状态。"
          + (parsed.warning ? " " + parsed.warning : "");
        const values = [parsed.url, parsed.product_id, parsed.sku_id, type, explanation];
        lines.push(values.map((value, column) => csvCell(value, column === 1 || column === 2)).join(","));
      }
    }
    return "\uFEFF" + lines.join("\r\n") + "\r\n";
  }

  function titleChecks(text) {
    const title = typeof text === "string" ? text.slice(0, MAX_INPUT_LENGTH) : "";
    const length = Array.from(title).length;
    const terms = title.match(/[\p{L}\p{N}]+/gu) || [];
    const counts = new Map();
    const duplicates = [];
    for (const term of terms) {
      const key = term.toLocaleLowerCase("zh-CN");
      const count = (counts.get(key) || 0) + 1;
      counts.set(key, count);
      if (count === 2) duplicates.push(term);
    }
    const warnings = [];
    if (!title.trim()) warnings.push("请先填写标题。");
    if (duplicates.length) warnings.push("发现重复词，请人工核对是否有必要保留。");
    if (typeof text === "string" && text.length > MAX_INPUT_LENGTH) warnings.push("仅检查前 100000 字符。");
    return { length, duplicate_terms: duplicates, warnings };
  }

  function catalogHealth(row = {}) {
    const stock = field(row, "stock_text").trim();
    const parsed = /^(?:\d+|\d{1,3}(?:,\d{3})+)$/.test(stock) ? Number(stock.replace(/,/g, "")) : null;
    const known = parsed !== null && Number.isSafeInteger(parsed) && parsed >= 0;
    const issues = [];
    if (!identifier(ownValue(row, "product_id"))) issues.push("商品编号未读取");
    if (!identifier(ownValue(row, "sku_id"))) issues.push("规格编号未读取");
    // The catalog projection may not carry observation evidence for an empty
    // code. Do not diagnose a real blank from absent/unreadable source data.
    if (!field(row, "merchant_code").trim()) issues.push("商家编码待核对（未读取或为空）");
    const price = field(row, "price_text").trim();
    if (!price || /^(?:--?|—|未知|未读取)$/.test(price)) issues.push("价格未读");
    else if (!/^(?:[¥￥]\s*)?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?(?:\s*元)?$/.test(price)) issues.push("价格为区间或格式需复核");
    if (!known) issues.push("库存原文未能精确判断");
    else if (parsed === 0) issues.push("本行库存为 0");
    const losses = ownValue(row, "bulk_source_issues");
    if (Array.isArray(losses) && losses.length) issues.push("源字段有清理或冲突");
    return { stock_state: known ? parsed === 0 ? "zero" : "known" : "unknown", code_missing: !field(row, "merchant_code").trim(), identity_missing: !identifier(ownValue(row, "product_id")) || !identifier(ownValue(row, "sku_id")), issues,
      next_step: !known || parsed === 0 ? "先在抖店页面核对规格库存与可售状态。" : issues.length ? "先核对当前商品与规格资料，再检查价格和商家编码。" : "本行资料可用于人工核对；请结合来源页面和读取时间确认。", execution_authorized: false };
  }
  function matchesCatalogHealth(row, value = "all") {
    const health = catalogHealth(row);
    return value === "stock_zero" ? health.stock_state === "zero" : value === "stock_unknown" ? health.stock_state === "unknown" : value === "code_missing" ? health.code_missing : value === "identity_missing" ? health.identity_missing : value === "review_complete" ? !health.issues.length : true;
  }
  return Object.freeze({ normalizeLink, parseLinks, normalizeCatalogRows, toCsv, toLinksCsv, titleChecks, catalogHealth, matchesCatalogHealth });
});
