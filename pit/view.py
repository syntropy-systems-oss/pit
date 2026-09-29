"""`q view`: a stdlib server for the terminal (view/terminal.html at / and /terminal) and /market.json[?upto=N]."""
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import market, book as B

TERMINAL = Path(__file__).resolve().parent.parent / "view" / "terminal.html"


def make_handler(get_rows, cfg):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/market.json":
                rows, upto = get_rows(), parse_qs(u.query).get("upto")
                sl = rows[:int(upto[0])] if upto and upto[0].isdigit() else rows
                now = B.parse_t(sl[-1]["ts"]) if sl and upto else None      # a replay reads its clock from the last visible row
                self.send(200, json.dumps({**market.market_json(sl, cfg, now), "total_rows": len(rows)}).encode(), "application/json")
            elif u.path in ("/", "/terminal"):
                self.send(200, TERMINAL.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send(404, b"not found", "text/plain")

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def serve(get_rows, cfg, port=8790, open_browser=True, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), make_handler(get_rows, cfg))
    url = f"http://{host}:{srv.server_port}/"
    print(f"pit terminal: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
