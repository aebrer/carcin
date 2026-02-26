# 🦀 Carcin

**Claude Code from your phone, via Telegram.**

Named after [carcinization](https://en.wikipedia.org/wiki/Carcinisation) — the evolutionary phenomenon where everything eventually becomes a crab. It just keeps happening. And every Claude Code setup eventually needs a mobile interface.

Carcin bridges Telegram and Claude Code, giving you full access to your AI coding assistant from anywhere. Send a message on Telegram, get Claude Code's full power — tool use, file editing, web search, subagents, everything — streamed back to your phone in real time.

**Note:** Carcin is designed for single-user operation — one person, one bot, one machine. The architecture assumes a single active conversation at a time. If you need multi-user access, you'd want separate bot instances.

## Why Carcin?

There are other Claude-Telegram bridges. Carcin solves the problems they don't:

- **No hanging on complex tasks.** Other bridges deadlock when Claude uses TodoWrite or Task agents because stderr's pipe buffer fills up and blocks stdout. Carcin drains stderr concurrently — the fix is simple but critical.
- **Live tool visibility.** See what Claude is doing as it works: file reads, web searches, bash commands, todo checklists — all streamed as they happen, not dumped at the end.
- **Non-blocking /stop.** Interrupt Claude mid-task without killing the bot. The subprocess runs as a background asyncio task, keeping the event loop free for commands.
- **Session management.** Continue conversations, list recent sessions, resume any previous session by ID prefix.
- **File support in both directions.** Send files to Claude (images, documents, audio, video). Have Claude send files back to you via the `telegram-send` skill.
- **Message replay.** Missed a long response because Telegram was in the background? `/recent` re-sends it, properly split for Telegram's message limits.

## Features

| Feature | Description |
|---------|-------------|
| Full Claude Code access | Every tool Claude Code has — Bash, Read, Edit, Write, WebSearch, Task agents, etc. |
| Live streaming | Tool use and text responses streamed in real time as Claude works |
| TodoWrite rendering | Todo lists rendered as live checklists (✅ 🔄 ⬜) |
| File uploads | Send images, documents, audio, video — Claude analyzes them |
| File sending | Claude can send files back via `[[telegram:send:path]]` markers |
| Session persistence | `/sessions` to list, `/resume` to continue any past conversation |
| Message replay | `/recent [N]` re-sends last N messages, split for Telegram limits |
| Interrupt | `/stop` sends SIGINT — Claude stops after the current operation |
| Compaction notification | PreCompact hook alerts you when Claude is compacting context |
| Auth | Only configured Telegram user IDs can interact with the bot |

## Setup

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Python 3.12+
- A Telegram bot token (from [@BotFather](https://t.me/botfather))
- Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))

### Install

```bash
git clone https://github.com/aebrer/carcin.git
cd carcin
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Set environment variables:

```bash
export TELEGRAM_BOT_TOKEN='your-bot-token'
export ALLOWED_USER_IDS='your-telegram-user-id'
export CLAUDE_WORKING_DIR='/path/to/your/projects'  # defaults to $HOME
```

Optional:

```bash
export CLAUDE_PATH='/path/to/claude'       # defaults to 'claude' (on PATH)
export CARCIN_DONE_MARKER='Done.'          # customize the completion message
export CARCIN_SERVICE_NAME='carcin'        # systemd service name for /restart
```

### Run

```bash
python bot.py
```

### Run as a systemd service (recommended)

Copy and edit the service template:

```bash
cp carcin.service.template ~/.config/systemd/user/carcin.service
# Edit the file: fill in your bot token, user ID, paths
```

Then enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable carcin
systemctl --user start carcin
systemctl --user status carcin
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help and command list |
| `/status` | Check Claude Code connection and version |
| `/cwd` | Show current working directory |
| `/new` | Start a fresh session (next message won't continue previous) |
| `/sessions` | List recent sessions with timestamps and previews |
| `/resume <id>` | Resume a specific session (prefix matching — first 4-8 chars is enough) |
| `/recent [N]` | Re-send last N assistant messages from current session |
| `/stop` | Interrupt running Claude task via SIGINT |
| `/restart` | Restart the bot service |

## Claude Code Permission Mode

Carcin runs Claude Code non-interactively with `-p` (prompt mode). Since there's no TTY for Claude to ask permission, you'll want to configure Claude Code's permission settings appropriately.

The simplest approach is **bypass mode** (`--dangerously-skip-permissions`), which lets Claude run any tool without asking. This is powerful but means Claude has full access to your system.

**If you use bypass mode, understand what that means:**
- Claude can read, write, and delete any file your user account can access
- Claude can run any shell command
- Claude can make network requests
- There is no confirmation step

For safer operation, configure Claude Code's allowlist to permit specific tools and directories. See the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for permission configuration.

## Skills

Carcin supports Claude Code's skill system. Skills are Markdown files that teach Claude new capabilities specific to the Telegram context.

### Included: telegram-send

The `telegram-send` skill lets Claude send files back to you through Telegram. When Claude includes a `[[telegram:send:/path/to/file]]` marker in its response, the bot intercepts it and sends the file as a Telegram attachment.

### Adding your own skills

Drop skill files into the `skills/` directory following the [Claude Code skill format](https://docs.anthropic.com/en/docs/claude-code). Skills that reference Telegram-specific behavior (like `telegram-send`) go here. General-purpose skills should go in your `~/.claude/skills/` directory instead.

## Context Compaction Notification

When Claude's context window fills up, it auto-compacts — summarizing the conversation to free space. This causes a long pause with no output, which is confusing on Telegram where you can't see what's happening.

Carcin includes a [PreCompact hook](https://docs.anthropic.com/en/docs/claude-code) that sends a Telegram notification when compaction starts, so you know why the stream went quiet.

### Setup

Copy the hook script somewhere permanent:

```bash
mkdir -p ~/.claude/hooks
cp hooks/notify-compact.py ~/.claude/hooks/
```

Register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/notify-compact.py"
          }
        ]
      }
    ]
  }
}
```

The hook uses stdlib only (`urllib.request`) — no extra dependencies. It inherits `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_IDS` from the bot's subprocess chain automatically:

```
systemd service → bot.py → claude subprocess → PreCompact hook
```

## Architecture

```
Telegram ←→ Bot (bot.py) ←→ Claude Code subprocess
                               ↓
                          stdout (NDJSON stream)
                          stderr (drained concurrently)
```

- Messages from Telegram are passed to Claude Code via `-p` flag
- Claude Code runs as a subprocess with `--output-format stream-json`
- stdout is parsed as newline-delimited JSON, events streamed back to Telegram
- stderr is drained concurrently in a separate asyncio task to prevent pipe buffer deadlocks
- Each user's subprocess runs as a background asyncio task so `/stop` and other commands work without blocking
- Sessions are persisted to `~/.carcin-sessions.json` for cross-restart continuity

## Security

**This bot executes arbitrary commands on your machine.** Treat it accordingly.

- **Restrict access.** Always set `ALLOWED_USER_IDS` to your own Telegram user ID. Without this, anyone who finds your bot can run commands on your machine.
- **Understand the blast radius.** Claude Code has access to everything your user account can access. Files, environment variables, network, SSH keys — all of it.
- **Don't run as root.** Just don't.
- **Consider a sandbox.** For maximum safety, run Carcin in a VM, container, or on a dedicated user account with limited permissions.
- **Bot token security.** Your Telegram bot token is equivalent to full access to your machine (via the bot). Keep it secret. Don't commit it to version control.
- **No warranty.** This is a personal tool shared as-is. See the LICENSE file.

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by [@aebrer](https://github.com/aebrer) with Claude Code.
