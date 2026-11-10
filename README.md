# wrong-hole-guard: stop Claude Code from acting on a prompt typed into the wrong window

**A Claude Code hook that detects when you send a prompt to the wrong session
and makes Claude ask "is this the right window?" before it changes anything.**

You run several Claude Code sessions in parallel, one terminal per project or
repository. You paste a task meant for one session into another. Claude does not
know it is the wrong window, so it does the work in the wrong context: edits the
wrong repo, closes an issue, opens a pull request, runs a deploy.

wrong-hole-guard keeps a short, stable description of what each Claude Code
window is about, and checks every new prompt against it with Claude Haiku. When
a prompt clearly belongs to a different project, the hook tells Claude to stop
and confirm with you first. Prompts are never delayed and never blocked.

Works with Claude Code hooks (`UserPromptSubmit`, `Stop`), the `claude` CLI and
your existing login. No API key, no packages, one Python file.

## The problem it solves

- Multiple Claude Code sessions open at once: tmux panes, iTerm2 or Ghostty
  tabs, several terminal windows, worktrees.
- A prompt goes to the wrong terminal. It happens to anyone switching between
  projects all day, often with a copied issue or PR link.
- Claude Code has no built-in notion of "this session is about project X",
  and a model that follows instructions will happily do a task in the wrong
  repository.
- Rules in `CLAUDE.md` or memory ("ask when the topic changes") are easy for the
  model to miss. A hook runs every time.

## How it works

Two hooks, both running in the background:

1. **After every response** (`Stop` hook, async), Haiku updates the window's
   context from the previous context, your last prompt and Claude's last reply.
2. **On every prompt** (`UserPromptSubmit` hook, `asyncRewake`), Haiku judges
   the new prompt against that context and answers `FIT` or `MISMATCH: reason`.
   On a mismatch the hook wakes Claude with an instruction: ask the user in one
   line whether this is the right window, and take no action until they confirm.

Follow-ups, approvals ("ok", "merged"), questions about the same systems and
plausibly related new tasks count as a fit. Only a clearly different project,
repository or system triggers the guard.

### The window context

One JSON file per session, `~/.claude/wrong-hole-guard/<session_id>.json`:

| Field | Set from | How it may change |
|---|---|---|
| `header` | the session's first prompt and reply | small edits only (text similarity ≥ 0.6) |
| `description` | the first exchange | appends, or small corrections (similarity ≥ 0.5); never rewritten |
| `worklog` | one line per exchange | append-only, last 50 kept |

Haiku proposes changes and the script decides what to keep, so a session's
topic cannot drift away in one step. Haiku always sees the context as labelled
plain text (`Topic`, `About`, `Recent work`), never raw JSON.

A new session, including after `/clear`, has no context yet: its first prompt is
not checked, and checking starts from the second. `/compact` and `--resume` keep
the session and its context. Installed into a session that is already running,
the first update seeds the context from that session's first exchange and recent
prompts.

## Install

Requirements: Claude Code with the `claude` CLI on `PATH` and logged in (a
claude.ai subscription or an API key both work), and Python 3.9 or newer.

```sh
mkdir -p ~/.claude/hooks
curl -fsSL https://raw.githubusercontent.com/cyberantonz/wrong-hole-guard/main/wrong-hole-guard.py \
  -o ~/.claude/hooks/wrong-hole-guard.py
chmod +x ~/.claude/hooks/wrong-hole-guard.py
```

Merge this into `~/.claude/settings.json`, keeping your existing keys:

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

User-level settings apply to every Claude Code window. If a running session does
not pick the change up, open `/hooks` once or restart it. To see what each
window thinks it is about:

```sh
cat ~/.claude/wrong-hole-guard/*.json
```

### Synchronous mode

Use `check` instead of `wake` to judge the prompt before it reaches Claude and
inject the instruction alongside it. Nothing slips through, but every prompt
waits for Haiku, several seconds through the CLI:

```json
{ "type": "command", "timeout": 30,
  "command": "python3 \"$HOME/.claude/hooks/wrong-hole-guard.py\" check" }
```

## FAQ

### How do I stop Claude Code from running a prompt I sent to the wrong terminal?

Install this hook. It cannot un-send the prompt, but Claude is told to confirm
before editing files, committing, opening pull requests, changing issues,
pushing or deploying.

### Why not just colour the terminal tabs or rename sessions?

Do that too: `/rename` sets the tab title, and plugins can colour tabs per
project. They help you avoid the mistake before pressing Enter. This hook is the
safety net for when you are in the wrong tab anyway.

### Why not block the prompt instead of warning?

A `UserPromptSubmit` hook that blocks removes the prompt from the conversation,
so you would have to retype it. Claude Code's built-in model-evaluated hook
types (`"type": "prompt"` and `"agent"`) are not available for
`UserPromptSubmit`, so the hook calls Haiku through the `claude` CLI itself.

### Does it need an API key?

No. It runs `claude -p --model haiku` with whatever login your Claude Code
already uses.

### Does it slow down my prompts?

Not in the default `wake` mode: the check runs alongside your prompt and only
interrupts on a mismatch. The trade-off is that Claude may start before the
warning arrives (usually with read-only steps); it is told to say so if it
already acted.

### Will it nag me when I deliberately change topic?

At most once. After you confirm, the next context update records the new topic.
To repurpose a window completely, `/clear` starts a fresh session and context.

## Limits

- Each Haiku call starts a `claude -p` process: typically 4-8 s, sometimes over
  20 s. That is why both hooks run in the background.
- Prompts with no visible project, such as "start from #4043", usually pass.
- `update` rereads the session transcript each time, which is fine for normal
  sessions.
- If two updates overlap, the later write wins and a worklog line can be lost.
- Every failure (timeout, no login, unparseable reply) is silent. The guard
  never blocks or breaks a prompt.

## Privacy

Your prompts and excerpts of Claude's replies are sent to Haiku through your own
`claude` CLI: the same provider and account your sessions already use. Context
files stay in `~/.claude/wrong-hole-guard/` and are deleted after 30 days
without updates.

## Uninstall

Remove the two hook entries from `~/.claude/settings.json`, then:

```sh
rm -r ~/.claude/hooks/wrong-hole-guard.py ~/.claude/wrong-hole-guard
```

## Keywords

Claude Code hook, wrong window, wrong terminal, wrong session, prompt sent to
the wrong Claude Code session, multiple Claude Code sessions, parallel Claude
Code agents, topic drift detection, context guard, UserPromptSubmit hook,
asyncRewake, Stop hook, Claude Haiku classifier, tmux, iTerm2, Ghostty, git
worktrees, AI coding agent safety, confirm before acting.

## License

MIT, see [LICENSE](LICENSE).
