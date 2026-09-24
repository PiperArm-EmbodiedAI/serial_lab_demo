import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from .common import ApiError, Client, JsonStore, ProcessLock, digest, dumps, read_json, write_json


class Agent:
    def __init__(self, config_path, handlers=None):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self.config = read_json(self.config_path)
        self.handlers = handlers or {}
        self.node_id = self.config["node_id"]
        self.instance_id = uuid.uuid4().hex
        self.steps = {s["step_id"]: s for s in self.config["steps"]}
        if len(self.steps) != len(self.config["steps"]):
            raise ValueError("Duplicate step_id in config")
        for step in self.steps.values():
            kinds = [key for key in ("function", "command", "handler") if key in step]
            if len(kinds) != 1:
                raise ValueError("Each step needs exactly one of function, command, handler")
            if "handler" in step and step["handler"] not in self.handlers:
                raise ValueError("This config requires an embedded handler: " + step["handler"])
            if "command" in step and (not isinstance(step["command"], list)
                    or not step["command"] or not all(isinstance(x, str) for x in step["command"])):
                raise ValueError("command must be a nonempty array of arguments")
        self.config_hash = digest({"node_id": self.node_id, "steps": self.config["steps"]})
        self.state_dir = (self.root / self.config.get("state_dir", "state-" + self.node_id)).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.process_lock = ProcessLock(self.state_dir / "agent.lock")
        self.store = JsonStore(self.state_dir / "agent.sqlite3", {"tasks": {}, "outbox": []})
        self.client = Client(self.config["server_url"])
        self.stop = threading.Event()
        self.thread = None
        self.task_lock = threading.Lock()
        self.current_task_id = None
        self.current_process = None
        self.cancel_requested = threading.Event()
        self.cancel_signal_sent = False
        with self.store.edit() as state:
            for local in state["tasks"].values():
                if local["status"] == "running":
                    local["status"] = "unknown"
                    self._queue(state, local["task"], "unknown", error="adapter restarted during execution; inspect hardware")

    def _queue(self, state, task, status, result=None, error=None):
        event = {"event_id": uuid.uuid4().hex, "node_id": self.node_id,
                 "run_id": task["run_id"], "task_id": task["task_id"], "status": status,
                 "result": result, "error": error}
        state["outbox"].append(event)
        return event

    def busy(self):
        state = self.store.read()
        return bool(state["outbox"]) or any(t["status"] in {"running", "unknown"} for t in state["tasks"].values())

    def identity(self):
        return {"node_id": self.node_id, "instance_id": self.instance_id, "busy": self.busy()}

    def registration(self):
        return dict(self.identity(), config_hash=self.config_hash,
                    steps=[{key: value for key, value in step.items()
                            if key in {"step_id", "order", "name", "params", "timeout_seconds"}}
                           for step in self.config["steps"]])

    def flush(self):
        # Durable, ordered outbox: losing an acknowledgement never repeats the action.
        for event in self.store.read()["outbox"]:
            self.client.call("POST", "/api/events", dict(event, instance_id=self.instance_id))
            with self.store.edit() as state:
                state["outbox"] = [e for e in state["outbox"] if e["event_id"] != event["event_id"]]

    def request_cancel(self, task_id):
        with self.task_lock:
            if self.current_task_id != task_id:
                return False
            if self.cancel_requested.is_set():
                return True
            self.cancel_requested.set()
            process = self.current_process
            if process is None or process.poll() is not None:
                return True
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGINT)
                self.cancel_signal_sent = True
            except (OSError, ProcessLookupError, AttributeError, ValueError):
                return False
            return True

    def accept(self, task):
        if task["config_hash"] != self.config_hash:
            raise RuntimeError("Received a task for a different local configuration")
        with self.store.edit() as state:
            if task["task_id"] in state["tasks"]:
                return  # Same task ID must never execute again, across restarts too.
            if any(t["status"] in {"running", "unknown"} for t in state["tasks"].values()):
                return
            if task.get("cancel_requested"):
                error = "Cancellation requested before local execution; inspect hardware before recovery"
                state["tasks"][task["task_id"]] = {"task": task, "status": "failed", "error": error}
                self._queue(state, task, "failed", error=error)
                print(f"[{self.node_id}] CANCELLED before start {task['step_id']}", flush=True)
                return
            state["tasks"][task["task_id"]] = {"task": task, "status": "running"}
            self._queue(state, task, "running")
        # Journal commit precedes any user code or subprocess launch.
        with self.task_lock:
            self.current_task_id = task["task_id"]
            self.current_process = None
            self.cancel_requested.clear()
            self.cancel_signal_sent = False
        valid_until = time.monotonic() + task["expires_in_seconds"]
        self.thread = threading.Thread(target=self._execute, args=(task, valid_until), name="business-task", daemon=False)
        self.thread.start()

    @staticmethod
    def _validate_result(result):
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise ValueError("Task must return a JSON object or None; raise an exception on failure")
        if len(dumps(result).encode("utf-8")) > 65536:
            raise ValueError("Result exceeds 64 KiB; return a file path or summary")
        return result

    def _execute(self, task, valid_until):
        print(f"[{self.node_id}] START {task['step_id']} task={task['task_id']}", flush=True)
        folder = self.state_dir / "tasks" / task["task_id"]
        input_file, result_file = folder / "input.json", folder / "result.json"
        result, error, status = None, None, "failed"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_json(input_file, task)
            if time.monotonic() >= valid_until:
                raise RuntimeError("Task expired before local execution; no action started")
            step = self.steps[task["step_id"]]
            if "handler" in step:
                result = self._validate_result(self.handlers[step["handler"]](task))
            else:
                python = step.get("python", sys.executable)
                cwd = (self.root / step.get("cwd", ".")).resolve()
                if "function" in step:
                    filename, function = step["function"].rsplit(":", 1)
                    argv = [python, str(Path(__file__).with_name("worker.py")),
                            str((self.root / filename).resolve()), function, str(input_file), str(result_file)]
                else:
                    argv = [python if x == "{python}" else x for x in step["command"]]
                env = os.environ.copy()
                env.update({"LABFLOW_TASK_FILE": str(input_file), "LABFLOW_RESULT_FILE": str(result_file),
                            "LABFLOW_TASK_ID": task["task_id"], "LABFLOW_RUN_ID": task["run_id"],
                            "LABFLOW_STEP_ID": task["step_id"], "PYTHONUNBUFFERED": "1"})
                with open(folder / "stdout.log", "wb") as log:
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=(os.name != "nt"), creationflags=creationflags)
                    with self.task_lock:
                        self.current_process = process
                        already_cancelled = self.cancel_requested.is_set()
                    if already_cancelled:
                        self.request_cancel(task["task_id"])
                    exit_code = process.wait()
                    with self.task_lock:
                        self.current_process = None
                if self.cancel_requested.is_set():
                    raise RuntimeError("Cancellation requested; inspect hardware before recovery")
                if exit_code != 0:
                    raise RuntimeError(f"Process exited with code {exit_code}; see {folder / 'stdout.log'}")
                result = self._validate_result(read_json(result_file) if result_file.exists() else {"exit_code": 0})
            if self.cancel_requested.is_set():
                raise RuntimeError("Cancellation requested; inspect hardware before recovery")
            status = "succeeded"
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:4000]
        finally:
            with self.store.edit() as state:
                state["tasks"][task["task_id"]].update(status=status, result=result, error=error)
                self._queue(state, task, status, result, error)
            with self.task_lock:
                self.current_task_id = None
                self.current_process = None
                self.cancel_requested.clear()
                self.cancel_signal_sent = False
            print(f"[{self.node_id}] {status.upper()} {task['step_id']}" + (f" {error}" if error else ""), flush=True)

    def serve(self):
        registered = False
        last_message = ""
        interval = max(0.1, min(float(self.config.get("poll_seconds", 1)), 3))
        print(f"[{self.node_id}] connecting to {self.client.url}", flush=True)
        try:
            while not self.stop.is_set():
                try:
                    if not registered:
                        self.client.call("POST", "/api/register", self.registration())
                        registered = True
                        print(f"[{self.node_id}] registered {len(self.steps)} step(s)", flush=True)
                    self.flush()
                    response = self.client.call("POST", "/api/poll", self.identity())
                    task = response.get("task")
                    if task and task.get("cancel_requested"):
                        self.request_cancel(task["task_id"])
                    if task:
                        self.accept(task)
                    last_message = ""
                except Exception as exc:
                    if isinstance(exc, ApiError) and exc.status in {404, 409}:
                        registered = False
                    message = str(exc)
                    if message != last_message:
                        print(f"[{self.node_id}] waiting: {message}", flush=True)
                        last_message = message
                self.stop.wait(interval)
        except KeyboardInterrupt:
            print("Stopping task intake. An existing action is not automatically cancelled.", flush=True)
        finally:
            self.stop.set()
            if self.thread and self.thread.is_alive():
                print("Waiting for the local action to return. Use its own controls to stop hardware.", flush=True)
                self.thread.join()
            try:
                if registered:
                    self.flush()
            except Exception:
                pass  # Outbox remains on disk for the next startup.
            self.store.close()
            self.process_lock.close()


def run(config_path, handlers=None):
    """Optional embedded use: run(config_path, {'handler_name': existing_function})."""
    Agent(config_path, handlers=handlers).serve()
