"""Launch only the bundled fake modules; never loads user module configs."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from labflow.common import Client, read_json, write_json

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="One-command mock demo; requires only Python 3.9+")
    parser.add_argument("--port", type=int, default=8765, help="Use 0 for a free port")
    parser.add_argument("--auto", action="store_true", help="Run once, check outcome, exit")
    parser.add_argument("--fail-step", choices=["a", "b", "c"])
    args = parser.parse_args()
    port = args.port
    if port == 0:
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            port = temporary.getsockname()[1]
    directory = ROOT / "runtime" / ("demo-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    url = f"http://127.0.0.1:{port}"
    processes, logs = [], []
    import os
    env = dict(os.environ, PYTHONUNBUFFERED="1")

    def launch(name, argv):
        log = open(directory / (name + ".log"), "wb")
        logs.append(log)
        processes.append(subprocess.Popen([sys.executable] + argv, cwd=str(ROOT), env=env,
                                          stdout=log, stderr=subprocess.STDOUT))

    client = Client(url)
    try:
        launch("server", ["server.py", "--port", str(port), "--state-dir", str(directory / "server")])
        ready = False
        for _ in range(100):
            if processes[0].poll() is not None:
                raise RuntimeError("Server did not start; check " + str(directory / "server.log"))
            try:
                client.call("GET", "/health")
                ready = True
                break
            except Exception:
                time.sleep(0.1)
        if not ready:
            raise RuntimeError("Server readiness timeout")
        for name in "abc":
            config = read_json(ROOT / "configs" / ("node_" + name + ".json"))
            config["server_url"] = url
            config["state_dir"] = str(directory / ("node_" + name))
            config["steps"][0]["function"] = str(ROOT / "examples" / "demo_task.py") + ":execute"
            config["steps"][0]["params"]["fail"] = args.fail_step == name
            path = directory / ("node_" + name + ".json")
            write_json(path, config)
            launch("node_" + name, ["node.py", "--config", str(path)])
        for _ in range(100):
            plan = client.call("GET", "/api/plan")
            if len(plan["steps"]) == 3:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Not all demo nodes registered")
        print("Demo ready: " + url, flush=True)
        print("Run logs: " + str(directory), flush=True)
        if not args.auto:
            print("Open the page and click 开始一次实验. Ctrl+C stops these MOCK processes.", flush=True)
            while True:
                time.sleep(0.5)
        run = client.call("POST", "/api/runs", {"plan_version": plan["plan_version"]})
        for _ in range(200):
            run = client.call("GET", "/api/runs/" + run["run_id"])
            if run["status"] in {"succeeded", "blocked"}:
                break
            time.sleep(0.1)
        expected = "blocked" if args.fail_step else "succeeded"
        assert run["status"] == expected, run
        if args.fail_step:
            failed_index = "abc".index(args.fail_step)
            assert run["tasks"][failed_index]["status"] == "failed", run
            assert all(t["status"] == "pending" for t in run["tasks"][failed_index + 1:]), run
        else:
            for previous, following in zip(run["tasks"], run["tasks"][1:]):
                assert previous["finished_at"] <= following["assigned_at"], run
            assert set(run["tasks"][-1]["result"]["previous_steps"]) == {"demo_step_a", "demo_step_b"}
        write_json(directory / "report.json", run)
        print(json.dumps({"verified": True, "status": run["status"],
                          "steps": [t["status"] for t in run["tasks"]]}, ensure_ascii=False), flush=True)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        # This launcher owns fake processes only. It is not a real-device stop mechanism.
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
