#!/usr/bin/env python3
"""wrong-hole-guard: catch prompts typed into the wrong Claude Code window.

  wrong-hole-guard.py update   (Stop hook, async)   refresh this session's window context
  wrong-hole-guard.py check    (UserPromptSubmit)   judge the new prompt against it
  wrong-hole-guard.py wake     (UserPromptSubmit, asyncRewake)  the same check in the
                           background: the prompt is not delayed, and a mismatch
                           wakes the assistant through exit code 2 and stderr

The window context is stored as JSON, one file per session, in
~/.claude/wrong-hole-guard/<session_id>.json:
  header       the session's topic, set from the first prompt and reply; later
               only small edits are accepted
  description  what the session is about more widely; appends and small
               corrections are accepted, rewrites are not
  worklog      one plain-text line per exchange, newest last

Haiku proposes changes after every response and this script decides which to
keep; Sonnet judges each new prompt. Both see the context rendered as labelled
plain text, never the raw JSON. On a mismatch the check injects a confirm-first instruction and shows
a warning; it never blocks (a blocked prompt is erased from the conversation).
Files untouched for 30 days are pruned. Any failure is silent: the guard must
never get in the way of a prompt.
"""
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "wrong-hole-guard"
INNER = "WRONG_HOLE_GUARD_INNER"
# The check needs judgement (Haiku let "ok, merged <other repo's issue>" through);
# updates are cheap summaries guarded by the stability rules below.
CHECK_MODEL = "sonnet"
UPDATE_MODEL = "haiku"
UPDATE_TIMEOUT_S = 90
CHECK_TIMEOUT_S = 25
PRUNE_AFTER_S = 30 * 86400
MAX_PROMPT_CHARS = 4000
MAX_REPLY_CHARS = 3000
SEED_PROMPTS = 10
SEED_PROMPT_CHARS = 300
HEADER_MIN_SIMILARITY = 0.6
DESCRIPTION_MIN_SIMILARITY = 0.5
MAX_HEADER_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
MAX_ENTRY_CHARS = 200
KEEP_WORKLOG = 50
SHOW_WORKLOG = 8
LAST_PROMPT_CHARS = 1000
NOT_A_PROMPT = ("<task-notification", "<command-", "<local-command", "Another Claude session",
                "<system-reminder", "[SYSTEM NOTIFICATION")

UPDATE_SYSTEM = (
    "You keep the context of one terminal window running an AI coding assistant, so a later "
    "check can tell whether a new message belongs to this window. Reply with one JSON object "
    "and nothing else: {\"header\": ..., \"description\": ..., \"worklog_entry\": ...}.\n"
    "header: the session's topic in under 12 words, naming the main project or repository. "
    "If a current header is given, return it unchanged unless a word is plainly wrong.\n"
    "description: 2-5 sentences on what the session works on: projects, repositories, systems, "
    "hosts, open threads. If a current description is given, return it with at most a sentence "
    "appended or a detail corrected; never rewrite it.\n"
    "worklog_entry: one short line on what was done in the latest exchange, with concrete names "
    "(issue/PR/MR numbers, hosts, files)."
)
CHECK_SYSTEM = (
    "You judge whether a user's new message belongs to an ongoing session in one terminal "
    "window, or was probably typed into the wrong window (the user runs several sessions in "
    "parallel, each on a different project). The stored context may not include the most "
    "recent exchanges, so the user's previous message is shown too: a message on the same "
    "topic as that previous message FITS even if the stored context does not mention it yet. "
    "Bare approvals with no new subject ('ok', 'merged', 'done'), follow-ups, questions about "
    "the same systems, and new tasks that plausibly relate to the window's work all FIT. A "
    "message that names a repository, issue, PR or project is judged by what it names, even "
    "if it starts with 'ok' or 'merged'. Say MISMATCH only when the message is clearly about "
    "a different project, repository or system with no visible link to the window's work "
    "or to the user's previous message. Reply with exactly one "
    "line: FIT, or MISMATCH: <reason in at most 15 words>."
)


