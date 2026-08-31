const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const js = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

const systemTree = html.slice(html.indexOf('data-nav-tree="system"'), html.indexOf("</nav>"));
assert.match(systemTree, /data-workspace-key="ai-connection"[^>]*data-workspace-target="ai-connection-center"/);
assert.doesNotMatch(html.slice(0, html.indexOf('data-nav-tree="system"')), /data-workspace-target="ai-connection-center"/);

const centerStart = html.indexOf('id="ai-connection-center"');
const centerEnd = html.indexOf('id="oceanengine-oauth-card"');
assert.ok(centerStart > 0 && centerEnd > centerStart, "AI center must live before the remaining system cards");
const center = html.slice(centerStart, centerEnd);

assert.match(center, /id="ai-connection-state"[^>]*>AI未连接/);
assert.match(js, /label: "AI未连接"/);
assert.match(js, /label: "分析可用"/);
assert.match(js, /label: "影子运行"/);

assert.match(center, /<option value="openai">GPT（OpenAI）<\/option>/);
assert.match(center, /<option value="deepseek">DeepSeek<\/option>/);
assert.match(center, /<option value="qwen_bailian">千问 \/ 阿里云百炼<\/option>/);
assert.match(center, /<option value="glm_zhipu">智谱 GLM<\/option>/);
assert.match(center, /<option value="hunyuan_tencent">腾讯混元<\/option>/);
assert.match(center, /<option value="doubao_ark">豆包 \/ 火山引擎方舟<\/option>/);
assert.match(center, /<option value="openai_compatible">OpenAI 兼容服务<\/option>/);
assert.match(center, /<option value="local">本地模型<\/option>/);
assert.match(js, /qwen_bailian:\s*\{/);
assert.match(js, /defaultModel: "qwen-plus"/);
assert.match(js, /defaultBaseUrl: "https:\/\/dashscope\.aliyuncs\.com\/compatible-mode\/v1"/);
assert.match(js, /glm_zhipu:\s*\{[\s\S]{0,500}defaultModel: "glm-5\.2"[\s\S]{0,200}defaultBaseUrl: "https:\/\/open\.bigmodel\.cn\/api\/paas\/v4"/);
assert.match(js, /hunyuan_tencent:\s*\{[\s\S]{0,500}defaultModel: "hy3"[\s\S]{0,200}defaultBaseUrl: "https:\/\/tokenhub\.tencentmaas\.com\/v1"/);
assert.match(js, /默认使用腾讯云 TokenHub；国际站\/备用域名请严格按控制台填写/);
assert.match(js, /doubao_ark:\s*\{[\s\S]{0,600}defaultModel: "doubao-seed-2-0-lite-260215"[\s\S]{0,200}defaultBaseUrl: "https:\/\/ark\.cn-beijing\.volces\.com\/api\/v3"/);
assert.match(js, /当前预设仅支持火山引擎方舟北京官方推理地址/);
assert.match(center, /id="ai-provider-note"/);
assert.match(js, /getElementById\("ai-provider-note"\)\.textContent = definition\.providerNote/);
assert.match(js, /专属推理接入点请填写控制台提供的 Model\/Endpoint ID/);
assert.match(center, /id="ai-base-url-note"/);
assert.match(js, /如业务空间提供独立官方地址，可替换为该官方地址/);
assert.match(js, /function applyAiProviderDefaults\(\)[\s\S]{0,500}definition\.defaultModel[\s\S]{0,180}definition\.defaultBaseUrl/);
assert.match(center, /id="ai-api-key-input"[^>]*type="password"[^>]*autocomplete="new-password"/);
assert.doesNotMatch(center, /id="ai-api-key-input"[^>]*\svalue=/);
assert.doesNotMatch(js, /getElementById\("ai-api-key-input"\)\.value\s*=\s*String\(currentAiStatus/);
assert.match(js, /Secret values are deliberately never hydrated/);
assert.match(js, /function redactAiSecrets/);
assert.match(js, /\[密钥已隐藏\]/);
assert.match(center, /id="ai-remote-consent"[^>]*type="checkbox"/);
assert.match(center, /同意把上方预览中的脱敏聚合数据发送给所选 AI/);
assert.match(js, /remote_access_approved:/);
assert.match(js, /function aiRemoteConsentReady/);
assert.match(js, /return payload\.provider === "local" \|\| payload\.remote_access_approved === true/);
assert.match(js, /连接云端 AI 前，请先确认/);
assert.match(js, /ai-provider-select"\)\.addEventListener\("change"[\s\S]{0,180}applyAiProviderDefaults\(\)/);
assert.match(js, /function applyAiProviderDefaults\(\)[\s\S]{0,650}ai-remote-consent"\)\.checked = false/);
assert.match(js, /ai-base-url-input"\)\.addEventListener\("input"[\s\S]{0,160}ai-remote-consent"\)\.checked = false/);

assert.match(center, /id="ai-context-included"/);
assert.match(center, /id="ai-context-excluded"/);
assert.match(center, /Cookie、Token、API Key 和登录凭证/);
assert.match(js, /secretPattern = \/api/);

assert.match(center, /id="ai-test-connection"[^>]*>测试连接/);
assert.match(center, /id="ai-save-connection"[^>]*>保存连接/);
assert.match(center, /id="ai-disable-connections"[^>]*>停用全部 AI/);
assert.match(center, /id="ai-run-shadow"[^>]*>运行影子分析/);
assert.match(center, /id="ai-view-proposals"[^>]*>查看建议/);
assert.match(js, /dashboardRead\("\/ai\/status", generation\)/);
assert.match(js, /dashboardRead\("\/ai\/context-preview", generation\)/);
assert.match(js, /bridgeFetch\("\/ai\/providers\/test"/);
assert.match(js, /bridgeFetch\("\/ai\/providers\/configure"/);
assert.match(js, /bridgeFetch\("\/ai\/providers\/disable-all"/);
assert.match(js, /bridgeFetch\("\/ai\/shadow\/run"/);
assert.match(js, /bridgeFetch\("\/ai\/proposals"/);

assert.match(center, /AI 可以[\s\S]*分析与提出建议/);
assert.match(center, /AI 不可以[\s\S]*授权或执行投放/);
assert.match(center, /AI 连接 ≠ 投放授权/);
assert.doesNotMatch(center, />\s*(?:执行投放|确认执行|授权投放|调整预算|暂停计划)\s*</);
assert.doesNotMatch(center, /id="ai-(?:execute|confirm|authorize)/);

assert.match(css, /\.ai-connection-layout[\s\S]*grid-template-columns/);
assert.match(css, /simple-show-ai/);
assert.match(css, /@media \(max-width: 700px\)[\s\S]*\.ai-capability-boundary/);

console.log("AI connection center tests passed");
