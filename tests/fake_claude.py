#!/usr/bin/env python3
"""Stand-in for `claude -p` in the tests.

The tests link this file into a temporary bin/ as `claude`. It records each
call in $WHG_TEST_ROOT/calls.jsonl and answers like the model would:

  reply_<model>.txt   printed to stdout (e.g. reply_sonnet.txt), if present
  exit_code           exit status, if present (default 0)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

root = Path(os.environ["WHG_TEST_ROOT"])
args = sys.argv[1:]


def option(name):
    return args[args.index(name) + 1] if name in args else ""


model = option("--model")
calls = root / "calls.jsonl"

with calls.open("a") as log:
    log.write(
        json.dumps(
            {
                "model": model,
                "args": args,
                "system_prompt": option("--system_prompt-prompt"),
                "input": sys.stdin.read(),
                "inner": os.environ.get("WRONG_HOLE_GUARD_INNER"),
            }
        )
        + "\n"
    )

reply = root / f"reply_{model}.txt"
if reply.exists():
    sys.stdout.write(reply.read_text())
exit_code = root / "exit_code"
sys.exit(int(exit_code.read_text()) if exit_code.exists() else 0)
