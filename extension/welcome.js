const BRIDGE_URL = "http://127.0.0.1:8765";

async function checkBridge() {
  const button = document.getElementById("check-bridge");
  const status = document.getElementById("bridge-status");
  const help = document.getElementById("bridge-help");
  button.disabled = true;
  button.textContent = "检测中…";
  try {
    const response = await fetch(`${BRIDGE_URL}/health`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.status !== "ok") throw new Error("bridge_not_ready");
    status.textContent = `已连接 v${data.version || ""}`;
    status.style.color = "#067647";
    help.hidden = true;
  } catch (_error) {
    status.textContent = "未检测到";
    status.style.color = "#b42318";
    help.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "重新检测";
  }
}

function openUrl(url) {
  chrome.tabs.create({ url });
}

document.getElementById("check-bridge").addEventListener("click", checkBridge);
document.getElementById("open-doudian").addEventListener(
  "click",
  () => openUrl("https://fxg.jinritemai.com/ffa/mshop/homepage/index"),
);
document.getElementById("open-qianchuan").addEventListener(
  "click",
  () => openUrl("https://qianchuan.jinritemai.com/"),
);
setTimeout(checkBridge, 500);
