"""The dashboard API on a demo engine: the loopback security rules, a valid state, and the retired
Windows / PC-export surface staying gone."""
import http.client
import json
import tempfile
import threading
import unittest

import tests.helpers  # noqa: F401  (safe mode before accountdeck loads)

from accountdeck import contract, demo, fixtures
from accountdeck.config import Settings
from accountdeck.core import DeckError
from accountdeck.server import Server


class DashboardAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ad2-test-")
        cls.engine = demo.build_demo(cls.temp.name, "typical", now=fixtures.BASE_NOW, start=False, inline=True)
        cls.server = Server(("127.0.0.1", 0), cls.engine)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.engine.stop()
        cls.temp.cleanup()

    def request(self, method, path, body=None, *, cookie=None, origin=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-AccountDeck": "1"})
            if origin:
                headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if data else None), response.getheader("Set-Cookie")

    def test_api_is_local_authenticated_and_serves_a_valid_mac_state(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/api/state", host="localhost")[0], 403)
        self.assertEqual(self.request("POST", "/api/session", {"token": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/session", {"token": self.server.token}, origin=False)[0], 403)
        status, _, cookie = self.request("POST", "/api/session", {"token": self.server.token})
        self.assertEqual(status, 200)
        cookie = cookie.split(";")[0]
        status, state, _ = self.request("GET", "/api/state", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(contract.validate_state(state), [])
        self.assertEqual(state["platform"]["os"], "mac")
        self.assertNotIn("v1", state)
        self.assertEqual(self.request("POST", "/api/action", {"action": "refresh"}, cookie=cookie)[0], 200)
        for retired in ("export-accounts", "confirm-moved", "import-v1", "retire-v1", "set-aumid"):
            status, body, _ = self.request("POST", "/api/action", {"action": retired}, cookie=cookie)
            self.assertEqual((status, body["code"]), (400, "invalid"), retired)
        self.assertEqual(self.request("GET", "/api/move-status", cookie=cookie)[0], 404)
        status, body, _ = self.request("POST", "/api/settings", {"patch": {"taskbar_widget": False}}, cookie=cookie)
        self.assertEqual((status, body["code"]), (400, "invalid"))

    def test_settings_ignore_retired_keys_and_fall_back_from_bad_values(self):
        saved = {}
        settings = Settings({"taskbar_widget": False, "claude_aumid": "x", "v1_retired_at": 1.0, "first_run_done": True,
                             "cli_threshold": 250, "tray": True, "cwd": "/Users/me/work"},
                            save=saved.__setitem__, home="/Users/me")
        public = settings.public()
        self.assertEqual((public["cli_threshold"], public["tray"], public["cwd"]), (95, True, "/Users/me/work"))
        self.assertFalse({"taskbar_widget", "claude_aumid", "v1_retired_at", "first_run_done"} & set(public))
        for patch in ({"taskbar_widget": True}, {"alert_latches": {}}, {"cwd": "C:\\Users\\me"}, {"cli_threshold": 100}):
            with self.assertRaises(DeckError):
                settings.update(patch)
        self.assertEqual(settings.update({"cli_threshold": 90}), {"cli_threshold": 90})
        self.assertEqual(saved, {"cli_threshold": 90})


if __name__ == "__main__":
    unittest.main()
