const assert = require("assert");
const fs = require("fs");
const path = require("path");

const js = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.doesNotMatch(js, /bridgeFetch\("\/tasks\/track"/);
assert.match(js, /item\.status === "doing" \? \[\["进入效果观察", "observing"\], \["转交", "transfer"\], \["阻止", "blocked"\]\]/);
assert.match(js, /item\.status === "observing" \? \[\[observationAction\.label, "observation_progress"\]\]/);
assert.doesNotMatch(js, /item\.status === "observing" \? \[\["填写结果并结案", "done"\]/);
assert.match(js, /button\.dataset\.observationPrimary = "true"/);
assert.match(js, /if \(item\.status !== "observing"\) appendCopyAction\(card, item\.action_params\)/);
assert.match(js, /if \(item\.status !== "observing"\) card\.append\(feedback\)/);
assert.match(js, /options\.showModuleLink && item\.status !== "observing"/);
assert.match(js, /完成操作后先进入效果观察，不能直接结案/);
assert.match(js, /请填写结案说明：做了什么、结果怎样、是否需要继续观察/);
assert.match(js, /action: item\.action[\s\S]*acceptance: item\.acceptance[\s\S]*observation_window: item\.observation_window/);
assert.match(js, /manual_verified: "人工结案"[\s\S]*inconclusive: "证据不足"/);
assert.match(js, /这些不计入自动有效率/);
assert.match(js, /人工结案任务:/);
assert.match(js, /任务证据结果:/);
assert.match(js, /投放已回读:/);
assert.match(js, /首次价值待回读：看过任务或人工结案，不等于已经产生经营效果/);
assert.doesNotMatch(js, /已完成任务: summary\.completed_tasks/);
assert.match(js, /task_contract\?\.navigation\?\.target_id/);
assert.match(js, /contract_fingerprint: taskContract\?\.contract_fingerprint/);
assert.match(js, /eligibility\.can_start/);
assert.match(js, /task-contract-blocker/);

console.log("operating loop UI tests passed");
