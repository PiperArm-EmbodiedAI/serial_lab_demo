from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import uuid

from labflow.common import ApiError, digest
from labflow.coordinator import Coordinator


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.now = 1000.0
        self.server = Coordinator(self.path, lease_seconds=15, clock=lambda: self.now)

    def tearDown(self):
        if self.server is not None:
            self.server.store.close()
        self.temp.cleanup()

    def registration(self, name, order, timeout=100):
        return {"node_id": name, "instance_id": "instance_" + name, "busy": False,
                "config_hash": digest(name), "steps": [{"step_id": "step_" + name,
                "order": order, "name": name, "params": {}, "timeout_seconds": timeout}]}

    def nodes(self):
        for name, order in [("a", 10), ("b", 20), ("c", 30)]:
            self.server.register(self.registration(name, order))

    def start(self):
        return self.server.start({"plan_version": self.server.plan()["plan_version"]})

    def poll(self, name, busy=False):
        return self.server.poll({"node_id": name, "instance_id": "instance_" + name, "busy": busy})["task"]

    def event_body(self, task, name, status="succeeded", result=None):
        return {"node_id": name, "instance_id": "instance_" + name,
                "task_id": task["task_id"], "run_id": task["run_id"], "event_id": uuid.uuid4().hex,
                "status": status, "result": result, "error": None}

    def test_registration_order_and_conflict_are_atomic(self):
        self.server.register(self.registration("c", 30))
        self.server.register(self.registration("a", 10))
        with self.assertRaises(ApiError):
            self.server.register(self.registration("b", 10))
        plan = self.server.plan()
        self.assertEqual([s["order"] for s in plan["steps"]], [10, 30])
        self.assertEqual(len(plan["nodes"]), 2)

    def test_step_id_conflict(self):
        self.nodes()
        registration = self.registration("d", 40)
        registration["steps"][0]["step_id"] = "step_a"
        with self.assertRaises(ApiError):
            self.server.register(registration)

    def test_start_rejects_stale_plan_and_offline_node(self):
        self.server.register(self.registration("a", 10))
        old_plan = self.server.plan()
        self.server.register(self.registration("b", 20))
        with self.assertRaises(ApiError):
            self.server.start({"plan_version": old_plan["plan_version"]})
        self.now += 16
        with self.assertRaises(ApiError):
            self.start()

    def test_selected_run_allows_omitted_offline_node_and_freezes_subset(self):
        self.nodes()
        self.now += 16
        self.server.register(self.registration("a", 10))
        self.server.register(self.registration("b", 20))
        plan = self.server.plan()
        run = self.server.start({"plan_version": plan["plan_version"], "step_ids": ["step_b"]})
        self.assertEqual(run["selected_step_ids"], ["step_b"])
        self.assertEqual([x["step_id"] for x in run["skipped_steps"]], ["step_a", "step_c"])
        self.assertEqual([t["step"]["step_id"] for t in run["tasks"]], ["step_b"])
        task = self.poll("b")
        self.assertEqual(task["previous_results"], {})
        self.server.event(self.event_body(task, "b"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "succeeded")

    def test_selected_run_validates_ids_and_only_selected_node_availability(self):
        self.nodes()
        plan = self.server.plan()
        payload = {"plan_version": plan["plan_version"], "step_ids": ["step_a"]}
        self.now += 16
        with self.assertRaises(ApiError):
            self.server.start(payload)
        self.server.register(self.registration("a", 10))
        plan = self.server.plan()
        for ids in ([], ["step_a", "step_a"], ["missing"]):
            with self.assertRaises(ApiError):
                self.server.start({"plan_version": plan["plan_version"], "step_ids": ids})
        run = self.server.start({"plan_version": plan["plan_version"], "step_ids": ["step_a"], "mode": "single"})
        self.assertEqual(run["mode"], "single")

    def test_strict_serial_duplicate_delivery_and_result_passing(self):
        self.nodes()
        run = self.start()
        self.assertIsNone(self.poll("b"))
        a = self.poll("a")
        self.assertEqual(a["task_id"], self.poll("a")["task_id"])
        self.assertIsNone(self.poll("b"))
        event = self.event_body(a, "a", result={"value": 42})
        self.server.event(event)
        self.assertTrue(self.server.event(event)["duplicate"])
        b = self.poll("b")
        self.assertEqual(b["previous_results"], {"step_a": {"value": 42}})
        self.assertIsNone(self.poll("c"))
        self.server.event(self.event_body(b, "b"))
        c = self.poll("c")
        self.server.event(self.event_body(c, "c"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "succeeded")

    def test_concurrent_start_has_one_winner(self):
        self.nodes()
        version = self.server.plan()["plan_version"]
        def start_one(_):
            try:
                return self.server.start({"plan_version": version})["run_id"]
            except ApiError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start_one, range(16)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_concurrent_claims_only_one_task_id(self):
        self.nodes()
        self.start()
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(lambda _: self.poll("a"), range(16)))
        self.assertEqual(len({t["task_id"] for t in tasks}), 1)
        self.assertIsNone(self.poll("b"))

    def test_running_plan_is_frozen_and_existing_config_cannot_change(self):
        self.nodes()
        run = self.start()
        self.server.register(self.registration("d", 15))
        self.assertEqual(len(self.server.status(run["run_id"])["tasks"]), 3)
        changed = self.registration("a", 11)
        with self.assertRaises(ApiError):
            self.server.register(changed)

    def test_failure_blocks_next_step(self):
        self.nodes()
        run = self.start()
        self.server.event(self.event_body(self.poll("a"), "a", "failed"))
        self.assertIsNone(self.poll("b"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "blocked")
        with self.assertRaises(ApiError):
            self.server.control(run["run_id"], "resume", {})

    def test_timeout_and_late_success_do_not_resume(self):
        self.server.register(self.registration("a", 10, timeout=1))
        self.server.register(self.registration("b", 20))
        run = self.start()
        task = self.poll("a")
        self.now += 2
        self.server.monitor()
        self.assertEqual(self.server.status(run["run_id"])["tasks"][0]["status"], "unknown")
        self.server.event(self.event_body(task, "a"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "blocked")
        self.assertIsNone(self.poll("b"))

    def test_heartbeat_expiry_blocks_and_does_not_reassign(self):
        self.nodes()
        run = self.start()
        self.poll("a")
        self.now += 16
        self.server.monitor()
        self.assertEqual(self.server.status(run["run_id"])["status"], "blocked")
        self.assertIsNone(self.poll("a"))

    def test_pause_only_stops_future_dispatch(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        self.server.control(run["run_id"], "pause", {})
        self.assertEqual(self.poll("a")["task_id"], task["task_id"])
        self.server.event(self.event_body(task, "a"))
        self.assertIsNone(self.poll("b"))
        self.server.control(run["run_id"], "resume", {})
        self.assertIsNotNone(self.poll("b"))

    def test_pause_after_current_waits_for_active_task_then_pauses(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        self.server.control(run["run_id"], "pause-after-current", {})
        self.assertEqual(self.server.status(run["run_id"])["status"], "pausing")
        self.assertEqual(self.poll("a")["task_id"], task["task_id"])
        self.server.event(self.event_body(task, "a"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "paused")
        self.assertIsNone(self.poll("b"))
        self.server.control(run["run_id"], "resume", {})
        self.assertIsNotNone(self.poll("b"))

    def test_pause_after_current_when_nothing_dispatched_pauses_immediately(self):
        self.nodes()
        run = self.start()
        self.server.control(run["run_id"], "pause-after-current", {})
        self.assertEqual(self.server.status(run["run_id"])["status"], "paused")
        self.assertIsNone(self.poll("a"))

    def test_failure_during_pause_request_still_blocks(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        self.server.control(run["run_id"], "pause-after-current", {})
        self.server.event(self.event_body(task, "a", "failed"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "blocked")

    def test_wrong_node_and_conflicting_event_cannot_advance(self):
        self.nodes()
        self.start()
        task = self.poll("a")
        with self.assertRaises(ApiError):
            self.server.event(self.event_body(task, "b"))
        event = self.event_body(task, "a", result={"x": 1})
        self.server.event(event)
        event["result"] = {"x": 2}
        with self.assertRaises(ApiError):
            self.server.event(event)
        with self.assertRaises(ApiError):
            self.server.event(self.event_body(task, "a", "failed"))

    def test_late_running_does_not_regress_terminal_state(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        self.server.event(self.event_body(task, "a"))
        self.server.event(self.event_body(task, "a", "running"))
        self.assertEqual(self.server.status(run["run_id"])["tasks"][0]["status"], "succeeded")

    def test_restart_preserves_state_and_requires_inspection(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        self.server.store.close()
        self.server = Coordinator(self.path, clock=lambda: self.now)
        status = self.server.status(run["run_id"])
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["tasks"][0]["status"], "unknown")
        self.assertEqual(status["mode"], "all")
        self.assertEqual(status["selected_step_ids"], ["step_a", "step_b", "step_c"])
        self.assertEqual(self.server.store.read()["schema_version"], 2)
        self.server.event(self.event_body(task, "a"))
        self.assertIsNone(self.poll("b"))

    def test_schema_migration_is_idempotent_and_preserves_nodes_and_runs(self):
        self.nodes()
        run = self.start()
        self.server.store.close()
        self.server = Coordinator(self.path, clock=lambda: self.now)
        self.server.store.close()
        self.server = Coordinator(self.path, clock=lambda: self.now)
        self.assertEqual(len(self.server.plan()["steps"]), 3)
        self.assertEqual(self.server.status(run["run_id"])["selected_step_ids"],
                         ["step_a", "step_b", "step_c"])

    def test_unsupported_future_schema_is_rejected(self):
        self.server.store.close()
        from labflow.common import JsonStore
        store = JsonStore(self.path, {"nodes": {}, "runs": [], "events": {}})
        with store.edit() as state:
            state["schema_version"] = 999
        store.close()
        with self.assertRaises(ApiError):
            Coordinator(self.path, clock=lambda: self.now)
        self.server = None

    def test_duplicate_node_process_and_restart(self):
        self.nodes()
        run = self.start()
        self.poll("a")
        duplicate = self.registration("a", 10)
        duplicate["instance_id"] = "replacement"
        with self.assertRaises(ApiError):
            self.server.register(duplicate)
        self.now += 16
        self.server.register(duplicate)
        self.assertEqual(self.server.status(run["run_id"])["status"], "blocked")
        with self.assertRaises(ApiError):
            self.poll("a")

    def test_close_is_explicit_and_late_events_cannot_restart_it(self):
        self.nodes()
        run = self.start()
        task = self.poll("a")
        with self.assertRaises(ApiError):
            self.server.control(run["run_id"], "close", {})
        self.server.control(run["run_id"], "close", {"physical_checked": True, "note": "mock action stopped"})
        self.server.event(self.event_body(task, "a"))
        self.assertIsNone(self.poll("b"))
        self.assertEqual(self.server.status(run["run_id"])["status"], "closed")

    def test_busy_node_cannot_join_a_new_run(self):
        body = self.registration("a", 10)
        body["busy"] = True
        self.server.register(body)
        with self.assertRaises(ApiError):
            self.start()

    def test_remove_node_requires_offline_and_no_active_run(self):
        self.nodes()
        with self.assertRaises(ApiError):
            self.server.remove_node("a")
        self.now += 16
        self.server.remove_node("a")
        self.assertEqual(len(self.server.plan()["steps"]), 2)


if __name__ == "__main__":
    unittest.main()
