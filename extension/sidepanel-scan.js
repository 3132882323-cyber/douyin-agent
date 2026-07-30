/** 工作台巡查进度与数据体检单渲染（依赖 sidepanel.js 中的 LABELS / empty / 账号与 dashboard）。 */

const SCAN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
let scanPoller = null;
let scanStartTime = 0;

function scanReceiptFromStatus(scan = {}) {
  const results = (scan.results || []).filter((item) => item && typeof item === "object").map((item) => {
    const score = Math.max(0, Math.min(100, Number(item.quality?.score || 0)));
    const paginationTruncated = Boolean(item.quality?.pagination_truncated);
    return {
      ...item,
      source: item.source || (String(item.id || "").startsWith("qianchuan") ? "qianchuan" : "doudian"),
      quality_score: score,
      metric_count: Number(item.quality?.metric_count || 0),
      row_count: Number(item.quality?.row_count || 0),
      pagination_truncated: paginationTruncated,
      needs_review: Boolean(item.ok) && (score < 70 || paginationTruncated),
    };
  });
  const total = Math.max(Number(scan.total || 0), results.length);
  const success = results.filter((item) => item.ok).length;
  const failed = results.filter((item) => !item.ok).length;
  const needsReview = results.filter((item) => item.needs_review).length;
  const truncated = results.filter((item) => item.pagination_truncated).length;
  const coverageRate = total ? Math.round(results.length / total * 100) : 0;
  const running = scan.status === "running";
  const ready = scan.status === "completed" && coverageRate === 100 && failed === 0 && needsReview === 0;
  return {
    scan_status: scan.status || "idle",
    readiness: running ? "running" : ready ? "ready" : results.length ? "attention" : "empty",
    readiness_label: running ? "正在采集" : ready ? "数据可用于分析" : results.length ? "需要补采或复核" : "等待巡查",
    account_label: results.find((item) => item.account_label)?.account_label || "",
    finished_at: Number(scan.finished_at || 0),
    summary: {
      total,
      completed: results.length,
      success,
      failed,
      needs_review: needsReview,
      coverage_rate: coverageRate,
      row_count: results.reduce((sum, item) => sum + item.row_count, 0),
    },
    warnings: [
      failed ? `${failed} 个页面读取失败，可在下方单独重试。` : "",
      needsReview ? `${needsReview} 个页面质量不足或列表被截断，相关建议需先补采。` : "",
      truncated ? `${truncated} 个页面列表超过采集页数上限，止损/预算方案已锁定。` : "",
      total && results.length < total && !running ? `巡查仅覆盖 ${results.length}/${total} 个页面。` : "",
    ].filter(Boolean),
    results,
  };
}

function renderScanReceipt(receipt = {}) {
  const state = document.getElementById("scan-receipt-state");
  state.className = receipt.readiness || "";

  const summary = receipt.summary || {};
  const results = [...(receipt.results || [])].sort((a, b) => Number(a.ok) - Number(b.ok) || Number(b.needs_review) - Number(a.needs_review));
  const issues = results.filter((item) => !item.ok || item.needs_review);
  const passed = results.filter((item) => item.ok && !item.needs_review);
  state.textContent = receipt.readiness === "running"
    ? receipt.readiness_label || "巡查中"
    : results.length
      ? issues.length ? `${issues.length} 项需处理` : `${passed.length} 项通过`
      : receipt.readiness_label || "等待巡查";
  const metrics = [
    ["覆盖率", `${summary.coverage_rate || 0}%`],
    ["成功页面", `${summary.success || 0}/${summary.total || 0}`],
    ["需复核", summary.needs_review || 0],
    ["读取行数", summary.row_count || 0],
  ];
  const summaryContainer = document.getElementById("scan-receipt-summary");
  summaryContainer.replaceChildren(...metrics.map(([label, value]) => {
    const cell = document.createElement("div");
    const strong = document.createElement("strong"); strong.textContent = String(value);
    const small = document.createElement("small"); small.textContent = label;
    cell.append(strong, small);
    return cell;
  }));

  const warning = document.getElementById("scan-receipt-warning");
  const account = receipt.account_label ? `千川账号：${receipt.account_label}。` : "";
  const finished = receipt.finished_at ? `完成于 ${new Date(receipt.finished_at).toLocaleString()}。` : "";
  warning.textContent = receipt.warnings?.length
    ? `${account}${receipt.warnings.join(" ")}`
    : receipt.readiness === "ready"
      ? `${account}${finished}页面覆盖和质量检查均通过。`
      : receipt.readiness === "running"
        ? "正在生成体检单，巡查完成前不要依据不完整数据调整投放。"
        : "巡查完成后会显示页面覆盖率、数据质量和失败原因。";
  warning.className = `receipt-warning${receipt.readiness === "ready" ? " ready" : ""}`;

  const container = document.getElementById("scan-receipt-pages");
  if (!results.length) return empty(container, "尚未生成数据体检单");
  container.className = "receipt-pages";
  const pageRow = (item) => {
    const cardRow = document.createElement("article");
    cardRow.className = `receipt-page${!item.ok ? " failed" : item.needs_review ? " review" : ""}`;
    const title = document.createElement("strong"); title.textContent = item.label || item.id || "未命名页面";
    const tag = document.createElement("span"); tag.className = "receipt-status";
    tag.textContent = !item.ok ? "失败" : item.pagination_truncated ? "需补采" : item.needs_review ? "需复核" : "通过";
    const detail = document.createElement("small");
    detail.textContent = !item.ok
      ? item.error || "页面读取失败"
      : item.pagination_truncated
        ? `${LABELS[item.source] || item.source} · 列表截断 · 质量 ${item.quality_score || 0} · ${item.row_count || 0} 行`
        : `${LABELS[item.source] || item.source} · 质量 ${item.quality_score || 0} · ${item.row_count || 0} 行 · ${item.metric_count || 0} 项指标`;
    cardRow.append(title, tag, detail);
    if ((!item.ok || item.needs_review) && item.id) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = item.ok ? "补采" : "只重试这一页";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        retry.textContent = item.ok ? "正在补采…" : "正在重试…";
        const response = await chrome.runtime.sendMessage({
          type: "start-full-scan",
          page_ids: [item.id],
          account_key: selectedQianchuanAccount,
        });
        if (!response?.ok) {
          retry.disabled = false;
          retry.textContent = response?.error || (item.ok ? "补采失败" : "重试失败");
          return;
        }
        await loadDashboard();
      });
      cardRow.append(retry);
    }
    return cardRow;
  };
  const rows = issues.map(pageRow);
  if (!issues.length) {
    const success = document.createElement("p");
    success.className = "receipt-success-note";
    success.textContent = `${passed.length} 个页面均已通过，本轮数据可以用于经营判断。`;
    rows.push(success);
  }
  if (passed.length) {
    const fold = document.createElement("details");
    fold.className = "passed-pages-fold";
    const foldSummary = document.createElement("summary");
    foldSummary.textContent = `查看 ${passed.length} 个已通过页面`;
    const passedList = document.createElement("div");
    passedList.className = "passed-pages-list";
    passedList.replaceChildren(...passed.map(pageRow));
    fold.append(foldSummary, passedList);
    rows.push(fold);
  }
  container.replaceChildren(...rows);
}

