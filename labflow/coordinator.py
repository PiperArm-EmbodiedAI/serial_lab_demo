import copy
import re
import time
import uuid

from .common import JsonStore, digest, dumps, require


ACTIVE = {"running", "pausing", "paused", "stopping", "blocked"}
FINISHED = {"succeeded", "failed"}
COMPLETED_TASKS = {"succeeded", "skipped"}
STATE_VERSION = 3
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


class Coordinator:
    def __init__(self, db_path, lease_seconds=15, clock=time.time):
        self.store = JsonStore(db_path, {"nodes": {}, "runs": [], "events": {}})
        self.clock = clock
        self.lease = lease_seconds
        try:
            with self.store.edit() as state:
                # Idempotent migration from the v0.1.1 unversioned state document.
                state.setdefault("nodes", {})
                state.setdefault("runs", [])
                state.setdefault("events", {})
                version = state.get("schema_version", 1)
                require(type(version) is int and version <= STATE_VERSION,
                        "unsupported server state schema version")
                if version < STATE_VERSION:
                    for run in state["runs"]:
                        run.setdefault("mode", "all")
                        run.setdefault("selected_step_ids", [t["step"]["step_id"] for t in run["tasks"]])
                        run.setdefault("skipped_steps", [])
                    state["schema_version"] = STATE_VERSION
                for run in state["runs"]:
                    if run["status"] in ACTIVE:
                        self._block(run, "server_restarted: inspect the experiment before closing this run")
                        for task in run["tasks"]:
                            if task["status"] in {"assigned", "running"}:
                                task["status"] = "unknown"
                # Require a fresh heartbeat following every server restart.
                for node in state["nodes"].values():
                    node["last_seen"] = 0
        except BaseException:
            self.store.close()
            raise

    @staticmethod
    def _block(run, reason):
        run["status"] = "blocked"
        run["reason"] = reason

    @staticmethod
    def _active(state):
        return next((r for r in reversed(state["runs"]) if r["status"] in ACTIVE), None)

    @staticmethod
    def _run(state, run_id):
        run = next((r for r in state["runs"] if r["run_id"] == run_id), None)
        require(run is not None, "run not found", 404)
        return run

    @staticmethod
    def _current(run):
        return next((t for t in run["tasks"] if t["status"] not in COMPLETED_TASKS), None)

    def _online(self, node):
        return self.clock() - node["last_seen"] < self.lease

    def _identity(self, state, body):
        node = state["nodes"].get(body.get("node_id"))
        require(node is not None, "node must register first", 404)
        require(node["instance_id"] == body.get("instance_id"), "node instance was replaced", 409)
        return node

    def _plan(self, state):
        steps = []
        for node_id, node in state["nodes"].items():
            for step in node["steps"]:
                steps.append(dict(step, node_id=node_id, config_hash=node["config_hash"]))
        steps.sort(key=lambda x: x["order"])
        return steps

    def plan(self):
        state = self.store.read()
        steps = self._plan(state)
        return {"plan_version": digest(steps), "steps": steps,
                "nodes": [{"node_id": key, "online": self._online(value),
                           "busy": value.get("busy", False), "last_seen": value["last_seen"]}
                          for key, value in state["nodes"].items()]}

    def register(self, body):
        node_id = body.get("node_id")
        require(valid_id(node_id), "invalid node_id")
        require(valid_id(body.get("instance_id")), "invalid instance_id")
        require(isinstance(body.get("config_hash"), str) and len(body["config_hash"]) == 64,
                "config_hash must be a SHA-256 digest")
        steps = body.get("steps")
        require(isinstance(steps, list) and 0 < len(steps) <= 100, "steps must contain 1..100 steps")
        clean = []
        for step in steps:
            require(isinstance(step, dict), "step must be an object")
            require(valid_id(step.get("step_id")), "invalid step_id")
            require(type(step.get("order")) is int and step["order"] > 0, "order must be a positive integer")
            timeout = step.get("timeout_seconds", 600)
            require(type(timeout) in (int, float) and 0 < timeout <= 604800,
                    "timeout_seconds must be 0..604800")
            require(isinstance(step.get("params", {}), dict), "params must be an object")
            clean.append({"step_id": step["step_id"], "order": step["order"],
                          "name": str(step.get("name", step["step_id"]))[:200],
                          "timeout_seconds": timeout, "params": step.get("params", {})})
        require(len({s["step_id"] for s in clean}) == len(clean), "duplicate step_id in node")
        require(len({s["order"] for s in clean}) == len(clean), "duplicate order in node")
        with self.store.edit() as state:
            old = state["nodes"].get(node_id)
            active = self._active(state)
            if old:
                changed = old["config_hash"] != body["config_hash"] or old["steps"] != clean
                in_run = active and any(t["step"]["node_id"] == node_id for t in active["tasks"])
                require(not (changed and in_run), "cannot change a node used by an active run", 409)
                if old["instance_id"] != body["instance_id"]:
                    require(not self._online(old), "node_id is already online in another process", 409)
                    if in_run:
                        for task in active["tasks"]:
                            if task["step"]["node_id"] == node_id and task["status"] in {"assigned", "running"}:
                                task["status"] = "unknown"
                                self._block(active, "node_restarted: " + node_id)
            for other_id, other in state["nodes"].items():
                if other_id == node_id:
                    continue
                for step in clean:
                    require(all(s["step_id"] != step["step_id"] for s in other["steps"]),
                            "step_id already registered: " + step["step_id"], 409)
                    require(all(s["order"] != step["order"] for s in other["steps"]),
                            "order already registered: " + str(step["order"]), 409)
            state["nodes"][node_id] = {"instance_id": body["instance_id"], "steps": clean,
                "config_hash": body["config_hash"], "last_seen": self.clock(), "busy": bool(body.get("busy"))}
        return {"ok": True}

    def remove_node(self, node_id):
        with self.store.edit() as state:
            require(self._active(state) is None, "cannot remove nodes during an active run", 409)
            require(node_id in state["nodes"], "node not found", 404)
            require(not self._online(state["nodes"][node_id]), "stop the node and wait for its lease to expire", 409)
            del state["nodes"][node_id]
        return {"ok": True}

    def start(self, body):
        with self.store.edit() as state:
            require(self._active(state) is None, "an active run already exists", 409)
            all_steps = self._plan(state)
            require(bool(all_steps), "no registered steps", 409)
            require(body.get("plan_version") == digest(all_steps), "plan changed; inspect it again", 409)
            step_ids = body.get("step_ids")
            requested_mode = body.get("mode")
            require(requested_mode in (None, "all", "selected", "single"), "invalid run mode")
            if step_ids is None:
                require(requested_mode in (None, "all"), "step_ids are required for selected or single mode", 400)
                selected = all_steps
                mode = "all"
            else:
                require(requested_mode in (None, "selected", "single"),
                        "step_ids require selected or single mode", 400)
                require(isinstance(step_ids, list) and bool(step_ids),
                        "step_ids must be a nonempty list", 400)
                require(all(isinstance(step_id, str) for step_id in step_ids),
                        "step_ids must contain strings", 400)
                require(len(set(step_ids)) == len(step_ids), "step_ids must not contain duplicates", 400)
                by_id = {step["step_id"]: step for step in all_steps}
                require(all(step_id in by_id for step_id in step_ids),
                        "step_ids include an unregistered step", 409)
                selected = [step for step in all_steps if step["step_id"] in set(step_ids)]
                mode = "single" if len(selected) == 1 and body.get("mode") == "single" else "selected"
                require(body.get("mode") in (None, "selected", "single"), "invalid run mode")
                if body.get("mode") == "single":
                    require(len(selected) == 1, "single mode requires exactly one step", 400)
            require(bool(selected), "no selected steps", 400)
            selected_nodes = {step["node_id"] for step in selected}
            for node_id in selected_nodes:
                node = state["nodes"][node_id]
                require(self._online(node), "selected node is offline: " + node_id, 409)
                require(not node.get("busy"), "selected node is busy: " + node_id, 409)
            selected_ids = {step["step_id"] for step in selected}
            skipped = [{"step_id": step["step_id"], "name": step["name"], "order": step["order"],
                        "node_id": step["node_id"]}
                       for step in all_steps if step["step_id"] not in selected_ids]
            run = {"run_id": uuid.uuid4().hex, "status": "running", "reason": "",
                   "created_at": self.clock(), "tasks": [], "mode": mode,
                   "selected_step_ids": [step["step_id"] for step in selected],
                   "skipped_steps": skipped}
            for step in selected:
                run["tasks"].append({"task_id": uuid.uuid4().hex, "step": copy.deepcopy(step),
                                     "status": "pending", "result": None, "error": None})
            state["runs"].append(run)
        return run

    def _task_payload(self, run, task, cancel_requested=False):
        return {"run_id": run["run_id"], "task_id": task["task_id"],
                "step_id": task["step"]["step_id"], "params": task["step"]["params"],
                "config_hash": task["step"]["config_hash"], "deadline": task["deadline"],
                "expires_in_seconds": max(0, task["deadline"] - self.clock()),
                "cancel_requested": cancel_requested,
                "previous_results": {t["step"]["step_id"]: t["result"] for t in run["tasks"]
                                     if t["status"] == "succeeded" and t["result"] is not None}}

    def poll(self, body):
        with self.store.edit() as state:
            node = self._identity(state, body)
            node["last_seen"] = self.clock()
            node["busy"] = bool(body.get("busy"))
            run = self._active(state)
            if not run:
                return {"task": None, "run_status": "idle"}
            task = self._current(run)
            if run["status"] == "blocked":
                if (task and task.get("cancel_requested")
                        and task["step"]["node_id"] == body["node_id"]
                        and task.get("instance_id") == body["instance_id"]):
                    return {"task": self._task_payload(run, task, cancel_requested=True),
                            "run_status": run["status"]}
                return {"task": None, "run_status": run["status"]}
            if run["status"] == "stopping" and task is not None:
                if task["step"]["node_id"] == body["node_id"] and task.get("instance_id") == body["instance_id"]:
                    return {"task": self._task_payload(run, task, cancel_requested=True),
                            "run_status": run["status"]}
                return {"task": None, "run_status": run["status"]}
            if task is None or task["step"]["node_id"] != body["node_id"]:
                return {"task": None, "run_status": run["status"]}
            if task["status"] == "pending":
                if run["status"] != "running" or node["busy"]:
                    return {"task": None, "run_status": run["status"]}
                require(node["config_hash"] == task["step"]["config_hash"], "configuration changed", 409)
                task.update(status="assigned", assigned_at=self.clock(), instance_id=body["instance_id"])
                task["deadline"] = self.clock() + task["step"]["timeout_seconds"]
            if task["status"] not in {"assigned", "running"}:
                return {"task": None, "run_status": run["status"]}
            if task["instance_id"] != body["instance_id"]:
                return {"task": None, "run_status": run["status"]}
            # Re-delivery has the same task_id, including when a poll response is lost.
            return {"task": self._task_payload(run, task), "run_status": run["status"]}

    def event(self, body):
        require(valid_id(body.get("event_id")), "invalid event_id")
        require(body.get("status") in {"running", "succeeded", "failed", "unknown"}, "invalid status")
        require(len(dumps(body.get("result")).encode("utf-8")) <= 65536, "result is limited to 64 KiB")
        canonical = {key: value for key, value in body.items() if key != "instance_id"}
        with self.store.edit() as state:
            self._identity(state, body)
            seen = state["events"].get(body["event_id"])
            if seen:
                require(seen == digest(canonical), "event_id reused with different content", 409)
                return {"ok": True, "duplicate": True}
            run = self._run(state, body.get("run_id"))
            task = next((t for t in run["tasks"] if t["task_id"] == body.get("task_id")), None)
            require(task is not None, "task not found", 404)
            require(task["step"]["node_id"] == body.get("node_id"), "task belongs to another node", 403)
            require(task["status"] != "pending", "task was never assigned", 409)
            status = body["status"]
            if run["status"] in {"blocked", "closed"}:
                task.setdefault("late_events", []).append({"status": status, "result": body.get("result"),
                    "error": body.get("error"), "received_at": self.clock()})
            elif task.get("operator_resolution"):
                task.setdefault("late_events", []).append({"status": status, "result": body.get("result"),
                    "error": body.get("error"), "received_at": self.clock()})
            elif task["status"] in FINISHED:
                # Stale running/unknown events cannot regress a terminal task.
                require(status not in FINISHED or (status == task["status"]
                        and body.get("result") == task["result"] and body.get("error") == task["error"]),
                        "conflicting terminal result", 409)
            elif status == "running":
                if task["status"] == "assigned":
                    task["status"] = "running"
                    task["started_at"] = self.clock()
            else:
                task.update(status=status, result=body.get("result"), error=body.get("error"),
                            finished_at=self.clock())
                if run["status"] in {"running", "pausing", "paused", "stopping"}:
                    if status in {"failed", "unknown"}:
                        self._block(run, status + ": " + task["step"]["step_id"])
                    elif all(t["status"] in COMPLETED_TASKS for t in run["tasks"]):
                        run.update(status="succeeded", finished_at=self.clock())
                    elif run["status"] in {"pausing", "stopping"}:
                        run["status"] = "paused"
                        run["reason"] = "operator_requested_stop_completed: " + task["step"]["step_id"]
                # A late success is recorded, but never resumes a blocked/closed run.
            state["events"][body["event_id"]] = digest(canonical)
        return {"ok": True, "duplicate": False}

    def control(self, run_id, action, body):
        with self.store.edit() as state:
            run = self._run(state, run_id)
            if action == "pause":
                require(run["status"] == "running", "only a running run can be paused", 409)
                run["status"] = "paused"
            elif action == "pause-after-current":
                require(run["status"] == "running", "only a running run can request a pause", 409)
                current = self._current(run)
                if current and current["status"] in {"assigned", "running"}:
                    run["status"] = "pausing"
                else:
                    run["status"] = "paused"
            elif action == "resume":
                require(run["status"] == "paused", "only a manually paused run can resume", 409)
                run["status"] = "running"
            elif action == "stop-current":
                require(run["status"] in {"running", "blocked"},
                        "only a running or blocked run can request a stop", 409)
                current = self._current(run)
                require(current is not None and current["status"] in {"assigned", "running", "unknown"},
                        "there is no active or unresolved task to stop", 409)
                require(not current.get("cancel_requested"), "stop already requested", 409)
                require(str(body.get("note", "")).strip(), "stop request requires a reason")
                current["cancel_requested"] = True
                current["stop_note"] = str(body["note"]).strip()
                current["cancel_requested_at"] = self.clock()
                if current["status"] in {"assigned", "running"}:
                    run.update(status="stopping", reason="operator_requested_stop: " + current["step"]["step_id"])
            elif action in {"operator-complete", "skip-current"}:
                require(run["status"] in {"blocked", "paused"},
                        "run must be blocked or paused before manual resolution", 409)
                current = self._current(run)
                require(current is not None, "there is no unresolved task", 409)
                require(current["status"] in {"failed", "unknown"},
                        "only a failed or unknown task can be manually resolved", 409)
                require(body.get("physical_checked") is True and str(body.get("note", "")).strip(),
                        "manual resolution requires physical_checked=true and an inspection note")
                original_status = current["status"]
                resolution = {"action": action, "note": str(body["note"]).strip(),
                              "physical_checked": True, "resolved_at": self.clock(),
                              "original_status": original_status}
                if action == "operator-complete":
                    result = body.get("result")
                    require(result is None or isinstance(result, dict), "result must be a JSON object")
                    require(len(dumps(result).encode("utf-8")) <= 65536, "result is limited to 64 KiB")
                    current.update(status="succeeded", result=result)
                    resolution["result_provided"] = result is not None
                else:
                    current.update(status="skipped", result=None)
                current["operator_resolution"] = resolution
                current["error"] = current.get("error")
                run["reason"] = ""
                if all(t["status"] in COMPLETED_TASKS for t in run["tasks"]):
                    run.update(status="succeeded", finished_at=self.clock())
                else:
                    run["status"] = "running"
            elif action == "close":
                require(run["status"] in ACTIVE, "run is already finished", 409)
                require(body.get("physical_checked") is True and str(body.get("note", "")).strip(),
                        "close requires physical_checked=true and an inspection note")
                run.update(status="closed", close_note=body["note"], finished_at=self.clock())
            else:
                require(False, "unknown action", 404)
        return run

    def monitor(self):
        with self.store.edit() as state:
            run = self._active(state)
            if not run or run["status"] == "blocked":
                return
            task = self._current(run)
            if task is None:
                return
            node = state["nodes"].get(task["step"]["node_id"])
            if task["status"] in {"assigned", "running"}:
                if not node or not self._online(node) or self.clock() > task["deadline"]:
                    task["status"] = "unknown"
                    self._block(run, "task_timeout_or_node_offline: " + task["step"]["step_id"])
            elif run["status"] == "running" and (not node or not self._online(node)):
                self._block(run, "next_node_offline: " + task["step"]["node_id"])

    def status(self, run_id=None):
        state = self.store.read()
        if run_id:
            return self._run(state, run_id)
        return {"run": state["runs"][-1] if state["runs"] else None,
                "history": [{"run_id": r["run_id"], "status": r["status"], "created_at": r["created_at"]}
                            for r in reversed(state["runs"][-30:])]}
