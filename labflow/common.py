import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import urllib.error
import urllib.request


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dumps(value), encoding="utf-8")
    os.replace(temporary, path)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def require(condition, message, status=400):
    if not condition:
        raise ApiError(status, message)


class JsonStore:
    """One transactional document, deliberately small for this single-server demo."""

    def __init__(self, path, initial):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (dumps(initial),))
        self.db.commit()

    @contextlib.contextmanager
    def edit(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                state = json.loads(self.db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])
                yield state
                self.db.execute("UPDATE state SET data=? WHERE id=1", (dumps(state),))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def read(self):
        with self.lock:
            return json.loads(self.db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])

    def close(self):
        with self.lock:
            self.db.close()


class ProcessLock:
    """OS lock released on crash; lock the whole state directory, not just a PID."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.file.close()
            raise RuntimeError("This state directory is already in use: " + str(path))

    def close(self):
        self.file.close()


class Client:
    def __init__(self, url, timeout=5):
        self.url = url.rstrip("/")
        self.timeout = timeout
        # LAN control traffic should not accidentally use HTTP_PROXY from a shell.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        body = None if data is None else dumps(data).encode("utf-8")
        request = urllib.request.Request(self.url + path, body, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", str(error))
            except (ValueError, AttributeError):
                message = str(error)
            raise ApiError(error.code, message) from error
