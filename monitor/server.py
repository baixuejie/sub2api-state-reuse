import os
import http.server, json, pathlib, time, urllib.request, urllib.error, datetime

ROOT = pathlib.Path(os.environ.get("STATE_ROOT", "/var/lib/sub2api-state-reuse"))
WEB = pathlib.Path(__file__).parent
BASE_URL = os.environ.get("SUB2API_BASE_URL", "http://127.0.0.1:8081/api/v1").rstrip(
    "/"
)
FIELDS = {
    "at",
    "account_id",
    "event",
    "source",
    "phase",
    "http",
    "length",
    "actual_model",
    "completed",
    "transport_error",
    "error",
    "error_code",
    "stop",
    "captured",
    "renewed",
    "test_success",
    "next_attempt",
    "auth_block",
    "business_schedulable",
    "scheduling_reason",
    "model",
    "egress_ip", "egress_status", "attempt_no", "request_no", "ip_changes", "ip_changed",
}


def read_json(name, default):
    try:
        return json.loads((ROOT / name).read_text())
    except (OSError, ValueError):
        return default


def tail_events():
    p = ROOT / "harvest-events.jsonl"
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 524288))
            lines = f.read().splitlines()
        if size > 524288:
            lines = lines[1:]
    except OSError:
        return []
    rows = []
    for line in lines[-500:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rows.append(
            {
                k: v
                for k, v in row.items()
                if k in FIELDS and isinstance(v, (str, int, float, bool, type(None)))
            }
        )
    return rows


def snapshot():
    data = read_json("cron-status.json", {})
    scheduling = read_json("scheduling-status.json", {})
    automation = read_json("automation-status.json", {})
    models = data.get("models") or ["gpt-6-astra", "gpt-5.6-sol"]
    gates = {row["account_id"]: row for row in scheduling.get("accounts", [])}
    events = tail_events()
    # Historical events predate model tags; those probes were Astra-only.
    for event in events:
        if not event.get("model") and event.get("event") in {
            "attempt_started", "attempt_finished", "candidate_queued", "account_finished", "paused"
        }:
            event["model"] = "gpt-6-astra"
    accounts = {}
    for row in data.get("before", []):
        aid = row.get("account_id")
        if not isinstance(aid, int):
            continue
        account = accounts.setdefault(aid, {"account_id": aid,
            "account_name": row.get("account_name") or "", "plan": row.get("plan"), "models": {}})
        model = row.get("model") or "gpt-6-astra"
        detail = {key: row.get(key) for key in (
            "status", "expires_at", "issued_at", "length", "next_attempt", "auth_block")}
        stats = row.get("egress") or {}
        detail["egress"] = {key: stats[key] for key in
            ("attempts", "requests", "ip_changes", "last_confirmed_ip", "egress_status") if key in stats}
        last = row.get("last_probe")
        if isinstance(last, dict):
            detail["last_probe"] = {key: last.get(key) for key in (
                "at", "http", "actual_model", "length", "phase", "completed", "transport_error",
                "error", "error_code", "captured", "renewed", "egress_ip", "egress_status",
                "attempt_no", "request_no", "ip_changes", "ip_changed")}
        account["models"][model] = detail
    for aid, gate in gates.items():
        account = accounts.setdefault(aid, {"account_id": aid, "account_name": "", "models": {}})
        for key in ("business_schedulable", "scheduling_reason", "required_models", "missing_models"):
            if key in gate:
                account[key] = gate[key]
        for model, ticket in gate.get("model_tickets", {}).items():
            if model in models:
                account["models"].setdefault(model, {}).update(ticket)
    tickets = [{key: ticket.get(key) for key in
        ("account_id", "model", "length", "issued_at", "expires_at")}
        for ticket in data.get("valid_tickets", [])]
    for account in accounts.values():
        for model in models:
            account["models"].setdefault(model, {"status": "pending"})
    return {
        "server_time": time.time(), "updated_at": data.get("at"),
        "scheduling_updated_at": scheduling.get("at"),
        "accounts": sorted(accounts.values(), key=lambda account: account["account_id"]),
        "models": models, "tickets": tickets, "events": events,
        "scheduling_enabled": scheduling.get("enabled", False) and automation.get("enabled", True),
        "automation": automation,
        "policy": {"check_seconds": 20, "account_interval": 20, "renew_interval": 30,
            "routes_per_cycle": 1, "concurrency": 3,
            "required_models": data.get("required_models") or ["gpt-6-astra"]},
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/fn-state/api":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or len(auth) > 8192:
                return self.send(401, {"error": "请先登录号池管理员账号"})
            req = urllib.request.Request(
                BASE_URL + "/auth/me", headers={"Authorization": auth}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    user = json.load(r).get("data", {})
            except urllib.error.HTTPError as e:
                return self.send(
                    401 if e.code == 401 else 403, {"error": "登录已过期或无访问权限"}
                )
            except Exception:
                return self.send(503, {"error": "暂时无法验证登录状态"})
            if user.get("role") != "admin":
                return self.send(403, {"error": "仅管理员可查看采集日志"})
            try:
                return self.send(200, snapshot())
            except Exception:
                return self.send(503, {"error": "采集状态暂不可用"})
        files = {
            "/admin/state-harvest": ("index.html", "text/html; charset=utf-8"),
            "/fn-state/app.js": ("app.js", "application/javascript"),
            "/fn-state/entry.js": ("entry.js", "application/javascript"),
        }
        if path not in files:
            return self.send(404, {"error": "not found"})
        name, ctype = files[path]
        self.send(200, (WEB / name).read_bytes(), ctype)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(
        (os.environ.get("STATE_MONITOR_HOST", "127.0.0.1"), int(os.environ.get("STATE_MONITOR_PORT", "17843"))), Handler
    ).serve_forever()
