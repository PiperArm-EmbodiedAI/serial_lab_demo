"""Fake task: sleeps, prints, returns metadata. Does not connect to equipment."""
import time


def execute(context):
    params = context["params"]
    print("Mock action:", context["step_id"], "previous:", context["previous_results"], flush=True)
    time.sleep(float(params.get("seconds", 1)))
    if params.get("fail", False):
        raise RuntimeError("Intentional demo failure")
    return {"message": params.get("message", "done"), "previous_steps": list(context["previous_results"])}
