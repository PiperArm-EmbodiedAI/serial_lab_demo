import argparse
from pathlib import Path
import threading

from labflow.common import ProcessLock
from labflow.coordinator import Coordinator
from labflow.http_api import make_server, monitor_loop


def main():
    parser = argparse.ArgumentParser(description="Serial experiment coordinator (Python 3.9+)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", default="runtime/server")
    parser.add_argument("--lease-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.lease_seconds < 5:
        parser.error("lease-seconds must be at least 5")
    directory = Path(args.state_dir).resolve()
    lock = ProcessLock(directory / "server.lock")
    coordinator = Coordinator(directory / "server.sqlite3", args.lease_seconds)
    server = make_server(coordinator, args.host, args.port)
    stop = threading.Event()
    monitor = threading.Thread(target=monitor_loop, args=(coordinator, stop), daemon=True)
    monitor.start()
    print(f"Serial Lab Demo: http://{args.host}:{server.server_port}  state={directory}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        monitor.join()
        coordinator.store.close()
        lock.close()


if __name__ == "__main__":
    main()
