const BRIDGE_URL = "http://127.0.0.1:8765";
const WORKBENCH_URL = chrome.runtime.getURL("sidepanel.html");

async function enableSentinel() {
  await chrome.runtime.sendMessage({
    type: "update-settings",
    settings: {
      operatingMode: "sentinel",
      autoSync: false,
      autoFullScan: false,
      privacyMode: true,
    },
  });
}

async function checkBridge() {
  const button = document.getElementById("check-bridge");
  const bridgeStatus = document.getElementById("bridge-status");
  const storeStatus = document.getElementById("store-status");
  button.disabled = true;
  bridgeStatus.textContent = "检测中";
  bridgeStatus.className = "state";
  try {
    const health = await fetch(`${BRIDGE_URL}/health`, { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });
    const stores = await fetch(`${BRIDGE_URL}/stores`, { cache: "no-store" }).then((response) => response.json());
    bridgeStatus.textContent = `已连接 v${health.version || ""}`;
    bridgeStatus.className = "state ok";
    storeStatus.textContent = stores.store_count
      ? `已识别 ${stores.store_count} 个店铺`
      : "尚未连接店铺";
    storeStatus.className = stores.store_count ? "state ok" : "state warn";
  } catch {
    bridgeStatus.textContent = "未启动，请先运行安装包中的本地 Agent";
    bridgeStatus.className = "state warn";
    storeStatus.textContent = "本地 Agent 启动后检测";
    storeStatus.className = "state";
  } finally {
    button.disabled = false;
  }
}

function openUrl(url) {
  chrome.tabs.create({ url });
}

document.getElementById("check-bridge").addEventListener("click", checkBridge);
document.getElementById("open-workbench").addEventListener("click", () => openUrl(WORKBENCH_URL));
document.getElementById("finish-setup").addEventListener("click", () => openUrl(WORKBENCH_URL));
document.getElementById("open-doudian").addEventListener(
  "click",
  () => openUrl("https://fxg.jinritemai.com/ffa/mshop/homepage/index"),
);
document.getElementById("open-qianchuan").addEventListener(
  "click",
  () => openUrl("https://qianchuan.jinritemai.com/"),
);

enableSentinel().catch(() => undefined);
setTimeout(checkBridge, 350);
