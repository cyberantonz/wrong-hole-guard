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

from __future__ import annotations

# any nonstandard libs, like pydantic, are prohibited
import difflib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".claude" / "wrong-hole-guard"
# This env value stops running this script recursively in sessions started by this script
INNER = "WRONG_HOLE_GUARD_INNER"
# The check needs judgement (Haiku let "ok, merged <other repo's issue>" through);
# updates are cheap summaries guarded by the stability rules below.
CHECK_MODEL = "sonnet"
UPDATE_MODEL = "haiku"
UPDATE_TIMEOUT_S = 90
CHECK_TIMEOUT_S = 25
PRUNE_AFTER_S = 30 * 24 * 60 * 60
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
NOT_A_PROMPT = (
    "<task-notification",
    "<command-",
    "<local-command",
    "Another Claude session",
    "<system-reminder",
    "[SYSTEM NOTIFICATION",
)
MISMATCH = "mismatch"


UPDATE_SYSTEM = (
    "You keep the context of one terminal window running an AI coding assistant, so a later "
    "check can tell whether a new message belongs to this window. Reply with one JSON object "
    'and nothing else: {"header": ..., "description": ..., "worklog_entry": ...}.\n'
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

Event = dict[str, Any]


@dataclass
class State:
    header: str = field(default="")
    description: str = field(default="")
    worklog_entries: list[str] = field(default_factory=list)
    created: int = field(default_factory=lambda: int(time.time()))
    updated: int = field(default=0)

    def save(self, path: Path) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        tmp_file = path.with_suffix(".tmp")
        state_dict = asdict(self)

        tmp_file.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2) + "\n")
        tmp_file.replace(path)

    @classmethod
    def from_event(cls, event: Event) -> State | None:
        """
        The stored context, or None when absent or unreadable. Fields of the wrong
        type (a hand-edited or damaged file) are reset rather than trusted.
        """
        state_file = state_file_from_event(event)

        if state_file is None:
            return None

        try:
            loaded_state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            return None

        if not isinstance(loaded_state, dict):
            return None

        state = State()

        # One by one, not so much keys
        header = loaded_state.get("header")

        if isinstance(header, str):
            state.header = header

        if not state.header:
            return None

        description = loaded_state.get("description")

        if isinstance(description, str):
            state.description = description

        created = loaded_state.get("created")

        if isinstance(created, int):
            state.created = created

        updated = loaded_state.get("updated")

        if isinstance(updated, int):
            state.updated = updated

        worklog = loaded_state.get("worklog") or loaded_state.get("worklog_entries")

        if isinstance(worklog, list):
            entries = []

            for item in worklog:
                if not isinstance(item, str):
                    continue

                entries.append(item)

            state.worklog_entries = entries

        return state

    def render(self) -> str:
        worklog = "\n".join(f"- {e}" for e in self.worklog_entries[-SHOW_WORKLOG:]) or "- (none yet)"

        return f"Topic: {self.header}\nAbout: {self.description}\nRecent work:\n{worklog}"


@dataclass
class Exchange:
    user: str
    assistant: str = field(default="")


def ask_model(model: str, system_prompt: str, user: str, timeout: int) -> str | None:
    env = dict(os.environ, **{INNER: "1"})

    try:
        out = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--no-session-persistence",
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
                "--tools",
                "",
                "--strict-mcp-config",
                "--system-prompt",
                system_prompt,
            ],
            input=user,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if out.returncode != 0:
        return None

    return out.stdout.strip()


def safe_string_from_event(event: Event, key: str) -> str | None:
    value = event.get(key)

    if value is None:
        return None

    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value:
        return None

    return value


def state_file_from_event(event: Event) -> Path | None:
    session_id = safe_string_from_event(event, "session_id")

    if session_id is None:
        return None

    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")

    if not safe:
        return None

    return STATE_DIR / f"{safe}.json"


def prune() -> None:
    cutoff = time.time() - PRUNE_AFTER_S
    for old in STATE_DIR.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def linearize_conversation_list(items_list: list[dict[str, Any]]) -> str:
    parts = []

    for item in items_list:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "text":
            continue

        part = item.get("text")

        if part is None:
            continue

        if not isinstance(part, str):
            continue

        parts.append(part)

    return "\n".join(parts)


def process_line(line: str) -> tuple[str | None, str | None]:
    try:
        e = json.loads(line)
    except ValueError:
        return None, None

    if not isinstance(e, dict):
        return None, None

    if e.get("isMeta"):
        return None, None

    message = e.get("message")

    if message is None:
        return None, None

    if not isinstance(message, dict):
        return None, None

    text_or_list = message.get("content")

    if text_or_list is None:
        return None, None

    text = ""

    if isinstance(text_or_list, list):
        text = linearize_conversation_list(text_or_list)
    elif isinstance(text_or_list, str):
        text = text_or_list

    text = text.strip()

    if not text:
        return None, None

    if e.get("type") == "user" and not text.startswith(NOT_A_PROMPT):
        return text, None

    if e.get("type") == "assistant":
        return None, text

    return None, None


