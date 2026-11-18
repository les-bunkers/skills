"""Serve a folder on localhost for a human to review (pages, videos, audio), with HTTP Range so players can seek.

`python -m http.server` answers every request with the whole file: browsers cannot seek a video or audio element, and
long media stalls. This server answers Range requests (206 + Content-Range), binds 127.0.0.1 only, refuses a port that
is already taken (it never serves on top of another server), and prints the URL and its process id.

    python serve.py <folder> --port 8731              # foreground (stops with the session that runs it)
    python serve.py <folder> --port 8731 --detach     # own process: survives the agent session; prints PID, log path
    stop: kill that PID (never by process name: other servers and tools run the same interpreter)

Stdlib only.
"""
import argparse
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
from functools import partial
from pathlib import Path

RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mp4": "video/mp4", ".m4a": "audio/mp4", ".wav": "audio/wav", ".mov": "video/quicktime",
                      ".webm": "video/webm", ".vtt": "text/vtt", ".srt": "text/plain; charset=utf-8",
                      ".json": "application/json", ".js": "text/javascript", ".mjs": "text/javascript"}

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")  # a re-rendered file shows up on reload
        super().end_headers()

    def send_head(self):
        m = RANGE.match(self.headers.get("Range", "").strip())
        path = self.translate_path(self.path)
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        a, b = m.groups()
        if a == "" and b == "":
            return super().send_head()
        if a == "":  # last N bytes
            start, end = max(0, size - int(b)), size - 1
        else:
            start, end = int(a), min(size - 1, int(b)) if b else size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_left", None)
        if left is None:
            return shutil.copyfileobj(source, outputfile)
        self._left = None
        try:
            while left > 0:
                chunk = source.read(min(1 << 20, left))
                if not chunk:
                    break
                outputfile.write(chunk)
                left -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # the player seeks elsewhere and drops the connection: normal

    def log_message(self, fmt, *args):
        if os.environ.get("SERVE_QUIET"):
            return
        super().log_message(fmt, *args)


def port_taken(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--detach", action="store_true", help="start in its own process (survives the session); print PID")
    ap.add_argument("--log", help="detached log file (default <folder>/.serve-<port>.log)")
    a = ap.parse_args()
    root = Path(a.folder).resolve()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")
    if port_taken(a.port):
        sys.exit(f"port {a.port} is taken on 127.0.0.1: pick another port or stop that server by its PID "
                 f"(Windows: Get-NetTCPConnection -LocalPort {a.port} -State Listen; else lsof -i :{a.port})")
    if a.detach:
        log = Path(a.log) if a.log else root / f".serve-{a.port}.log"
        kw = {"stdin": subprocess.DEVNULL, "stdout": open(log, "ab"), "stderr": subprocess.STDOUT, "cwd": str(root)}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), str(root), "--port", str(a.port)], **kw)
        print(f"http://127.0.0.1:{a.port}/  pid {p.pid}  log {log}")
        return
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), partial(Handler, directory=str(root)))
    print(f"http://127.0.0.1:{a.port}/  pid {os.getpid()}  serving {root}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
