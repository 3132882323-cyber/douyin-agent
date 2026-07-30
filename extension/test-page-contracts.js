/**
 * Page-type contract tests against desensitized HTML fixtures.
 * Run: node test-page-contracts.js
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { parseHTML } = (() => {
  // Tiny HTML table/metric parser — no external DOM dependency.
  function textOf(html) {
    return String(html || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  }

  function parseTables(html) {
    const tables = [];
    for (const tableHtml of html.matchAll(/<table\b[\s\S]*?<\/table>/gi)) {
      const rows = [];
      for (const rowHtml of tableHtml[0].matchAll(/<tr\b[\s\S]*?<\/tr>/gi)) {
        const hasHeaderCells = /<th\b/i.test(rowHtml[0]);
        const cells = [...rowHtml[0].matchAll(/<t[hd]\b[^>]*>([\s\S]*?)<\/t[hd]>/gi)]
          .map((item) => textOf(item[1]))
          .filter(Boolean);
        if (cells.length) rows.push({ cells, hasHeaderCells });
      }
      if (!rows.length) continue;
      const headerIndex = rows.findIndex((row) => row.hasHeaderCells);
      const headers = headerIndex >= 0 ? rows[headerIndex].cells : [];
      const dataRows = rows.filter((_, index) => index !== headerIndex).map((row) => row.cells);
      tables.push({ headers, rows: dataRows });
    }
    return tables;
  }

  function parseMetrics(html) {
    const metrics = {};
    for (const block of html.matchAll(/class="[^"]*metric[^"]*"[\s\S]*?<\/div>/gi)) {
      const label = textOf((block[0].match(/class="[^"]*label[^"]*"[^>]*>([\s\S]*?)</i) || [])[1] || "");
      const value = textOf((block[0].match(/class="[^"]*value[^"]*"[^>]*>([\s\S]*?)</i) || [])[1] || "");
      if (label && value) metrics[label] = value;
    }
    return metrics;
  }

  return { parseHTML: (html) => ({ tables: parseTables(html), metrics: parseMetrics(html), text: textOf(html) }) };
})();

require("./content-common.js");

const fixtureDir = path.join(__dirname, "fixtures");
const fixtures = fs.readdirSync(fixtureDir)
  .filter((name) => name.endsWith(".json"))
  .map((name) => JSON.parse(fs.readFileSync(path.join(fixtureDir, name), "utf8")));

function assertNoSecrets(text) {
  assert.equal(/1[3-9]\d{9}/.test(text), false, "fixture leaked a phone-like number");
  assert.equal(/\b\d{17}[\dXx]\b/.test(text), false, "fixture leaked an id-like number");
  assert.equal(/@example\.com|@qq\.com|@163\.com/i.test(text), false, "fixture leaked an email");
}

for (const fixture of fixtures) {
  assert.ok(fixture.id && fixture.source && fixture.expected_page_type && fixture.html, `${fixture.id || "?"}: incomplete fixture`);
  assertNoSecrets(JSON.stringify(fixture));

  const parsed = parseHTML(fixture.html);
  const score = globalThis.DianAgentExtractor.qualityScore(
    parsed.tables,
    parsed.metrics,
    parsed.text,
    fixture.expected_page_type,
  );

  if (fixture.required_headers) {
    assert.ok(parsed.tables.length >= 1, `${fixture.id}: expected at least one table`);
    const headers = parsed.tables[0].headers || [];
    for (const required of fixture.required_headers) {
      assert.ok(headers.includes(required), `${fixture.id}: missing header ${required}`);
    }
    const rowCount = parsed.tables.reduce((sum, table) => sum + table.rows.length, 0);
    assert.ok(rowCount >= Number(fixture.min_rows || 1), `${fixture.id}: min_rows not met (${rowCount})`);
  }

  if (fixture.required_metric_labels) {
    for (const label of fixture.required_metric_labels) {
      assert.ok(Object.hasOwn(parsed.metrics, label), `${fixture.id}: missing metric ${label}`);
    }
    assert.ok(
      Object.keys(parsed.metrics).length >= Number(fixture.min_metrics || 1),
      `${fixture.id}: min_metrics not met`,
    );
  }

  if (fixture.account) {
    const account = fixture.account;
    assert.match(String(account.key || ""), /^[a-z0-9_]{4,}$/i);
    assert.ok(String(account.label || "").length >= 2);
    assert.ok(["high", "medium", "low"].includes(account.confidence));
    assert.ok(["platform_id", "account_label"].includes(account.identity_source));
  }

  assert.ok(score >= 55, `${fixture.id}: quality too low (${score})`);
  assert.equal(globalThis.DianAgentExtractor.isSensitiveHeader("收货地址"), true);
}

console.log(`page contract tests passed (${fixtures.length} fixtures)`);
