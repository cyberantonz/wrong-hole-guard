#!/usr/bin/env python3
"""Stand-in for `claude -p` in the tests.

The tests link this file into a temporary bin/ as `claude`. It records each
call in $WHG_TEST_ROOT/calls.jsonl and answers like the model would:

  reply_<model>.txt   printed to stdout (e.g. reply_sonnet.txt), if present
  exit_code           exit status, if present (default 0)

Like the real CLI, it refuses options it does not know: it prints
"error: unknown option '...'" and exits 1, and the call is logged as rejected.
OPTIONS lists the real `claude` options the script uses (see `claude --help`),
each with the number of values it takes; add to it when the script starts using
another real option.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OPTIONS = {
    "-p": 0,
    "--print": 0,
    "--model": 1,
    "--no-session-persistence": 0,
    "--setting-sources": 1,
    "--settings": 1,
    "--tools": 1,
    "--strict-mcp-config": 0,
    "--system-prompt": 1,
}

root = Path(os.environ["WHG_TEST_ROOT"])
args = sys.argv[1:]

parsed: dict[str, str] = {}
rejected = ""
i = 0
while i < len(args):
    name = args[i]
    arity = OPTIONS.get(name)
    if arity is None:
        rejected = f"unknown option '{name}'" if name.startswith("-") else f"unexpected argument '{name}'"
        break
    if arity and i + arity >= len(args):
        rejected = f"option '{name}' argument missing"
        break
    parsed[name] = args[i + 1] if arity else ""
    i += 1 + arity

with (root / "calls.jsonl").open("a") as log:
    log.write(
        json.dumps(
            {
                "model": parsed.get("--model", ""),
                "args": args,
                "system_prompt": parsed.get("--system-prompt", ""),
                "input": sys.stdin.read(),
                "inner": os.environ.get("WRONG_HOLE_GUARD_INNER"),
                "rejected": rejected,
            }
        )
        + "\n"
    )

if rejected:
    print(f"error: {rejected}", file=sys.stderr)  # noqa: T201
    sys.exit(1)

reply = root / f"reply_{parsed.get('--model', '')}.txt"
if reply.exists():
    sys.stdout.write(reply.read_text())
exit_code = root / "exit_code"
sys.exit(int(exit_code.read_text()) if exit_code.exists() else 0)
