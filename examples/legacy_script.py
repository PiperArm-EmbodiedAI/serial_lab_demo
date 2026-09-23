"""Example existing CLI script: no dependency on labflow. stdout goes to a task log."""
import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=1)
args = parser.parse_args()
print("Existing CLI script started", flush=True)
time.sleep(args.seconds)
print("Existing CLI script completed", flush=True)
