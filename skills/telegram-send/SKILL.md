---
name: telegram-send
description: Send files to the user via Telegram. Use when responding through the Telegram bot and the user asks you to send, share, or deliver a file to them.
---

# Telegram File Send

When responding via the Telegram bot bridge and you need to send a file to the user, include this marker in your response:

```
[[telegram:send:/absolute/path/to/file]]
```

The bot will detect this pattern, strip it from the displayed message, and send the file as a Telegram attachment.

## When to use

- User asks "send me that file"
- User asks "share the script you created"
- User asks to "deliver" or "export" something as a file
- After creating a file the user explicitly wants sent to them

## Examples

**User:** "Create a Python script and send it to me"
```
Here's the script I created.

[[telegram:send:/tmp/hello.py]]
```

**User:** "Send me my bashrc"
```
Here's your bashrc file:

[[telegram:send:/home/user/.bashrc]]
```

## Notes

- Use absolute paths only
- The file must exist and be readable
- Multiple files: include multiple `[[telegram:send:...]]` markers
- Only works when responding via the Telegram bot (the bot parses this syntax)
- The marker is stripped from the message before display
