#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegation_efficiency.privacy import purge_aggregates, purge_detail, purge_session, retain, status
from delegation_efficiency.schema import configure_state_dir

def _confirm(parser, args, description):
    if args.yes:
        return
    if not sys.stdin.isatty():
        parser.error(f"{description} requires --yes when stdin is not a terminal")
    answer = input(f"{description}. Type 'yes' to continue: ")
    if answer.strip().lower() != "yes":
        parser.error("purge cancelled")

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", help="override the plugin state directory")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("status", "retain", "purge-session", "purge-detail", "purge-aggregates"):
        sub.add_parser(name).add_argument("--state-dir", help=argparse.SUPPRESS, default=argparse.SUPPRESS)
    # Keep command-specific options on their subcommands while accepting the
    # state override in either conventional argparse position.
    sub.choices["retain"].add_argument("--days", type=int, default=90)
    s = sub.choices["purge-session"]; s.add_argument("session_id"); s.add_argument("generation", type=int); s.add_argument("--yes", action="store_true", help="confirm deletion")
    sub.choices["purge-detail"].add_argument("--yes", action="store_true", help="confirm deletion")
    sub.choices["purge-aggregates"].add_argument("--yes", action="store_true", help="confirm deletion")
    a = p.parse_args()
    configure_state_dir(a.state_dir)
    if a.command == "status": result = status()
    elif a.command == "retain": result = retain(a.days)
    elif a.command == "purge-session": _confirm(p, a, "purge session detail"); result = purge_session(a.session_id, a.generation)
    elif a.command == "purge-detail": _confirm(p, a, "purge all detail"); result = purge_detail()
    else: _confirm(p, a, "purge all aggregates"); result = purge_aggregates()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
if __name__ == "__main__": raise SystemExit(main())
