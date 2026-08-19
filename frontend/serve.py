#!/usr/bin/env python3
"""Static dev server for the Finio frontend.

`python -m http.server` sends no cache headers, so browsers hold on to old
copies of api.js / config.js / styles.css. With ES modules that fails in a
genuinely confusing way: the browser keeps a stale module and reports

    SyntaxError: The requested module './config.js' does not provide
                 an export named 'CURRENCY'

even though the export is right there on disk and a plain reload doesn't clear
it. This server sends no-store on everything so an edit is always the thing you
see. Development only — a real host should cache static assets.

    python3 serve.py [port]      (default 5500)
"""

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Keep the terminal readable: only surface failures.
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5500
    handler = partial(NoCacheHandler, directory=str(__file__.rsplit("/", 1)[0]))
    with ThreadingHTTPServer(("", port), handler) as httpd:
        print(f"Finio frontend → http://localhost:{port}  (no-cache, Ctrl-C to stop)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
