"""Runs inside the module owner's Python environment; only stdlib dependencies."""
import importlib.util
import json
from pathlib import Path
import sys


def main():
    filename, function, input_path, output_path = sys.argv[1:]
    sys.path.insert(0, str(Path(filename).parent))
    spec = importlib.util.spec_from_file_location("labflow_user_task", filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    context = json.loads(Path(input_path).read_text(encoding="utf-8"))
    result = getattr(module, function)(context)
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise ValueError("Return a dict or None; raise an exception on failure")
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Result exceeds 64 KiB")
    Path(output_path).write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
