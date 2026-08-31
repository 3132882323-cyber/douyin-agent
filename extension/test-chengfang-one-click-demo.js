const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = __dirname;
const html = fs.readFileSync(path.join(root, "sidepanel.html"), "utf8");
const js = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");
const css = fs.readFileSync(path.join(root, "sidepanel.css"), "utf8");
const policy = require("./chengfang-trial-policy.js");

assert.match(html, /id="chengfang-one-click-demo"/);
assert.match(html, /1 分钟一键看完 A2 自动投放演示/);
assert.match(html, /不是实际投放，不读取真实账户/);
assert.match(html, /SYNTHETIC · DEMO_FIXTURE · platform_write_attempted=false/);
assert.match(html, /id="chengfang-one-click-demo-clear"[^>]*hidden/);
assert.match(css, /\.chengfang-one-click-demo\[hidden\]\s*\{\s*display:\s*none/);
assert.match(js, /bridgeFetch\("\/chengfang\/a2-pilot\/demo"/);
assert.match(js, /JSON\.stringify\(\{ confirm: true, environment: "demo" \}\)/);
assert.match(js, /currentChengfangTrialConfig\.environment === "production"/);
assert.match(js, /result\.synthetic !== true[\s\S]*result\.demo_fixture !== true/);
assert.match(js, /result\.platform_write_attempted !== false[\s\S]*result\.runtime_persisted !== false/);
assert.match(js, /currentChengfangDemoFixture\?\.runtime/);
assert.match(js, /SYNTHETIC 合成作用域 · 不属于真实账户/);

const effect = policy.deriveEffect({
  synthetic: true,
  demo_fixture: true,
  platform_write_attempted: false,
  a2_pilot: {
    synthetic: true,
    demo_fixture: true,
    candidates: [{
      candidate_id: "demo-candidate",
      state: "verified",
      current_value: 1000,
      target_value: 900,
      platform_write_attempted: false,
    }],
    executions: [{
      execution_id: "demo-execution",
      candidate_id: "demo-candidate",
      state: "verified",
      execution_kind: "simulation",
      current_value: 1000,
      target_value: 900,
      platform_write_attempted: false,
      adapter_receipt: {
        ok: true,
        receipt_id: "demo-receipt",
        execution_kind: "simulation",
        platform_write_attempted: false,
      },
      readback: {
        source: "demo_fixture",
        execution_kind: "simulation",
        observed_value: 900,
        expected_value: 900,
        matched: true,
        platform_write_observed: false,
      },
    }],
  },
});
assert.equal(effect.synthetic, true);
assert.equal(effect.demo_fixture, true);
assert.equal(effect.platform_write_attempted, false);
assert.equal(effect.budget_change, -100);
assert.equal(effect.readback.matched, true);
assert.match(effect.evidence_notice, /不是实际投放效果/);

console.log("chengfang one-click demo tests passed");
