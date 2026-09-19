import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "state_monitor", Path(__file__).resolve().parents[1] / "monitor/server.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def test_admin_authorization(self):
        server = monitor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), monitor.Handler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/fn-state/api"
        fetch = urllib.request.urlopen
        try:
            with self.assertRaises(urllib.error.HTTPError) as result:
                fetch(url)
            self.assertEqual(result.exception.code, 401)
            with patch.object(
                monitor.urllib.request,
                "urlopen",
                return_value=io.BytesIO(
                    json.dumps({"data": {"role": "user"}}).encode()
                ),
            ):
                with self.assertRaises(urllib.error.HTTPError) as result:
                    fetch(
                        urllib.request.Request(
                            url, headers={"Authorization": "Bearer synthetic"}
                        )
                    )
                self.assertEqual(result.exception.code, 403)
            with patch.object(
                monitor.urllib.request,
                "urlopen",
                return_value=io.BytesIO(
                    json.dumps({"data": {"role": "admin"}}).encode()
                ),
            ), patch.object(monitor, "snapshot", return_value={"events": []}):
                self.assertEqual(
                    fetch(
                        urllib.request.Request(
                            url, headers={"Authorization": "Bearer synthetic"}
                        )
                    ).status,
                    200,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_event_whitelist(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            monitor, "ROOT", Path(temp)
        ):
            (Path(temp) / "harvest-events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "test",
                        "account_id": 101,
                        "value": "SECRET",
                        "credential_hash": "SECRET",
                        "password": "SECRET",
                    }
                )
                + "\n{partial"
            )
            rows = monitor.tail_events()
            self.assertEqual(rows, [{"event": "test", "account_id": 101}])


if __name__ == "__main__":
    unittest.main()