function renderFullScan(scan = {}) {
  const running = scan.status === "running";
  if (running && !scanStartTime) scanStartTime = Date.now();
  if (!running) scanStartTime = 0;
  if (running && scanStartTime && (Date.now() - scanStartTime > SCAN_TIMEOUT_MS)) {
    chrome.runtime.sendMessage({ type: "cancel-full-scan" }).catch(() => undefined);
    document.getElementById("scan-detail").textContent = `巡检超过 ${SCAN_TIMEOUT_MS / 60000} 分钟已自动停止，请检查页面状态后重试`;
    return;
  }
  const state = document.getElementById("scan-state");
  const labels = { idle: "未运行", running: "巡检中", completed: "已完成", partial: "部分完成", cancelled: "已停止", interrupted: "已中断", error: "失败" };
  state.textContent = labels[scan.status] || "未运行";
  state.className = `scan-tag ${running || scan.status === "completed" ? "ok" : ["partial", "interrupted"].includes(scan.status) ? "warn" : scan.status === "error" ? "error" : "idle"}`;
  const total = Number(scan.total || 18);
  const index = Number(scan.index || 0);
  document.getElementById("scan-progress-bar").style.width = `${Math.min(100, total ? index / total * 100 : 0)}%`;
  document.getElementById("scan-detail").textContent = running ? `正在采集：${scan.current || "准备中"}（${index}/${total}）` : scan.finished_at ? `上次完成：成功 ${scan.success || 0}，失败 ${scan.failed || 0}` : "按清单自动打开页面并采集，不需要 API";
  const rows = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.row_count || 0), 0);
  const virtualPasses = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.virtual_scroll_passes || 0), 0);
  document.getElementById("scan-summary").textContent = scan.error ? `失败原因：${scan.error}` : `成功 ${scan.success || 0} 页，失败 ${scan.failed || 0} 页，低质量 ${scan.low_quality || 0} 页；读取 ${rows} 行，滚动采集 ${virtualPasses} 次`;
  document.getElementById("full-scan-button").disabled = running || accountSelectionRequired;
  document.getElementById("full-scan-button").textContent = running ? "正在自动获取…" : accountSelectionRequired ? "请先选择千川账号" : "自动获取全店数据";
  document.getElementById("cancel-scan-button").hidden = !running;
  const receipt = scanReceiptFromStatus(scan);
  const needsRescan = Number(scan.failed || 0) > 0 || Number(receipt.summary?.needs_review || 0) > 0;
  document.getElementById("retry-scan-button").hidden = running || !needsRescan;
  document.getElementById("retry-scan-button").textContent = Number(receipt.summary?.needs_review || 0) > 0 && !(scan.failed > 0)
    ? "补采低质量页"
    : "补采失败/低质量页";
  renderScanReceipt(receipt);
  if (running && !scanPoller) scanPoller = setInterval(() => pollFullScan().catch(() => undefined), 1500);
  if (!running && scanPoller) { clearInterval(scanPoller); scanPoller = null; }
}
