import argparse
from pathlib import Path
import uuid

from labflow.agent import run
from labflow.common import JsonStore, ProcessLock, read_json


def main():
    parser = argparse.ArgumentParser(description="Register local steps and execute assigned tasks")
    parser.add_argument("--config", required=True)
    parser.add_argument("--resolve-unknown", metavar="TASK_ID", help="Offline local recovery after inspecting equipment")
    parser.add_argument("--note", help="Record what was checked; marks local unknown task as failed, never reruns it")
    args = parser.parse_args()
    if not args.resolve_unknown:
        return run(args.config)
    if not args.note:
        parser.error("--resolve-unknown requires --note, after checking that the old action has stopped")
    path = Path(args.config).resolve()
    cfg = read_json(path)
    directory = (path.parent / cfg.get("state_dir", "state-" + cfg["node_id"])).resolve()
    lock = ProcessLock(directory / "agent.lock")
    store = JsonStore(directory / "agent.sqlite3", {"tasks": {}, "outbox": []})
    try:
        with store.edit() as state:
            task = state["tasks"].get(args.resolve_unknown)
            if not task or task["status"] not in {"unknown", "running"}:
                raise ValueError("No unresolved local task with this ID")
            error = "operator inspected/stopped old action: " + args.note
            task.update(status="failed", error=error, result=None)
            state["outbox"].append({"event_id": uuid.uuid4().hex, "node_id": cfg["node_id"],
                "task_id": task["task"]["task_id"], "run_id": task["task"]["run_id"],
                "status": "failed", "result": None, "error": error})
        print("Recorded as failed locally. Restart the adapter to report it; no action was executed.")
    finally:
        store.close()
        lock.close()


if __name__ == "__main__":
    main()
