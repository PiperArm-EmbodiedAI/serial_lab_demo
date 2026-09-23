from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import traceback
from urllib.parse import urlsplit

from .common import ApiError, dumps, require


def make_server(coordinator, host, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format_string, *args):
            pass

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            payload = data.encode("utf-8") if isinstance(data, str) else dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def dispatch(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/":
                return self.reply(200, Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if self.command == "GET" and path == "/health":
                return self.reply(200, {"ok": True, "version": "0.1.1"})
            body = {}
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                require(0 < length <= 262144, "body must be 1..262144 bytes", 413)
                body = json.loads(self.rfile.read(length))
                require(isinstance(body, dict), "JSON body must be an object")
            coordinator.monitor()
            if self.command == "GET" and path == "/api/plan":
                result = coordinator.plan()
            elif self.command == "GET" and path == "/api/status":
                result = coordinator.status()
            elif self.command == "GET" and path.startswith("/api/runs/"):
                result = coordinator.status(path.rsplit("/", 1)[1])
            elif self.command == "POST" and path == "/api/register":
                result = coordinator.register(body)
            elif self.command == "POST" and path == "/api/poll":
                result = coordinator.poll(body)
            elif self.command == "POST" and path == "/api/events":
                result = coordinator.event(body)
            elif self.command == "POST" and path == "/api/runs":
                result = coordinator.start(body)
            elif self.command == "POST" and path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                require(len(parts) == 4, "invalid run control path", 404)
                result = coordinator.control(parts[2], parts[3], body)
            elif self.command == "DELETE" and path.startswith("/api/nodes/"):
                result = coordinator.remove_node(path.rsplit("/", 1)[1])
            else:
                raise ApiError(404, "route not found")
            self.reply(200, result)

        def handle_request(self):
            try:
                self.dispatch()
            except ApiError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                traceback.print_exc()
                self.reply(500, {"error": "internal error; inspect server log"})

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    return ThreadingHTTPServer((host, port), Handler)


def monitor_loop(coordinator, stop):
    while not stop.wait(0.5):
        try:
            coordinator.monitor()
        except Exception:
            traceback.print_exc()
