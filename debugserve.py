from __future__ import annotations

import argparse
import os
import threading
import time
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable


ROOT_DIR = Path(__file__).resolve().parent
WATCH_TARGETS = (ROOT_DIR / "index.html", ROOT_DIR / "data")
WATCH_INTERVAL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int


class ReloadState:
    def __init__(self) -> None:
        self._version = 0
        self._condition = threading.Condition()

    def bump(self) -> int:
        with self._condition:
            self._version += 1
            self._condition.notify_all()
            return self._version

    def wait_for_change(self, version: int, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._version != version, timeout=timeout)
            return self._version

    @property
    def version(self) -> int:
        with self._condition:
            return self._version


def iter_watch_files() -> Iterable[Path]:
    for target in WATCH_TARGETS:
        if target.is_file():
            yield target
            continue

        if not target.is_dir():
            continue

        for current_root, dir_names, file_names in os.walk(target):
            dir_names[:] = [name for name in dir_names if name not in {".git", "__pycache__"}]
            for file_name in file_names:
                yield Path(current_root) / file_name


def snapshot_files() -> dict[str, FileState]:
    snapshot: dict[str, FileState] = {}

    for path in iter_watch_files():
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue

        relative_path = path.relative_to(ROOT_DIR).as_posix()
        snapshot[relative_path] = FileState(size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    return snapshot


def watch_files(reload_state: ReloadState, stop_event: threading.Event) -> None:
    previous_snapshot = snapshot_files()

    while not stop_event.wait(WATCH_INTERVAL_SECONDS):
        current_snapshot = snapshot_files()
        if current_snapshot != previous_snapshot:
            previous_snapshot = current_snapshot
            version = reload_state.bump()
            print(f"[reload] change detected, version={version}")


class DevRequestHandler(SimpleHTTPRequestHandler):
    server_version = "WorkGameDevServer/1.0"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
    }

    def __init__(self, *args, directory: str | os.PathLike[str] | None = None, **kwargs) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    @property
    def reload_state(self) -> ReloadState:
        return self.server.reload_state  # type: ignore[attr-defined]

    @property
    def stop_event(self) -> threading.Event:
        return self.server.stop_event  # type: ignore[attr-defined]

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path == "/__reload":
            self.handle_reload_stream()
            return

        super().do_GET()

    def do_HEAD(self) -> None:
        if self.path == "/__reload":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            return

        super().do_HEAD()

    def translate_path(self, path: str) -> str:
        if path in {"", "/"}:
            path = "/index.html"

        return super().translate_path(path)

    def handle_reload_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        version = self.reload_state.version
        last_keepalive = time.monotonic()

        try:
            while not self.stop_event.is_set():
                changed_version = self.reload_state.wait_for_change(version, timeout=1.0)
                if changed_version != version:
                    self.wfile.write(f"event: reload\ndata: {changed_version}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    version = changed_version
                    last_keepalive = time.monotonic()
                    continue

                if time.monotonic() - last_keepalive >= KEEPALIVE_SECONDS:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_keepalive = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dev server for workgame with live reload.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", default=8000, type=int, help="Bind port. Default: 8000")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reload_state = ReloadState()
    stop_event = threading.Event()

    handler = partial(DevRequestHandler, directory=str(ROOT_DIR))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.reload_state = reload_state  # type: ignore[attr-defined]
    httpd.stop_event = stop_event  # type: ignore[attr-defined]

    watcher = threading.Thread(target=watch_files, args=(reload_state, stop_event), daemon=True)
    watcher.start()

    print(f"Serving {ROOT_DIR}")
    print(f"Open http://{args.host}:{args.port}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
