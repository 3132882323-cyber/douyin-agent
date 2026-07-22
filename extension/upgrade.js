chrome.runtime.reload();
document.getElementById("status").textContent = "店策 Agent 已重新加载，可以关闭此页面。";
setTimeout(() => globalThis.close(), 800);
