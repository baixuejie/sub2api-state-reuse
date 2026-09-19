"""Verified candidate format and atomic handoff to the plugin."""

import base64, hashlib, json, os, time
import settings

MODEL = settings.MODEL


def candidate(a, value):
    try:
        b = base64.b64decode(value, altchars=b"-_", validate=True)
        issued = int.from_bytes(b[1:9], "big")
        if (
            (len(value), len(b)) not in ((292, 217), (332, 249))
            or b[0] != 128
            or not time.time() - 3000 < issued <= time.time() + 30
        ):
            return None
        return {
            "account_id": a["id"],
            "model": MODEL,
            "credential_hash": a["hash"],
            "issued": issued,
            "value": value,
        }
    except (ValueError, IndexError):
        return None


def queue_ticket(store, t):
    p = store.parent / "incoming"
    p.mkdir(mode=0o700, exist_ok=True)
    set_owner(p)
    key = f"{t['account_id']}:{t['model']}:{t['credential_hash']}"
    f = p / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(t))
    os.chmod(tmp, 0o600)
    set_owner(tmp)
    tmp.replace(f)


def set_owner(path):
    current = path.stat()
    if (current.st_uid, current.st_gid) != (settings.PLUGIN_UID, settings.PLUGIN_GID):
        os.chown(path, settings.PLUGIN_UID, settings.PLUGIN_GID)
