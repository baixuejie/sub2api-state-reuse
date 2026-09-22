"""Verified egress observations; proxy listener addresses are never egress proof."""
import ipaddress
import json

FIELDS = ("egress_ip", "egress_status", "attempt_no", "request_no", "ip_changes", "ip_changed")


def transfer_results(stdout):
    results = {}
    for line in (stdout or "").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("transfer") in ("upstream", "trace"):
                results[value["transfer"]] = value
        except (ValueError, TypeError):
            pass
    return results


def observation(transfers, trace_body):
    upstream, trace = transfers.get("upstream", {}), transfers.get("trace", {})
    # A new curl process performs exactly two sequential requests to chatgpt.com.
    # Zero new connections on the second transfer proves its CONNECT/TLS tunnel
    # is the one used for the first request. HTTP/1.1 disables stream multiplexing.
    if not upstream or upstream.get("exitcode") != 0:
        return {"egress_status": "request_failed"}
    if trace.get("exitcode") != 0 or trace.get("http_code") != 200:
        return {"egress_status": "trace_unavailable"}
    if trace.get("num_connects") != 0:
        return {"egress_status": "connection_changed"}
    values = dict(line.split("=", 1) for line in trace_body.splitlines() if "=" in line)
    try:
        address = ipaddress.ip_address(values.get("ip", ""))
        if values.get("h") != "chatgpt.com" or not address.is_global:
            raise ValueError()
    except ValueError:
        return {"egress_status": "trace_unavailable"}
    return {"egress_ip": str(address), "egress_status": "confirmed"}


def counters(state, account_id, model):
    # Account/model scope intentionally survives OAuth credential refreshes.
    return state.setdefault("_egress", {}).setdefault(f"{account_id}:{model}", {
        "attempts": 0, "requests": 0, "ip_changes": 0,
    })


def record(stats, result):
    stats["requests"] += 1
    address = result.get("egress_ip") if result.get("egress_status") == "confirmed" else None
    changed = bool(address and stats.get("last_confirmed_ip") and stats["last_confirmed_ip"] != address)
    if address:
        stats["ip_changes"] += int(changed)
        stats["last_confirmed_ip"] = address
    stats["egress_status"] = result.get("egress_status", "trace_unavailable")
    result.update(attempt_no=stats["attempts"], request_no=stats["requests"],
                  ip_changes=stats["ip_changes"], ip_changed=changed)
