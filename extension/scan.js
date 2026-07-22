(async () => {
  const status = document.getElementById("status");
  try {
    const result = await chrome.runtime.sendMessage({ type: "start-full-scan" });
    status.textContent = result?.started === false ? "巡检已经在进行中。" : "全店巡检已启动，可在侧边栏查看进度。";
  } catch (error) {
    status.textContent = `启动失败：${error.message || error}`;
  }
  setTimeout(() => globalThis.close(), 1800);
})();
