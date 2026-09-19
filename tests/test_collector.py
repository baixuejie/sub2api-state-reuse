import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import local_ip_harvest as harvest
import ticket_store
import settings


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(harvest, "EVENTS", Path(self.temp.name) / "events.jsonl")
        p.start()
        self.addCleanup(p.stop)
        self.account = {"id": 101, "hash": "a" * 64}
        self.routes = [{"key": str(i), "name": str(i)} for i in range(5)]

    def test_denied_credentials_are_not_retried(self):
        state = {}
        with patch.object(
            harvest, "request", return_value=({"stop": True, "auth_block": True}, "")
        ) as request:
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertEqual(request.call_count, 1)

    def test_rate_limit_does_not_rotate(self):
        state = {}
        with patch.object(
            harvest, "request", return_value=({"stop": True, "cooldown": 7200}, "")
        ) as request:
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertEqual(request.call_count, 1)
            self.assertGreater(
                state["101:" + "a" * 64]["next_attempt"], time.time() + 7100
            )

    def test_failed_verification_never_queues(self):
        with patch.object(
            harvest,
            "request",
            side_effect=[
                ({"completed": True, "actual_model": settings.MODEL}, "test"),
                ({"completed": False}, ""),
            ],
        ), patch.object(
            ticket_store, "candidate", return_value={"value": "test"}
        ), patch.object(ticket_store, "queue_ticket") as queue:
            harvest.collect(
                self.account, self.routes[:1], {}, None, time.monotonic() + 150
            )
            queue.assert_not_called()

    def test_success_remembers_route(self):
        state = {}
        with patch.object(
            harvest,
            "request",
            return_value=({"completed": True, "actual_model": settings.MODEL}, "test"),
        ), patch.object(
            ticket_store, "candidate", return_value={"value": "test"}
        ), patch.object(ticket_store, "queue_ticket") as queue:
            result = harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertTrue(result["captured"])
            queue.assert_called_once()
            self.assertEqual(state["101:" + "a" * 64]["preferred"], "0")

    def test_rotates_at_most_three(self):
        state = {}
        with patch.object(
            harvest,
            "request",
            return_value=({"completed": True, "actual_model": "other-model"}, ""),
        ) as request:
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertEqual(request.call_count, 3)
            self.assertEqual(state["101:" + "a" * 64]["cursor"], 3)

    def test_shapes_age_and_owner_handoff(self):
        for size in (217, 249, 233, 265):
            raw = b"\x80" + int(time.time()).to_bytes(8, "big") + bytes(size - 9)
            candidate = ticket_store.candidate(
                self.account, base64.urlsafe_b64encode(raw).decode()
            )
            self.assertEqual(candidate is not None, size in (217, 249))
            if candidate:
                with patch.object(settings, "PLUGIN_UID", os.getuid()), patch.object(
                    settings, "PLUGIN_GID", os.getgid()
                ):
                    ticket_store.queue_ticket(
                        Path(self.temp.name) / "tickets.json", candidate
                    )
        files = list((Path(self.temp.name) / "incoming").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        old = b"\x80" + int(time.time() - 3600).to_bytes(8, "big") + bytes(208)
        self.assertIsNone(
            ticket_store.candidate(self.account, base64.urlsafe_b64encode(old).decode())
        )

    def test_event_redaction(self):
        harvest.emit(
            101,
            "test",
            source="node-a",
            value="SENSITIVE",
            password="SENSITIVE",
            authorization="SENSITIVE",
        )
        text = harvest.EVENTS.read_text()
        self.assertNotIn("SENSITIVE", text)
        self.assertEqual(json.loads(text)["source"], "node-a")

    def test_missing_clash_file_allows_ip_management(self):
        class Fake:
            @staticmethod
            def sql(_):
                return [
                    {
                        "id": 1,
                        "name": "example",
                        "protocol": "socks5",
                        "host": "192.0.2.1",
                        "port": 1080,
                        "username": "u@x",
                        "password": "p:x",
                    }
                ]

        with patch.object(
            settings, "ROUTES_FILE", Path(self.temp.name) / "missing.json"
        ):
            routes = harvest.routes_for(Fake)
        self.assertEqual(routes[0]["url"], "socks5h://u%40x:p%3Ax@192.0.2.1:1080")


if __name__ == "__main__":
    unittest.main()
