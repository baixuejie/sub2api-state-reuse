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
    d = read_json("cron-status.json", {})
    state = read_json("cron-state.json", {})
    accounts = []
    now = time.time()
    for a in d.get("before", []):
        aid = a.get("account_id")
        item = {k: a.get(k) for k in ("account_id", "model", "status", "expires_at")}
        item["next_attempt"] = a.get("next_attempt", 0)
        item["auth_block"] = bool(a.get("auth_block", False))
        accounts.append(item)
    tickets = [
        {
            k: t.get(k)
            for k in ("account_id", "model", "length", "issued_at", "expires_at")
        }
        for t in d.get("valid_tickets", [])
    ]
    return {
        "server_time": now,
        "updated_at": d.get("at"),
        "accounts": accounts,
        "tickets": tickets,
        "events": tail_events(),
        "policy": {
            "check_seconds": 20,
            "account_interval": 20,
            "renew_interval": 300,
            "routes_per_cycle": 1,
            "concurrency": 3,
        },
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
        ("127.0.0.1", int(os.environ.get("STATE_MONITOR_PORT", "17843"))), Handler
    ).serve_forever()
