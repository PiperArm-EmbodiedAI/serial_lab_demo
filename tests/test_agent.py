from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from labflow.agent import Agent
from labflow.common import write_json


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "module.json"
        write_json(self.config, {"node_id": "test_node", "server_url": "http://127.0.0.1:1",
            "state_dir": "state", "steps": [{"step_id": "step", "order": 10, "handler": "work"}]})
        self.calls = []
        self.release = threading.Event()
        def work(context):
            self.calls.append(context["task_id"])
            self.release.wait(2)
            return {"ok": True}
        self.handlers = {"work": work}
        self.agent = Agent(self.config, self.handlers)

    def tearDown(self):
        self.release.set()
        if self.agent.thread:
            self.agent.thread.join()
        self.agent.store.close()
        self.agent.process_lock.close()
        self.temp.cleanup()

    def task(self):
        return {"run_id": "run_1", "task_id": uuid.uuid4().hex, "step_id": "step", "params": {},
                "previous_results": {}, "config_hash": self.agent.config_hash,
                "deadline": time.time() + 10, "expires_in_seconds": 10}

    def test_duplicate_task_executes_once_even_after_restart(self):
        task = self.task()
        self.agent.accept(task)
        self.agent.accept(task)
        self.release.set()
        self.agent.thread.join()
        self.agent.accept(task)
        self.assertEqual(self.calls, [task["task_id"]])
        self.agent.store.close()
        self.agent.process_lock.close()
        self.agent = Agent(self.config, self.handlers)
        self.agent.accept(task)
        self.assertEqual(self.calls, [task["task_id"]])

    def test_outbox_retries_keep_event_id_and_do_not_rerun(self):
        task = self.task()
        self.release.set()
        self.agent.accept(task)
        self.agent.thread.join()
        event_ids = [e["event_id"] for e in self.agent.store.read()["outbox"]]
        received = []
        class LostAck:
            def call(self, *args):
                received.append(args[-1]["event_id"])
                raise OSError("lost acknowledgement")
        self.agent.client = LostAck()
        for _ in range(2):
            with self.assertRaises(OSError):
                self.agent.flush()
        self.assertEqual(received, [event_ids[0], event_ids[0]])
        self.assertEqual(self.calls, [task["task_id"]])
        class Ack:
            def call(self, *args):
                return {"ok": True}
        self.agent.client = Ack()
        self.agent.flush()
        self.assertEqual(self.agent.store.read()["outbox"], [])

    def test_crash_journal_becomes_unknown_and_never_executes(self):
        task = self.task()
        with self.agent.store.edit() as state:
            state["tasks"][task["task_id"]] = {"task": task, "status": "running"}
        self.agent.store.close()
        self.agent.process_lock.close()
        self.agent = Agent(self.config, self.handlers)
        self.agent.accept(task)
        self.agent.accept(self.task())
        self.assertTrue(self.agent.busy())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.agent.store.read()["outbox"][-1]["status"], "unknown")

    def test_duplicate_state_directory_is_locked(self):
        with self.assertRaises(RuntimeError):
            Agent(self.config, self.handlers)

    def test_invalid_result_is_a_failure(self):
        self.agent.handlers["work"] = lambda _: False
        task = self.task()
        self.agent.accept(task)
        self.agent.thread.join()
        self.assertEqual(self.agent.store.read()["tasks"][task["task_id"]]["status"], "failed")

    def test_existing_command_needs_no_imports_from_framework(self):
        task = self.task()
        script = self.root / "existing.py"
        script.write_text("print('ordinary script')\n", encoding="utf-8")
        self.agent.steps["step"] = {"command": ["{python}", str(script)]}
        self.agent.accept(task)
        self.agent.thread.join()
        record = self.agent.store.read()["tasks"][task["task_id"]]
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"], {"exit_code": 0})


if __name__ == "__main__":
    unittest.main()
