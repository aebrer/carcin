#!/usr/bin/env python3
"""PreCompact hook: notify user via Telegram that context compaction is starting.

Called by Claude Code before compaction. Reads trigger info from stdin (JSON)
and sends a Telegram message so the user knows why the stream went quiet.

Requires TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS env vars (inherited from
the bot's subprocess chain: systemd → bot.py → claude → this hook).

Setup: Register this hook in ~/.claude/settings.json:
{
  "hooks": {
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/notify-compact.py"
          }
        ]
      }
    ]
  }
}
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = os.environ.get("ALLOWED_USER_IDS", "")

    if not bot_token or not allowed:
        # Can't notify without credentials — exit silently
        return

    # Parse hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    trigger = hook_input.get("trigger", "auto")
    if trigger == "manual":
        text = "\U0001f504 _Compacting context (manual)..._"
    else:
        text = "\U0001f504 _Compacting context \u2014 window full, this may take a moment..._"

    # Notify each allowed user (in private chats, user_id == chat_id)
    for user_id in allowed.split(","):
        user_id = user_id.strip()
        if not user_id:
            continue

        payload = json.dumps({
            "chat_id": int(user_id),
            "text": text,
            "parse_mode": "Markdown",
        }).encode("utf-8")

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.URLError:
            pass  # Best effort — don't block compaction


if __name__ == "__main__":
    main()
