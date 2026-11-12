"""Black-box tests for wrong-hole-guard.py.

Each test runs the script the way Claude Code does: a hook event as JSON on
stdin, the mode as the only argument. A fake `claude` on PATH (fake_claude.py)
answers instead of the model and records every call; HOME points at a temporary directory, so
state lands in <tmp>/.claude/wrong-hole-guard and the real ~/.claude is never
touched. The tests know nothing about the script's internals.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "wrong-hole-guard.py"

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"

HEADER = "gears CI cache stats collector"
DESCRIPTION = "Fixing the cache-stats collector on ci-gw-1 for gears-rust."
FIRST_PROMPT = "check the gears CI cache stats in grafana"
SECOND_PROMPT = "ok, fix the step-name matcher"
FIRST_REPLY = "Found it: gears-rust renamed the ci-cache step."
SECOND_REPLY = "Fixed in MR !26 and deployed to ci-gw-1."


def proposal(header=HEADER, description=DESCRIPTION, entry="Fixed the matcher"):
    return json.dumps({"header": header, "description": description, "worklog_entry": entry})


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="whg-test-"))
        self.home = self.root / "home"
        self.state_dir = self.home / ".claude" / "wrong-hole-guard"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").symlink_to(FAKE_CLAUDE)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "WHG_TEST_ROOT": str(self.root),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        self.env.pop("WRONG_HOLE_GUARD_INNER", None)
        self.transcript = self.root / "transcript.jsonl"
        self.write_transcript([(FIRST_PROMPT, FIRST_REPLY), (SECOND_PROMPT, SECOND_REPLY)])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- helpers ---------------------------------------------------------

    def write_transcript(self, exchanges, extra=()):
        lines = []
        for prompt, reply in exchanges:
            lines.append({"type": "user", "message": {"content": prompt}})
            lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
        lines.extend(extra)
        self.transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    def model_replies(self, model, text):
        (self.root / f"reply_{model}.txt").write_text(text)

    def event(self, **overrides):
        event = {
            "session_id": "sess-1",
            "transcript_path": str(self.transcript),
            "cwd": str(self.root),
            "hook_event_name": "Stop",
        }
        event.update(overrides)
        return event

    def run_guard(self, mode, event=None, stdin=None, extra_env=None, args=None):
        env = {**self.env, **(extra_env or {})}
        argv = [sys.executable, str(SCRIPT)] + (args if args is not None else [mode])
        data = stdin if stdin is not None else json.dumps(event if event is not None else self.event())
        result = subprocess.run(argv, input=data, capture_output=True, text=True, env=env, timeout=60)
        return result

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def state_file(self, session="sess-1"):
        return self.state_dir / f"{session}.json"

    def state(self, session="sess-1"):
        return json.loads(self.state_file(session).read_text())

    def write_state(self, data, session="sess-1"):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file(session).write_text(data if isinstance(data, str) else json.dumps(data))

    def worklog(self, state):
        # The worklog's key is the script's business; the stored list is what matters.
        lists = [v for k, v in state.items() if k.startswith("worklog") and isinstance(v, list)]
        self.assertEqual(len(lists), 1, f"expected one worklog list in the state, got keys {sorted(state)}")
        return lists[0]

    def assertQuiet(self, result, code=0):
        self.assertEqual(result.returncode, code, f"exit {result.returncode}\nstderr:\n{result.stderr}")
        self.assertNotIn("Traceback", result.stderr)


class UpdateTests(GuardTest):
    def test_first_update_creates_context_from_model_proposal(self):
        self.model_replies("haiku", proposal())
        result = self.run_guard("update")
        self.assertQuiet(result)
        state = self.state()
        self.assertEqual(state["header"], HEADER, "header must keep the model's wording and case")
        self.assertEqual(state["description"], DESCRIPTION)
        self.assertEqual(self.worklog(state), ["Fixed the matcher"])

    def test_first_update_seeds_from_first_exchange_and_latest_one(self):
        self.model_replies("haiku", proposal())
        self.run_guard("update")
        (call,) = self.calls()
        self.assertEqual(call["model"], "haiku")
        self.assertIn(FIRST_PROMPT, call["input"])
        self.assertIn(FIRST_REPLY, call["input"])
        self.assertIn(SECOND_PROMPT, call["input"])
        self.assertIn(SECOND_REPLY, call["input"])

    def test_worklog_survives_save_and_reload(self):
        self.model_replies("haiku", proposal(entry="first step"))
        self.run_guard("update")
        self.model_replies("haiku", proposal(entry="second step"))
        self.run_guard("update")
        self.assertEqual(self.worklog(self.state()), ["first step", "second step"])

    def test_later_update_sends_current_context_as_plain_text(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": ["earlier step"]})
        self.model_replies("haiku", proposal())
        self.run_guard("update")
        (call,) = self.calls()
        self.assertIn(f"Topic: {HEADER}", call["input"])
        self.assertIn(f"About: {DESCRIPTION}", call["input"])
        self.assertIn("- earlier step", call["input"])
        self.assertNotIn('"header"', call["input"], "the model must not see raw JSON")

    def test_header_accepts_small_edit(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": []})
        self.model_replies("haiku", proposal(header="gears CI cache-stats collector"))
        self.run_guard("update")
        self.assertEqual(self.state()["header"], "gears CI cache-stats collector")

    def test_header_rejects_rewrite(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": []})
        self.model_replies("haiku", proposal(header="Slack connector monitoring"))
        self.run_guard("update")
        self.assertEqual(self.state()["header"], HEADER)

    def test_description_accepts_append(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": []})
        appended = DESCRIPTION + " Also tuning S3 retention."
        self.model_replies("haiku", proposal(description=appended))
        self.run_guard("update")
        self.assertEqual(self.state()["description"], appended)

    def test_description_rejects_rewrite(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": []})
        self.model_replies("haiku", proposal(description="Building a Slack bot for team standups."))
        self.run_guard("update")
        self.assertEqual(self.state()["description"], DESCRIPTION)

    def test_worklog_is_capped(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": [f"step {i}" for i in range(60)]})
        self.model_replies("haiku", proposal(entry="newest"))
        self.run_guard("update")
        worklog = self.worklog(self.state())
        self.assertLessEqual(len(worklog), 50)
        self.assertEqual(worklog[-1], "newest")

    def test_model_failure_is_silent_and_keeps_state(self):
        original = {"header": HEADER, "description": DESCRIPTION, "worklog": ["kept"]}
        self.write_state(original)
        (self.root / "exit_code").write_text("1")
        result = self.run_guard("update")
        self.assertQuiet(result)
        self.assertEqual(self.state()["header"], HEADER)
        self.assertEqual(self.worklog(self.state()), ["kept"])

    def test_unparseable_model_reply_is_silent(self):
        self.model_replies("haiku", "Sure! Here is my summary of the session.")
        result = self.run_guard("update")
        self.assertQuiet(result)
        self.assertFalse(self.state_file().exists())

    def test_json_wrapped_in_prose_is_accepted(self):
        self.model_replies("haiku", f"Here you go:\n```json\n{proposal()}\n```")
        self.run_guard("update")
        self.assertEqual(self.state()["header"], HEADER)

    def test_missing_transcript_is_silent(self):
        result = self.run_guard("update", self.event(transcript_path=str(self.root / "absent.jsonl")))
        self.assertQuiet(result)
        self.assertEqual(self.calls(), [])

    def test_notifications_are_not_user_prompts(self):
        self.write_transcript(
            [(FIRST_PROMPT, FIRST_REPLY)],
            extra=[
                {"type": "user", "message": {"content": "<task-notification> job done </task-notification>"}},
                {"type": "user", "isMeta": True, "message": {"content": "meta entry"}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "tool output"}]}},
            ],
        )
        self.model_replies("haiku", proposal())
        self.run_guard("update")
        (call,) = self.calls()
        self.assertNotIn("task-notification", call["input"])
        self.assertNotIn("meta entry", call["input"])
        self.assertNotIn("tool output", call["input"])

    def test_old_context_files_are_pruned(self):
        self.write_state({"header": "old"}, session="stale")
        old = time.time() - 31 * 24 * 3600
        os.utime(self.state_file("stale"), (old, old))
        self.model_replies("haiku", proposal())
        self.run_guard("update")
        self.assertFalse(self.state_file("stale").exists())
        self.assertTrue(self.state_file().exists())


class DamagedStateTests(GuardTest):
    def test_invalid_json_is_rebuilt(self):
        self.write_state("{broken")
        self.model_replies("haiku", proposal())
        result = self.run_guard("update")
        self.assertQuiet(result)
        self.assertEqual(self.state()["header"], HEADER)

    def test_non_object_is_rebuilt(self):
        self.write_state("[1, 2, 3]")
        self.model_replies("haiku", proposal())
        result = self.run_guard("update")
        self.assertQuiet(result)
        self.assertEqual(self.state()["header"], HEADER)

    def test_wrong_field_types_are_reset(self):
        self.write_state({"header": 5, "description": None, "worklog": "oops"})
        self.model_replies("haiku", proposal())
        result = self.run_guard("update")
        self.assertQuiet(result)
        self.assertEqual(self.state()["header"], HEADER)
        self.assertEqual(self.worklog(self.state()), ["Fixed the matcher"])

    def test_mixed_worklog_keeps_text_entries(self):
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": ["keep me", 3, None, "and me"]})
        self.model_replies("haiku", proposal(entry="new"))
        self.run_guard("update")
        self.assertEqual(self.worklog(self.state()), ["keep me", "and me", "new"])


class CheckTests(GuardTest):
    def setUp(self):
        super().setUp()
        self.write_state({"header": HEADER, "description": DESCRIPTION, "worklog": ["Fixed the matcher"]})
        self.prompt_event = self.event(
            hook_event_name="UserPromptSubmit", prompt="ok, close https://github.com/acme/other-repo/issues/7"
        )

    def test_fit_is_silent(self):
        self.model_replies("sonnet", "FIT")
        result = self.run_guard("check", self.prompt_event)
        self.assertQuiet(result)
        self.assertEqual(result.stdout, "")

    def test_check_uses_check_model(self):
        self.model_replies("sonnet", "FIT")
        self.run_guard("check", self.prompt_event)
        (call,) = self.calls()
        self.assertEqual(call["model"], "sonnet")

    def test_mismatch_injects_instruction_and_warns(self):
        self.model_replies("sonnet", "MISMATCH: different repository other-repo")
        result = self.run_guard("check", self.prompt_event)
        self.assertQuiet(result)
        out = json.loads(result.stdout)
        self.assertIn("different repository other-repo", out["systemMessage"], "reason must keep its case")
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "UserPromptSubmit")
        self.assertIn("different repository other-repo", hook["additionalContext"])
        self.assertIn(HEADER, hook["additionalContext"])

    def test_lowercase_verdict_counts(self):
        self.model_replies("sonnet", "mismatch: other project")
        result = self.run_guard("check", self.prompt_event)
        self.assertTrue(result.stdout, "a lower-case MISMATCH must still be reported")

    def test_mismatch_without_reason(self):
        self.model_replies("sonnet", "MISMATCH")
        result = self.run_guard("check", self.prompt_event)
        self.assertQuiet(result)
        self.assertTrue(json.loads(result.stdout)["systemMessage"])

    def test_wake_mode_exits_2_with_instruction_on_stderr(self):
        self.model_replies("sonnet", "MISMATCH: different repository other-repo")
        result = self.run_guard("wake", self.prompt_event)
        self.assertEqual(result.returncode, 2)
        self.assertIn("different repository other-repo", result.stderr)
        self.assertIn(HEADER, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_wake_mode_fit_exits_0(self):
        self.model_replies("sonnet", "FIT")
        result = self.run_guard("wake", self.prompt_event)
        self.assertQuiet(result)

    def test_previous_user_prompt_is_sent_but_not_the_reply(self):
        self.model_replies("sonnet", "FIT")
        self.run_guard("check", self.prompt_event)
        (call,) = self.calls()
        self.assertIn(SECOND_PROMPT, call["input"])
        self.assertNotIn(SECOND_REPLY, call["input"], "assistant replies are left out on purpose")
        self.assertIn("other-repo/issues/7", call["input"])

    def test_current_prompt_already_in_transcript_is_not_its_own_previous(self):
        self.write_transcript(
            [(FIRST_PROMPT, FIRST_REPLY), (SECOND_PROMPT, SECOND_REPLY)],
            extra=[{"type": "user", "message": {"content": self.prompt_event["prompt"]}}],
        )
        self.model_replies("sonnet", "FIT")
        self.run_guard("check", self.prompt_event)
        (call,) = self.calls()
        previous = call["input"].split("New message:")[0]
        self.assertIn(SECOND_PROMPT, previous)
        self.assertNotIn("other-repo/issues/7", previous)

    def test_no_context_yet_skips_model(self):
        self.state_file().unlink()
        result = self.run_guard("check", self.prompt_event)
        self.assertQuiet(result)
        self.assertEqual(self.calls(), [])

    def test_empty_header_skips_model(self):
        self.write_state({"header": "", "description": DESCRIPTION, "worklog": []})
        result = self.run_guard("check", self.prompt_event)
        self.assertQuiet(result)
        self.assertEqual(self.calls(), [])

    def test_slash_command_skips_model(self):
        result = self.run_guard("check", self.event(prompt="/clear"))
        self.assertQuiet(result)
        self.assertEqual(self.calls(), [])

    def test_model_failure_is_silent(self):
        (self.root / "exit_code").write_text("1")
        result = self.run_guard("wake", self.prompt_event)
        self.assertQuiet(result)


class SafetyTests(GuardTest):
    def test_recursion_guard_skips_everything(self):
        self.model_replies("haiku", proposal())
        result = self.run_guard("update", extra_env={"WRONG_HOLE_GUARD_INNER": "1"})
        self.assertQuiet(result)
        self.assertEqual(self.calls(), [])

    def test_inner_claude_runs_with_recursion_guard_and_no_hooks(self):
        self.model_replies("haiku", proposal())
        self.run_guard("update")
        (call,) = self.calls()
        self.assertEqual(call["inner"], "1")
        self.assertIn("--no-session-persistence", call["args"])
        self.assertIn('{"disableAllHooks":true}', call["args"])

    def test_garbage_stdin_is_silent(self):
        for mode in ("update", "check", "wake"):
            with self.subTest(mode=mode):
                self.assertQuiet(self.run_guard(mode, stdin="not json"))

    def test_empty_event_is_silent(self):
        for mode in ("update", "check", "wake"):
            with self.subTest(mode=mode):
                self.assertQuiet(self.run_guard(mode, event={}))
        self.assertEqual(self.calls(), [])

    def test_unknown_mode_and_bad_args_are_silent(self):
        self.assertQuiet(self.run_guard(None, args=["nope"]))
        self.assertQuiet(self.run_guard(None, args=[]))
        self.assertQuiet(self.run_guard(None, args=["check", "extra"]))
        self.assertEqual(self.calls(), [])

    def test_session_id_cannot_escape_state_dir(self):
        self.model_replies("haiku", proposal())
        self.run_guard("update", self.event(session_id="../../escape"))
        self.assertFalse((self.home / "escape.json").exists())
        self.assertFalse((self.home / ".claude" / "escape.json").exists())
        written = list(self.state_dir.glob("*.json"))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].parent, self.state_dir)


if __name__ == "__main__":
    unittest.main()