def ask(model, system, user, timeout):
    env = dict(os.environ, **{INNER: "1"})
    try:
        out = subprocess.run(
            ["claude", "-p", "--model", model, "--no-session-persistence",
             "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
             "--tools", "", "--strict-mcp-config", "--system-prompt", system],
            input=user, capture_output=True, text=True, timeout=timeout,
            cwd="/tmp", env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def state_file(session_id):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return STATE_DIR / f"{safe}.json" if safe else None


def load(path):
    """The stored context, or None when absent or unreadable. Fields of the wrong
    type (a hand-edited or damaged file) are reset rather than trusted."""
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    for key in ("header", "description"):
        if not isinstance(state.get(key), str):
            state[key] = ""
    worklog = state.get("worklog")
    state["worklog"] = [e for e in worklog if isinstance(e, str)] if isinstance(worklog, list) else []
    return state


def save(path, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def prune():
    cutoff = time.time() - PRUNE_AFTER_S
    for old in STATE_DIR.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def render(state):
    """The context as the models see it: labelled plain text."""
    worklog = "\n".join(f"- {e}" for e in state.get("worklog", [])[-SHOW_WORKLOG:]) or "- (none yet)"
    return (f"Topic: {state.get('header', '')}\n"
            f"About: {state.get('description', '')}\n"
            f"Recent work:\n{worklog}")


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def exchanges(transcript_path):
    """[(prompt, last assistant text after it)] for the user's real prompts."""
    pairs = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("isMeta"):
                    continue
                text = text_of(e.get("message", {}).get("content")).strip()
                if not text:
                    continue
                if e.get("type") == "user" and not text.startswith(NOT_A_PROMPT):
                    pairs.append([text, ""])
                elif e.get("type") == "assistant" and pairs:
                    pairs[-1][1] = text
    except OSError:
        pass
    return pairs


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def parse_json(reply):
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def merge(state, proposal):
    header = str(proposal.get("header") or "").strip()[:MAX_HEADER_CHARS]
    description = str(proposal.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]
    entry = str(proposal.get("worklog_entry") or "").strip()[:MAX_ENTRY_CHARS]
    old_header, old_description = state.get("header", ""), state.get("description", "")
    if header and (not old_header or similar(old_header, header) >= HEADER_MIN_SIMILARITY):
        state["header"] = header
    if description and (not old_description or description.startswith(old_description)
                        or similar(old_description, description) >= DESCRIPTION_MIN_SIMILARITY):
        state["description"] = description
    if entry:
        state["worklog"] = (state.get("worklog", []) + [entry])[-KEEP_WORKLOG:]
    state["updated"] = int(time.time())
    return state


def update(event):
    path = state_file(event.get("session_id") or "")
    if not path:
        return
    prune()
    pairs = exchanges(event.get("transcript_path") or "")
    if not pairs:
        return
    prompt, reply = pairs[-1][0][:MAX_PROMPT_CHARS], pairs[-1][1][-MAX_REPLY_CHARS:]
    state = load(path)
    if state:
        history = f"Current context:\n{render(state)}"
    else:
        # First run: the session's first exchange sets the topic, recent prompts fill in.
        first_prompt, first_reply = pairs[0][0][:MAX_PROMPT_CHARS], pairs[0][1][-MAX_REPLY_CHARS:]
        recent = "\n".join(f"- {p[:SEED_PROMPT_CHARS]}" for p, _ in pairs[1:-1][-SEED_PROMPTS:])
        history = (f"No context yet.\n\nSession's first message:\n{first_prompt}\n\n"
                   f"First reply:\n{first_reply}\n\nLater user messages:\n{recent or '- (none)'}")
        state = {"created": int(time.time())}
    proposal = parse_json(ask(UPDATE_MODEL, UPDATE_SYSTEM, f"{history}\n\nLatest user message:\n{prompt}\n\n"
                                               f"Latest reply:\n{reply}", UPDATE_TIMEOUT_S))
    if proposal:
        save(path, merge(state, proposal))


def check(event, wake=False):
    prompt = (event.get("prompt") or "").strip()
    path = state_file(event.get("session_id") or "")
    if not prompt or prompt.startswith("/") or not path:
        return
    state = load(path)
    if not state or not state.get("header"):
        return
    pairs = exchanges(event.get("transcript_path") or "")
    if pairs and pairs[-1][0].strip() == prompt:
        pairs = pairs[:-1]
    # The user's previous prompt only: assistant replies often mention other
    # projects, which made unrelated messages look related.
    last = f"User's previous message:\n{pairs[-1][0][:LAST_PROMPT_CHARS]}\n\n" if pairs else ""
    verdict = ask(CHECK_MODEL, CHECK_SYSTEM, f"Window context:\n{render(state)}\n\n{last}"
                                  f"New message:\n{prompt[:MAX_PROMPT_CHARS]}", CHECK_TIMEOUT_S)
    if not verdict.upper().startswith("MISMATCH"):
        return
    reason = verdict.split(":", 1)[1].strip() if ":" in verdict else "different project"
    instruction = (
        f"WRONG-HOLE GUARD: this message looks unrelated to this window's work ({reason}).\n"
        f"{render(state)}\n"
        "The user runs several sessions in parallel and sometimes types into the wrong "
        "one. Ask in one line whether this is the right window. Take no action (no edits, "
        "commits, PRs, issue changes, pushes, deploys) until the user confirms; if one is "
        "already done, say so in that line. Read-only lookups are fine."
    )
    if wake:
        print(instruction, file=sys.stderr)
        sys.exit(2)
    print(json.dumps({
        "systemMessage": f"wrong-hole guard: {reason}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": instruction,
        },
    }))


def main():
    if os.environ.get(INNER) or len(sys.argv) != 2:
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    modes = {"update": update, "check": check, "wake": lambda e: check(e, wake=True)}
    modes.get(sys.argv[1], lambda _: None)(event)


if __name__ == "__main__":
    main()
