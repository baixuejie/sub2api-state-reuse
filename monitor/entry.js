(() => {
  const id = "fn-state-monitor-link";
  function update() {
    let user;
    try {
      user = JSON.parse(localStorage.getItem("auth_user") || "null");
    } catch {}
    const visible =
      user?.role === "admin" && location.pathname.startsWith("/admin");
    let a = document.getElementById(id);
    if (!visible) {
      a?.remove();
      return;
    }
    if (a) return;
    a = document.createElement("a");
    a.id = id;
    a.href = "/admin/state-harvest";
    a.textContent = "STATE 采集日志";
    a.style.cssText =
      "position:fixed;right:20px;bottom:20px;z-index:60;padding:10px 16px;border-radius:9px;background:#173a61;color:#fff;border:1px solid #5685b7;font:13px system-ui;text-decoration:none;box-shadow:0 4px 16px #0003";
    document.body.append(a);
  }
  setInterval(update, 1500);
  update();
})();
