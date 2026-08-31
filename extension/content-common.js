/** Shared page extractor. Raw DOM and browser credentials never leave the page. */
(function () {
  "use strict";

  const MAX_TABLES = 8;
  const MAX_ROWS = 100;
  // The current Qianchuan promotion grid exposes 25 business columns. Keep a
  // small bounded margin for future official columns instead of silently
  // dropping the last metric before header/body alignment is evaluated.
  const MAX_CELLS = 32;
  const MAX_TEXT = 6000;
  const SENSITIVE_HEADER = /收货|收件|联系人|客户姓名|真实姓名|姓名|详细地址|收货地址|配送地址|联系地址|手机|电话|联系方式|订单号|订单编号|交易号|支付单号|买家账号|买家昵称|用户账号|用户ID|用户编号|店铺ID|商家ID|门店ID|shop\s*id|store\s*id|merchant\s*id|身份证|证件号|邮箱/i;
  const LOCAL_ENTITY_HEADER_KINDS = Object.freeze({
    商品id: "douyin_product_id",
    抖音商品id: "douyin_product_id",
    productid: "douyin_product_id",
    goodsid: "douyin_product_id",
    skuid: "douyin_sku_id",
    商品skuid: "douyin_sku_id",
    规格id: "douyin_sku_id",
    商家编码: "merchant_product_code",
    商品编码: "merchant_product_code",
    货号: "merchant_product_code",
    计划id: "qianchuan_plan_id",
    广告计划id: "qianchuan_plan_id",
    项目id: "qianchuan_plan_id",
    素材id: "qianchuan_material_id",
    创意id: "qianchuan_material_id",
    视频id: "douyin_content_id",
    内容id: "douyin_content_id",
    直播间id: "douyin_live_room_id",
    房间id: "douyin_live_room_id",
    场次id: "douyin_live_session_id",
  });
  const SYNTHETIC_ENTITY_HEADERS = Object.freeze({
    douyin_product_id: "商品ID",
    douyin_sku_id: "SKU ID",
    merchant_product_code: "商家编码",
    qianchuan_plan_id: "计划ID",
    qianchuan_material_id: "素材ID",
    douyin_content_id: "视频ID",
    douyin_live_room_id: "直播间ID",
    douyin_live_session_id: "场次ID",
  });
  const LABELED_ENTITY_PATTERNS = Object.freeze({
    douyin_product_id: [/(?:抖音)?商品\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi, /(?:product|goods)[_\s-]*id\s*[:：#=-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    douyin_sku_id: [/(?:商品\s*)?SKU\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi, /规格\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    merchant_product_code: [/(?:商家编码|商品编码|货号)\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    qianchuan_plan_id: [/(?:(?:广告|推广|投放|商品|直播)?计划|广告组|单元)\s*(?:ID|编号)\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi, /(?:广告|推广|投放)?项目\s*(?:ID|编号)\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    qianchuan_material_id: [/(?:素材|创意)\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    douyin_content_id: [/(?:视频|内容)\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    douyin_live_room_id: [/(?:直播间|房间)\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
    douyin_live_session_id: [/场次\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})/gi],
  });
  const SAFE_METRIC_LABELS = [
    "用户支付金额", "订单量", "曝光人数", "点击人数", "成交人数", "点击成交率",
    "成交订单数", "成交件数", "观看次数", "退款金额", "直播场次", "成交金额",
    "结算金额", "成交退款金额", "投放消耗（店铺被投）", "投放效率（店铺被投）",
    "投放费比（剔除退款、店铺被投）", "投放贡献成交金额", "投放贡献成交退款金额",
    "直播间观看次数", "直播间观看人数", "商品点击人数", "商品点击率", "成交转化率",
    "直播间点击率", "在线人数", "最高在线人数", "GPM", "千次观看成交金额",
    "整体消耗(元)", "整体支付ROI", "整体成交金额(元)", "整体成交订单数", "整体成交订单成本(元)",
    "净成交ROI", "净成交金额(元)", "净成交订单数", "净成交订单成本(元)", "1小时内退款率",
    "展示次数", "点击次数", "点击率", "进入直播间人数", "进房率", "直播间商品点击人数",
    "直播间成交订单数", "直播间成交金额", "视频消耗", "视频点击率",
  ];
  const SAFE_SIGNAL_PATTERNS = [
    /猜你喜欢未入选/, /商品主图存在不良暗示/, /流量低于[^，。]{0,30}同行/, /转化低于[^，。]{0,30}同行/,
    /近7天销量(?:下滑|较低)/, /商品卡成交较差/, /暂无数据/, /当前待直播计划\s*0/,
    /(?:暂无|没有|无)(?:符合条件的|相关的?)?(?:投放)?计划/, /共\s*0\s*条计划/,
    /(?:计划数|计划总数|当前计划)\s*[:：]?\s*0(?:\D|$)/,
  ];

  function compact(value, max = 300) {
    return String(value || "")
      .replace(/\u200b/g, "")
      .replace(/[\t\r ]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, max);
  }

  const SHA256_ROUND_CONSTANTS = Object.freeze([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  function sha256Hex(value) {
    const input = String(value || "");
    const bytes = [];
    for (let index = 0; index < input.length; index += 1) {
      let codePoint = input.charCodeAt(index);
      if (codePoint >= 0xd800 && codePoint <= 0xdbff) {
        const low = input.charCodeAt(index + 1);
        if (low >= 0xdc00 && low <= 0xdfff) {
          codePoint = 0x10000 + ((codePoint - 0xd800) << 10) + (low - 0xdc00);
          index += 1;
        } else {
          codePoint = 0xfffd;
        }
      } else if (codePoint >= 0xdc00 && codePoint <= 0xdfff) {
        codePoint = 0xfffd;
      }
      if (codePoint <= 0x7f) bytes.push(codePoint);
      else if (codePoint <= 0x7ff) bytes.push(0xc0 | (codePoint >>> 6), 0x80 | (codePoint & 0x3f));
      else if (codePoint <= 0xffff) bytes.push(0xe0 | (codePoint >>> 12), 0x80 | ((codePoint >>> 6) & 0x3f), 0x80 | (codePoint & 0x3f));
      else bytes.push(0xf0 | (codePoint >>> 18), 0x80 | ((codePoint >>> 12) & 0x3f), 0x80 | ((codePoint >>> 6) & 0x3f), 0x80 | (codePoint & 0x3f));
    }

    const bitLength = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    const lengthHigh = Math.floor(bitLength / 0x100000000);
    const lengthLow = bitLength >>> 0;
    for (let shift = 24; shift >= 0; shift -= 8) bytes.push((lengthHigh >>> shift) & 0xff);
    for (let shift = 24; shift >= 0; shift -= 8) bytes.push((lengthLow >>> shift) & 0xff);

    const state = [
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];
    const words = new Uint32Array(64);
    const rotateRight = (word, bits) => (word >>> bits) | (word << (32 - bits));
    for (let offset = 0; offset < bytes.length; offset += 64) {
      for (let index = 0; index < 16; index += 1) {
        const byteOffset = offset + index * 4;
        words[index] = (
          (bytes[byteOffset] << 24)
          | (bytes[byteOffset + 1] << 16)
          | (bytes[byteOffset + 2] << 8)
          | bytes[byteOffset + 3]
        ) >>> 0;
      }
      for (let index = 16; index < 64; index += 1) {
        const previous15 = words[index - 15];
        const previous2 = words[index - 2];
        const sigma0 = rotateRight(previous15, 7) ^ rotateRight(previous15, 18) ^ (previous15 >>> 3);
        const sigma1 = rotateRight(previous2, 17) ^ rotateRight(previous2, 19) ^ (previous2 >>> 10);
        words[index] = (words[index - 16] + sigma0 + words[index - 7] + sigma1) >>> 0;
      }
      let [a, b, c, d, e, f, g, h] = state;
      for (let index = 0; index < 64; index += 1) {
        const choice = (e & f) ^ (~e & g);
        const majority = (a & b) ^ (a & c) ^ (b & c);
        const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
        const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
        const temporary1 = (h + sum1 + choice + SHA256_ROUND_CONSTANTS[index] + words[index]) >>> 0;
        const temporary2 = (sum0 + majority) >>> 0;
        h = g;
        g = f;
        f = e;
        e = (d + temporary1) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (temporary1 + temporary2) >>> 0;
      }
      state[0] = (state[0] + a) >>> 0;
      state[1] = (state[1] + b) >>> 0;
      state[2] = (state[2] + c) >>> 0;
      state[3] = (state[3] + d) >>> 0;
      state[4] = (state[4] + e) >>> 0;
      state[5] = (state[5] + f) >>> 0;
      state[6] = (state[6] + g) >>> 0;
      state[7] = (state[7] + h) >>> 0;
    }
    return state.map((word) => word.toString(16).padStart(8, "0")).join("");
  }

  function stableEntityToken(value) {
    // 128 bits of SHA-256 keep the token synchronous in a content script while
    // making accidental/adversarial plan-ID collisions impractical. Legacy
    // 32-bit pid tokens intentionally do not match after this upgrade: an old
    // authorization must be refreshed from a new scan instead of being reused.
    return `pid_${sha256Hex(value).slice(0, 32)}`;
  }

  function pseudonymizePlanIdentifier(value, header = "") {
    const text = compact(value, 20000);
    const planHeader = /计划|项目|广告组|单元/i.test(String(header || ""));
    if (!planHeader) return text;
    if (/id|编号/i.test(String(header || "")) && /^[A-Za-z0-9_-]{4,64}$/.test(text)) {
      return stableEntityToken(text);
    }
    return text.replace(
      /((?:(?:计划|项目|广告组|单元)\s*)?(?:ID|编号)\s*[:：#-]?\s*)([A-Za-z0-9_-]{4,64})/gi,
      (_, prefix, identifier) => `${prefix}${stableEntityToken(identifier)}`,
    );
  }

  function maskText(value) {
    const protectedPlanTokens = [];
    let text = compact(value, 20000).replace(/\bpid_[a-f0-9]{32}\b/gi, (token) => {
      const placeholder = `\uE000${protectedPlanTokens.length}\uE001`;
      protectedPlanTokens.push(token);
      return placeholder;
    });
    text = text
      .replace(/((?:订单|支付|交易|用户|买家|账号|账户|ID|id|编号|单号)[\s：:#-]*)(?!pid_)([A-Za-z0-9_-]{6,})/g, "$1[已隐藏]")
      .replace(/(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)/g, "$1****$2")
      .replace(/(?<![\dXx])(\d{6})\d{8}([\dXx]{4})(?![\dXx])/g, "$1********$2")
      .replace(/([\w.+-]{2})[\w.+-]*(@[\w.-]+\.[A-Za-z]{2,})/g, "$1***$2");
    return text.replace(/\uE000(\d+)\uE001/g, (_, index) => protectedPlanTokens[Number(index)] || "[已隐藏]");
  }

  function clean(value, privacyMode, max = 300) {
    const text = compact(value, max);
    return privacyMode ? maskText(text) : text;
  }

  function isSensitiveHeader(value) {
    return SENSITIVE_HEADER.test(compact(value, 100));
  }

  function entityHeaderKey(value) {
    return normalizedBusinessHeader(value);
  }

  function normalizedBusinessHeader(value) {
    return compact(value, 120)
      .normalize("NFKC")
      .toLowerCase()
      .replace(/(?:升序|降序|可排序|排序|筛选)/g, "")
      .replace(/[\s\-_/（）()：:·|.]/g, "");
  }

  function isPlanNameHeader(value) {
    const normalized = normalizedBusinessHeader(value);
    const entity = "(?:(?:商品|直播|广告|推广|投放|全域推广|标准推广|乘方)?计划|(?:广告|推广|投放)?项目|广告组|单元)";
    // A pure ID/number column is identity evidence, not a plan-name column.
    if (new RegExp(`^${entity}(?:id|编号)$`, "i").test(normalized)) return false;
    return new RegExp(`^${entity}(?:名称|信息|详情)?(?:及|和|与|含)?(?:id|编号)?$`, "i").test(normalized);
  }

  function entityKindForHeader(value) {
    const key = entityHeaderKey(value);
    if (LOCAL_ENTITY_HEADER_KINDS[key]) return LOCAL_ENTITY_HEADER_KINDS[key];
    if (/^(?:(?:商品|直播|广告|推广|投放|全域推广|标准推广|乘方)?计划|(?:广告|推广|投放)?项目|广告组|单元)(?:id|编号)$/i.test(key)) {
      return "qianchuan_plan_id";
    }
    return "";
  }

  function isLocalEntityIdentifier(value) {
    return /^[A-Za-z0-9_-]{4,80}$/.test(compact(value, 100));
  }

  function compositeEntityKindsForHeader(header) {
    const key = entityHeaderKey(header);
    const kinds = [];
    if (/商品信息|商品详情|商品名称|商品$/.test(key)) kinds.push("douyin_product_id", "merchant_product_code");
    if (/sku|规格/.test(key)) kinds.push("douyin_sku_id");
    if (/计划|项目|广告组|单元/.test(key)) kinds.push("qianchuan_plan_id");
    if (/素材|创意/.test(key)) kinds.push("qianchuan_material_id");
    if (/视频|内容/.test(key)) kinds.push("douyin_content_id", "qianchuan_material_id");
    if (/直播间|房间/.test(key)) kinds.push("douyin_live_room_id");
    if (/场次/.test(key)) kinds.push("douyin_live_session_id");
    return [...new Set(kinds)];
  }

  function extractLabeledEntityIdentifiers(value, header = "") {
    const text = compact(value, 20000);
    const result = {};
    for (const kind of compositeEntityKindsForHeader(header)) {
      const candidates = new Set();
      for (const pattern of LABELED_ENTITY_PATTERNS[kind] || []) {
        pattern.lastIndex = 0;
        for (const match of text.matchAll(pattern)) {
          if (isLocalEntityIdentifier(match[1])) candidates.add(compact(match[1], 100));
        }
      }
      // Some Douyin product lists use a bare "ID：..." line inside the
      // allowlisted 商品信息 cell. It is safe to treat this as a product ID
      // only in that column, never in orders or arbitrary page text.
      if (kind === "douyin_product_id" && candidates.size === 0 && /商品信息|商品详情/.test(entityHeaderKey(header))) {
        for (const bare of text.matchAll(/(?:^|\n)\s*ID\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})(?=\s|$)/gi)) {
          if (isLocalEntityIdentifier(bare[1])) candidates.add(compact(bare[1], 100));
        }
      }
      // Both the Chengfang table and legacy plan tables put a bare
      // `ID: ...` or `编号: ...` line inside the composite plan-name cell. There is no
      // dedicated ID column or useful data attribute on these rows, so this
      // scoped fallback is the stable identity evidence for those pages.
      if (kind === "qianchuan_plan_id" && candidates.size === 0 && /计划|项目|广告组|单元/.test(entityHeaderKey(header))) {
        for (const bare of text.matchAll(/(?:^|\n)\s*(?:ID|编号)\s*[:：#-]?\s*([A-Za-z0-9_-]{4,80})(?=\s|$)/gi)) {
          if (isLocalEntityIdentifier(bare[1])) candidates.add(compact(bare[1], 100));
        }
      }
      // Conflicting identifiers in one cell are deliberately unresolved.
      if (candidates.size === 1) result[kind] = [...candidates][0];
    }
    return result;
  }

  function extractDomEntityIdentifiers(cell, header = "") {
    if (!cell || typeof cell.querySelectorAll !== "function") return {};
    const allowedKinds = new Set(compositeEntityKindsForHeader(header));
    const candidates = {};
    const add = (kind, value) => {
      if (!allowedKinds.has(kind) || !isLocalEntityIdentifier(value)) return;
      (candidates[kind] ||= new Set()).add(compact(value, 100));
    };
    const elements = [cell, ...cell.querySelectorAll("[data-product-id], [data-goods-id], [data-item-id], [data-sku-id], [data-material-id], [data-plan-id], [data-ad-id], [data-campaign-id], [data-project-id], a[href]")].slice(0, 80);
    for (const element of elements) {
      add("douyin_product_id", element.getAttribute?.("data-product-id"));
      add("douyin_product_id", element.getAttribute?.("data-goods-id"));
      add("douyin_product_id", element.getAttribute?.("data-item-id"));
      add("douyin_sku_id", element.getAttribute?.("data-sku-id"));
      add("qianchuan_material_id", element.getAttribute?.("data-material-id"));
      add("qianchuan_plan_id", element.getAttribute?.("data-plan-id"));
      add("qianchuan_plan_id", element.getAttribute?.("data-ad-id"));
      add("qianchuan_plan_id", element.getAttribute?.("data-campaign-id"));
      add("qianchuan_plan_id", element.getAttribute?.("data-project-id"));
      const href = String(element.getAttribute?.("href") || "");
      for (const match of href.matchAll(/[?&](product_id|productId|goods_id|goodsId|item_id|itemId|sku_id|skuId|material_id|materialId|plan_id|planId|ad_id|adId|campaign_id|campaignId|project_id|projectId)=([A-Za-z0-9_-]{4,80})/g)) {
        const key = match[1].toLowerCase();
        const kind = key.includes("sku") ? "douyin_sku_id" : key.includes("material") ? "qianchuan_material_id" : /plan|campaign|project|^ad/.test(key) ? "qianchuan_plan_id" : "douyin_product_id";
        add(kind, match[2]);
      }
    }
    return Object.fromEntries(Object.entries(candidates).filter(([, values]) => values.size === 1).map(([kind, values]) => [kind, [...values][0]]));
  }

  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  }

  function tableCellText(cell) {
    return compact(
      cell?.innerText
      || cell?.getAttribute?.("aria-label")
      || cell?.getAttribute?.("data-column-title")
      || cell?.getAttribute?.("title")
      || "",
    );
  }

  function extractTables(privacyMode) {
    const output = [];
    const candidateSelector = [
      "table", "[role='table']", "[role='grid']", "[role='rowgroup']",
      "[class*='virtual-table']", "[class*='virtualTable']", "[class*='VirtualTable']",
    ].join(", ");
    const candidates = Array.from(document.querySelectorAll(candidateSelector)).filter((candidate) => {
      // Native/ARIA tables already include their rowgroups. Only accept a
      // standalone rowgroup as a split virtual-table root.
      if (candidate.getAttribute?.("role") !== "rowgroup") return true;
      const owner = candidate.closest?.("table, [role='table'], [role='grid']");
      return !owner;
    });
    let inspectedTables = 0;
    // Qianchuan virtualizes wide tables as two adjacent DOM tables: the first
    // owns the header/summary and the second owns the business rows. Preserve
    // the last compatible header contract so identities are extracted before
    // privacy masking, rather than trying to repair already-masked rows later.
    let pendingSplitHeaders = [];
    for (const table of candidates) {
      inspectedTables += 1;
      if (output.length >= MAX_TABLES || inspectedTables > 40) break;
      if (!visible(table)) continue;
      const rows = [];
      let inspectedRows = 0;
      const rowSelector = [
        "tr", "[role='row']", "[class*='table-row']", "[class*='tableRow']", "[class*='TableRow']",
        "[class*='virtual-row']", "[class*='virtualRow']", "[class*='VirtualRow']",
      ].join(", ");
      for (const row of table.querySelectorAll(rowSelector)) {
        inspectedRows += 1;
        if (rows.length >= MAX_ROWS || inspectedRows > 240) break;
        if (!visible(row)) continue;
        const cellElements = Array.from(row.querySelectorAll("th, td, [role='columnheader'], [role='cell'], [role='gridcell'], [data-column-key], [data-col-key]"))
          .slice(0, MAX_CELLS);
        // Preserve empty cells so later values remain aligned with headers.
        const cells = cellElements.map(tableCellText);
        const hasHeaderCells = row.querySelectorAll("th, [role='columnheader']").length > 0;
        if (cells.some(Boolean)) rows.push({ cells, cellElements, hasHeaderCells });
      }
      if (!rows.length) continue;

      const headerIndex = rows.findIndex((row) => row.hasHeaderCells);
      const nonHeaderRows = rows.filter((row, index) => index !== headerIndex);
      const isPlanSummaryRow = (row) => {
        const firstCell = row.cells.map((cell) => compact(cell)).find(Boolean) || "";
        return /^(?:共\s*\d+\s*条(?:投放)?计划|\d+\s*条(?:投放)?计划)$/i.test(firstCell);
      };
      let headers = headerIndex >= 0 ? rows[headerIndex].cells.map((cell) => clean(cell, privacyMode)) : [];
      if (!headers.length) {
        // Some virtual grids render the columnheader row as a sibling of the
        // scrolling rows. Reading headers from the table root keeps column
        // alignment without guessing from arbitrary page text.
        const detachedHeaders = Array.from(table.querySelectorAll("th, [role='columnheader']"))
          .filter(visible)
          .slice(0, MAX_CELLS)
          .map(tableCellText);
        if (detachedHeaders.some(Boolean) && rows.some((row) => row.cells.length === detachedHeaders.length)) {
          headers = detachedHeaders.map((cell) => clean(cell, privacyMode));
        }
      }
      if (headers.length) {
        const hasBusinessRows = nonHeaderRows.some((row) => {
          const cells = row.cells.map((cell) => compact(cell)).filter(Boolean);
          const text = cells.join(" ");
          // Qianchuan's fixed header table often appends aggregate metrics to
          // "共 N 条计划".  Classify by the leading summary cell instead of
          // requiring the whole row to equal that label; otherwise the split
          // body table loses its header contract and every plan ID disappears.
          const isEmptyState = /^(?:暂无(?:相关|符合条件的)?(?:投放)?计划|暂无数据)$/i.test(text);
          return text && !isPlanSummaryRow(row) && !isEmptyState;
        });
        // Keep headers only when this root is a header/summary fragment. A
        // complete table must not donate its schema to an unrelated list.
        pendingSplitHeaders = hasBusinessRows ? [] : headers.slice();
      } else if (pendingSplitHeaders.length && rows.some((row) => row.cells.length === pendingSplitHeaders.length)) {
        // Header inheritance is single-use. A later unrelated headerless table
        // with the same width must not inherit a stale Qianchuan schema.
        headers = pendingSplitHeaders.slice();
        pendingSplitHeaders = [];
      } else {
        pendingSplitHeaders = [];
      }
      // Aggregate rows are display-only evidence and must never enter identity
      // coverage as a fake plan without an ID.
      const preparedRows = nonHeaderRows.filter((row) => !isPlanSummaryRow(row)).map((row) => {
        const values = row.cells.slice();
        const claims = {};
        values.forEach((cell, index) => {
          const header = headers[index] || "";
          const explicitKind = entityKindForHeader(header);
          if (explicitKind && isLocalEntityIdentifier(cell)) claims[explicitKind] = compact(cell, 100);
          for (const [kind, value] of Object.entries(extractLabeledEntityIdentifiers(cell, header))) claims[kind] = value;
          for (const [kind, value] of Object.entries(extractDomEntityIdentifiers(row.cellElements[index], header))) {
            if (!claims[kind] || claims[kind] === value) claims[kind] = value;
            else delete claims[kind];
          }
        });
        return { values, claims };
      });
      const existingKinds = new Set(headers.map(entityKindForHeader).filter(Boolean));
      const syntheticKinds = Object.keys(SYNTHETIC_ENTITY_HEADERS).filter((kind) =>
        !existingKinds.has(kind) && preparedRows.some((row) => row.claims[kind]),
      );
      for (const kind of syntheticKinds) headers.push(SYNTHETIC_ENTITY_HEADERS[kind]);
      for (const row of preparedRows) {
        for (const kind of syntheticKinds) row.values.push(row.claims[kind] || "");
      }
      const sensitiveColumns = new Set();
      if (privacyMode) {
        headers.forEach((header, index) => {
          if (isSensitiveHeader(header)) sensitiveColumns.add(index);
        });
      }
      const dataRows = preparedRows.map((row) =>
        row.values.map((cell, index) => {
          if (sensitiveColumns.has(index)) return "[已隐藏]";
          const entityKind = entityKindForHeader(headers[index] || "");
          // Exact commerce IDs travel only in the ephemeral localhost request.
          // The bridge replaces them with an installation-local HMAC before any
          // JSON/SQLite write. Plan IDs retain the legacy browser token because
          // supervised execution still consumes the operational plan column.
          if (privacyMode && entityKind && entityKind !== "qianchuan_plan_id" && isLocalEntityIdentifier(cell)) {
            return compact(cell, 100);
          }
          const safeCell = privacyMode ? pseudonymizePlanIdentifier(cell, headers[index] || "") : cell;
          return clean(safeCell, privacyMode);
        }),
      );
      output.push({ headers, rows: dataRows });
    }
    return output;
  }

  function mergeTables(target, incoming) {
    incoming.forEach((table, index) => {
      const key = table.headers.length ? `h:${table.headers.join("|")}` : `i:${index}`;
      let existing = target.find((item) => item.__key === key);
      if (!existing) {
        existing = { __key: key, headers: table.headers, rows: [] };
        target.push(existing);
      }
      const known = new Set(existing.rows.map((row) => JSON.stringify(row)));
      table.rows.forEach((row) => {
        const signature = JSON.stringify(row);
        if (!known.has(signature) && existing.rows.length < 500) {
          existing.rows.push(row);
          known.add(signature);
        }
      });
    });
  }

  function commerceIdentityCoverage(tables) {
    const byKind = {};
    let eligibleRows = 0;
    let identifiedRows = 0;
    for (const table of tables || []) {
      const headers = Array.isArray(table.headers) ? table.headers : [];
      const entityColumns = headers.map((header, index) => ({ index, kind: entityKindForHeader(header) })).filter((item) => item.kind);
      if (!entityColumns.length) continue;
      for (const row of table.rows || []) {
        eligibleRows += 1;
        let identified = false;
        for (const { index, kind } of entityColumns) {
          if (!isLocalEntityIdentifier(row[index])) continue;
          identified = true;
          byKind[kind] = (byKind[kind] || 0) + 1;
        }
        if (identified) identifiedRows += 1;
      }
    }
    return {
      eligible_rows: eligibleRows,
      identified_rows: identifiedRows,
      coverage_rate: eligibleRows ? Math.round(identifiedRows / eligibleRows * 100) : 0,
      by_kind: byKind,
    };
  }

  function planIdentityCoverage(tables) {
    let eligibleRows = 0;
    let identifiedRows = 0;
    for (const table of tables || []) {
      const headers = Array.isArray(table.headers) ? table.headers : [];
      const planNameIndex = headers.findIndex(isPlanNameHeader);
      if (planNameIndex < 0) continue;
      const planIdIndex = headers.findIndex((header) => entityKindForHeader(header) === "qianchuan_plan_id");
      for (const row of table.rows || []) {
        const rowText = (row || []).map((value) => compact(value)).filter(Boolean).join("").replace(/\s+/g, "");
        if (/^(?:暂无(?:相关|符合条件的)?(?:投放)?计划|没有(?:相关|符合条件的)?(?:投放)?计划|无(?:相关|符合条件的)?(?:投放)?计划|暂无数据|无数据|暂无内容|共\s*0\s*条计划|0\s*条计划)$/i.test(rowText)) continue;
        const planName = compact(row?.[planNameIndex], 300);
        if (!planName || /^共\s*\d+\s*条计划/.test(planName)) continue;
        eligibleRows += 1;
        if (planIdIndex >= 0 && isLocalEntityIdentifier(row?.[planIdIndex])) identifiedRows += 1;
      }
    }
    return {
      eligible_rows: eligibleRows,
      identified_rows: identifiedRows,
      coverage_rate: eligibleRows ? Math.round(identifiedRows / eligibleRows * 100) : 0,
    };
  }

  function tableSchemaEvidence(inputTables) {
    const tables = Array.isArray(inputTables) ? inputTables : extractTables(true);
    const candidates = [];
    for (const table of tables || []) {
      const headers = (table.headers || []).map((header) => compact(header, 120)).filter(Boolean);
      if (!headers.length) continue;
      const normalized = headers.map(normalizedBusinessHeader);
      const hasPlanName = headers.some(isPlanNameHeader);
      const hasDeliveryMetric = normalized.some((header) => /投放状态|计划状态|消耗|预算|支付roi|成交roi|综合营销roi|成交金额|订单成本/.test(header));
      const materialSignals = normalized.filter((header) => /^(?:素材|创意|视频)(?:名称|信息|类型|id)?$|审核状态|创作者声明|素材建议|素材评估|视频追投/.test(header)).length;
      const liveSignals = normalized.filter((header) => /抖音号|直播间|直播场次|开播时间|主播|直播计划/.test(header)).length;
      const productSignals = normalized.filter((header) => /商品信息|商品名称|商品id|商品卡|推广商品/.test(header)).length;
      if (materialSignals >= 3 || (materialSignals >= 2 && !hasPlanName)) {
        candidates.push({ page_type: "materials", headers, confidence: "high", source: "table_schema" });
      } else if (hasPlanName && liveSignals >= 1 && hasDeliveryMetric) {
        candidates.push({ page_type: "qianchuan_live", headers, confidence: "high", source: "table_schema" });
      } else if (hasPlanName && hasDeliveryMetric && liveSignals === 0) {
        candidates.push({ page_type: "campaigns", headers, confidence: productSignals ? "high" : "medium", source: "table_schema" });
      }
    }
    const highTypes = [...new Set(candidates.filter((item) => item.confidence === "high").map((item) => item.page_type))];
    if (highTypes.length === 1) return candidates.find((item) => item.page_type === highTypes[0] && item.confidence === "high");
    if (highTypes.length > 1) return { page_type: "unknown", confidence: "conflict", source: "conflicting_table_schemas", headers: [] };
    const mediumTypes = [...new Set(candidates.map((item) => item.page_type))];
    if (mediumTypes.length === 1) return candidates[0];
    return {
      page_type: "unknown",
      confidence: mediumTypes.length > 1 ? "conflict" : "none",
      source: mediumTypes.length > 1 ? "conflicting_table_schemas" : "unverified",
      headers: [],
    };
  }

  function nextPageButton() {
    const candidates = document.querySelectorAll("button[aria-label*='下一页'], [class*='pagination-next'] button, li[class*='pagination-next'], button[title*='下一页']");
    return Array.from(candidates).find((button) => visible(button)
      && !button.disabled
      && button.getAttribute("aria-disabled") !== "true"
      && !/disabled/i.test(button.className || ""));
  }

  function tableHarvestSignature(tables = []) {
    return JSON.stringify((tables || []).map((table) => {
      const rows = Array.isArray(table.rows) ? table.rows : [];
      return [table.headers || [], rows.length, rows[0] || [], rows[rows.length - 1] || []];
    }));
  }

  function tableRowCount(tables = []) {
    return (tables || []).reduce((sum, table) => sum + (Array.isArray(table.rows) ? table.rows.length : 0), 0);
  }

  function tablesContainPlanId(tables = [], targetPlanId = "") {
    const target = compact(targetPlanId, 128).toLowerCase();
    if (!target) return false;
    return (tables || []).some((table) => {
      const planIdIndex = (table.headers || []).findIndex(
        (header) => entityKindForHeader(header) === "qianchuan_plan_id",
      );
      return planIdIndex >= 0 && (table.rows || []).some(
        (row) => compact(row?.[planIdIndex], 128).toLowerCase() === target,
      );
    });
  }

  async function harvestTables(privacyMode, includePagination, options = {}) {
    const assertContextStable = () => {
      if (typeof options.assertContextStable !== "function") return;
      try {
        options.assertContextStable();
      } catch (error) {
        if (!error.code) error.code = "COLLECTION_CONTEXT_CHANGED";
        throw error;
      }
    };
    assertContextStable();
    const targetPlanId = compact(options.targetPlanId, 128);
    if (!includePagination) {
      const tables = extractTables(privacyMode);
      assertContextStable();
      return {
        tables, pages: 1, virtualPasses: 0,
        truncated: false, paginationStalled: false, virtualTruncated: false,
        targetPlanFound: tablesContainPlanId(tables, targetPlanId),
      };
    }
    const merged = [];
    let virtualPasses = 0;
    let pages = 1;
    let truncated = false;
    let paginationStalled = false;
    let virtualTruncated = false;
    const harvestCurrentPage = async () => {
      assertContextStable();
      const initialTables = extractTables(privacyMode);
      assertContextStable();
      mergeTables(merged, initialTables);
      if (targetPlanId && tablesContainPlanId(initialTables, targetPlanId)) return true;
      const scrollables = Array.from(document.querySelectorAll("[role='grid'], [class*='virtual'], [class*='scroll'], [class*='table'], main, section"))
        .slice(0, 500)
        .filter((element) => visible(element) && element.clientHeight >= 120 && element.scrollHeight > element.clientHeight * 1.5)
        .sort((a, b) => b.scrollHeight - a.scrollHeight)
        .slice(0, 3);
      for (const container of scrollables) {
        const original = container.scrollTop;
        let stablePasses = 0;
        let previousRows = tableRowCount(merged);
        let previousHeight = Number(container.scrollHeight || 0);
        let reachedBottom = false;
        for (let step = 1; step <= 12; step += 1) {
          const maximum = Math.max(0, Number(container.scrollHeight || 0) - Number(container.clientHeight || 0));
          if (maximum <= 0) {
            reachedBottom = true;
            break;
          }
          const stride = Math.max(Number(container.clientHeight || 0) * 0.8, maximum / 8, 120);
          const nextTop = Math.min(maximum, Math.max(Number(container.scrollTop || 0) + stride, maximum * step / 8));
          assertContextStable();
          container.scrollTop = Math.round(nextTop);
          assertContextStable();
          await new Promise((resolve) => setTimeout(resolve, 220));
          assertContextStable();
          const nextTables = extractTables(privacyMode);
          assertContextStable();
          mergeTables(merged, nextTables);
          if (targetPlanId && tablesContainPlanId(nextTables, targetPlanId)) return true;
          virtualPasses += 1;
          const currentRows = tableRowCount(merged);
          const currentHeight = Number(container.scrollHeight || 0);
          stablePasses = currentRows === previousRows && currentHeight === previousHeight ? stablePasses + 1 : 0;
          previousRows = currentRows;
          previousHeight = currentHeight;
          reachedBottom = Number(container.scrollTop || 0) + Number(container.clientHeight || 0) >= currentHeight - 4;
          if (reachedBottom && stablePasses >= 1) break;
        }
        if (!reachedBottom) virtualTruncated = true;
        assertContextStable();
        container.scrollTop = original;
        assertContextStable();
      }
      return false;
    };
    let targetPlanFound = await harvestCurrentPage();
    if (includePagination && !targetPlanFound) {
      for (let page = 2; page <= 5; page += 1) {
        assertContextStable();
        const next = nextPageButton();
        if (!next) break;
        const before = tableHarvestSignature(extractTables(privacyMode));
        assertContextStable();
        next.click();
        assertContextStable();
        let changed = false;
        for (let wait = 0; wait < 12; wait += 1) {
          await new Promise((resolve) => setTimeout(resolve, 300));
          assertContextStable();
          if (tableHarvestSignature(extractTables(privacyMode)) !== before) {
            changed = true;
            break;
          }
        }
        if (!changed) {
          paginationStalled = true;
          break;
        }
        pages = page;
        targetPlanFound = await harvestCurrentPage();
        assertContextStable();
        if (targetPlanFound) break;
      }
      truncated = !targetPlanFound && Boolean(nextPageButton());
    }
    return {
      tables: merged.map(({ __key, ...table }) => table),
      pages,
      virtualPasses,
      truncated,
      paginationStalled,
      virtualTruncated,
      targetPlanFound,
    };
  }

  function extractMetrics(privacyMode) {
    const metrics = {};
    const selectors = [
      "[class*='metric']", "[class*='card']", "[class*='stat']",
      "[class*='overview']", "[class*='data-item']", "[class*='summary']",
      "[class*='indicator']", "[class*='index']",
    ];
    let inspected = 0;
    for (const element of document.querySelectorAll(selectors.join(","))) {
      inspected += 1;
      if (Object.keys(metrics).length >= 80 || inspected > 1200) break;
      if (!visible(element)) continue;
      const label = element.querySelector("[class*='label'], [class*='title'], [class*='name'], [class*='desc']");
      const value = element.querySelector("[class*='value'], [class*='number'], [class*='num'], [class*='amount']");
      if (!label || !value) continue;
      const key = clean(label.innerText, privacyMode).slice(0, 40);
      const metricValue = isSensitiveHeader(key) && privacyMode
        ? "[已隐藏]"
        : clean(value.innerText, privacyMode).slice(0, 80);
      if (key && metricValue && key !== metricValue) metrics[key] = metricValue;
    }
    return metrics;
  }

  function extractKnownMetrics(bodyText, privacyMode) {
    const lines = String(bodyText || "").split(/\n+/).map((line) => compact(line, 120)).filter(Boolean);
    const metrics = {};
    const labelSet = new Set(SAFE_METRIC_LABELS);
    for (let index = 0; index < lines.length; index += 1) {
      const label = lines[index];
      if (!labelSet.has(label) || Object.hasOwn(metrics, label)) continue;
      let currency = "";
      for (let offset = 1; offset <= 6 && index + offset < lines.length; offset += 1) {
        const candidate = lines[index + offset];
        if (labelSet.has(candidate) || /^(较上周期|基础|占比本店)$/.test(candidate)) break;
        if (candidate === "¥" || candidate === "￥") {
          currency = "¥";
          continue;
        }
        if (candidate === "-") {
          metrics[label] = "-";
          break;
        }
        if (/^-?\d[\d,]*(?:\.\d+)?%?$/.test(candidate)) {
          let value = candidate;
          if (index + offset + 2 < lines.length && lines[index + offset + 1] === "." && /^\d{1,2}$/.test(lines[index + offset + 2])) {
            value = `${candidate}.${lines[index + offset + 2]}`;
          }
          metrics[label] = clean(`${currency}${value}`, privacyMode, 80);
          break;
        }
      }
    }
    return metrics;
  }

  function extractKnownSignals(bodyText) {
    const lines = String(bodyText || "").split(/\n+/).map((line) => compact(line, 180)).filter(Boolean);
    const signals = [];
    for (const line of lines) {
      if (SAFE_SIGNAL_PATTERNS.some((pattern) => pattern.test(line)) && !signals.includes(line)) signals.push(line);
      if (signals.length >= 20) break;
    }
    return signals;
  }

  function qualityScore(tables, metrics, pageText, pageType) {
    let score = 0;
    if (pageType && pageType !== "unknown") score += 25;
    if (Object.keys(metrics).length) score += 30;
    if (tables.some((table) => table.rows.length)) score += 30;
    if (pageText.length > 300) score += 15;
    return Math.min(100, score);
  }

  async function collect(source, pageType, privacyMode = true, reason = "auto", options = {}) {
    const scanHarvest = String(reason).startsWith("full-scan-list-");
    const targetPlanId = compact(options.targetPlanId, 128);
    const targetedCollection = Boolean(targetPlanId);
    const harvested = await harvestTables(privacyMode, scanHarvest, options);
    const tables = harvested.tables;
    const identityCoverage = commerceIdentityCoverage(tables);
    const planCoverage = planIdentityCoverage(tables);
    const metrics = extractMetrics(privacyMode);
    const rawVisibleText = document.body?.innerText || "";
    const visibleText = clean(rawVisibleText, privacyMode, MAX_TEXT);
    const safeMetrics = extractKnownMetrics(rawVisibleText, privacyMode);
    const signals = extractKnownSignals(rawVisibleText);
    // Page-wide text is intentionally omitted in privacy mode because names and
    // addresses can appear outside structured fields. Metrics and safe columns
    // remain available for diagnostics.
    const bodyText = privacyMode ? "" : visibleText;
    const loginRequired = /(登录|扫码登录|验证码)/.test(visibleText.slice(0, 1000))
      && !/(退出登录|店铺|投放管理)/.test(visibleText.slice(0, 1500));
    let score = qualityScore(tables, metrics, visibleText, pageType);
    const warnings = [];
    if (loginRequired) warnings.push("页面可能未登录");
    if (pageType === "unknown") warnings.push("暂未识别该页面类型");
    if (!tables.length && !Object.keys(metrics).length) warnings.push("未发现结构化指标或表格");
    if (harvested.truncated) warnings.push("分页超过 5 页，本轮仅采集前 5 页");
    if (harvested.paginationStalled) warnings.push("翻页后内容没有更新，本轮未继续读取后续页");
    if (harvested.virtualTruncated) warnings.push("虚拟列表在安全次数内未到达末尾，本轮数据可能不完整");
    const productKinds = (identityCoverage.by_kind.douyin_product_id || 0)
      + (identityCoverage.by_kind.douyin_sku_id || 0)
      + (identityCoverage.by_kind.merchant_product_code || 0);
    if (["products", "inventory"].includes(pageType) && identityCoverage.eligible_rows > 0 && productKinds === 0) {
      warnings.push("商品表已读取，但没有稳定商品 ID、SKU ID 或商家编码；单品经营链暂不可用");
      score = Math.min(score, 59);
    }
    if (["campaigns", "qianchuan_campaigns", "qianchuan_live"].includes(pageType)
      && planCoverage.eligible_rows > 0
      && planCoverage.identified_rows < planCoverage.eligible_rows) {
      warnings.push("当前表格缺少计划 ID，不能作为千川计划证据");
      score = Math.min(score, 59);
    }

    return {
      schema_version: 3,
      source,
      page_type: pageType || "unknown",
      url: `${location.origin}${location.pathname}`,
      title: privacyMode ? `${source}/${pageType || "unknown"}` : clean(document.title, false).slice(0, 120),
      captured_at: Date.now(),
      reason,
      privacy: {
        masked: Boolean(privacyMode),
        raw_dom_sent: false,
        page_text_included: !privacyMode,
        commerce_identity_contract: privacyMode ? "bridge_hmac_v1" : "plain_local_v1",
        raw_entity_ids_written_by_extension: false,
      },
      quality: {
        score,
        metric_count: Object.keys(metrics).length,
        table_count: tables.length,
        row_count: tables.reduce((sum, table) => sum + table.rows.length, 0),
        warnings,
        pages_scanned: harvested.pages,
        virtual_scroll_passes: harvested.virtualPasses,
        pagination_truncated: harvested.truncated,
        pagination_stalled: harvested.paginationStalled,
        virtual_scroll_truncated: harvested.virtualTruncated,
        // A target lookup deliberately stops as soon as the exact plan is
        // found. It is evidence for one execution only, never a replacement
        // for the account's canonical full plan list.
        targeted: targetedCollection,
        scope: targetedCollection ? "target_plan" : "full_page",
        coverage_complete: !targetedCollection && !harvested.truncated && !harvested.paginationStalled && !harvested.virtualTruncated,
        collection_complete: !targetedCollection && !harvested.truncated && !harvested.paginationStalled && !harvested.virtualTruncated,
        target_plan_id: targetPlanId,
        target_plan_found: harvested.targetPlanFound === true,
        login_required: loginRequired,
        commerce_identity_coverage: identityCoverage,
        plan_identity_coverage: planCoverage,
      },
      metrics,
      safe_metrics: safeMetrics,
      signals,
      tables,
      page_text: bodyText,
    };
  }

  globalThis.DianAgentExtractor = {
    collect,
    compact,
    maskText,
    pseudonymizePlanIdentifier,
    isSensitiveHeader,
    entityKindForHeader,
    isPlanNameHeader,
    isLocalEntityIdentifier,
    extractLabeledEntityIdentifiers,
    extractTables,
    commerceIdentityCoverage,
    planIdentityCoverage,
    tableSchemaEvidence,
    tableHarvestSignature,
    extractKnownMetrics,
    extractKnownSignals,
  };
})();
