import os, datetime
import ipaddress
import json, time, subprocess, tempfile, pathlib, urllib.parse
import urllib.request
import ticket_store as mh
import settings
import egress
from automation import AutomationStopped

MODEL = settings.MODEL
EVENTS = settings.ROOT / "harvest-events.jsonl"


def emit(account_id, event, **fields):
    allowed = (
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
    )
    row = {
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "account_id": account_id,
        "event": event,
    }
    row.update({k: v for k, v in fields.items() if k in allowed + egress.FIELDS})
    line = json.dumps(row, ensure_ascii=False) + "\n"
    EVENTS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(EVENTS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    print(line.strip(), flush=True)


def run_curl(args, cfg, should_run=None):
    if should_run is None:
        return subprocess.run(args, input=cfg, text=True, capture_output=True, timeout=40)
    if not should_run():
        raise AutomationStopped()
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        deadline = time.monotonic() + 40
        first = True
        try:
            while True:
                if not should_run():
                    raise AutomationStopped()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, 35)
                try:
                    stdout, stderr = process.communicate(input=cfg if first else None, timeout=0.5)
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    first = False
        except BaseException:
            process.kill()
            process.communicate()
            raise


def local_cookies(hs):
    # Keep only name=value pairs from dumped response headers; cookie
    # attributes (Path, HttpOnly, ...) stay local to the exit that set them.
    cookies = [
        l.split(":", 1)[1].strip().split(";")[0]
        for l in hs.splitlines()
        if l.lower().startswith("set-cookie:")
    ]
    return [c for c in cookies if c]


