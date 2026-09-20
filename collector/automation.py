"""Shared plugin switch for enrollment, collection and scheduling."""

import threading
import time


class AutomationStopped(RuntimeError):
    pass


class PluginSwitch:
    def __init__(self, fetch, initial=None):
        self.fetch = fetch
        self.plugin = initial or {}
        self.checked_at = 0.0
        self.stopped = False
        self.lock = threading.Lock()

    def enabled(self, force=False):
        with self.lock:
            if self.stopped:
                return False
            now = time.monotonic()
            if force or now - self.checked_at >= 1:
                try:
                    self.plugin = self.fetch()
                except Exception:
                    self.stopped = True
                    return False
                self.checked_at = now
            if self.plugin.get("plugin_key") != "local.flownode.state-reuse" or self.plugin.get("state") != "enabled":
                self.stopped = True
                return False
            return True

    def require(self):
        if not self.enabled(force=True):
            raise AutomationStopped("plugin automation is paused")
