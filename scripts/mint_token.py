"""Create or rotate an agent token. Prints the token once; tokens.json keeps only its SHA-256.

Usage (on the server):  python3 scripts/mint_token.py claude-laptop [--scopes 'global,project:*,...']
"""

import argparse
import hashlib
import json
import secrets
from pathlib import Path

TOKENS = Path("./data/tokens.json")

parser = argparse.ArgumentParser()
parser.add_argument("agent_id")
parser.add_argument("--scopes", help="comma-separated fnmatch patterns; default: global, project:*, session:*, agent:<id>")
parser.add_argument("--file", type=Path, default=TOKENS)
args = parser.parse_args()

book = json.loads(args.file.read_text()) if args.file.exists() else {"agents": {}}
token = "mc_" + secrets.token_urlsafe(32)
entry = {"sha256": hashlib.sha256(token.encode()).hexdigest()}
if args.scopes:
    entry["scopes"] = [s.strip() for s in args.scopes.split(",") if s.strip()]
book["agents"][args.agent_id] = entry
args.file.write_text(json.dumps(book, indent=2) + "\n")
args.file.chmod(0o644)  # hashes only; the app container runs as nobody and must read it
print(token)
