# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bake-off step 3: blind answer-key page on localhost -> <job>/key.json (human labels). Stdlib only.

Serves key_page.html with the job's candidates (candidates.json), its Words and the take's unfiltered audio
(<job>/work/page.wav, 48 kHz 16-bit mono, made once with ffmpeg). Blind: the page never shows which detector
flagged a candidate. One candidate on stage: spectrogram 0-8 kHz with the words lane, keys Space (play with 1 s of
context) / B (band only) / 1-5 (breath, euh, other noise, speech, nothing) / Enter or arrows / U (next unmarked) /
drag an orange band edge, R resets it. Every change autosaves to key.json:
  {"name", "labels": {"<candidate id>": {"labels": [...], "t0", "t1", "at", "edge": [t0, t1] when dragged}}}
Sets: --preview N (spread over the detector combinations: show ~20 first, check the format works for the labeller),
--sample (stratified by bakeoff.json "strata", ~100 marks; writes sample.json with each stratum's full size, the
scoring weights), --ids a,b,c (a targeted top-up), default every candidate.
Binds 127.0.0.1 without SO_REUSEADDR: a busy port fails instead of silently sharing it with another server (on
Windows a second http.server otherwise binds the same port and requests reach the old one). Stop it by its PID
(printed at start), never by process name. After a restart, the labeller reloads the tab: an old tab keeps posting
its old set.

usage: python key_server.py <job> [--preview 20 | --sample | --ids 3,8,12] [--port 8791]
"""
import argparse
import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ears_job as J  # noqa: E402

LOCK = threading.Lock()


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def preview_ids(cands, n):
    """n candidates spread over the detector-source combinations (deterministic)."""
    rnd = random.Random(7)
    by = {}
    for c in cands:
        by.setdefault("+".join(c["srcs"]), []).append(c["id"])
    picked = []
    groups = sorted(by.values(), key=len, reverse=True)
    while len(picked) < n and any(set(g) - set(picked) for g in groups):
        for g in groups:
            rest = [i for i in g if i not in picked]
            if rest and len(picked) < n:
                picked.append(rnd.choice(rest))
    return sorted(picked)


def sample_ids(cfg, cands):
    """Stratified sample (cfg strata quotas); already-labelled ids count toward their stratum's quota."""
    d = cfg["dir"]
    kp = d / "key.json"
    done = {int(i) for i in (J.read_json(kp)["labels"] if kp.is_file() else {})}
    prev = J.read_json(d / "sample.json") if (d / "sample.json").is_file() else {}
    rnd = random.Random(11)
    out = {}
    for name, quota in cfg["strata"].items():
        ids = [c["id"] for c in cands if J.stratum(c, cfg["weak_euh"]) == name]
        keep = [i for i in ids if i in done or i in set(prev.get(name, {}).get("ids", []))]
        rest = [i for i in ids if i not in keep]
        rnd.shuffle(rest)
        need = len(ids) if quota is None else max(0, quota - len(keep))
        out[name] = {"size": len(ids), "quota": quota, "ids": sorted(keep + rest[:need])}
    J.save_json(d / "sample.json", out)
    return {i for s in out.values() for i in s["ids"]}


def build(cfg, preview, sample=False, only=None):
    cands = J.read_json(cfg["dir"] / "candidates.json")["candidates"]
    if only:
        ids = set(only)
    elif preview:
        ids = set(preview_ids(cands, preview))
    elif sample:
        ids = sample_ids(cfg, cands)
    else:
        ids = {c["id"] for c in cands}
    ws, _ = J.words(cfg["words"])
    return {"key": cfg["dir"].name, "name": cfg["name"], "preview": bool(preview), "sample": bool(sample),
            "topup": bool(only), "total": len(cands),
            "candidates": [{"id": c["id"], "t0": c["t0"], "t1": c["t1"]} for c in cands if c["id"] in ids],
            "words": [[round(w["start"], 3), round(w["end"], 3), w["text"]] for w in J.speech_words(ws)]}


def handler(cfg, data, audio):
    key_path = cfg["dir"] / "key.json"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    page = (HERE / "key_page.html").read_bytes()

    def read_key():
        return J.read_json(key_path) if key_path.is_file() else {"name": cfg["name"], "labels": {}}

    class H(BaseHTTPRequestHandler):  # serves only the routes below, never the working folder
        def log_message(self, *a):
            pass

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/data.json":
                return self._send(200, body)
            if path == "/key.json":
                return self._send(200, json.dumps(read_key(), ensure_ascii=False).encode("utf-8"))
            if path == "/audio.wav":
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(audio.stat().st_size))
                self.end_headers()
                with open(audio, "rb") as f:
                    while chunk := f.read(1 << 20):
                        self.wfile.write(chunk)
                return
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if self.path != "/key.json":
                return self._send(404, b'{"error":"not found"}')
            n = int(self.headers.get("Content-Length", 0))
            try:
                incoming = json.loads(self.rfile.read(n).decode("utf-8"))
                assert isinstance(incoming.get("labels"), dict)
            except Exception as e:  # noqa: BLE001
                return self._send(400, json.dumps({"error": str(e)}).encode())
            with LOCK:
                kd = read_key()
                kd["name"] = cfg["name"]
                for cid, v in incoming["labels"].items():
                    if v is None or not v.get("labels"):
                        kd["labels"].pop(cid, None)
                    else:
                        kd["labels"][cid] = v
                J.save_json(key_path, kd)
            return self._send(200, json.dumps({"ok": True, "n": len(kd["labels"])}).encode())

    return H


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Blind labelling page for a bake-off job (localhost) -> key.json.")
    ap.add_argument("job", help="bake-off job folder with candidates.json (union.py)")
    ap.add_argument("--preview", type=int, default=0, help="show N candidates spread over detector combinations")
    ap.add_argument("--sample", action="store_true", help="stratified sample (~100) instead of every candidate")
    ap.add_argument("--ids", default="", help="comma-separated candidate ids (a targeted top-up)")
    ap.add_argument("--port", type=int, default=8791)
    a = ap.parse_args()
    cfg = J.load_job(a.job)
    only = [int(i) for i in a.ids.split(",") if i.strip()]
    data = build(cfg, a.preview, a.sample, only)
    audio = J.extract_wav(cfg["audio"], cfg["dir"] / "work" / "page.wav", 48000, codec="pcm_s16le")
    try:
        srv = Server(("127.0.0.1", a.port), handler(cfg, data, audio))
    except OSError as e:
        sys.exit(f"port {a.port} is busy ({e}); pick another --port")
    print(f"http://127.0.0.1:{a.port}/  {len(data['candidates'])} candidates, labels -> {cfg['dir'] / 'key.json'} "
          f"(pid {os.getpid()})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
