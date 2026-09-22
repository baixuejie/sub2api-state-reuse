(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const token = new URLSearchParams(location.hash.slice(1)).get("bridge_token");
  const pending = new Map();
  let sequence = 0, ready = false;
  const origin = (() => { try { return new URL(document.referrer).origin; } catch { return "*"; } })();
  function send(type, fields = {}) {
    const id = String(++sequence);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("宿主响应超时，请重新读取后再试。")); }, 30000);
      pending.set(id, { resolve, reject, timer });
      parent.postMessage({ source: "sub2api-plugin-ui", bridge_token: token, request_id: id, type, ...fields }, origin);
    });
  }
  window.addEventListener("message", event => {
    const data = event.data;
    if (event.source !== parent || (origin !== "*" && event.origin !== origin) || !data ||
        data.source !== "sub2api-plugin-host" || data.bridge_token !== token) return;
    const request = pending.get(data.request_id);
    if (!request) return;
    clearTimeout(request.timer); pending.delete(data.request_id);
    if (data.ok) request.resolve(data);
    else request.reject(new Error(data.error || "配置未保存，请检查输入或宿主提示。"));
  });
  window.addEventListener("pagehide", () => {
    for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error("页面已关闭")); }
    pending.clear(); $("proxy").value = ""; $("generator").value = "";
  });
  async function run(action) {
    $("save").disabled = $("reload").disabled = true;
    $("status").className = "";
    try { await action(); }
    catch (error) { $("status").textContent = error.message; $("status").className = "error"; }
    finally { $("reload").disabled = false; $("save").disabled = !ready; }
  }
  function fill(config) {
    $("proxy").value = config.proxy_url || "";
    $("generator").value = config.harvest_proxy_api || "";
    ready = true;
  }
  function validate(proxy, generator) {
    let parsed;
    try { parsed = new URL(proxy); } catch { throw new Error("请输入完整代理 URL。"); }
    if (!["http:", "https:", "socks5:", "socks5h:"].includes(parsed.protocol) || !parsed.hostname ||
        parsed.search || parsed.hash || !["", "/"].includes(parsed.pathname)) throw new Error("代理需使用 HTTP(S) 或 SOCKS5(h)，且不能包含路径或查询参数。");
    if (generator) {
      try { parsed = new URL(generator); } catch { throw new Error("动态 IP 接口 URL 无效。"); }
      if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.hash)
        throw new Error("动态 IP 接口需使用 HTTP(S) URL，支持路径和查询参数。");
    }
  }
  async function load() {
    fill((await send("config.load")).config);
    $("status").textContent = "已读取当前代理配置。";
  }
  $("reload").onclick = () => run(load);
  $("save").onclick = () => run(async () => {
    const proxy = $("proxy").value.trim().replace(/\/$/, ""), generator = $("generator").value.trim();
    validate(proxy, generator);
    // Re-read immediately before saving so automatic enrollment and suspensions survive.
    const latest = (await send("config.load")).config;
    const config = { ...latest, proxy_url: proxy, harvest_proxy_api: generator };
    fill((await send("config.save", { config })).config);
    $("status").textContent = "已保存，下一轮采集生效。";
  });
  $("reveal").onchange = () => {
    for (const id of ["proxy", "generator"]) $(id).type = $("reveal").checked ? "text" : "password";
  };
  if (token && parent !== window) {
    parent.postMessage({ source: "sub2api-plugin-ui", bridge_token: token, type: "sub2api.plugin.ready" }, origin);
    run(load);
  } else $("status").textContent = "请从主站「插件管理 → 配置」打开此页面。";
})();
