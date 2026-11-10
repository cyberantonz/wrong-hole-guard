# wrong-hole-guard

A Claude Code hook that catches prompts typed into the wrong window.

If you run several Claude Code sessions in parallel, one per project, sooner or
later you paste a task into the wrong terminal. The session happily does it:
closes an issue, opens a PR, in the wrong context. wrong-hole-guard keeps a short
description of what each window is about and, when a new prompt clearly belongs
somewhere else, makes Claude ask "is this the right window?" before acting.

## How it works

Two hooks, both in the background, so prompts are never delayed:

- **After every response** (`Stop`, async): Haiku updates the window's context
  from the previous context, your last prompt and Claude's last reply.
- **On every prompt** (`UserPromptSubmit`, `asyncRewake`): Haiku judges the new
  prompt against that context. On a mismatch the hook wakes Claude with an
  instruction to ask you in one line before taking any action.

The context is a JSON file per session, `~/.claude/wrong-hole-guard/<session_id>.json`:

| Field | Set | Changes |
|---|---|---|
| `header` | from the session's first prompt and reply | small edits only (similarity ≥ 0.6) |
| `description` | from the first exchange | appends, or corrections with similarity ≥ 0.5; no rewrites |
| `worklog` | one line per exchange | append-only, last 50 kept |

Haiku proposes changes; the script decides what to keep, so a session's topic
cannot drift in one step. Haiku always sees the context as labelled plain text.

A fresh session (including after `/clear`) has no context yet: its first prompt
is not checked, and checking starts from the second. Installed into a running
session, the first update seeds the context from the session's first exchange
and recent prompts.

## Requirements

- Claude Code with the `claude` CLI on `PATH`, logged in (claude.ai login or API
  key both work; Haiku is called through `claude -p --model haiku`).
- Python 3.9+. No packages.

## Install

```sh
mkdir -p ~/.claude/hooks
curl -fsSL https://raw.githubusercontent.com/cyberantonz/wrong-hole-guard/main/wrong-hole-guard.py \
  -o ~/.claude/hooks/wrong-hole-guard.py
chmod +x ~/.claude/hooks/wrong-hole-guard.py
```

Merge into `~/.claude/settings.json` (keep your existing keys):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "asyncRewake": true, "timeout": 60,
                     "command": "python3 \"$HOME/.claude/hooks/wrong-hole-guard.py\" wake" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "async": true, "timeout": 120,
                     "command": "python3 \"$HOME/.claude/hooks/wrong-hole-guard.py\" update" } ] }
    ]
  }
}
```

Open `/hooks` once (or restart) if a running session does not pick it up.
`cat ~/.claude/wrong-hole-guard/*.json` shows what each window thinks it is about.

### Synchronous variant

`check` instead of `wake` runs the same judgement before the prompt reaches
Claude and injects the instruction alongside it. Nothing slips through, but
every prompt waits for Haiku (several seconds through the CLI):

```json
{ "type": "command", "timeout": 30,
  "command": "python3 \"$HOME/.claude/hooks/wrong-hole-guard.py\" check" }
```

## Limits

- Each Haiku call starts a `claude -p` process: 4-8 s typical, sometimes 20 s+.
  That is why both hooks run in the background. In `wake` mode Claude may start
  before the warning lands; the instruction asks it to say if it already acted.
- Ambiguous prompts with no visible project ("start from #4043") usually pass.
- `update` rereads the whole transcript each time; fine for normal sessions.
- Two overlapping updates: the later write wins and a worklog line can be lost.
- Every failure (timeout, no login, bad JSON) is silent; the guard never blocks
  or breaks a prompt.

## Privacy

Your prompts and excerpts of Claude's replies are sent to Haiku through your own
`claude` CLI, the same provider and account your sessions already use. Context
files stay in `~/.claude/wrong-hole-guard/` and are deleted after 30 days
without updates.

## Uninstall

Remove the two hook entries from `~/.claude/settings.json`, then
`rm -r ~/.claude/hooks/wrong-hole-guard.py ~/.claude/wrong-hole-guard`.

## License

MIT
