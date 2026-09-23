from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from labflow.common import ApiError, Client, digest
from labflow.coordinator import Coordinator
from labflow.http_api import make_server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.coordinator = Coordinator(Path(self.temp.name) / "state.sqlite3")
        self.server = make_server(self.coordinator, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = Client(self.url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.coordinator.store.close()
        self.temp.cleanup()

    def test_health_and_state_are_directly_accessible(self):
        self.assertTrue(self.client.call("GET", "/health")["ok"])
        self.assertEqual(self.client.call("GET", "/api/plan")["steps"], [])

    def test_ui_is_served_and_control_routes_work(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.url + "/") as response:
            self.assertIn("Run selected steps", response.read().decode("utf-8"))
        identity = {"node_id": "a", "instance_id": "a1", "busy": False}
        self.client.call("POST", "/api/register", dict(identity, config_hash=digest("a"),
            steps=[{"step_id": "a_step", "order": 10}]))
        plan = self.client.call("GET", "/api/plan")
        self.assertIn("Test step", opener.open(self.url + "/").read().decode("utf-8"))
        run = self.client.call("POST", "/api/runs", {"plan_version": plan["plan_version"],
            "step_ids": ["a_step"], "mode": "single"})
        path = "/api/runs/" + run["run_id"]
        self.assertEqual(run["mode"], "single")
        self.assertEqual(self.client.call("POST", path + "/pause-after-current", {})["status"], "paused")
        self.assertEqual(self.client.call("POST", path + "/resume", {})["status"], "running")
        self.assertEqual(self.client.call("POST", path + "/pause", {})["status"], "paused")
        self.assertIsNone(self.client.call("POST", "/api/poll", identity)["task"])
        self.assertEqual(self.client.call("POST", path + "/resume", {})["status"], "running")
        self.assertIsNotNone(self.client.call("POST", "/api/poll", identity)["task"])
        closed = self.client.call("POST", path + "/close", {"physical_checked": True, "note": "mock inspected"})
        self.assertEqual(closed["status"], "closed")

    def test_invalid_registration_is_a_client_error(self):
        with self.assertRaises(ApiError) as error:
            self.client.call("POST", "/api/register", {"node_id": "broken"})
        self.assertEqual(error.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
