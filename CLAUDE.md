# CLAUDE.md — Carcin

This is a Claude Code Telegram bridge. When responding via Telegram:

- Keep responses concise — Telegram truncates long messages at ~4096 characters
- The bot splits long messages automatically, but shorter is better
- Use the `telegram-send` skill to send files back to the user
- EnterPlanMode doesn't render on Telegram — discuss plans in regular messages instead
- The user may be on mobile — keep that in mind for formatting

## About This Project

Single-file Python bot (`bot.py`) that bridges Telegram and Claude Code.

### Key Architecture Decisions

- Claude Code runs as a subprocess with `--output-format stream-json`
- stderr is drained concurrently to prevent pipe buffer deadlocks (this is critical)
- Each user's subprocess runs as a background asyncio task so /stop works without blocking
- Sessions persist to disk for cross-restart continuity