def get_conversation_from_event(event: Event) -> list[Exchange]:
    """[(prompt, last assistant text after it)] for the user's real prompts."""
    exchanges: list[Exchange] = []

    transcript_path_str = safe_string_from_event(event, "transcript_path")

    if transcript_path_str is None:
        return []

    transcript_file = Path(transcript_path_str)

    if not transcript_file.exists():
        return []

    try:
        with transcript_file.open(encoding="utf-8") as f:
            for line in f:
                user, assistant = process_line(line)

                if not user and not assistant:
                    continue

                if user:
                    exchanges.append(Exchange(user=user))
                    continue

                if assistant and exchanges:
                    exchanges[-1].assistant = assistant
    except OSError:
        pass

    return exchanges


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_and_parse_json(reply: str) -> Event | None:
    match = re.search(r"\{.*\}", reply, re.DOTALL)

    if not match:
        return None

    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None

    if not isinstance(value, dict):
        return None

    return value


def merge(state: State, proposal: Event) -> State:
    new_header = str(proposal.get("header") or "").strip()[:MAX_HEADER_CHARS]
    new_description = str(proposal.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]
    new_entry = str(proposal.get("worklog_entry") or "").strip()[:MAX_ENTRY_CHARS]

    if new_header and (not state.header or similar(state.header, new_header) >= HEADER_MIN_SIMILARITY):
        state.header = new_header

    if new_description and (
        not state.description
        or new_description.startswith(state.description)
        or similar(state.description, new_description) >= DESCRIPTION_MIN_SIMILARITY
    ):
        state.description = new_description

    if new_entry:
        worklog = state.worklog_entries
        worklog.append(new_entry)
        state.worklog_entries = worklog[-KEEP_WORKLOG:]

    state.updated = int(time.time())
    return state


def update(event: Event) -> None:
    prune()

    exchanges = get_conversation_from_event(event)

    if not exchanges:
        return None

    prompt, reply = exchanges[-1].user[:MAX_PROMPT_CHARS], exchanges[-1].assistant[-MAX_REPLY_CHARS:]
    state = State.from_event(event)

    if state:
        history = f"Current context:\n{state.render()}"
    else:
        # First run: the session's first exchange sets the topic, recent prompts fill in.
        first_prompt = exchanges[0].user[:MAX_PROMPT_CHARS]
        first_reply = exchanges[0].assistant[-MAX_REPLY_CHARS:]
        recent = "\n".join(f"- {p.user[:SEED_PROMPT_CHARS]}" for p in exchanges[1:-1][-SEED_PROMPTS:])
        history = (
            f"No context yet.\n\nSession's first message:\n{first_prompt}\n\n"
            f"First reply:\n{first_reply}\n\nLater user messages:\n{recent or '- (none)'}"
        )
        state = State()

    response = ask_model(
        UPDATE_MODEL,
        UPDATE_SYSTEM,
        f"{history}\n\nLatest user message:\n{prompt}\n\nLatest reply:\n{reply}",
        UPDATE_TIMEOUT_S,
    )

    if response is None:
        return None

    proposal = find_and_parse_json(response)

    if proposal is None:
        return None

    path = state_file_from_event(event)

    if path is None:
        return None

    merged_state = merge(state, proposal)

    merged_state.save(path)


def check(event: Event, wake: bool = False) -> None:
    prompt = safe_string_from_event(event, "prompt")

    if prompt is None:
        return None

    if prompt.startswith("/"):
        return None

    path = state_file_from_event(event)

    if path is None:
        return None

    state = State.from_event(event)

    if state is None:
        return None

    exchanges = get_conversation_from_event(event)

    if exchanges and exchanges[-1].user == prompt:
        exchanges = exchanges[:-1]

    # The user's previous prompt only: assistant replies often mention other
    # projects, which made unrelated messages look related.
    last = f"User's previous message:\n{exchanges[-1].user[:LAST_PROMPT_CHARS]}\n\n" if exchanges else ""

    verdict = ask_model(
        CHECK_MODEL,
        CHECK_SYSTEM,
        f"Window context:\n{state.render()}\n\n{last}New message:\n{prompt[:MAX_PROMPT_CHARS]}",
        CHECK_TIMEOUT_S,
    )

    if verdict is None:
        return None

    verdict_match = verdict[: len(MISMATCH)]

    if not verdict_match.lower() == MISMATCH:
        return None

    # Fragile. Need to redo.
    reason = verdict.split(":", 1)[1].strip() if ":" in verdict else "different project"

    instruction = (
        f"WRONG-HOLE GUARD: this message looks unrelated to this window's work ({reason}).\n"
        f"{state.render()}\n"
        "The user runs several sessions in parallel and sometimes types into the wrong "
        "one. Ask in one line whether this is the right window. Take no action (no edits, "
        "commits, PRs, issue changes, pushes, deploys) until the user confirms; if one is "
        "already done, say so in that line. Read-only lookups are fine."
    )

    if wake:
        print(instruction, file=sys.stderr)  # noqa: T201
        sys.exit(2)

    print(  # noqa: T201
        json.dumps(
            {
                "systemMessage": f"wrong-hole guard: {reason}",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": instruction},
            }
        )
    )


def check_no_wake(event: Event) -> None:
    return check(event, wake=False)


def check_and_wake(event: Event) -> None:
    return check(event, wake=True)


def main() -> None:
    # We called claude, just do nothing
    if os.environ.get(INNER):
        return None

    if len(sys.argv) != 2:
        return None

    # Get claude event passed to the hook
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return None

    if not isinstance(event, dict):
        return None

    available_handlers = {"update": update, "check": check_no_wake, "wake": check_and_wake}

    command_handler = available_handlers.get(sys.argv[1])

    if command_handler is None:
        return None

    command_handler(event)


if __name__ == "__main__":
    main()
