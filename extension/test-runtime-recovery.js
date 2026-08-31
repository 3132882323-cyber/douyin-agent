"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const recovery = require("./runtime-recovery.js");

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

assert.equal(recovery.isRecoverableRuntimeError(new Error("Extension context invalidated.")), true);
assert.equal(recovery.isRecoverableRuntimeError(new Error("Could not establish connection. Receiving end does not exist.")), true);
assert.equal(recovery.isRecoverableRuntimeError(new Error("HTTP 409")), false);
assert.equal(recovery.guardKey({ pathname: "/sidepanel.html" }), "dianAgentRuntimeRecoveryV1:/sidepanel.html");

const storage = new MemoryStorage();
const key = recovery.guardKey({ pathname: "/sidepanel.html" });
assert.equal(recovery.canReload(storage, key, 100_000), true);
storage.setItem(key, JSON.stringify({ requested_at: 99_000 }));
assert.equal(recovery.canReload(storage, key, 100_000), false, "recovery must not enter a reload loop");
assert.equal(recovery.canReload(storage, key, 100_000 + recovery.GUARD_WINDOW_MS), true);

for (const file of ["welcome.html", "popup.html", "sidepanel.html"]) {
  const html = fs.readFileSync(path.join(__dirname, file), "utf8");
  assert.match(html, /<script src="runtime-recovery\.js"><\/script>/);
}

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
assert.match(background, /message\.type === "runtime-context-ping"/);
assert.match(background, /refreshStaleExtensionDocumentsOnce/);
assert.match(background, /coordinateActivationDocuments\("worker-start"\)/);

console.log("runtime recovery tests passed");
