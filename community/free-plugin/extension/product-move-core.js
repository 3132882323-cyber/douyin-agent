(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports ? require("./shelf-detail-core.js") : root.DianShelfDetailCore,
    typeof module === "object" && module.exports ? require("./shelf-tools-core.js") : root.DianShelfToolsCore);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianProductMoveCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (detailCore, linkCore) {
  "use strict";

  // Original, DOM-free local contracts. No network, persistence, connector,
  // publication, platform draft creation, or authorization exists in this core.
  const LIMITS = Object.freeze({ skus: 200, attributes: 100, specifications: 20, assets: 100, warnings: 40, mappings: 200, batch: 20, text: 4000, url: 8192 });
  const STATES = Object.freeze(["collecting", "mapping", "preview", "blocked", "cancelled", "localpreviewready"]);
  // Business-source type is separate from a collection method such as framework.
  // Explicit saved drafts use their own broker scope; marketplace is unsupported.
  const SOURCE_KINDS = Object.freeze(["doudian_edit_product", "doudian_store_product", "doudian_draft_product"]);
  const CONTROL = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/;
  const POISON = new Set(["__proto__", "prototype", "constructor"]);
  const NEXT = Object.freeze({ collecting: ["mapping", "blocked", "cancelled"], mapping: ["preview", "blocked", "cancelled"], preview: ["localpreviewready", "blocked", "cancelled"], blocked: ["mapping", "cancelled"], localpreviewready: ["mapping", "cancelled"], cancelled: [] });

  function own(object, key) {
    try { const d = Object.getOwnPropertyDescriptor(object, key); return d && Object.hasOwn(d, "value") ? d.value : undefined; }
    catch (_) { return undefined; }
  }
  function has(object, key) { try { return !!Object.getOwnPropertyDescriptor(object, key); } catch (_) { return false; } }
  function issue(list, path, code, message) { list.push({ path, code, message }); }
  function plain(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    try { const p = Object.getPrototypeOf(value); return p === null || p === Object.prototype; } catch (_) { return false; }
  }
  function inspect(value, issues, whitespacePath) {
    const seen = new Set(); let nodes = 0;
    function walk(v, path, depth) {
      if (++nodes > 12000 || depth > 12) { issue(issues, path, "input_limit", "输入结构超过安全上限。"); return false; }
      if (typeof v === "string") {
        const checked = path === whitespacePath ? v.replace(/[\t\n\r\f\v\u2028\u2029]/g, " ") : v;
        if (v.length > LIMITS.url || CONTROL.test(checked)) issue(issues, path, "unsafe_text", "文本过长或含控制字符；未截断或修复。");
        return true;
      }
      if (v === null || v === undefined || typeof v === "boolean") return true;
      if (typeof v === "number") { if (!Number.isFinite(v) || Math.abs(v) > Number.MAX_SAFE_INTEGER) issue(issues, path, "unsafe_number", "数值非有限值或可能已丢失精度。"); return true; }
      if (typeof v !== "object") { issue(issues, path, "unsupported_value", "只接受自身数据字段，不接受函数、符号或大整数对象。"); return false; }
      if (seen.has(v)) { issue(issues, path, "cyclic_input", "不接受循环对象引用。"); return false; }
      seen.add(v);
      let keys, descriptors;
      try {
        const p = Object.getPrototypeOf(v);
        if (Array.isArray(v) ? p !== Array.prototype : p !== null && p !== Object.prototype) { issue(issues, path, "unsafe_prototype", "仅接受普通对象或普通数组的自身数据字段。"); return false; }
        keys = Reflect.ownKeys(v); descriptors = Object.getOwnPropertyDescriptors(v);
      } catch (_) { issue(issues, path, "unreadable_object", "无法安全读取自身字段。"); return false; }
      if (keys.length > 1000) { issue(issues, path, "input_limit", "对象字段或数组长度超过安全上限。"); return false; }
      for (const key of keys) {
        const next = path ? path + "." + String(key) : String(key);
        if (typeof key !== "string" || POISON.has(key)) { issue(issues, next, "poison_key", "不接受原型相关字段或符号字段。"); continue; }
        const d = descriptors[key];
        if (!Object.hasOwn(d, "value")) { issue(issues, next, "accessor_field", "不接受 getter/setter，未执行该访问器。"); continue; }
        if (Array.isArray(v) && key === "length") {
          if (d.value > 1000) issue(issues, next, "input_limit", "数组超过安全上限。");
          continue;
        }
        if (Array.isArray(v) && !/^(?:0|[1-9]\d*)$/.test(key)) { issue(issues, next, "array_property", "数组仅接受自身索引数据。"); continue; }
        if (!walk(d.value, next, depth + 1)) return false;
      }
      seen.delete(v);
      return true;
    }
    walk(value, "", 0);
  }
  function fields(value, allowed, path, issues) {
    if (!plain(value)) { issue(issues, path, "object_required", "需要普通数据对象。"); return false; }
    for (const key of Object.getOwnPropertyNames(value)) if (!allowed.includes(key)) issue(issues, path ? path + "." + key : key, "unknown_field", "字段不属于本地规范，未采用。");
    return true;
  }
  function text(value, path, issues, limit = LIMITS.text, id = false) {
    if (typeof value !== "string" || value.length > limit || CONTROL.test(value) || (id && !/^[A-Za-z0-9_-]{1,128}$/.test(value))) {
      issue(issues, path, id ? "exact_text_id_required" : "invalid_text", id ? "身份必须是完整精确文本；不转换数值 ID。" : "文本格式无效或超过上限。"); return undefined;
    }
    return value;
  }
  function optionalText(input, output, key, path, issues, limit, id) {
    if (!has(input, key) || own(input, key) === undefined) return;
    const value = text(own(input, key), path ? path + "." + key : key, issues, limit, id);
    if (value !== undefined) output[key] = value;
  }
  function rawText(value, path, issues) {
    if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) return String(value);
    return text(value, path, issues, 500);
  }
  function array(value, path, max, issues, map) {
    if (!Array.isArray(value) || own(value, "length") > max) { issue(issues, path, "array_limit", "列表格式无效或超过 " + max + " 项上限。"); return undefined; }
    const result = [];
    for (let i = 0; i < own(value, "length"); i++) {
      if (!has(value, String(i))) { issue(issues, path + "." + i, "sparse_array", "不接受缺项数组。"); continue; }
      const item = map(own(value, String(i)), path + "." + i);
      if (item !== undefined) result.push(item);
    }
    return result;
  }
  function attributeMetadata(value,path,issues,display) {
    if(!fields(value,["property_id","selected","ui_type_raw","property_type_raw","cardinality"],path,issues))return undefined;
    const out={};optionalText(value,out,"property_id",path,issues,128,true);
    if(!out.property_id)issue(issues,path+".property_id","field_required","来源属性编号缺失。");
    const ui=own(value,"ui_type_raw"),isText=ui==="text";
    if(!["select","multi_select","search","text"].includes(ui)||own(value,"cardinality")!=="unknown")issue(issues,path,"attribute_metadata_unverified","仅保留已识别的来源内容，目标填写方式仍需核对。");
    else {out.ui_type_raw=ui;out.cardinality="unknown";}
    if(has(value,"property_type_raw")){const type=own(value,"property_type_raw");if(!Number.isSafeInteger(type)||type<0)issue(issues,path+".property_type_raw","unsafe_number","属性类型原值无效。");else out.property_type_raw=type;}
    if(["text","search"].includes(ui)&&out.property_type_raw!==1)issue(issues,path+".property_type_raw","attribute_metadata_unverified","来源填写方式未核实。");
    const ids=new Set(),names=new Set();out.selected=array(own(value,"selected"),path+".selected",isText?1:100,issues,(v,at)=>{if(!fields(v,isText?["value_name"]:["value_id","value_name"],at,issues))return;const e={};if(!isText)optionalText(v,e,"value_id",at,issues,128,true);optionalText(v,e,"value_name",at,issues,500);if(!isText&&(!e.value_id||ids.has(e.value_id))||!e.value_name?.trim()||names.has(e.value_name))issue(issues,at,"attribute_selection_ambiguous","来源内容缺失或重复，无法明确对应。");if(!isText)ids.add(e.value_id);names.add(e.value_name);return e;});
    if(!out.selected?.length||out.selected.map(e=>e.value_name).join(" / ")!==display)issue(issues,path,"attribute_display_mismatch","显示属性值与来源枚举不一致。");return out;
  }
  function namedValue(value, path, issues, attribute=false) {
    if (!fields(value, attribute?["name", "value","source_metadata"]:["name", "value"], path, issues)) return undefined;
    const out = {};
    for (const key of ["name", "value"]) {
      if (!has(value, key) || own(value, key) === undefined) { issue(issues, path + "." + key, "field_required", "属性名称和原值必须显式提供。"); continue; }
      optionalText(value, out, key, path, issues, 500);
    }
    if(attribute&&has(value,"source_metadata"))out.source_metadata=attributeMetadata(own(value,"source_metadata"),path+".source_metadata",issues,out.value);
    return out;
  }
  function attributeIdentities(entries,path,issues){const ids=new Set();for(const [i,entry]of(entries||[]).entries()){const id=entry.source_metadata?.property_id;if(id){if(ids.has(id))issue(issues,path+"."+i+".source_metadata.property_id","attribute_identity_duplicate","来源属性编号重复，不能按显示名称合并。");ids.add(id);}}}
  function reference(value, path, issues) {
    if (!fields(value, ["source_id", "label"], path, issues)) return undefined;
    const out = {};
    optionalText(value, out, "source_id", path, issues, 128, true);
    optionalText(value, out, "label", path, issues, 500);
    return out;
  }
  function source(value, issues) {
    const path = "source", out = {};
    if (!fields(value, ["platform", "source_kind", "product_id", "shop_name", "store_key", "source_url", "context_key", "captured_at"], path, issues)) return undefined;
    if (own(value, "platform") !== "doudian") issue(issues, "source.platform", "unsupported_source", "来源平台必须是 doudian。");
    else out.platform = "doudian";
    if (has(value, "source_kind")) {
      const kind = own(value, "source_kind");
      if (!SOURCE_KINDS.includes(kind)) issue(issues, "source.source_kind", "unsupported_source_kind", "该来源类型没有已接入的资料适配器；不转换成抖店后台商品。");
      else out.source_kind = kind;
    }
    if (!has(value, "product_id")) issue(issues, "source.product_id", "field_required", "必须提供精确来源商品 ID。");
    optionalText(value, out, "product_id", path, issues, 128, true);
    for (const key of ["shop_name", "store_key", "context_key"]) optionalText(value, out, key, path, issues, key === "context_key" ? 2000 : 500);
    if (has(value, "source_url") && own(value, "source_url") !== undefined) {
      const parsed = linkCore && typeof linkCore.normalizeLink === "function" ? linkCore.normalizeLink(own(value, "source_url")) : null;
      if (!parsed || !parsed.ok || parsed.warning || parsed.kind === "short_link" || (parsed.product_id && parsed.product_id !== out.product_id)) issue(issues, "source.source_url", "unsafe_source_url", "来源地址不安全、身份冲突或无法核验；未拼接替代地址。");
      else out.source_url = parsed.url;
    }
    if (has(value, "captured_at") && own(value, "captured_at") !== undefined) {
      const raw = own(value, "captured_at");
      if (typeof raw !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(raw)) issue(issues, "source.captured_at", "invalid_timestamp", "采集时间必须是显式有效的 UTC 时间文本。");
      else {
        const date = new Date(raw);
        if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== raw.slice(0, 19)) issue(issues, "source.captured_at", "invalid_timestamp", "日期无效，未推测或修复。");
        else out.captured_at = raw;
      }
    }
    if (out.source_kind) {
      let url; try { url = new URL(out.source_url); } catch (_) { /* Evidence missing. */ }
      const known = url?.origin === "https://fxg.jinritemai.com" && !url.hash;
      const edit = known && url.pathname === "/ffa/g/create" && url.searchParams.getAll("product_id").length === 1 && url.searchParams.get("product_id") === out.product_id && url.searchParams.getAll("entrance").length === 1 && url.searchParams.get("entrance") === "edit";
      const draft = known && url.pathname === "/ffa/g/create" && url.searchParams.getAll("product_id").length === 1 && url.searchParams.get("product_id") === out.product_id && url.searchParams.getAll("entrance").length === 1 && url.searchParams.get("entrance") === "draft" && [...url.searchParams.keys()].every(k=>['product_id','entrance'].includes(k));
      const list = known && url.pathname === "/ffa/g/list";
      if (out.source_kind === "doudian_edit_product" ? !edit : out.source_kind === "doudian_draft_product" ? !draft : !list) issue(issues, "source.source_kind", "source_kind_route_mismatch", "来源类型与已有原生页面地址不匹配；未重新分类或猜测。");
    }
    return out;
  }
  function sku(value, path, issues) {
    if (!fields(value, ["sku_id", "specifications", "price_raw", "stock_raw", "merchant_code_raw", "status_raw"], path, issues)) return undefined;
    const out = {};
    optionalText(value, out, "sku_id", path, issues, 128, true);
    if (has(value, "specifications")) out.specifications = array(own(value, "specifications"), path + ".specifications", LIMITS.specifications, issues, (item, at) => namedValue(item, at, issues));
    for (const key of ["price_raw", "stock_raw", "merchant_code_raw", "status_raw"]) if (has(value, key) && own(value, key) !== undefined) {
      const raw = rawText(own(value, key), path + "." + key, issues);
      if (raw !== undefined) out[key] = raw;
    }
    return out;
  }
  function media(value, issues) {
    const keys = ["main_images", "detail_images", "sku_images", "videos"], out = {}; let count = 0;
    if (!fields(value, keys, "media", issues)) return undefined;
    for (const key of keys) if (has(value, key)) out[key] = array(own(value, key), "media." + key, LIMITS.assets, issues, (entry, path) => {
      if (++count > LIMITS.assets) { issue(issues, path, "asset_limit", "媒体总数超过 100 项上限。"); return undefined; }
      if (!fields(entry, key === "sku_images" ? ["url", "sku_id"] : ["url"], path, issues)) return undefined;
      const parser=key==="videos"?detailCore?.normalizeVideoAssetUrl:detailCore?.normalizeAssetUrl;
      const parsed = typeof parser === "function" ? parser(own(entry, "url")) : null;
      if (!parsed || !parsed.ok) { issue(issues, path + ".url", "unsafe_asset_url", "媒体地址未通过现有安全校验；未下载或替换。"); return undefined; }
      const result = { url: parsed.url };
      if (key === "sku_images") {
        if (!has(entry, "sku_id")) issue(issues, path + ".sku_id", "sku_image_identity_required", "规格图片必须明确绑定原生 SKU ID，不能按顺序猜测。");
        else optionalText(entry, result, "sku_id", path, issues, 128, true);
      }
      return result;
    });
    return out;
  }
  function validateSkuAssets(mediaValue, skuValues, issues) {
    const known = new Set((skuValues || []).map(entry => entry.sku_id)), seen = new Set();
    for (const [index, image] of (mediaValue?.sku_images || []).entries()) {
      const path = "media.sku_images." + index + ".sku_id";
      if (!image.sku_id || !known.has(image.sku_id)) issue(issues, path, "unknown_sku_image_identity", "规格图片的身份不在当前已读 SKU 清单中。");
      else if (seen.has(image.sku_id)) issue(issues, path, "duplicate_sku_image_identity", "同一 SKU 存在多条图片记录，未任意选择其中一条。");
      seen.add(image.sku_id);
    }
  }
  function normalizePackage(input) {
    const issues = []; inspect(input, issues);
    if (issues.length) return { ok: false, product: null, issues };
    if (!fields(input, ["schema_version", "source", "title", "category", "brand", "attributes", "skus", "media", "shipping", "coverage", "warnings"], "", issues)) return { ok: false, product: null, issues };
    const out = {};
    if (own(input, "schema_version") !== 1) issue(issues, "schema_version", "unsupported_schema", "仅支持显式 schema_version: 1。"); else out.schema_version = 1;
    out.source = source(own(input, "source"), issues);
    optionalText(input, out, "title", "", issues);
    for (const key of ["category", "brand"]) if (has(input, key)) out[key] = reference(own(input, key), key, issues);
    if (has(input, "attributes")) out.attributes = array(own(input, "attributes"), "attributes", LIMITS.attributes, issues, (entry, at) => namedValue(entry, at, issues,true));
    attributeIdentities(out.attributes,"attributes",issues);
    if (has(input, "skus")) out.skus = array(own(input, "skus"), "skus", LIMITS.skus, issues, (entry, at) => sku(entry, at, issues));
    if (has(input, "media")) out.media = media(own(input, "media"), issues);
    validateSkuAssets(out.media, out.skus, issues);
    if (has(input, "shipping") && fields(own(input, "shipping"), ["template_id", "template_name"], "shipping", issues)) {
      out.shipping = {};
      optionalText(own(input, "shipping"), out.shipping, "template_id", "shipping", issues, 128, true);
      optionalText(own(input, "shipping"), out.shipping, "template_name", "shipping", issues, 500);
    }
    if (has(input, "coverage") && fields(own(input, "coverage"), ["sku_complete", "media_complete"], "coverage", issues)) {
      out.coverage = {};
      for (const key of ["sku_complete", "media_complete"]) if (has(own(input, "coverage"), key)) {
        const raw = own(own(input, "coverage"), key);
        if (typeof raw !== "boolean") issue(issues, "coverage." + key, "boolean_required", "覆盖范围必须显式是布尔值，不能推测完整性。"); else out.coverage[key] = raw;
      }
    }
    if (has(input, "warnings")) out.warnings = array(own(input, "warnings"), "warnings", LIMITS.warnings, issues, (entry, at) => text(entry, at, issues, 500));
    return { ok: !issues.length, product: issues.length ? null : out, issues };
  }
  function mapping(value, path, issues) {
    return array(value, path, LIMITS.mappings, issues, (entry, at) => {
      if (!fields(entry, ["source_id", "target_id", "target_label"], at, issues)) return undefined;
      const out = {};
      for (const key of ["source_id", "target_id"]) {
        if (!has(entry, key)) issue(issues, at + "." + key, "field_required", "映射必须显式指定精确来源与目标 ID。");
        optionalText(entry, out, key, at, issues, 128, true);
      }
      optionalText(entry, out, "target_label", at, issues, 500);
      return out;
    });
  }
  function policy(value, path, issues) {
    const isPrice = path === "price_policy";
    if (!fields(value, isPrice ? ["mode", "value", "factor", "offset", "decimals"] : ["mode", "value"], path, issues)) return undefined;
    const out = {}, mode = own(value, "mode");
    if (mode !== "preserve_raw" && mode !== "set" && !(isPrice && mode === "formula")) issue(issues, path + ".mode", "invalid_policy", "价格策略支持 preserve_raw、set、formula；库存仅支持 preserve_raw 或 set。"); else out.mode = mode;
    if (isPrice && mode !== "formula") for (const key of ["factor", "offset", "decimals"]) if (has(value, key)) issue(issues, path + "." + key, "policy_conflict", "公式参数仅允许在 formula 策略中提供。");
    if (mode === "formula" && isPrice) {
      if (has(value, "value")) issue(issues, path + ".value", "policy_conflict", "formula 不接受固定覆盖值。");
      for (const key of ["factor", "offset", "decimals"]) {
        const raw = own(value, key);
        if (!has(value, key)) issue(issues, path + "." + key, "field_required", "公式必须显式提供系数、加减额和小数位文本。");
        else if (typeof raw !== "string" || (key === "decimals" ? !/^[0-6]$/.test(raw) : !decimal(key === "offset" && raw.startsWith("-") ? raw.slice(1) : raw, false))) issue(issues, path + "." + key, "invalid_formula_parameter", "系数为非负十进制文本，加减额可为负；小数位必须为 0–6 的整数文本。");
        else out[key] = raw;
      }
      return out;
    }
    if (mode === "set") {
      if (!has(value, "value")) issue(issues, path + ".value", "field_required", "set 策略必须显式提供数值原文。");
      else {
        const raw = rawText(own(value, "value"), path + ".value", issues);
        if (raw !== undefined) {
          if (!decimal(raw, path === "stock_policy")) issue(issues, path + ".value", "invalid_policy_value", "价格需要非负十进制原文；库存需要非负整数原文。"); else out.value = raw;
        }
      }
    } else if (has(value, "value")) issue(issues, path + ".value", "policy_conflict", "preserve_raw 不接受覆盖值。");
    return out;
  }
  function specificationPolicy(value, issues) {
    const path = "specification_policy", out = {};
    if (!fields(value, ["swap_axes", "color_first", "remove_words"], path, issues)) return undefined;
    if (has(value, "swap_axes")) {
      if (typeof own(value, "swap_axes") !== "boolean") issue(issues, path + ".swap_axes", "boolean_required", "交换两维必须显式是布尔值。");
      else out.swap_axes = own(value, "swap_axes");
    }
    if (has(value, "color_first")) {
      if (typeof own(value, "color_first") !== "boolean") issue(issues, path + ".color_first", "boolean_required", "颜色排在前面必须明确选择。");
      else out.color_first = own(value, "color_first");
    }
    if (out.swap_axes && out.color_first) issue(issues, path + ".color_first", "specification_order_conflict", "颜色排在前面和手动对调不能同时使用，请选择一种顺序处理方式。");
    if (has(value, "remove_words")) {
      const raw = own(value, "remove_words");
      if (typeof raw !== "string" || raw.length > LIMITS.text) issue(issues, path + ".remove_words", "invalid_text", "清理词必须是有界文本，按空白分词，不使用正则表达式。");
      else out.remove_words = raw.split(/\s+/).filter(Boolean).join(" ");
    }
    return out;
  }
  function normalizeConfig(input) {
    const issues = []; inspect(input, issues, "specification_policy.remove_words");
    if (issues.length) return { ok: false, config: null, issues };
    if (!fields(input, ["target_platform", "target_context_key", "intent", "title_replace", "category_mapping", "shipping_mapping", "price_policy", "stock_policy", "specification_policy"], "", issues)) return { ok: false, config: null, issues };
    const out = {};
    if (!has(input, "target_platform")) issue(issues, "target_platform", "field_required", "必须显式选择目标平台。");
    optionalText(input, out, "target_platform", "", issues, 128, true);
    optionalText(input, out, "target_context_key", "", issues, 2000);
    if (has(input, "intent")) {
      const intent = own(input, "intent");
      if (!["preview", "platform_draft", "publish"].includes(intent)) issue(issues, "intent", "invalid_intent", "意图必须是 preview、platform_draft 或 publish。"); else out.intent = intent;
    }
    if (has(input, "title_replace") && fields(own(input, "title_replace"), ["from", "to"], "title_replace", issues)) {
      const entry = own(input, "title_replace"); out.title_replace = {};
      for (const key of ["from", "to"]) {
        if (!has(entry, key)) issue(issues, "title_replace." + key, "field_required", "字面替换必须显式提供 from 与 to。");
        optionalText(entry, out.title_replace, key, "title_replace", issues);
      }
      if (out.title_replace.from === "") issue(issues, "title_replace.from", "empty_replacement_source", "替换来源不能为空；不使用正则表达式。");
    }
    for (const key of ["category_mapping", "shipping_mapping"]) if (has(input, key)) out[key] = mapping(own(input, key), key, issues);
    for (const key of ["price_policy", "stock_policy"]) if (has(input, key)) out[key] = policy(own(input, key), key, issues);
    if (has(input, "specification_policy")) out.specification_policy = specificationPolicy(own(input, "specification_policy"), issues);
    return { ok: !issues.length, config: issues.length ? null : out, issues };
  }
  function decimal(value, integer) { return typeof value === "string" && value.length <= 64 && (integer ? /^\d+$/.test(value) : /^\d+(?:\.\d+)?$/.test(value)); }
  function decimalParts(value) {
    const parts = value.split(".");
    return { units: BigInt(parts.join("")), scale: parts[1] ? parts[1].length : 0 };
  }
  function formulaPrice(raw, rule, path, conflicts) {
    // Only the raw source price enters the calculation. No previous preview
    // value is accepted as a new basis, and no binary float handles money.
    const base = decimalParts(raw), factor = decimalParts(rule.factor), offset = decimalParts(rule.offset);
    const power = scale => 10n ** BigInt(scale);
    if (factor.units === power(factor.scale) && offset.units === 0n) return raw;
    const multipliedScale = base.scale + factor.scale, scale = Math.max(multipliedScale, offset.scale);
    const total = base.units * factor.units * power(scale - multipliedScale) + offset.units * power(scale - offset.scale);
    if (total < 0n) { issue(conflicts, path, "price_formula_negative", "公式结果为负，未归零、舍入或采用该价格。"); return undefined; }
    const decimals = Number(rule.decimals);
    let rounded;
    if (scale <= decimals) rounded = total * power(decimals - scale);
    else {
      const divisor = power(scale - decimals), quotient = total / divisor;
      rounded = quotient + (total % divisor * 2n >= divisor ? 1n : 0n);
    }
    const digits = rounded.toString().padStart(decimals + 1, "0");
    const result = decimals ? digits.slice(0, -decimals) + "." + digits.slice(-decimals) : digits;
    if (!decimal(result, false)) { issue(conflicts, path, "price_formula_limit", "公式结果超过价格文本上限，未截断或采用。"); return undefined; }
    return result;
  }
  function adaptSpecifications(skus, rule, conflicts) {
    const words = (rule.remove_words || "").split(/\s+/).filter(Boolean);
    if (!rule.swap_axes && !rule.color_first && !words.length) return null;
    const start = conflicts.length, rows = skus || [], first = rows[0]?.specifications;
    if (!rows.length) { issue(conflicts, "skus", "specifications_unknown", "未读取 SKU 规格，不能处理或校验组合。"); return null; }
    if ((rule.swap_axes || rule.color_first) && (!rows.length || !first || first.length !== 2 || rows.some(entry => !entry.specifications || entry.specifications.length !== 2 || entry.specifications.some((part, index) => part.name !== first[index].name)))) {
      issue(conflicts, rule.color_first ? "specification_policy.color_first" : "specification_policy.swap_axes", "specification_axes_mismatch", "仅处理所有规格都具有顺序一致的两项规格名称的商品；本次没有调整顺序。");
      return null;
    }
    let swap = rule.swap_axes === true;
    if (rule.color_first) {
      // Use exact source axis names, never cleaned labels, image filenames,
      // values, or substring guesses. SKU rows and sku_id→image links stay put.
      const colors = first.map((part, index) => ["颜色", "颜色分类"].includes(part.name) ? index : -1).filter(index => index >= 0);
      if (colors.length !== 1) {
        issue(conflicts, "specification_policy.color_first", "specification_color_axis_unknown", "没有唯一、明确的颜色规格名称。本次未调整顺序；可核对后使用手动对调。");
        return null;
      }
      swap = colors[0] === 1;
    }
    const clean = value => words.reduce((result, word) => result.split(word).join(""), value);
    const names = new Map(), combinations = new Set(), reported = new Set();
    // Keep the first concrete location for each failure type. A bounded input
    // can contain thousands of bad cells; diagnostics must remain serializable.
    const report = (path, code, message) => { if (!reported.has(code)) { reported.add(code); issue(conflicts, path, code, message); } };
    const adapted = rows.map((entry, index) => {
      const path = "skus." + index + ".specifications", seenNames = new Set();
      if (!entry.specifications || !entry.specifications.length) { report(path, "specifications_unknown", "未读取该 SKU 规格，不能清理或校验组合。"); return undefined; }
      const specs = entry.specifications.map((part, axis) => {
        const out = { name: typeof part.name === "string" ? clean(part.name) : "", value: typeof part.value === "string" ? clean(part.value) : "" };
        if (!out.name.trim() || !out.value.trim()) report(path + "." + axis, "specification_empty", "规格清理后名称或值为空，未采用该规格处理结果。");
        if (seenNames.has(out.name) || (names.has(out.name) && names.get(out.name) !== part.name)) report(path + "." + axis + ".name", "specification_name_conflict", "规格名称重复或清理后合并了不同维度，未采用该规格处理结果。");
        seenNames.add(out.name); names.set(out.name, part.name);
        return out;
      });
      // Encode complete name/value pairs, not delimiter-joined labels. Axis
      // order is irrelevant to identity; SKU IDs and image links never change.
      const combination = JSON.stringify(specs.map(part => [part.name, part.value]).sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
      if (combinations.has(combination)) report(path, "specification_combination_collision", "处理后多个 SKU 的规格组合相同，未合并 SKU 或覆盖图片。");
      combinations.add(combination);
      return swap ? specs.reverse() : specs;
    });
    return conflicts.length === start ? adapted : null;
  }
  function stable(value) {
    if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
    if (value && typeof value === "object") return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + stable(value[key])).join(",") + "}";
    return JSON.stringify(value);
  }
  function keyFor(product, config) {
    const value = stable([product, config]); let a = 2166136261, b = 2246822519;
    for (let i = 0; i < value.length; i++) { a = Math.imul(a ^ value.charCodeAt(i), 16777619); b = Math.imul(b ^ value.charCodeAt(i), 3266489917); }
    return "local-move-v1:" + (a >>> 0).toString(16).padStart(8, "0") + (b >>> 0).toString(16).padStart(8, "0");
  }
  function dedupKey(product, config) {
    const p = normalizePackage(product), c = normalizeConfig(config);
    return p.ok && c.ok ? keyFor(p.product, c.config) : null;
  }
  function buildPreview(input, configInput) {
    const p = normalizePackage(input), c = normalizeConfig(configInput), missing = [], conflicts = [];
    const preview = { kind: "local_adaptation_preview", structural_ok: p.ok && c.ok, business_ready: false, target_fields: {}, missing, conflicts, warnings: [], coverage: { sku: "UNKNOWN", media: "UNKNOWN" }, connector_supported: false, platform_draft_created: false, platform_publish_created: false, execution_authorized: false, complete: false };
    if (!p.ok || !c.ok) { conflicts.push(...p.issues, ...c.issues); return preview; }
    const product = p.product, config = c.config, target = preview.target_fields;
    preview.dedup_key = keyFor(product, config);
    preview.target_platform = config.target_platform;
    if (has(config, "target_context_key")) preview.target_context_key = config.target_context_key;
    if (has(config, "intent")) preview.requested_intent = config.intent;
    preview.warnings = has(product, "warnings") ? product.warnings.slice() : [];
    preview.warnings.push("仅供本地适配预览；未创建平台草稿，未发布，不能作为执行授权。");
    if (!product.source.source_kind) issue(missing, "source.source_kind", "source_kind_unknown", "旧资料未标明受支持来源类型；仍可查看，但不能进入本地就绪状态。");
    // Both attested list-model snapshots and visible edit-page snapshots now
    // share this local package adapter. Readiness is never a platform grant.
    if (config.target_platform !== "doudian") issue(conflicts, "target_platform", "unsupported_local_target", "该目标没有已实现的本地字段适配器。");
    if (config.intent === "platform_draft" || config.intent === "publish") issue(conflicts, "intent", "connector_unavailable", "真实平台连接器未接入；草稿或发布意图不受支持。");
    if (!config.target_context_key || !config.target_context_key.trim()) issue(missing, "target_context_key", "target_context_unknown", "目标店铺上下文未知。");
    if (!product.source.context_key || !product.source.context_key.trim() || /^route-only:/i.test(product.source.context_key)
      || !product.source.store_key || !product.source.store_key.trim()) issue(missing, "source.context_key", "source_context_unknown", "来源店铺上下文未知；商品路由不能证明店铺身份，必须有明确读取的店铺键。");
    if (!product.source.source_url) issue(missing, "source.source_url", "source_url_unknown", "缺少已有的安全来源地址。");
    if (!product.source.captured_at) issue(missing, "source.captured_at", "capture_time_unknown", "采集时间未知，不能证明资料时效。");
    target.source_product_id = product.source.product_id;
    if (has(product, "title")) {
      if (config.title_replace) {
        const parts = product.title.split(config.title_replace.from);
        const length = product.title.length + (parts.length - 1) * (config.title_replace.to.length - config.title_replace.from.length);
        if (length > LIMITS.text) issue(conflicts, "title", "title_limit", "替换后标题超过上限，未生成、截断或采用该超长标题。");
        else target.title = parts.join(config.title_replace.to);
      } else target.title = product.title;
      if (has(target, "title") && !target.title.trim()) issue(missing, "title", "title_unknown", "商品标题为空。");
    } else issue(missing, "title", "title_unknown", "商品标题未读取。");
    if (has(product, "brand")) target.brand = product.brand;
    if (has(product, "attributes")) target.attributes = product.attributes;
    function adaptRef(sourceId, map, path, field) {
      if (!sourceId) { issue(missing, path, "source_mapping_id_unknown", "来源 ID 未读取；名称不能代替 ID 映射。"); return; }
      const matches = (map || []).filter((entry) => entry.source_id === sourceId);
      if (!matches.length) { issue(missing, path, "mapping_missing", "缺少显式目标映射。"); return; }
      if (new Set(matches.map((entry) => stable(entry))).size > 1) { issue(conflicts, path, "mapping_conflict", "同一来源 ID 存在不同目标映射，未选择其中之一。"); return; }
      const entry = matches[0]; target[field] = { target_id: entry.target_id };
      if (has(entry, "target_label")) target[field].target_label = entry.target_label;
    }
    adaptRef(product.category && product.category.source_id, config.category_mapping, "category", "category");
    adaptRef(product.shipping && product.shipping.template_id, config.shipping_mapping, "shipping", "shipping");
    for (const pair of [["sku_complete", "sku"], ["media_complete", "media"]]) {
      if (product.coverage && product.coverage[pair[0]] === true) preview.coverage[pair[1]] = "DECLARED_COMPLETE";
      else issue(missing, "coverage." + pair[0], "coverage_unknown", "可见资料不证明完整覆盖；缺少或 false 都按 UNKNOWN 处理。");
    }
    if (!product.skus || !product.skus.length) issue(missing, "skus", "skus_unknown", "没有完整可核对的 SKU 列表。");
    for (const key of ["price_policy", "stock_policy"]) if (!config[key]) issue(missing, key, "policy_missing", "必须显式选择价格和库存策略，不能默认补值。");
    const skuIds = new Set();
    if (has(product, "skus")) target.skus = product.skus.map((entry, index) => {
      const out = { ...entry }, at = "skus." + index;
      if (!entry.sku_id) issue(missing, at + ".sku_id", "sku_id_unknown", "SKU 身份未读取。");
      else if (skuIds.has(entry.sku_id)) issue(conflicts, at + ".sku_id", "duplicate_sku", "SKU ID 重复，未合并或推测。");
      else skuIds.add(entry.sku_id);
      for (const field of ["price", "stock"]) {
        const rule = config[field + "_policy"];
        if (!rule) continue;
        const value = rule.mode === "set" ? rule.value : entry[field + "_raw"];
        if (value === undefined || value === "") issue(missing, at + "." + field + "_raw", field + "_unknown", "价格或库存原值未知；没有转换为零。");
        else if (!decimal(value, field === "stock")) issue(conflicts, at + "." + field + "_raw", field + "_not_numeric", "原文不能无歧义适配为非负价格或整数库存，未去符号或猜值。");
        else {
          const adapted = rule.mode === "formula" ? formulaPrice(value, rule, at + ".price_value", conflicts) : value;
          if (adapted !== undefined) out[field + "_value"] = adapted;
        }
      }
      return out;
    });
    if (config.specification_policy) {
      const specifications = adaptSpecifications(product.skus, config.specification_policy, conflicts);
      if (specifications) target.skus.forEach((entry, index) => { entry.specifications = specifications[index]; });
    }
    if (has(product, "media")) target.media = product.media;
    if (!product.media || !product.media.main_images || !product.media.main_images.length) issue(missing, "media.main_images", "main_images_unknown", "主图未读取。");
    if (!product.media || !product.media.detail_images || !product.media.detail_images.length) issue(missing, "media.detail_images", "detail_images_unknown", "详情媒体未读取；未使用主图补齐。");
    preview.business_ready = !missing.length && !conflicts.length;
    return preview;
  }
  function createTask(productInput, configInput) {
    const p = normalizePackage(productInput), c = normalizeConfig(configInput), issues = [...p.issues, ...c.issues];
    if (issues.length) return { ok: false, task: null, issues };
    return { ok: true, task: { kind: "local_move_task", schema_version: 1, state: "collecting", local_task_key: keyFor(p.product, c.config), product: p.product, config: c.config, platform_draft_created: false, execution_authorized: false }, issues: [] };
  }
  function normalizeTask(input) {
    const issues = []; inspect(input, issues);
    if (issues.length) return { ok: false, task: null, issues };
    if (!fields(input, ["kind", "schema_version", "state", "local_task_key", "product", "config", "result", "platform_draft_created", "execution_authorized"], "", issues)) return { ok: false, task: null, issues };
    if (own(input, "kind") !== "local_move_task" || own(input, "schema_version") !== 1 || !STATES.includes(own(input, "state"))) issue(issues, "state", "invalid_task", "仅接受本地任务及规定状态，不接受完成或发布状态。");
    if (own(input, "platform_draft_created") !== false || own(input, "execution_authorized") !== false) issue(issues, "execution_authorized", "authorization_forbidden", "本地任务不能声明创建草稿或执行授权。");
    const created = createTask(own(input, "product"), own(input, "config")); issues.push(...created.issues);
    if (created.ok && own(input, "local_task_key") !== created.task.local_task_key) issue(issues, "local_task_key", "key_mismatch", "本地键与当前规范输入不一致。");
    if (has(input, "result")) {
      const result = normalizeResult(own(input, "result")); issues.push(...result.issues);
      if (result.ok && (result.result.kind !== "local_adaptation_preview" || !["preview", "blocked", "localpreviewready"].includes(own(input, "state")))) issue(issues, "result", "invalid_task_result", "仅预览相关本地状态可包含本地预览；平台回执不是本地任务完成证明。");
    }
    if (issues.length) return { ok: false, task: null, issues };
    const task = { ...created.task, state: own(input, "state") };
    if (["preview", "localpreviewready", "blocked"].includes(task.state)) task.result = buildPreview(task.product, task.config);
    if (task.state === "localpreviewready" && !task.result.business_ready) issue(issues, "state", "preview_not_ready", "存在缺项或冲突，不能声明本地预览已就绪。");
    return { ok: !issues.length, task: issues.length ? null : task, issues };
  }
  function transitionTask(input, nextState) {
    const normalized = normalizeTask(input);
    if (!normalized.ok) return normalized;
    const task = normalized.task, issues = [];
    if (typeof nextState !== "string" || !NEXT[task.state].includes(nextState)) issue(issues, "state", "invalid_transition", "不允许此状态跳转；取消是终态，平台结果不能替代本地预览。");
    if (!issues.length && nextState === "localpreviewready" && !buildPreview(task.product, task.config).business_ready) issue(issues, "state", "preview_not_ready", "仍存在缺项或冲突，不能进入本地就绪状态。");
    if (issues.length) return { ok: false, task, issues };
    const next = { ...task, state: nextState };
    if (["preview", "blocked", "localpreviewready"].includes(nextState)) next.result = buildPreview(next.product, next.config);
    else delete next.result;
    return { ok: true, task: next, issues: [] };
  }
  function resultTarget(input, issues) {
    if (!fields(input, ["source_product_id", "title", "brand", "attributes", "category", "shipping", "skus", "media"], "target_fields", issues)) return {};
    const out = {};
    optionalText(input, out, "source_product_id", "target_fields", issues, 128, true);
    optionalText(input, out, "title", "target_fields", issues);
    if (has(input, "brand")) out.brand = reference(own(input, "brand"), "target_fields.brand", issues);
    if (has(input, "attributes")) out.attributes = array(own(input, "attributes"), "target_fields.attributes", LIMITS.attributes, issues, (entry, at) => namedValue(entry, at, issues,true));
    attributeIdentities(out.attributes,"target_fields.attributes",issues);
    if (has(input, "media")) out.media = media(own(input, "media"), issues);
    for (const key of ["category", "shipping"]) if (has(input, key) && fields(own(input, key), ["target_id", "target_label"], "target_fields." + key, issues)) {
      const entry = own(input, key); out[key] = {};
      if (!has(entry, "target_id")) issue(issues, "target_fields." + key + ".target_id", "field_required", "目标映射必须包含精确目标 ID。");
      optionalText(entry, out[key], "target_id", "target_fields." + key, issues, 128, true);
      optionalText(entry, out[key], "target_label", "target_fields." + key, issues, 500);
    }
    if (has(input, "skus")) out.skus = array(own(input, "skus"), "target_fields.skus", LIMITS.skus, issues, (entry, at) => {
      const keys = ["sku_id", "specifications", "price_raw", "stock_raw", "merchant_code_raw", "status_raw"];
      if (!fields(entry, keys.concat(["price_value", "stock_value"]), at, issues)) return undefined;
      const original = {};
      for (const key of keys) if (has(entry, key)) original[key] = own(entry, key);
      const result = sku(original, at, issues);
      for (const key of ["price_value", "stock_value"]) if (has(entry, key)) {
        const value = own(entry, key);
        if (!decimal(value, key === "stock_value")) issue(issues, at + "." + key, "invalid_target_value", "适配值必须是显式非负十进制价格或整数库存文本。"); else result[key] = value;
      }
      return result;
    });
    validateSkuAssets(out.media, out.skus, issues);
    return out;
  }
  function resultIssues(input, path, issues) {
    return array(input, path, 1000, issues, (entry, at) => {
      if (!fields(entry, ["path", "code", "message"], at, issues)) return undefined;
      const out = {};
      for (const key of ["path", "code", "message"]) {
        if (!has(entry, key)) issue(issues, at + "." + key, "field_required", "缺项或冲突提示必须保留字段、代码与说明。");
        optionalText(entry, out, key, at, issues, 500);
      }
      return out;
    });
  }
  function normalizeResult(input) {
    const issues = []; inspect(input, issues);
    if (issues.length || !plain(input)) return { ok: false, result: null, issues: issues.length ? issues : [{ path: "", code: "object_required", message: "结果需要普通数据对象。" }] };
    const kind = own(input, "kind");
    if (kind === "local_adaptation_preview") {
      fields(input, ["kind", "structural_ok", "business_ready", "target_fields", "missing", "conflicts", "warnings", "coverage", "connector_supported", "platform_draft_created", "platform_publish_created", "execution_authorized", "complete", "dedup_key", "target_platform", "target_context_key", "requested_intent"], "", issues);
      if (own(input, "connector_supported") !== false) issue(issues, "connector_supported", "connector_claim_forbidden", "本地核心未实现平台连接器。");
      for (const key of ["platform_draft_created", "platform_publish_created", "execution_authorized", "complete"]) if (own(input, key) !== false) issue(issues, key, "platform_claim_forbidden", "本地预览的写入、授权和完成标志必须为 false。");
      if (typeof own(input, "structural_ok") !== "boolean" || typeof own(input, "business_ready") !== "boolean" || !plain(own(input, "target_fields")) || !Array.isArray(own(input, "missing")) || !Array.isArray(own(input, "conflicts"))) issue(issues, "", "invalid_preview_result", "本地预览结果结构无效。");
      const missing = own(input, "missing"), conflicts = own(input, "conflicts");
      if (own(input, "business_ready") === true && (own(input, "structural_ok") !== true || !Array.isArray(missing) || !Array.isArray(conflicts) || missing.length || conflicts.length)) issue(issues, "business_ready", "readiness_conflict", "就绪声明与结构、缺项或冲突不一致。");
      const out = { kind, structural_ok: own(input, "structural_ok"), business_ready: own(input, "business_ready"), target_fields: resultTarget(own(input, "target_fields"), issues), missing: resultIssues(missing, "missing", issues), conflicts: resultIssues(conflicts, "conflicts", issues), connector_supported: false, platform_draft_created: false, platform_publish_created: false, execution_authorized: false, complete: false };
      out.warnings = array(own(input, "warnings"), "warnings", LIMITS.warnings + 1, issues, (entry, at) => text(entry, at, issues, 500));
      if (fields(own(input, "coverage"), ["sku", "media"], "coverage", issues)) {
        out.coverage = {};
        for (const key of ["sku", "media"]) {
          const value = own(own(input, "coverage"), key);
          if (value !== "UNKNOWN" && value !== "DECLARED_COMPLETE") issue(issues, "coverage." + key, "invalid_coverage_state", "覆盖范围仅能是 UNKNOWN 或 DECLARED_COMPLETE。"); else out.coverage[key] = value;
        }
      }
      for (const key of ["dedup_key", "target_platform", "target_context_key", "requested_intent"]) optionalText(input, out, key, "", issues, key === "target_context_key" ? 2000 : 128);
      if (has(out, "dedup_key") && !/^local-move-v1:[0-9a-f]{16}$/.test(out.dedup_key)) issue(issues, "dedup_key", "invalid_local_key", "仅接受本地输入指纹，不接受平台草稿 ID。");
      if (has(out, "requested_intent") && !["preview", "platform_draft", "publish"].includes(out.requested_intent)) issue(issues, "requested_intent", "invalid_intent", "预览意图无效。");
      if (out.business_ready === true) {
        const target = out.target_fields;
        if (out.target_platform !== "doudian" || !out.target_context_key || !out.target_context_key.trim() || (out.requested_intent && out.requested_intent !== "preview")
          || !target.source_product_id || !target.title || !target.title.trim() || !target.category || !target.shipping
          || !target.skus || !target.skus.length || target.skus.some((entry) => !entry || !entry.sku_id || !has(entry, "price_value") || !has(entry, "stock_value"))
          || (target.skus && new Set(target.skus.map((entry) => entry && entry.sku_id)).size !== target.skus.length)
          || !target.media || !target.media.main_images || !target.media.main_images.length || !target.media.detail_images || !target.media.detail_images.length
          || !out.coverage || out.coverage.sku !== "DECLARED_COMPLETE" || out.coverage.media !== "DECLARED_COMPLETE") issue(issues, "business_ready", "readiness_conflict", "目标字段、上下文或覆盖范围不足，不能声明本地预览就绪。");
      }
      return { ok: !issues.length, result: issues.length ? null : out, issues };
    }
    fields(input, ["kind", "target_platform", "connector_id", "receipt_id", "verified", "verification", "connector_supported", "platform_draft_created", "platform_publish_created", "execution_authorized", "complete"], "", issues);
    if (kind !== "platform_draft_receipt" && kind !== "platform_publish_receipt") issue(issues, "kind", "unsupported_result_kind", "结果必须区分本地预览、平台草稿回执和平台发布回执。");
    const out = { kind, verification: "unverified", connector_supported: false, platform_draft_created: false, platform_publish_created: false, execution_authorized: false, complete: false };
    for (const key of ["target_platform", "connector_id", "receipt_id"]) {
      if (!has(input, key)) issue(issues, key, "field_required", "声明的回执需要显式平台、连接器和回执 ID。");
      optionalText(input, out, key, "", issues, 128, true);
    }
    if (has(input, "verified") && own(input, "verified") !== false) issue(issues, "verified", "receipt_unverified", "本纯函数只接受未核验声明。");
    if (has(input, "verification") && own(input, "verification") !== "unverified") issue(issues, "verification", "receipt_unverified", "本纯函数只接受 unverified 状态。");
    for (const key of ["connector_supported", "platform_draft_created", "platform_publish_created", "execution_authorized", "complete"]) if (has(input, key) && own(input, key) !== false) issue(issues, key, "receipt_unverified", "回执声明不能在本地核心转换为执行、写入或完成事实。");
    if (own(input, "verified") === true || own(input, "verification") === "verified" || own(input, "complete") === true || own(input, "execution_authorized") === true || own(input, "platform_draft_created") === true || own(input, "platform_publish_created") === true || own(input, "connector_supported") === true) issue(issues, "verification", "receipt_unverified", "本纯函数不能核验平台回执；布尔值不能证明成功或授权。");
    return { ok: !issues.length, result: issues.length ? null : out, issues };
  }
  function createBatch(products, config) {
    const issues = []; inspect(products, issues);
    if (!Array.isArray(products) || own(products, "length") > LIMITS.batch) issue(issues, "products", "batch_limit", "一次本地批次最多 20 个商品，未截断。");
    const c = normalizeConfig(config); issues.push(...c.issues);
    if (issues.length) return { ok: false, tasks: [], duplicate_count: 0, issues };
    const tasks = [], seen = new Set(); let duplicates = 0;
    for (let i = 0; i < own(products, "length"); i++) {
      const p = normalizePackage(own(products, String(i)));
      if (!p.ok) { issues.push(...p.issues.map((entry) => ({ ...entry, path: "products." + i + "." + entry.path }))); continue; }
      // Compare full normalized input, not hash equality: the short display key
      // is a local fingerprint, not a collision-proof identity or auth token.
      const fingerprint = stable([p.product, c.config]);
      if (seen.has(fingerprint)) { duplicates++; continue; }
      seen.add(fingerprint); tasks.push(createTask(p.product, c.config).task);
    }
    return { ok: !issues.length, tasks: issues.length ? [] : tasks, duplicate_count: duplicates, issues };
  }
  function calculatePriceRule(rawMajor,rule){const checked=normalizeConfig({target_platform:'doudian',intent:'preview',price_policy:rule,stock_policy:{mode:'preserve_raw'}});if(!checked.ok)return{ok:false,value:null,issues:checked.issues};const issues=[];if(!decimal(rawMajor,false))issue(issues,'price_raw','price_source_unavailable','原价必须是明确十进制文本，未把空值或未知值补零。');if(issues.length)return{ok:false,value:null,issues};const p=checked.config.price_policy,value=p.mode==='formula'?formulaPrice(rawMajor,p,'price_value',issues):p.mode==='set'?p.value:rawMajor;return{ok:!issues.length,value:issues.length?null:value,issues};}
  return Object.freeze({ LIMITS, STATES, SOURCE_KINDS, normalizePackage, normalizeConfig, buildPreview, dedupKey, createTask, normalizeTask, transitionTask, normalizeResult, createBatch,calculatePriceRule });
});
