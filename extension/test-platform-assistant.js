"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const assistant = require("./platform-assistant.js");

assert.equal(assistant.sourceForLocation({ hostname: "fxg.jinritemai.com" }), "doudian");
assert.equal(assistant.sourceForLocation({ hostname: "qianchuan.jinritemai.com" }), "qianchuan");
assert.equal(assistant.sourceForLocation({ hostname: "buyin.jinritemai.com" }), "qianchuan");
assert.equal(assistant.sourceForLocation({ hostname: "example.com" }), "");
assert.equal(assistant.DISMISSED_KEY, "platformAssistantDismissedV2");
assert.equal(assistant.preferenceKey(assistant.DISMISSED_KEY, "doudian"), "platformAssistantDismissedV2.doudian");
assert.equal(assistant.dismissedFromStorage({ "platformAssistantDismissedV2.doudian": true }, "doudian"), true);
assert.equal(assistant.dismissedFromStorage({ "platformAssistantDismissedV2.doudian": true }, "qianchuan"), false);
assert.equal(assistant.dismissedFromStorage({ platformAssistantDismissedV1: true }, "qianchuan"), true);
assert.equal(assistant.compactVersion(" 4.14.1 "), "4.14.1");
assert.equal(assistant.compactVersion("latest"), "");
assert.equal(assistant.shortExtensionId("obpbbgjamjfkambmhidbjnaoiehfndcj"), "obpbbg…ndcj");
assert.equal(assistant.isRecoveryError(new Error("Extension context invalidated.")), true);
assert.equal(assistant.isRecoveryError(new Error("ordinary bridge error")), false);
assert.equal(assistant.isTrustedGesture({ isTrusted: true }), true);
assert.equal(assistant.isTrustedGesture({ isTrusted: false }), false);
assert.equal(assistant.clockLabel(1_000_000, 1_020_000), "刚刚同步");
assert.equal(assistant.clockLabel(1_000_000, 1_121_000), "2 分钟前同步");
assert.equal(assistant.pageLabel("qianchuan_live"), "直播计划");
assert.equal(assistant.pageLabel("campaigns"), "商品计划");
assert.equal(assistant.pageLabel("materials"), "视频素材");

const directory = __dirname;
const manifest = JSON.parse(fs.readFileSync(path.join(directory, "manifest.json"), "utf8"));
const compat = JSON.parse(fs.readFileSync(path.join(directory, "manifest.compat.json"), "utf8"));
for (const value of [manifest, compat]) {
  assert.equal(value.content_scripts.length, 2);
  for (const entry of value.content_scripts) {
    assert.equal(entry.js.at(-1), "platform-assistant.js", "assistant must mount after the platform collector");
  }
}

const background = fs.readFileSync(path.join(directory, "background.js"), "utf8");
const popup = fs.readFileSync(path.join(directory, "popup.js"), "utf8");
const assistantSource = fs.readFileSync(path.join(directory, "platform-assistant.js"), "utf8");
assert.match(background, /"platform-assistant-status"/);
assert.match(background, /"platform-assistant-sync"/);
assert.match(background, /"platform-assistant-open-workbench"/);
assert.match(background, /targetTabId: Number\(sender\?\.tab\?\.id\)/);
assert.match(background, /files: \["platform-assistant\.js"\]/);
assert.match(background, /message\.type === "show-platform-assistant"/);
assert.match(background, /PLATFORM_ASSISTANT_DISMISSED_KEY/);
assert.match(background, /PAGE_SYNC_RECEIPTS_KEY/);
assert.match(background, /presence\.some\(\(item\) => item\?\.result === true\)/);
assert.match(popup, /show-page-assistant-button/);
assert.match(popup, /type: "show-platform-assistant"/);
assert.match(assistantSource, /aria-label="关闭店策页内助手"/);
assert.match(assistantSource, /mini-close/);
assert.match(assistantSource, /writeDismissed\(root, source, true\)/);
assert.doesNotMatch(assistantSource, /setTimeout\(\(\) => root\.location\.reload\(\), 120\)/);
assert.match(assistantSource, /当前页面不会自动刷新/);
assert.match(assistantSource, /writeCollapsed\(root, source, true\)/);
assert.match(assistantSource, /collapsed: preferences\.collapsed \|\| !preferences\.collapsed_set/);
assert.match(assistantSource, /zIndex: "900"/);
assert.equal(typeof assistant.restorePageFocusAfterDismissal, "function");
assert.match(assistantSource, /data-dian-agent-platform-assistant-announcer/);

{
  let focused = false;
  let announcer = null;
  const attributes = new Map();
  const focusTarget = {
    hasAttribute: (name) => attributes.has(name),
    setAttribute: (name, value) => attributes.set(name, value),
    removeAttribute: (name) => attributes.delete(name),
    focus: () => { focused = true; },
  };
  const fakeRoot = {
    setTimeout(callback) { callback(); },
    document: {
      documentElement: {
        appendChild(element) { announcer = element; },
      },
      body: focusTarget,
      querySelector() { return focusTarget; },
      createElement() {
        return {
          style: {},
          textContent: "",
          setAttribute() {},
          remove() { this.removed = true; },
        };
      },
    },
  };
  assistant.restorePageFocusAfterDismissal(fakeRoot, "千川");
  assert.equal(focused, true, "closing the assistant should restore page focus");
  assert.match(announcer.textContent, /千川页内助手已关闭/);
  assert.equal(announcer.removed, true);
}

for (const collector of ["content-doudian.js", "content-qianchuan.js"]) {
  const source = fs.readFileSync(path.join(directory, collector), "utf8");
  assert.match(source, /dian-agent-content-status/);
  assert.match(source, /RuntimeVersion/);
}

console.log("platform assistant tests passed");
