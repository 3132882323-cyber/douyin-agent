const assert = require("assert");
const log = require("./promotion-operation-log.js");

const payload = {
  execution_enabled: false,
  actions: [
    {
      action_id: "a1", operation_type: "adjust_budget", operation_label: "降低预算 20%", state: "confirmed",
      state_updated_at_ms: new Date("2026-08-20T09:00:00").getTime(),
      target_ref: { id: "plan-1", name: "直播计划", account_key: "acct-a", account_label: "主账户" },
      change: { field: "预算", current_value: 1000, target_value: 800 },
      evidence_ref: { source: "qianchuan", page_type: "campaigns", quality_score: 90 },
    },
    {
      action_id: "a2", operation_type: "pause_plan", state: "cancelled",
      state_updated_at_ms: new Date("2026-08-19T09:00:00").getTime(),
      target_ref: { id: "plan-2", name: "商品计划", account_label: "副账户" },
      change: { field: "状态", current_value: "投放中", target_value: "暂停" },
    },
    {
      action_id: "a3", operation_type: "adjust_budget", state: "executing",
      state_updated_at_ms: new Date("2026-08-20T10:00:00").getTime(),
      target_ref: { id: "plan-3", name: "结果待确认计划", account_label: "主账户" },
      change: { field: "预算", current_value: 800, target_value: 640 },
    },
  ],
};

const view = log.deriveView(payload, { operation_type: "adjust_budget", query: "直播", date_from: "2026-08-20", date_to: "2026-08-20" });
assert.equal(view.rows.length, 1);
assert.equal(view.summary.total, 3);
assert.equal(view.summary.confirmed, 1);
assert.equal(view.summary.cancelled, 1);
assert.equal(view.rows[0].change_label, "预算：1,000 → 800");
assert.equal(view.platform_write_enabled, false);
assert.equal(log.deriveView(payload, { state: "executing" }).rows[0].state_label, "执行结果待确认 · 禁止重复提交");

const unsafe = log.deriveView({ ...payload, execution_enabled: true });
assert.equal(unsafe.safe, false);

const csv = log.toCsv(view.rows);
assert.ok(csv.startsWith("\uFEFF时间,动作"));
assert.match(csv, /降低预算 20%/);
assert.match(csv, /a1/);

const formulaPrefixes = [
  "=1+1", "+1+1", "-30% ROI 测试计划", "@SUM(1,1)",
  " \t\r\n=空白", "\u00A0=不换行空格", "\u3000=全角空格", "\uFEFF=BOM",
  "\u0000=空字符", "\u000B=纵向制表", "\u007F=删除控制",
  "\u200B=零宽空格", "\u200C=零宽非连接", "\u200D=零宽连接", "\u200E=方向标记", "\u2060=词连接",
  "＝兼容等号", "＋兼容加号", "－兼容减号", "＠兼容艾特",
];
for (const value of formulaPrefixes) {
  const dangerousCsv = log.toCsv([{
    timestamp: 0,
    operation_label: value,
    account_label: "正常账户",
    target_name: "正常计划",
    change_label: "预算：100 → 80",
    state_label: "已确认",
    evidence_label: "本地回读",
    action_id: "safe-id",
  }]);
  assert.ok(
    dangerousCsv.includes(`"'${String(value).trim().replace(/"/g, '""')}"`),
    `formula-like CSV cell must be neutralized: ${JSON.stringify(value)}`,
  );
}

const quotedCsv = log.toCsv([{
  timestamp: 0,
  operation_label: "普通, \"引号\"\r\n换行",
  account_label: "'=已经中和",
  target_name: "普通中文计划",
  change_label: "预算：1,000 → 800",
  state_label: "已确认",
  evidence_label: "本地回读",
  action_id: "safe-id",
}]);
assert.match(quotedCsv, /"普通, ""引号""\r\n换行"/);
assert.match(quotedCsv, /,"预算：1,000 → 800",/);
assert.ok(quotedCsv.includes("\"'=已经中和\""));
assert.ok(!quotedCsv.includes("''=已经中和"));

const alternateDelimiterCsv = log.toCsv([{
  timestamp: 0,
  operation_label: "普通;=1+1;尾部",
  account_label: "正常账户",
  target_name: "正常计划",
  change_label: "预算：100 → 80",
  state_label: "已确认",
  evidence_label: "本地回读",
  action_id: "safe-id",
}]);
assert.ok(alternateDelimiterCsv.includes('"普通;=1+1;尾部"'), "regional separators must remain inside one quoted cell");

console.log("promotion operation log tests passed");
