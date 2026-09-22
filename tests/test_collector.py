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
            harvest, "request", return_value=({"stop": True, "auth_block": True}, "", [])
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
            harvest, "request", return_value=({"stop": True, "cooldown": 7200}, "", [])
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
                ({"completed": True, "actual_model": settings.MODEL}, "test", []),
                ({"completed": False}, "", []),
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
            return_value=({"completed": True, "actual_model": settings.MODEL}, "test", []),
        ), patch.object(
            ticket_store, "candidate", return_value={"value": "test"}
        ), patch.object(ticket_store, "queue_ticket") as queue:
            result = harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertTrue(result["captured"])
            queue.assert_called_once()
            self.assertEqual(state["101:" + "a" * 64]["preferred"], "0")

    def test_rotates_one_route_per_cycle(self):
        state = {}
        with patch.object(
            harvest,
            "request",
            return_value=({"completed": True, "actual_model": "other-model"}, "", []),
        ) as request:
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertEqual(request.call_count, 1)
            self.assertEqual(state["101:" + "a" * 64]["cursor"], 1)

    def test_missing_and_renewal_intervals(self):
        self.assertEqual(harvest.retry_interval("missing"), 20)
        self.assertEqual(
            harvest.retry_interval("renew_due"), settings.TICKET_REFRESH_INTERVAL_SECONDS
        )

    def test_failed_preferred_route_rotates_after_twenty_seconds(self):
        state = {"101:" + "a" * 64: {"preferred": "0"}}
        response = ({"completed": True, "actual_model": "other-model"}, "", [])
        with patch.object(
            harvest, "request", return_value=response
        ) as request, patch.object(harvest.time, "time", return_value=1000):
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            st = state["101:" + "a" * 64]
            self.assertEqual(st["next_attempt"], 1020)
            self.assertNotIn("preferred", st)
            self.assertEqual(request.call_args.args[1]["key"], "0")
        with patch.object(
            harvest, "request", return_value=response
        ) as request, patch.object(harvest.time, "time", return_value=1019):
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            request.assert_not_called()
        with patch.object(
            harvest, "request", return_value=response
        ) as request, patch.object(harvest.time, "time", return_value=1020):
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            self.assertEqual(request.call_args.args[1]["key"], "1")

    def test_long_cooldown_survives_fast_schedule(self):
        state = {"101:" + "a" * 64: {"next_attempt": time.time() + 7200}}
        with patch.object(harvest, "request") as request:
            harvest.collect(
                self.account, self.routes, state, None, time.monotonic() + 150
            )
            request.assert_not_called()

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
        ), patch.object(
            settings,
            "DYNAMIC_PROXY_GENERATORS_FILE",
            Path(self.temp.name) / "missing-generators.json",
        ):
            routes = harvest.routes_for(Fake)
        self.assertEqual(routes[0]["url"], "socks5h://u%40x:p%3Ax@192.0.2.1:1080")

    def test_dynamic_generator_accepts_only_ip_and_port(self):
        config = Path(self.temp.name) / "generators.json"
        config.write_text(json.dumps([{"name": "rotating", "url": "https://example"}]))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            @staticmethod
            def read(_):
                return b"192.0.2.10:8080\n"

        with patch.object(
            settings, "DYNAMIC_PROXY_GENERATORS_FILE", config
        ), patch.object(harvest.urllib.request, "urlopen", return_value=Response()):
            routes = harvest.dynamic_routes()
        self.assertEqual(
            routes,
            [
                {
                    "key": "generator:rotating",
                    "name": "动态IP/rotating",
                    "url": "http://192.0.2.10:8080",
                }
            ],
        )

    def test_dynamic_generator_rejects_non_ip_response(self):
        config = Path(self.temp.name) / "generators.json"
        config.write_text(json.dumps([{"url": "https://example"}]))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            @staticmethod
            def read(_):
                return b"proxy.example:8080\n"

        with patch.object(
            settings, "DYNAMIC_PROXY_GENERATORS_FILE", config
        ), patch.object(harvest.urllib.request, "urlopen", return_value=Response()):
            self.assertEqual(harvest.dynamic_routes(), [])

    def test_dynamic_generator_alternates_with_static_routes(self):
        routes = [
            {"key": "generator:rotating", "name": "dynamic"},
            {"key": "clash:1", "name": "static-1"},
            {"key": "clash:2", "name": "static-2"},
        ]
        self.assertEqual(harvest.order_routes(routes, {})[0]["key"], "generator:rotating")
        state = {"last_route": "generator:rotating", "cursor": 1}
        self.assertEqual(harvest.order_routes(routes, state)[0]["key"], "clash:1")
        state = {"last_route": "clash:1", "cursor": 2}
        self.assertEqual(harvest.order_routes(routes, state)[0]["key"], "generator:rotating")

    def test_captured_cookies_are_replayed_on_verification(self):
        with patch.object(
            harvest,
            "request",
            side_effect=[
                ({"completed": True, "actual_model": settings.MODEL}, "test", ["session=abc", "other=xyz"]),
                ({"completed": True, "actual_model": settings.MODEL}, "test", []),
            ],
        ) as request, patch.object(
            ticket_store, "candidate", return_value={"value": "test", "cookies": ["session=abc", "other=xyz"]}
        ), patch.object(ticket_store, "queue_ticket") as queue:
            result = harvest.collect(
                self.account, self.routes[:1], {}, None, time.monotonic() + 150
            )
            self.assertTrue(result["captured"])
            self.assertEqual(
                request.call_args_list[1].kwargs.get("cookies"),
                ["session=abc", "other=xyz"],
            )
            queued = queue.call_args.args[1]
            self.assertEqual(queued["cookies"], ["session=abc", "other=xyz"])

    def test_candidate_keeps_name_value_cookies_only_from_headers(self):
        raw = b"\x80" + int(time.time()).to_bytes(8, "big") + bytes(240)
        value = base64.urlsafe_b64encode(raw).decode()
        cookies = harvest.local_cookies(
            "HTTP/1.1 200 OK\r\nset-cookie: session=abc; Path=/; HttpOnly\r\n"
            "SET-COOKIE: other=xyz; Secure\r\nX-Ignore: 1\r\n\r\n"
        )
        candidate = ticket_store.candidate(self.account, value, cookies=cookies)
        self.assertEqual(candidate["cookies"], ["session=abc", "other=xyz"])
        self.assertIsNone(
            ticket_store.candidate(
                self.account, value, cookies=["not from headers"], model="other"
            )
        )


if __name__ == "__main__":
    unittest.main()
