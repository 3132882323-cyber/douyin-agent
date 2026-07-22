(async () => {
  await chrome.runtime.sendMessage({ type: "cancel-full-scan" });
  document.getElementById("status").textContent = "巡检将在当前页面完成后停止。";
  setTimeout(() => globalThis.close(), 1500);
})();