def request(a, r, ticket=None, cookies=None, should_run=None, model=MODEL):
    if model not in settings.MODELS:
        raise ValueError("unsupported probe model")
    out = {"source": r["name"], "phase": "verify" if ticket else "capture", "model": model}
    body = {
        "model": model,
        "instructions": "Reply with OK.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply with OK."}],
            }
        ],
        "stream": True,
        "store": False,
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
    }
    headers = {
        "Authorization": "Bearer " + a["token"],
        "ChatGPT-Account-Id": a["account"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "codex_cli_rs/0.155.0",
        "Version": "0.155.0",
        "Originator": "codex_cli_rs",
    }
    if ticket:
        headers["X-Codex-Turn-State"] = ticket
    # Upstream pairs each issued state with session cookies; replay them
    # whenever the bundled state is sent again.
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    def q(v):
        return (
            '"'
            + v.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            + '"'
        )

    with tempfile.TemporaryDirectory(prefix="state-task-") as d:
        h = pathlib.Path(d) / "headers"
        b = pathlib.Path(d) / "body"
        trace = pathlib.Path(d) / "trace"
        cfg = "\n".join(
            [
                'url = "https://chatgpt.com/backend-api/codex/responses"',
                "proxy = " + q(r["url"]),
                'noproxy = ""',
                'request = "POST"',
                "data = " + q(json.dumps(body)),
                "dump-header = " + q(str(h)),
                "output = " + q(str(b)),
                "http1.1",
                'max-time = "30"',
                'connect-timeout = "10"',
                "write-out = " + q('{"transfer":"upstream","exitcode":%{exitcode}}\n'),
            ]
            + ["header = " + q(k + ": " + v) for k, v in headers.items()]
        )
        cfg += "\nnext\n" + "\n".join([
            'url = "https://chatgpt.com/cdn-cgi/trace"',
            "proxy = " + q(r["url"]), 'noproxy = ""', "http1.1",
            'max-time = "5"', 'connect-timeout = "3"', 'max-filesize = "4096"',
            "output = " + q(str(trace)),
            "write-out = " + q('{"transfer":"trace","exitcode":%{exitcode},"http_code":%{http_code},"num_connects":%{num_connects}}\n'),
        ])
        p = run_curl(
            [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--connect-timeout",
                "10",
                "--config",
                "-",
            ],
            cfg,
            should_run,
        )
        hs = h.read_text() if h.exists() else ""
        bs = b.read_text(errors="replace") if b.exists() else ""
        transfers = egress.transfer_results(p.stdout)
        request_code = transfers.get("upstream", {}).get("exitcode", p.returncode)
        out.update(egress.observation(transfers, trace.read_text(errors="replace") if trace.exists() else ""))
        codes = [int(l.split()[1]) for l in hs.splitlines() if l.startswith("HTTP/")]
        code = codes[-1] if codes else 0
        out["http"] = code
        value = next(
            (
                l.split(":", 1)[1].strip()
                for l in reversed(hs.splitlines())
                if l.lower().startswith("x-codex-turn-state:")
            ),
            "",
        )
        out["length"] = len(value)
        if request_code:
            out["transport_error"] = request_code
        if code in (401, 403, 429):
            out["stop"] = True
            out["auth_block"] = code in (401, 403)
            out["cooldown"] = 3600
            try:
                err = json.loads(bs).get("error", {})
                retry = next(
                    (
                        l.split(":", 1)[1].strip()
                        for l in hs.splitlines()
                        if l.lower().startswith("retry-after:")
                    ),
                    "0",
                )
                from email.utils import parsedate_to_datetime

                try:
                    retry_seconds = int(retry)
                except ValueError:
                    retry_seconds = int(
                        parsedate_to_datetime(retry).timestamp() - time.time()
                    )
                out["cooldown"] = max(
                    300,
                    int(err.get("resets_in_seconds") or 0),
                    int(err.get("resets_at") or 0) - int(time.time()),
                    retry_seconds,
                )
            except (ValueError, TypeError, AttributeError):
                pass
        complete = False
        failed = False
        for l in bs.splitlines():
            if not l.startswith("data:"):
                continue
            try:
                e = json.loads(l[5:])
            except ValueError:
                continue
            if e.get("type") == "response.completed":
                complete = True
                out["actual_model"] = e.get("response", {}).get("model")
            if e.get("type") in ("error", "response.failed", "response.incomplete"):
                failed = True
                out["error_code"] = (
                    e.get("error") or e.get("response", {}).get("error") or {}
                ).get("code")
                if out["error_code"] in (
                    "usage_limit_reached",
                    "rate_limit_exceeded",
                    "insufficient_quota",
                    "invalid_api_key",
                    "token_expired",
                    "account_deactivated",
                ):
                    out["stop"] = True
                    out["auth_block"] = out["error_code"] in (
                        "invalid_api_key",
                        "token_expired",
                        "account_deactivated",
                    )
                    out["cooldown"] = 3600
        out["completed"] = complete and not failed and request_code == 0
    return out, value, local_cookies(hs)


def resolve_route(route):
    """Resolve a configured generator once per capture; reuse it for verification."""
    if not route.get("generator_url"):
        return route
    endpoint = urllib.parse.urlsplit(route["generator_url"])
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.fragment:
        raise ValueError("invalid generator URL")
    with urllib.request.urlopen(route["generator_url"], timeout=10) as response:
        if response.status != 200:
            raise ValueError("generator HTTP error")
        raw = response.read(4097)
    if len(raw) > 4096:
        raise ValueError("oversized generator response")
    line = next((line.strip() for line in raw.decode().splitlines() if line.strip()), "")
    host, port = line.rsplit(":", 1)
    address = ipaddress.ip_address(host.strip("[]"))
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid generator port")
    host = f"[{address}]" if address.version == 6 else str(address)
    return dict(route, url=f"http://{host}:{int(port)}")


def dynamic_routes():
    if not settings.DYNAMIC_PROXY_GENERATORS_FILE.exists():
        return []
    try:
        generators = json.loads(settings.DYNAMIC_PROXY_GENERATORS_FILE.read_text())
    except (OSError, ValueError):
        return []
    routes = []
    for index, generator in enumerate(generators):
        try:
            with urllib.request.urlopen(generator["url"], timeout=10) as response:
                raw = response.read(4097)
            if len(raw) > 4096:
                continue
            endpoint = next(
                (line.strip() for line in raw.decode().splitlines() if line.strip()),
                "",
            )
            host, port_text = endpoint.rsplit(":", 1)
            ipaddress.ip_address(host)
            port = int(port_text)
            if not 1 <= port <= 65535:
                continue
            name = str(generator.get("name") or f"generator-{index + 1}")
            routes.append(
                {
                    "key": "generator:" + name,
                    "name": "动态IP/" + name,
                    "url": "http://" + endpoint,
                }
            )
        except (OSError, ValueError, KeyError, UnicodeError):
            continue
    return routes


def routes_for(m):
    proxies = m.sql(
        "select coalesce(json_agg(row_to_json(p)),'[]'::json) from proxies p where status='active' and deleted_at is null and (expires_at is null or expires_at>now())"
    )
    clash = (
        json.loads(settings.ROUTES_FILE.read_text())
        if settings.ROUTES_FILE.exists()
        else []
    )
    routes = [
        {
            "key": "clash:" + str(r["port"]),
            "name": "Clash/" + r["name"],
            "url": f"http://127.0.0.1:{int(r['port'])}",
        }
        for r in clash
    ]
    ip = []
    for p in proxies:
        if p["protocol"] not in ("http", "https", "socks5", "socks5h"):
            continue
        auth = (
            (
                urllib.parse.quote(p["username"], safe="")
                + ":"
                + urllib.parse.quote(p["password"] or "", safe="")
                + "@"
            )
            if p.get("username")
            else ""
        )
        scheme = "socks5h" if p["protocol"] == "socks5" else p["protocol"]
        ip.append(
            {
                "key": "ip:" + str(p["id"]),
                "name": "IP管理/" + p["name"],
                "url": f"{scheme}://{auth}{p['host']}:{p['port']}",
            }
        )
    # Interleave both sources; the first configured Clash route is tried first.
    merged = []
    for i in range(max(len(routes), len(ip))):
        if i < len(routes):
            merged.append(routes[i])
        if i < len(ip):
            merged.append(ip[i])
    return dynamic_routes() + merged


def order_routes(routes, st):
    if not routes:
        return []
    cursor = st.get("cursor", 0) % len(routes)
    ordered = routes[cursor:] + routes[:cursor]
    preferred = next((r for r in routes if r["key"] == st.get("preferred")), None)
    if preferred:
        ordered = [preferred] + [r for r in ordered if r != preferred]
    dynamic = next((r for r in routes if r["key"].startswith("generator:")), None)
    if dynamic and not preferred and dynamic["key"] != st.get("last_route"):
        ordered = [dynamic] + [r for r in ordered if r != dynamic]
    # Never pin a failed account to the same preferred route.
    if len(routes) > 1:
        ordered = [r for r in ordered if r["key"] != st.get("last_route")]
    return ordered[:1]


def retry_interval(status):
    # Valid bundles expire in minutes; refresh on the tight cadence, hunt for
    # missing ones on the collection cadence.
    return 20 if status == "missing" else settings.TICKET_REFRESH_INTERVAL_SECONDS


def account_key(a):
    return str(a["id"]) + ":" + a["hash"]


def model_key(a, model):
    # Keep the original Astra key so existing cooldowns and cursors survive.
    return account_key(a) if model == MODEL else account_key(a) + ":" + model


def shared_pause(state, a):
    shared = dict(state.get("_account_backoff", {}).get(account_key(a), {}))
    legacy = state.get(account_key(a), {})
    # Respect previously persisted Astra authorization/quota cooldowns when
    # introducing Sol. Changing model must never bypass those restrictions.
    shared["auth_block"] = bool(shared.get("auth_block") or legacy.get("auth_block"))
    shared["next_attempt"] = max(shared.get("next_attempt", 0),
        0 if legacy.get("retry_scoped") else legacy.get("next_attempt", 0))
    return shared


def collect(a, routes, state, store, deadline, interval=20, should_run=None, model=MODEL):
    legacy = state.get(account_key(a), {})
    if legacy and not legacy.get("retry_scoped"):
        state.setdefault("_account_backoff", {})[account_key(a)] = shared_pause(state, a)
        legacy["retry_scoped"] = True
    st = state.setdefault(model_key(a, model), {})
    stats = egress.counters(state, a["id"], model)
    st["retry_scoped"] = True
    shared = shared_pause(state, a)
    out = {
        "account_id": a["id"],
        "model": model,
        "captured": False,
        "probes": [],
        "harvest_proxy": "local-clash-and-ip-management",
    }
    if should_run is not None and not should_run():
        return dict(out, automation_stopped=True)
    if st.get("auth_block") or shared.get("auth_block") or max(st.get("next_attempt", 0), shared.get("next_attempt", 0)) > time.time():
        out["paused"] = True
        emit(
            a["id"],
            "paused",
            next_attempt=max(st.get("next_attempt", 0), shared.get("next_attempt", 0)),
            auth_block=bool(st.get("auth_block") or shared.get("auth_block")),
            model=model,
        )
        return out
    if time.monotonic() > deadline:
        out["budget_exhausted"] = True
        return out
    st["next_attempt"] = time.time() + interval
    for route in order_routes(routes, st):
        if should_run is not None and not should_run():
            return dict(out, automation_stopped=True)
        if time.monotonic() + 85 > deadline:
            break
        st["cursor"] = (routes.index(route) + 1) % len(routes)
        st["last_route"] = route["key"]
        # Restore preference only after a new capture and carry-ticket verification.
        if st.get("preferred") == route["key"]:
            st.pop("preferred", None)
        stats["attempts"] += 1
        emit(a["id"], "attempt_started", source=route["name"], phase="capture", model=model,
             attempt_no=stats["attempts"], request_no=stats["requests"]+1, ip_changes=stats["ip_changes"])
        try:
            route = resolve_route(route)
            r, value, cookies = request(a, route, should_run=should_run, model=model)
        except AutomationStopped:
            return dict(out, automation_stopped=True)
        except Exception as e:
            r = {"source": route["name"], "error": type(e).__name__, "model": model, "phase": "capture"}
            value = ""
            cookies = []
        egress.record(stats, r)
        out["probes"].append(r)
        emit(a["id"], "attempt_finished", **r)
        if r.get("stop"):
            st.update(
                auth_block=r.get("auth_block", False),
                next_attempt=time.time() + r.get("cooldown", 3600),
            )
            state.setdefault("_account_backoff", {})[account_key(a)] = {
                "auth_block": st["auth_block"], "next_attempt": st["next_attempt"]}
            break
        t = mh.candidate(a, value, cookies=cookies, model=model)
        if not (t and r.get("completed") and r.get("actual_model") == model):
            continue
        if should_run is not None and not should_run():
            return dict(out, automation_stopped=True)
        emit(a["id"], "attempt_started", source=route["name"], phase="verify", model=model,
             attempt_no=stats["attempts"], request_no=stats["requests"]+1, ip_changes=stats["ip_changes"])
        try:
            v, _, _ = request(a, route, value, cookies=cookies, should_run=should_run, model=model)
        except AutomationStopped:
            return dict(out, automation_stopped=True)
        except Exception as e:
            v = {"source": route["name"], "error": type(e).__name__, "model": model, "phase": "verify"}
        egress.record(stats, v)
        out["probes"].append(v)
        emit(a["id"], "attempt_finished", **v)
        if v.get("stop"):
            st.update(
                auth_block=v.get("auth_block", False),
                next_attempt=time.time() + v.get("cooldown", 3600),
            )
            state.setdefault("_account_backoff", {})[account_key(a)] = {
                "auth_block": st["auth_block"], "next_attempt": st["next_attempt"]}
            break
        if v.get("completed") and v.get("actual_model") == model:
            if should_run is not None and not should_run():
                return dict(out, automation_stopped=True)
            mh.queue_ticket(store, t)
            st["preferred"] = route["key"]
            out.update(captured=True, source=route["name"])
            emit(a["id"], "candidate_queued", captured=True, source=route["name"], model=model)
            break
    return out
