const fs = require("fs");
const path = require("path");
const assert = require("assert");
const crypto = require("crypto");

const root = __dirname;
const html = fs.readFileSync(path.join(root, "sidepanel.html"), "utf8");
const js = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");
const css = fs.readFileSync(path.join(root, "sidepanel.css"), "utf8");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
const compatManifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.compat.json"), "utf8"));
const installer = fs.readFileSync(path.join(root, "..", "tools", "install_release.ps1"), "utf8");

function extensionId(key) {
  const digest = crypto.createHash("sha256").update(Buffer.from(key, "base64")).digest().subarray(0, 16);
  return [...digest].map((value) => String.fromCharCode(97 + (value >> 4), 97 + (value & 15))).join("");
}

assert.equal(manifest.key, compatManifest.key);
assert.equal(extensionId(manifest.key), "obpbbgjamjfkambmhidbjnaoiehfndcj");
assert.match(installer, /Invoke-PackagedTrustProvisioner \$stagedAgentSource \$manifestPath \$InstallRoot/);
assert.match(installer, /Copy-Item -LiteralPath \$agentSource -Destination \$stagedAgentSource -Force/);
assert.match(installer, /--initialize-local-api-trust/);
assert.doesNotMatch(installer, /function Get-ChromiumExtensionId/);
assert.match(installer, /trustResult\.ok -ne \$true/);
assert.match(installer, /config\\trusted_extension_ids\.json/);
assert.match(installer, /trustResult\.extension_id/);

assert.match(html, /id="industry-pack-center"/);
assert.match(html, /不同店铺严格隔离/);
assert.match(html, /id="industry-pack-select"/);
assert.match(html, /id="activate-industry-pack"[^>]*>应用到当前店铺/);
assert.match(html, /导入已签名知识包/);
assert.match(html, /不会自动修改计划/);
assert.match(html, /知识包与经营数据均保存在本机/);

assert.match(js, /bridgeFetch\("\/rules\/packs\/import"/);
assert.match(js, /bridgeFetch\("\/rules\/packs\/bind"/);
assert.match(js, /安装完成后仍需明确应用到当前店铺/);
assert.match(js, /file\.size > 2 \* 1024 \* 1024/);
assert.match(js, /bound_pack_unavailable/);
assert.match(js, /其他店铺不受影响/);
assert.doesNotMatch(js, /bridgeFetch\("\/rules\/import-local"/);

assert.match(css, /\.industry-pack-center[\s\S]*grid-template-columns/);
assert.match(css, /\.industry-pack-actions button[\s\S]*min-height: 44px/);
assert.match(css, /\.industry-pack-selector select[\s\S]*font-size: 14px/);
