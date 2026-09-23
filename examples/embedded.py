"""Optional: keep an existing model/device in the same Python process."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labflow.agent import run


def existing_action(context):
    # Initialize your model/device once in main, then refer to it here.
    # This function is called in a business thread; HTTP polling stays responsive.
    print("Embedded demo:", context["params"])
    return {"demo_only": True}


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1] / "configs" / "embedded.json",
        handlers={"existing_action": existing_action})
