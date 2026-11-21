"""Live demo: turn a seed into a fresh, visible bug in TodoMVC and serve it on port 8100.

The bug is written to live/app and its answer key to live/meta.json. The script never prints what the bug is
unless --reveal is given.
"""
from __future__ import annotations

import argparse
import functools
import logging
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.mutate import make_one

APP_DIR = ROOT / "apps" / "todomvc"
LIVE_DIR = ROOT / "live"
PORT = 8100
MAX_SEED_STEPS = 25


class QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler that prints nothing and tells the browser not to cache the app."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def reveal() -> int:
    meta_path = LIVE_DIR / "meta.json"
    if not meta_path.is_file():
        print("No live bug yet. Run with --seed <number> first.")
        return 1
    print(meta_path.read_text(encoding="utf-8"))
    return 0


def make_bug(seed: int) -> int | None:
    """Write a visible mutant to live/. Tries seed, then seed+1, and so on. Returns the seed that worked."""
    for used in range(seed, seed + MAX_SEED_STEPS):
        if make_one(APP_DIR, used, LIVE_DIR) is not None:
            return used
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a fresh bug from a seed and serve it at http://localhost:8100 until Ctrl-C."
    )
    parser.add_argument("--seed", type=int, help="any whole number; the same seed always gives the same bug")
    parser.add_argument("--reveal", action="store_true", help="print live/meta.json (the answer) and exit")
    args = parser.parse_args()

    if args.reveal:
        return reveal()
    if args.seed is None:
        parser.error("give --seed <number>, or --reveal")

    logging.disable(logging.CRITICAL)  # nothing but the one line below may reach the screen
    used = make_bug(args.seed)
    if used is None:
        print(f"No visible bug found for seeds {args.seed} to {args.seed + MAX_SEED_STEPS - 1}. Try another number.")
        return 1
    try:
        handler = functools.partial(QuietHandler, directory=str(LIVE_DIR / "app"))
        server = LiveServer(("", PORT), handler)
    except OSError:
        print(f"Port {PORT} is already in use. Stop the other server and run again.")
        return 1
    print(f"Bug ready at http://localhost:{PORT} (seed {used})", flush=True)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
