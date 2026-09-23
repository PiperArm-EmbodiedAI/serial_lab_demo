import argparse
import json
import sys

from labflow.common import Client


def main():
    parser = argparse.ArgumentParser(description="Inspect and control the serial demo")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    commands.add_parser("status")
    start = commands.add_parser("start")
    start.add_argument("--expect-steps", type=int, help="Refuse start unless this many steps have registered")
    for action in ("pause", "resume", "close"):
        child = commands.add_parser(action)
        child.add_argument("--run-id", help="Defaults to the latest run")
        if action == "close":
            child.add_argument("--physical-checked", action="store_true")
            child.add_argument("--note", required=True)
    remove = commands.add_parser("remove-node")
    remove.add_argument("node_id")
    args = parser.parse_args()
    client = Client(args.url)
    try:
        if args.command == "plan":
            result = client.call("GET", "/api/plan")
        elif args.command == "status":
            result = client.call("GET", "/api/status")
        elif args.command == "start":
            plan = client.call("GET", "/api/plan")
            if args.expect_steps is not None and len(plan["steps"]) != args.expect_steps:
                raise ValueError(f"Expected {args.expect_steps} steps; registered {len(plan['steps'])}")
            result = client.call("POST", "/api/runs", {"plan_version": plan["plan_version"]})
        elif args.command == "remove-node":
            result = client.call("DELETE", "/api/nodes/" + args.node_id)
        else:
            run_id = args.run_id
            if not run_id:
                latest = client.call("GET", "/api/status")["run"]
                if not latest:
                    raise ValueError("No run exists")
                run_id = latest["run_id"]
            body = {"physical_checked": args.physical_checked, "note": args.note} if args.command == "close" else {}
            result = client.call("POST", "/api/runs/" + run_id + "/" + args.command, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
