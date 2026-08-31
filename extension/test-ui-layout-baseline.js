const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");

const failures = [];

function check(name, assertion) {
  try {
    assertion();
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
  }
}

function normalizeSelector(selector) {
  return selector.replace(/\s+/g, " ").trim();
}

function styleRules(source) {
  return [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
    selectors: match[1].split(",").map(normalizeSelector),
    body: match[2],
  }));
}

const rules = styleRules(css);

function lastPixelValue(selector, property) {
  const normalized = normalizeSelector(selector);
  let value = null;
  const declaration = new RegExp(`(?:^|;)\\s*${property.replace("-", "\\-")}\\s*:\\s*([0-9.]+)px\\b`, "g");

  for (const rule of rules) {
    if (!rule.selectors.includes(normalized)) continue;
    for (const match of rule.body.matchAll(declaration)) value = Number(match[1]);
  }
  return value;
}

function assertPixelFloor(selector, property, minimum) {
  const value = lastPixelValue(selector, property);
  assert.notEqual(value, null, `missing ${property} declaration for ${selector}`);
  assert.ok(value >= minimum, `${selector} has ${property}: ${value}px; expected at least ${minimum}px`);
}

function mediaBodies(maxWidth) {
  const pattern = new RegExp(`@media\\s*\\(\\s*max-width\\s*:\\s*${maxWidth}px\\s*\\)\\s*\\{`, "g");
  const bodies = [];
  let match;

  while ((match = pattern.exec(css))) {
    const open = pattern.lastIndex - 1;
    let depth = 1;
    let cursor = open + 1;
    let quote = null;

    for (; cursor < css.length && depth > 0; cursor += 1) {
      const char = css[cursor];
      if (quote) {
        if (char === quote && css[cursor - 1] !== "\\") quote = null;
        continue;
      }
      if (char === "\"" || char === "'") quote = char;
      else if (char === "{") depth += 1;
      else if (char === "}") depth -= 1;
    }

    assert.equal(depth, 0, `unclosed @media (max-width: ${maxWidth}px) block`);
    bodies.push(css.slice(open + 1, cursor - 1));
    pattern.lastIndex = cursor;
  }

  return bodies.join("\n");
}

function assertMediaRule(maxWidth, expression, message) {
  const body = mediaBodies(maxWidth);
  assert.ok(body, `missing @media (max-width: ${maxWidth}px)`);
  assert.match(body, expression, message);
}

check("workspace shell hierarchy", () => {
  assert.match(
    html,
    /<div class="workspace-shell">\s*<aside id="workspace-sidebar" class="workspace-sidebar">[\s\S]*?<\/aside>\s*<section class="workspace-stage">\s*<header class="workspace-topbar">[\s\S]*?<\/header>\s*<main class="panel-shell">/,
  );
});

check("primary page hierarchy", () => {
  assert.match(
    html,
    /<main class="panel-shell">\s*<section class="workspace-page-heading"[^>]*>[\s\S]*?<h1 id="workspace-page-title">[\s\S]*?<p id="workspace-page-subtitle">[\s\S]*?<div class="workspace-page-actions">[\s\S]*?<button id="workspace-primary-action"[^>]*>[\s\S]*?<\/section>\s*<section id="workspace-user-path" class="workspace-user-path"/,
  );
});

check("version and industry hierarchy", () => {
  assert.match(
    html,
    /<details id="data-version-center"[^>]*>[\s\S]*?<section class="version-center-panel">[\s\S]*?<section id="industry-pack-center" class="industry-pack-center"[\s\S]*?<div class="industry-pack-actions">[\s\S]*?id="activate-industry-pack"[\s\S]*?id="import-industry-pack"[\s\S]*?<div class="version-actions">[\s\S]*?id="check-updates"[\s\S]*?id="apply-knowledge-update"[\s\S]*?id="rollback-knowledge"[\s\S]*?<details class="version-advanced">/,
  );
});

check("critical DOM ids are unique", () => {
  const criticalIds = [
    "workspace-sidebar",
    "workspace-nav-toggle",
    "workspace-page-title",
    "workspace-page-subtitle",
    "workspace-primary-action",
    "workspace-user-path",
    "workspace-user-path-next",
    "priority-reminder",
    "priority-reminder-action",
    "data-version-center",
    "industry-pack-center",
    "industry-pack-select",
    "activate-industry-pack",
    "import-industry-pack",
    "check-updates",
    "apply-knowledge-update",
    "rollback-knowledge",
  ];

  for (const id of criticalIds) {
    const count = (html.match(new RegExp(`\\bid="${id}"`, "g")) || []).length;
    assert.equal(count, 1, `${id} should occur exactly once; found ${count}`);
  }
});

for (const selector of [
  ".workspace-nav button",
  ".workspace-nav-children button",
  ".workspace-nav-toggle",
  ".workspace-context-button",
  ".workspace-topbar-button",
  ".workspace-refresh-button",
  ".experience-mode-switch button",
  ".workspace-page-actions button",
  ".workspace-user-path > button",
  ".workspace-stage .priority-reminder button",
  ".qianchuan-sync-dock-control",
  ".workspace-stage .hero-actions button",
  ".industry-pack-selector select",
  ".industry-pack-actions button",
  ".version-actions button",
]) {
  check(`44px target ${selector}`, () => {
    assertPixelFloor(selector, "min-height", 44);
  });
}

