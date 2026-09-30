"""The runner: a small HTTP service that runs Pit jobs on the box it lives on (docs/runner.md).

    q runner --port 8791 [--host 0.0.0.0] [--slots 1] [--workdir ~/.pit-runner] [--token-env PIT_RUNNER_TOKEN]

POST /run  {"job": id, "command": "..."} or {"job": id, "repo": url, "ref": ref, "script": "..."},
           plus "funded_s": seconds and "env": {name: value}.
  command: `sh -c command` in <workdir>/slot-<n>.
  repo/ref/script: a persistent checkout per repo and slot under <workdir>/checkouts (cloned once, then
  `git fetch origin <ref>` + `git checkout --force FETCH_HEAD`: a branch, tag, sha or refs/pull/N/head), then
  `sh -c script` in it. The runner never builds anything; caching builds is the script's business.
  The reply is text/plain, streamed line by line (stdout and stderr merged). The run is killed at funded_s counted from
  the request (fetch included), and the last line is `pit: wall_s=S`, or `pit: stop=timeout wall_s=S` after a kill,
  where S is the whole request's wall. A client that disconnects kills the run.
GET /health  200 {"free": n} while a slot is free, else 503: use it as the lane's gate.
One run per slot; a request with no free slot gets 503. With a token set, requests need `Authorization: Bearer <token>`.
"""
import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_handler(workdir: Path, slots: int, token: str | None):
    free = queue.Queue()
    for i in range(slots):
        free.put(i)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"       # the body ends when the connection closes: no length needed for a stream

        def reply(self, code, text, ctype="text/plain; charset=utf-8"):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            got = self.headers.get("Authorization", "")
            return not token or hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, "not found\n")
            n = free.qsize()
            self.reply(200 if n else 503, json.dumps({"free": n}) + "\n", "application/json")

        def do_POST(self):
            if self.path != "/run":
                return self.reply(404, "not found\n")
            if not self.authorized():
                return self.reply(401, "bad or missing token\n")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                funded = float(req["funded_s"])
                env = {str(k): str(v) for k, v in (req.get("env") or {}).items()}
                if not (isinstance(req.get("command"), str) or all(isinstance(req.get(k), str) for k in ("repo", "ref", "script"))):
                    raise ValueError("give command, or repo + ref + script")
            except (ValueError, KeyError, TypeError) as e:
                return self.reply(400, f"bad request: {e}\n")
            try:
                slot = free.get_nowait()
            except queue.Empty:
                return self.reply(503, f"busy: all {slots} slot(s) running\n")
            try:
                self.run(req, funded, env, slot)
            finally:
                free.put(slot)

        def run(self, req, funded, env, slot):
            t0 = time.monotonic()
            deadline = t0 + funded
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()

            def say(line):
                self.wfile.write(line.encode())
                self.wfile.flush()

            try:
                if "command" in req:
                    cwd = workdir / f"slot-{slot}"
                    cwd.mkdir(parents=True, exist_ok=True)
                    cmd = req["command"]
                else:
                    cwd, why = checkout(workdir, req["repo"], req["ref"], slot, deadline)
                    if why:
                        say(f"runner: {why}\n")
                        return say(f"pit: verdict=invalid wall_s={time.monotonic() - t0:.1f}\n")
                    cmd = req["script"]
                killed = stream(cmd, cwd, {**os.environ, **env, "PIT_CACHE": str(workdir / "cache")}, deadline, say)
                say(f"pit: {'stop=timeout ' if killed else ''}wall_s={time.monotonic() - t0:.1f}\n")
            except (BrokenPipeError, ConnectionResetError):
                pass                      # the client hung up (a stop rule); stream() has killed the run

        def log_message(self, fmt, *a):
            print(f"runner {self.address_string()} {fmt % a}", flush=True)
    return H


def checkout(workdir: Path, repo: str, ref: str, slot: int, deadline: float) -> tuple[Path, str | None]:
    """A persistent checkout of `repo` at `ref` for this slot: clone once, then fetch + checkout. (dir, error or None)."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", repo.rstrip("/").rsplit("/", 1)[-1]) + "-" + hashlib.sha1(repo.encode()).hexdigest()[:8]
    d = workdir / "checkouts" / f"{name}-{slot}"
    steps = ([] if (d / ".git").exists() else [["git", "clone", "--no-checkout", repo, str(d)]]) + [
        ["git", "-C", str(d), "fetch", "--force", "origin", ref],
        ["git", "-C", str(d), "checkout", "--force", "--detach", "FETCH_HEAD"],
        ["git", "-C", str(d), "clean", "-fd"]]         # untracked files go; ignored build outputs stay for the script to reuse
    d.parent.mkdir(parents=True, exist_ok=True)
    for cmd in steps:
        step = cmd[3] if cmd[1] == "-C" else cmd[1]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return d, f"git {step} ran past the funding"
        if p.returncode:
            return d, f"git {step} {ref} failed: {p.stderr.strip()[-300:]}"
    return d, None


def stream(cmd: str, cwd: Path, env: dict, deadline: float, say) -> bool:
    """Run `sh -c cmd`, sending each output line through say(); kill at the deadline. True if killed there."""
    p = subprocess.Popen(["sh", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    killed = []
    timer = threading.Timer(max(0.0, deadline - time.monotonic()), lambda: (killed.append(1), kill(p)))
    timer.start()
    try:
        for line in p.stdout:
            say(line)
    except (BrokenPipeError, ConnectionResetError):
        kill(p)
        raise
    finally:
        timer.cancel()
        p.stdout.close()
        p.wait()
    return bool(killed)


def kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def serve(port: int, host: str = "127.0.0.1", slots: int = 1, workdir="~/.pit-runner", token: str | None = None):
    wd = Path(workdir).expanduser()
    wd.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), make_handler(wd, slots, token))
    print(f"runner on http://{host}:{srv.server_port}  slots {slots}  workdir {wd}" + ("  token required" if token else ""), flush=True)
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="q runner", description="Run Pit jobs on this box over HTTP (docs/runner.md).")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to accept other hosts (set a token)")
    ap.add_argument("--slots", type=int, default=1, help="runs at once")
    ap.add_argument("--workdir", default="~/.pit-runner")
    ap.add_argument("--token-env", default="PIT_RUNNER_TOKEN", help="the env var holding the shared token (unset = none)")
    a = ap.parse_args(argv)
    serve(a.port, a.host, a.slots, a.workdir, os.environ.get(a.token_env) or None)


if __name__ == "__main__":
    main()
