/** Shared page extractor. Raw DOM and browser credentials never leave the page. */
(function () {
  "use strict";

  const MAX_TABLES = 8;
  const MAX_ROWS = 100;
  const MAX_CELLS = 24;
  const MAX_TEXT = 6000;
  const SENSITIVE_HEADER = /收货|收件|联系人|客户姓名|真实姓名|姓名|详细地址|收货地址|配送地址|联系地址|手机|电话|联系方式|订单号|订单编号|交易号|支付单号|买家账号|买家昵称|用户账号|用户ID|用户编号|身份证|证件号|邮箱/i;
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
  ];

  function compact(value, max = 300) {
    return String(value || "")
      .replace(/\u200b/g, "")
      .replace(/[\t\r ]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, max);
  }

  function maskText(value) {
    return compact(value, 20000)
      .replace(/((?:订单|支付|交易|用户|买家|账号|账户|ID|id|编号|单号)[\s：:#-]*)([A-Za-z0-9_-]{6,})/g, "$1[已隐藏]")
      .replace(/(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)/g, "$1****$2")
      .replace(/(?<![\dXx])(\d{6})\d{8}([\dXx]{4})(?![\dXx])/g, "$1********$2")
      .replace(/([\w.+-]{2})[\w.+-]*(@[\w.-]+\.[A-Za-z]{2,})/g, "$1***$2");
  }

  function clean(value, privacyMode, max = 300) {
    const text = compact(value, max);
    return privacyMode ? maskText(text) : text;
  }

  function isSensitiveHeader(value) {
    return SENSITIVE_HEADER.test(compact(value, 100));
  }

  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  }

  function extractTables(privacyMode) {
    const output = [];
    const candidates = document.querySelectorAll("table, [role='table'], [role='grid']");
    let inspectedTables = 0;
    for (const table of candidates) {
      inspectedTables += 1;
      if (output.length >= MAX_TABLES || inspectedTables > 40) break;
      if (!visible(table)) continue;
      const rows = [];
      let inspectedRows = 0;
      for (const row of table.querySelectorAll("tr, [role='row']")) {
        inspectedRows += 1;
        if (rows.length >= MAX_ROWS || inspectedRows > 240) break;
        if (!visible(row)) continue;
        const cells = Array.from(row.querySelectorAll("th, td, [role='columnheader'], [role='cell'], [role='gridcell']"))
          .slice(0, MAX_CELLS)
          .map((cell) => compact(cell.innerText))
          .filter(Boolean);
        const hasHeaderCells = row.querySelectorAll("th, [role='columnheader']").length > 0;
        if (cells.length) rows.push({ cells, hasHeaderCells });
      }
      if (!rows.length) continue;

      const headerIndex = rows.findIndex((row) => row.hasHeaderCells);
      const headers = headerIndex >= 0 ? rows[headerIndex].cells.map((cell) => clean(cell, privacyMode)) : [];
      const sensitiveColumns = new Set();
      if (privacyMode) {
        headers.forEach((header, index) => {
          if (isSensitiveHeader(header)) sensitiveColumns.add(index);
        });
      }
      const dataRows = rows.filter((row, index) => index !== headerIndex).map((row) =>
        row.cells.map((cell, index) => sensitiveColumns.has(index) ? "[已隐藏]" : clean(cell, privacyMode)),
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

  function nextPageButton() {
    const candidates = document.querySelectorAll("button[aria-label*='下一页'], [class*='pagination-next'] button, li[class*='pagination-next'], button[title*='下一页']");
    return Array.from(candidates).find((button) => visible(button)
      && !button.disabled
      && button.getAttribute("aria-disabled") !== "true"
      && !/disabled/i.test(button.className || ""));
  }

  async function harvestTables(privacyMode, includePagination) {
    if (!includePagination) return { tables: extractTables(privacyMode), pages: 1, virtualPasses: 0, truncated: false };
    const merged = [];
    let virtualPasses = 0;
    let pages = 1;
    let truncated = false;
    const harvestCurrentPage = async () => {
      mergeTables(merged, extractTables(privacyMode));
      const scrollables = Array.from(document.querySelectorAll("[role='grid'], [class*='virtual'], [class*='scroll'], [class*='table'], main, section"))
        .slice(0, 500)
        .filter((element) => visible(element) && element.clientHeight >= 120 && element.scrollHeight > element.clientHeight * 1.5)
        .sort((a, b) => b.scrollHeight - a.scrollHeight)
        .slice(0, 3);
      for (const container of scrollables) {
        const original = container.scrollTop;
        const maximum = container.scrollHeight - container.clientHeight;
        for (let step = 1; step <= 6; step += 1) {
          container.scrollTop = Math.round(maximum * step / 6);
          await new Promise((resolve) => setTimeout(resolve, 180));
          mergeTables(merged, extractTables(privacyMode));
          virtualPasses += 1;
        }
        container.scrollTop = original;
      }
    };
    await harvestCurrentPage();
    if (includePagination) {
      for (let page = 2; page <= 5; page += 1) {
        const next = nextPageButton();
        if (!next) break;
        next.click();
        await new Promise((resolve) => setTimeout(resolve, 1100));
        pages = page;
        await harvestCurrentPage();
      }
      truncated = Boolean(nextPageButton());
    }
    return { tables: merged.map(({ __key, ...table }) => table), pages, virtualPasses, truncated };
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

  async function collect(source, pageType, privacyMode = true, reason = "auto") {
    const scanHarvest = String(reason).startsWith("full-scan-list-");
    const harvested = await harvestTables(privacyMode, scanHarvest);
    const tables = harvested.tables;
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
    const score = qualityScore(tables, metrics, visibleText, pageType);
    const warnings = [];
    if (loginRequired) warnings.push("页面可能未登录");
    if (pageType === "unknown") warnings.push("暂未识别该页面类型");
    if (!tables.length && !Object.keys(metrics).length) warnings.push("未发现结构化指标或表格");
    if (harvested.truncated) warnings.push("分页超过 5 页，本轮仅采集前 5 页");

    return {
      schema_version: 2,
      source,
      page_type: pageType || "unknown",
      url: `${location.origin}${location.pathname}`,
      title: clean(document.title, true).slice(0, 120),
      captured_at: Date.now(),
      reason,
      privacy: {
        masked: Boolean(privacyMode),
        raw_dom_sent: false,
        page_text_included: !privacyMode,
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
      },
      metrics,
      safe_metrics: safeMetrics,
      signals,
      tables,
      page_text: bodyText,
    };
  }

  globalThis.DianAgentExtractor = { collect, compact, maskText, isSensitiveHeader, extractKnownMetrics, extractKnownSignals };
})();