for (const selector of [
  ".workspace-nav-group",
  ".workspace-nav button > strong",
  ".workspace-nav button > em",
  ".workspace-nav-children button > strong",
  ".workspace-breadcrumb",
  ".workspace-agent-state",
  ".workspace-context-button small",
  ".workspace-context-button strong",
  ".workspace-topbar-button",
  ".experience-mode-switch button",
  ".workspace-page-heading > div:first-child > span",
  ".workspace-page-heading p",
  ".workspace-page-actions button",
  ".workspace-user-path-copy span",
  ".workspace-user-path-copy small",
  ".workspace-user-path li",
  ".workspace-user-path > button",
  ".workspace-stage .priority-reminder-copy > span",
  ".workspace-stage .priority-reminder-copy p",
  ".workspace-stage .priority-reminder button",
  ".workspace-stage .hero .eyebrow",
  ".workspace-stage .hero p",
  ".workspace-stage .hero-actions button",
  ".version-center-title",
  ".version-grid article small",
  ".version-grid article span",
  ".data-freshness-row small",
  ".update-message",
  ".industry-pack-heading small",
  ".industry-pack-heading span",
  ".industry-pack-selector",
  ".industry-pack-preview small",
  ".industry-pack-message",
  ".industry-pack-actions button",
  ".industry-pack-center > p",
  ".version-actions button",
]) {
  check(`12px type ${selector}`, () => {
    assertPixelFloor(selector, "font-size", 12);
  });
}

for (const selector of [".promotion-plan-table th", ".promotion-plan-table td", ".promotion-operation-log-table th", ".promotion-operation-log-table td"]) {
  check(`11px dense-table type ${selector}`, () => {
    assertPixelFloor(selector, "font-size", 11);
  });
}

for (const selector of [".promotion-plan-toolbar > button", ".promotion-plan-selection-bar button", ".promotion-plan-table button", ".promotion-bulk-config > button"]) {
  check(`40px dense-page action ${selector}`, () => {
    assertPixelFloor(selector, "min-height", 40);
  });
}

check("version center spans the main canvas", () => {
  assert.ok(
    /\.workspace-stage \.panel-shell > #data-version-center\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/s.test(css),
    "#data-version-center should explicitly span the full workspace grid",
  );
});

check("1080px workspace collapse", () => {
  assertMediaRule(1080, /\.workspace-shell\s*\{[^}]*display:\s*block/s, "workspace shell should collapse at 1080px");
  assertMediaRule(1080, /\.workspace-sidebar\s*\{[^}]*width:\s*100%/s, "sidebar should use the full width at 1080px");
  assertMediaRule(1080, /\.workspace-stage \.panel-shell > \.manager-card,[\s\S]*?\.workspace-stage \.panel-shell > \.scan-receipt-card\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/s, "asymmetric dashboard cards should stay full width near the 900px boundary");
  assertMediaRule(1080, /\.workspace-stage \[id\]\s*\{[^}]*scroll-margin-top:\s*136px/s, "focused sections should clear the stacked mobile navigation");
});

check("899px single-column grid fallback", () => {
  assertMediaRule(899, /\.workspace-stage \.panel-shell\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s, "main grid should become one column below 900px");
  assertMediaRule(899, /\.workspace-stage \.panel-shell\s*>\s*\*\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/s, "direct cards should span the single-column grid");
});

check("760px core layout reflow", () => {
  assertMediaRule(760, /\.workspace-page-heading\s*\{[^}]*flex-direction:\s*column/s, "page heading should stack at 760px");
  assertMediaRule(760, /\.workspace-page-actions\s*\{[^}]*grid-template-columns:\s*1fr\s+1fr/s, "page actions should use two balanced columns at 760px");
  assertMediaRule(760, /\.workspace-stage \.priority-reminder button\s*\{[^}]*width:\s*100%/s, "priority action should span the card at 760px");
  assertMediaRule(760, /\.workspace-stage \.hero\s*\{[^}]*grid-template-columns:\s*1fr/s, "hero should use one column at 760px");
  assertMediaRule(760, /\.industry-pack-center\s*\{[^}]*grid-template-columns:\s*1fr/s, "industry pack should use one column at 760px");
  assertMediaRule(760, /body\.workspace-tool-focus \.workspace-page-heading\s*\{[^}]*position:\s*static/s, "focused page heading should not cover mobile content");
});

check("520px compact layout keeps touch targets", () => {
  assertMediaRule(520, /\.workspace-page-actions,[\s\S]*?\.industry-pack-actions,[\s\S]*?\.version-actions\s*\{[^}]*grid-template-columns:\s*1fr/s, "primary action groups should stack at 520px");
  const body = mediaBodies(520);
  const compactRules = styleRules(body);
  for (const selector of [".workspace-nav-toggle", ".workspace-refresh-button"]) {
    let value = null;
    for (const rule of compactRules) {
      if (!rule.selectors.includes(selector)) continue;
      const match = rule.body.match(/(?:^|;)\s*min-height\s*:\s*([0-9.]+)px\b/);
      if (match) value = Number(match[1]);
    }
    assert.notEqual(value, null, `missing compact min-height for ${selector}`);
    assert.ok(value >= 44, `${selector} shrinks to ${value}px at 520px; expected at least 44px`);
  }
});

check("keyboard focus remains visible", () => {
  assert.match(css, /\.panel-shell button:focus-visible,[\s\S]*?\.workspace-nav button:focus-visible,[\s\S]*?\.workspace-topbar button:focus-visible\s*\{[^}]*outline:/s);
});

if (failures.length) {
  throw new Error(`UI layout baseline failed:\n- ${failures.join("\n- ")}`);
}

console.log("UI layout baseline tests passed");
