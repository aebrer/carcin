#!/usr/bin/env python3
"""
Carcin — Claude Code Telegram Bridge
Forwards messages to Claude Code and streams back tool use + responses.

Named after carcinization: the evolutionary phenomenon where everything
eventually becomes a crab. It just keeps happening.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from telegram import Update, InputFile
from telegram.ext import Application, MessageHandler, CommandHandler, filters
from telegram.constants import ParseMode

# Pattern for file send requests: [[telegram:send:/path/to/file]]
SEND_FILE_PATTERN = re.compile(r'\[\[telegram:send:([^\]]+)\]\]')

# Directory for downloaded files
UPLOAD_DIR = Path(tempfile.gettempdir()) / "carcin-uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

def log(msg):
    print(msg, flush=True)

# === CONFIGURATION ===
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x]
WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR", str(Path.home()))
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
DONE_MARKER = os.environ.get("CARCIN_DONE_MARKER", "Done.")

# Session management
USER_SESSIONS: dict[int, str] = {}
NEW_SESSION_FLAG: set[int] = set()  # Users who want next message to start fresh
RESUME_SESSION: dict[int, str] = {}  # Users who want to resume a specific session
RUNNING_PROCS: dict[int, asyncio.subprocess.Process] = {}  # Active subprocess per user

# Per-user message queue — prevents conversation forking by ensuring only one
# Claude subprocess runs at a time. Messages arriving while Claude is busy
# are queued and processed sequentially.
USER_QUEUES: dict[int, asyncio.Queue] = {}
QUEUE_PROCESSORS: dict[int, asyncio.Task] = {}

# File batching — handles two cases:
# 1. Media groups (photos/videos): Telegram assigns a shared media_group_id
# 2. Rapid sequential files (documents): no media_group_id, so we debounce per-user
# Both use the same buffer/timer pattern with different keys.
MEDIA_GROUP_BUFFERS: dict[str, dict] = {}  # buffer_key -> {files, caption, ...}
MEDIA_GROUP_TIMERS: dict[str, asyncio.Task] = {}  # buffer_key -> delayed task

# Session persistence file
SESSIONS_FILE = Path(os.environ.get("CLAUDE_SESSIONS_FILE", Path.home() / ".carcin-sessions.json"))

def load_sessions() -> dict:
    """Load saved sessions from disk."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_sessions(sessions: dict):
    """Save sessions to disk."""
    try:
        SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))
    except OSError as e:
        log(f"[WARN] Failed to save sessions: {e}")

def record_session(user_id: int, session_id: str, preview: str):
    """Record a session with metadata."""
    sessions = load_sessions()
    user_key = str(user_id)
    if user_key not in sessions:
        sessions[user_key] = []
    # Don't duplicate if same session_id already recorded
    for s in sessions[user_key]:
        if s["session_id"] == session_id:
            return
    sessions[user_key].append({
        "session_id": session_id,
        "timestamp": time.time(),
        "preview": preview[:80],
    })
    # Keep last 20 sessions per user
    sessions[user_key] = sessions[user_key][-20:]
    save_sessions(sessions)


def format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Format tool use for display in Telegram."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            return f"🔧 *Bash*: {desc}\n`{cmd[:500]}`"
        return f"🔧 *Bash*\n`{cmd[:500]}`"
    elif tool_name == "Read":
        return f"📖 *Read*: `{tool_input.get('file_path', '?')}`"
    elif tool_name == "Edit":
        return f"✏️ *Edit*: `{tool_input.get('file_path', '?')}`"
    elif tool_name == "Write":
        return f"📝 *Write*: `{tool_input.get('file_path', '?')}`"
    elif tool_name == "Glob":
        return f"🔍 *Glob*: `{tool_input.get('pattern', '?')}`"
    elif tool_name == "Grep":
        return f"🔎 *Grep*: `{tool_input.get('pattern', '?')}`"
    elif tool_name == "WebSearch":
        return f"🌐 *WebSearch*: {tool_input.get('query', '?')}"
    elif tool_name == "WebFetch":
        return f"🌐 *WebFetch*: {tool_input.get('url', '?')[:50]}"
    elif tool_name == "Task":
        return f"🤖 *Task* ({tool_input.get('subagent_type', '?')}): {tool_input.get('description', '?')}"
    elif tool_name == "TodoWrite":
        return format_todo_list(tool_input)
    elif tool_name == "EnterPlanMode":
        return "📐 *Plan Mode* (triggered — will continue normally)"
    else:
        return f"🔧 *{tool_name}*: {str(tool_input)[:200]}"


def format_todo_list(tool_input: dict) -> str:
    """Format TodoWrite input as a readable checklist."""
    todos = tool_input.get("todos", [])
    if not todos:
        return "📋 *TodoWrite*: (empty)"
    lines = ["📋 *Todo List*:"]
    for todo in todos:
        status = todo.get("status", "pending")
        # Use activeForm for in_progress, content otherwise
        label = todo.get("activeForm", "") if status == "in_progress" else todo.get("content", "?")
        if status == "completed":
            lines.append(f"  ✅ {label}")
        elif status == "in_progress":
            lines.append(f"  🔄 {label}")
        else:
            lines.append(f"  ⬜ {label}")
    return "\n".join(lines)


def truncate_for_telegram(text: str, max_len: int = 4000) -> str:
    """Truncate text to fit Telegram's message limit."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 20] + "\n\n_(truncated)_"


async def safe_reply(message, text: str, max_len: int = 4000):
    """Send reply, falling back to plain text if Markdown fails."""
    text = truncate_for_telegram(text, max_len)
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log(f"[WARN] Markdown failed ({e}), sending as plain text")
        # Strip markdown and send plain
        await message.reply_text(text, parse_mode=None)


async def send_long_message(message, text: str):
    """Send a message that may exceed Telegram's limit, splitting at newlines."""
    while text:
        if len(text) <= 4000:
            await safe_reply(message, text)
            break
        split_at = text.rfind("\n", 0, 4000)
        if split_at < 2000:
            split_at = 4000
        await safe_reply(message, text[:split_at])
        text = text[split_at:].lstrip("\n")


async def drain_stderr(stream):
    """Continuously drain stderr to prevent pipe buffer deadlocks.

    If stderr's pipe buffer fills up (~64KB), the subprocess blocks on any
    write to stderr, which in turn blocks stdout. Reading stderr concurrently
    prevents this deadlock — the root cause of the TodoWrite/Task hang bug.
    """
    stderr_lines = []
    while True:
        line = await stream.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").rstrip()
        if decoded:
            stderr_lines.append(decoded)
            log(f"[STDERR] {decoded}")
    return stderr_lines


async def read_ndjson_stream(stream):
    """Read newline-delimited JSON from stream, handling long lines."""
    buffer = ""
    while True:
        chunk = await stream.read(65536)  # 64KB chunks
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")

        # Process complete lines
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass

    # Process any remaining data
    if buffer.strip():
        try:
            yield json.loads(buffer.strip())
        except json.JSONDecodeError:
            pass


def extract_send_files(text: str) -> tuple[str, list[str]]:
    """Extract [[telegram:send:path]] markers and return (cleaned_text, file_paths)."""
    file_paths = SEND_FILE_PATTERN.findall(text)
    cleaned = SEND_FILE_PATTERN.sub('', text).strip()
    return cleaned, file_paths


async def send_files_to_telegram(message, file_paths: list[str]):
    """Send files to Telegram chat."""
    for file_path in file_paths:
        file_path = file_path.strip()
        path = Path(file_path)
        if not path.exists():
            log(f"[FILE-SEND] File not found: {file_path}")
            await message.reply_text(f"⚠️ File not found: `{file_path}`", parse_mode=ParseMode.MARKDOWN)
            continue
        if not path.is_file():
            log(f"[FILE-SEND] Not a file: {file_path}")
            await message.reply_text(f"⚠️ Not a file: `{file_path}`", parse_mode=ParseMode.MARKDOWN)
            continue
        try:
            log(f"[FILE-SEND] Sending: {file_path}")
            with open(path, 'rb') as f:
                await message.reply_document(
                    document=InputFile(f, filename=path.name),
                    caption=f"📎 {path.name}"
                )
            log(f"[FILE-SEND] Sent: {file_path}")
        except Exception as e:
            log(f"[FILE-SEND] Error sending {file_path}: {e}")
            await message.reply_text(f"❌ Failed to send `{path.name}`: {str(e)[:100]}", parse_mode=ParseMode.MARKDOWN)


async def _process_claude(user_id: int, message, cmd: list[str], user_message: str, status_msg):
    """Run Claude subprocess and stream results back to Telegram.

    Extracted from handle_message so it can run as a background asyncio.Task,
    freeing the event loop for /stop and other commands.
    """
    try:
        # Run Claude Code
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR,
        )

        # Track process so /stop can interrupt it
        RUNNING_PROCS[user_id] = proc

        # Drain stderr concurrently to prevent pipe buffer deadlocks
        stderr_task = asyncio.create_task(drain_stderr(proc.stderr))

        tool_messages = []
        tools_since_last_text = []  # Tools accumulated since last text checkpoint
        tool_count = 0
        text_messages = []  # All intermediate + final text blocks
        session_id = None
        interrupted = False

        try:
            async for event in read_ndjson_stream(proc.stdout):
                event_type = event.get("type")

                if event_type == "system":
                    session_id = event.get("session_id")
                    if session_id:
                        USER_SESSIONS[user_id] = session_id
                        record_session(user_id, session_id, user_message)

                elif event_type == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "?")
                            tool_input = block.get("input", {})
                            tool_count += 1
                            tool_msg = format_tool_use(tool_name, tool_input)
                            tool_messages.append(tool_msg)
                            tools_since_last_text.append(tool_msg)

                            # Update ephemeral status with tool count and recent tools
                            header = f"🔧 *Tool {tool_count}*\n\n"
                            if tool_name == "TodoWrite":
                                status_text = header + tool_msg
                            else:
                                status_text = header + "\n\n".join(tool_messages[-5:])
                            status_text = truncate_for_telegram(status_text)
                            try:
                                await status_msg.edit_text(status_text, parse_mode=ParseMode.MARKDOWN)
                            except Exception:
                                pass

                        elif block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                # Flush accumulated tools as a permanent message
                                if tools_since_last_text:
                                    tools_summary = f"📋 *{len(tools_since_last_text)} tools*:\n" + "\n".join(tools_since_last_text)
                                    await safe_reply(message, tools_summary, 2000)
                                    tools_since_last_text = []
                                text_messages.append(text)
                                await send_long_message(message, text)

                elif event_type == "result":
                    result_text = event.get("result", "").strip()
                    if result_text and result_text not in text_messages:
                        # Flush any remaining tools before final result
                        if tools_since_last_text:
                            tools_summary = f"📋 *{len(tools_since_last_text)} tools*:\n" + "\n".join(tools_since_last_text)
                            await safe_reply(message, tools_summary, 2000)
                            tools_since_last_text = []
                        text_messages.append(result_text)
                        await send_long_message(message, result_text)
        finally:
            await proc.wait()
            await stderr_task
            RUNNING_PROCS.pop(user_id, None)

        interrupted = proc.returncode != 0 and proc.returncode is not None

        # Delete ephemeral status message
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Flush any tools that ran after the last text (e.g. if Claude ended with tools)
        if tools_since_last_text:
            tools_summary = f"📋 *{len(tools_since_last_text)} tools*:\n" + "\n".join(tools_since_last_text)
            await safe_reply(message, tools_summary, 2000)

        if interrupted:
            await message.reply_text("⛔ _Interrupted._", parse_mode=ParseMode.MARKDOWN)

        # Send files if any were requested in the text
        all_text = "\n".join(text_messages)
        _, files_to_send = extract_send_files(all_text)
        if files_to_send:
            await send_files_to_telegram(message, files_to_send)

        if not text_messages:
            await message.reply_text("(No response)")

    except Exception as e:
        RUNNING_PROCS.pop(user_id, None)
        try:
            await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
        except Exception:
            await message.reply_text(f"❌ Error: {str(e)[:200]}")


def _is_claude_busy(user_id: int) -> bool:
    """Check if a queue processor is actively running for this user."""
    task = QUEUE_PROCESSORS.get(user_id)
    return task is not None and not task.done()


def _build_claude_cmd(user_id: int, prompt: str) -> list[str]:
    """Build Claude CLI command, consuming session flags."""
    start_fresh = user_id in NEW_SESSION_FLAG
    if start_fresh:
        NEW_SESSION_FLAG.discard(user_id)
    resume_id = RESUME_SESSION.pop(user_id, None)

    cmd = [CLAUDE_PATH, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    elif not start_fresh:
        cmd.append("--continue")
    return cmd


async def _queue_processor(user_id: int):
    """Process queued Claude requests sequentially for a user.

    Ensures only one Claude subprocess runs at a time, preventing conversation
    forks. Sends the done marker only when the queue is fully drained normally
    (not after /stop interruption).
    """
    queue = USER_QUEUES[user_id]
    last_message = None
    interrupted = False

    try:
        while not queue.empty():
            item = await queue.get()
            message, cmd, preview, status_msg, is_queued = item
            last_message = message

            if is_queued:
                await safe_reply(message, f"📨 _Processing queued message:_ {preview[:200]}")

            await _process_claude(user_id, message, cmd, preview, status_msg)

            # Check if the subprocess was interrupted (e.g. /stop)
            proc = RUNNING_PROCS.get(user_id)
            if proc and proc.returncode and proc.returncode != 0:
                interrupted = True

            queue.task_done()
    except Exception as e:
        log(f"[QUEUE] Processor crashed for user {user_id}: {e}")
        if last_message:
            try:
                await last_message.reply_text(f"❌ Queue error: {str(e)[:200]}")
            except Exception:
                pass
    finally:
        QUEUE_PROCESSORS.pop(user_id, None)

    # Only send DONE if queue drained normally (not after /stop)
    if last_message and not interrupted:
        try:
            await last_message.reply_text(f"🦀 _{DONE_MARKER}_", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


def _enqueue_claude(user_id: int, message, cmd: list[str], preview: str, status_msg):
    """Add a Claude request to the per-user queue.

    Starts the queue processor if not already running. Items queued while
    Claude is busy will be marked as queued so the user gets a notification
    when they start processing.
    """
    if user_id not in USER_QUEUES:
        USER_QUEUES[user_id] = asyncio.Queue()

    queue = USER_QUEUES[user_id]
    is_queued = not queue.empty() or _is_claude_busy(user_id)
    queue.put_nowait((message, cmd, preview, status_msg, is_queued))

    # Start processor if not already running
    existing = QUEUE_PROCESSORS.get(user_id)
    if not existing or existing.done():
        QUEUE_PROCESSORS[user_id] = asyncio.create_task(_queue_processor(user_id))


async def handle_message(update: Update, context) -> None:
    """Handle incoming Telegram messages.

    Does quick validation then enqueues the request. The per-user queue ensures
    only one Claude subprocess runs at a time, preventing conversation forks.
    """
    log(f"[MSG] Received message from {update.effective_user.id}: {update.message.text[:50] if update.message.text else '(empty)'}")
    user_id = update.effective_user.id

    # Security: only allow configured users
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        log(f"[MSG] User {user_id} not in allowed list {ALLOWED_USER_IDS}")
        await update.message.reply_text("⛔ Not authorized")
        return

    user_message = update.message.text
    if not user_message:
        return

    cmd = _build_claude_cmd(user_id, user_message)

    # Show appropriate status — "Queued" if Claude is already busy, "Thinking" if not
    if _is_claude_busy(user_id):
        status_msg = await update.message.reply_text("📋 _Queued..._", parse_mode=ParseMode.MARKDOWN)
    else:
        status_msg = await update.message.reply_text("🧠 _Thinking..._", parse_mode=ParseMode.MARKDOWN)

    # Enqueue — processes immediately if nothing running, waits otherwise
    _enqueue_claude(user_id, update.message, cmd, user_message[:80], status_msg)


def _get_file_meta(msg) -> tuple | None:
    """Extract file object and display name from a message (no I/O)."""
    if msg.document:
        return msg.document, msg.document.file_name or "document"
    elif msg.photo:
        return msg.photo[-1], "photo.jpg"
    elif msg.voice:
        return msg.voice, "voice.ogg"
    elif msg.audio:
        return msg.audio, msg.audio.file_name or "audio"
    elif msg.video:
        return msg.video, msg.video.file_name or "video.mp4"
    return None


async def _download_file(file_obj, message_id: int, file_name: str) -> Path:
    """Download a Telegram file to disk. Returns the local path."""
    tg_file = await file_obj.get_file()
    local_path = UPLOAD_DIR / f"{message_id}_{file_name}"
    await tg_file.download_to_drive(local_path)
    log(f"[FILE] Downloaded to {local_path}")
    return local_path


async def _flush_file_batch(buffer_key: str):
    """Wait for all files in a batch to arrive, then process as one Claude request.

    The debounce window (3s) covers both Telegram's delivery gap between group
    members AND the download time for large files like EPUBs. After the window,
    we await all pending downloads before building the prompt.

    INVARIANT: No `await` may be added between the asyncio.sleep return and the
    identity check below — the debounce race guard relies on these running in the
    same event loop tick.
    """
    await asyncio.sleep(3.0)

    # If a newer timer replaced us (debounce race), bail out
    if MEDIA_GROUP_TIMERS.get(buffer_key) is not asyncio.current_task():
        return

    buf = MEDIA_GROUP_BUFFERS.pop(buffer_key, None)
    MEDIA_GROUP_TIMERS.pop(buffer_key, None)
    if not buf:
        return

    user_id = buf["user_id"]
    message = buf["message"]
    status_msg = buf["status_msg"]
    pending = buf["pending"]  # List of (asyncio.Task<Path>, file_name)
    caption = buf["caption"]

    try:
        # Wait for all downloads to finish
        files = []
        failed = []
        for download_task, file_name in pending:
            try:
                local_path = await download_task
                files.append((local_path, file_name))
            except Exception as e:
                log(f"[FILE-BATCH] Download failed for {file_name}: {e}")
                failed.append(file_name)

        if not files:
            await status_msg.edit_text("❌ All downloads failed.")
            return

        n = len(files)
        log(f"[FILE-BATCH] Flushing {buffer_key}: {n} file(s)")

        # Notify user of partial failures
        if failed:
            await safe_reply(message, f"⚠️ Failed to download: {', '.join(failed)}")

        # Build prompt with all file paths
        files_text = "\n".join(f"File path: {path}" for path, _ in files)
        file_names = ", ".join(name for _, name in files)
        user_message = f"{caption}\n\n{files_text}"

        indicator = "📋 _Queued..._" if _is_claude_busy(user_id) else "🧠 _Processing..._"
        if n == 1:
            status = f"📥 Downloaded: `{file_names}`\n{indicator}"
        else:
            status = f"📥 Downloaded {n} files: {file_names}\n{indicator}"
        await status_msg.edit_text(status, parse_mode=ParseMode.MARKDOWN)

        cmd = _build_claude_cmd(user_id, user_message)
        preview = f"[{n} file(s): {file_names}] {caption[:60]}"
        _enqueue_claude(user_id, message, cmd, preview, status_msg)

    except Exception as e:
        log(f"[FILE-BATCH] Flush failed for {buffer_key}: {e}")
        try:
            await status_msg.edit_text(f"❌ File processing error: {str(e)[:200]}")
        except Exception:
            pass


async def handle_file(update: Update, context) -> None:
    """Handle incoming files (documents, photos, etc.).

    All files go through a 3s debounce buffer to catch rapid sequential sends.
    Media groups (photos/videos) are keyed by media_group_id; documents and
    other files are keyed by user_id. After the debounce, all buffered files
    are combined into a single Claude request.
    """
    user_id = update.effective_user.id
    log(f"[FILE] Received file from {user_id}")

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        log(f"[FILE] User {user_id} not in allowed list")
        await update.message.reply_text("⛔ Not authorized")
        return

    # Extract file metadata immediately (no I/O)
    meta = _get_file_meta(update.message)
    if not meta:
        await update.message.reply_text("❓ Unsupported file type")
        return
    file_obj, file_name = meta

    caption = update.message.caption or "I've uploaded a file. Please analyze it."

    # Buffer key: use media_group_id for albums, fall back to per-user key for
    # documents sent in quick succession (Telegram doesn't group those).
    media_group_id = update.message.media_group_id
    buffer_key = media_group_id or f"user_{user_id}"

    # Kick off download concurrently — don't await it here so the buffer
    # registration happens instantly regardless of file size.
    download_task = asyncio.create_task(
        _download_file(file_obj, update.message.message_id, file_name)
    )

    if buffer_key not in MEDIA_GROUP_BUFFERS:
        # First file in this batch — create buffer and status message
        status_msg = await update.message.reply_text("📥 Downloading files...")
        MEDIA_GROUP_BUFFERS[buffer_key] = {
            "user_id": user_id,
            "message": update.message,
            "status_msg": status_msg,
            "pending": [(download_task, file_name)],
            "caption": caption,
        }
    else:
        # Additional file in existing batch
        MEDIA_GROUP_BUFFERS[buffer_key]["pending"].append((download_task, file_name))
        # Use caption from whichever message has one (Telegram puts it on the first)
        if update.message.caption:
            MEDIA_GROUP_BUFFERS[buffer_key]["caption"] = caption

    # Cancel previous timer and restart (debounce)
    old_timer = MEDIA_GROUP_TIMERS.get(buffer_key)
    if old_timer and not old_timer.done():
        old_timer.cancel()
    MEDIA_GROUP_TIMERS[buffer_key] = asyncio.create_task(
        _flush_file_batch(buffer_key)
    )


async def cmd_start(update: Update, context) -> None:
    """Handle /start command."""
    log(f"[CMD] /start from {update.effective_user.id}")
    await update.message.reply_text(
        "🦀 *Carcin — Claude Code via Telegram*\n\n"
        "Send me a message and I'll forward it to Claude Code running on your machine.\n\n"
        "Commands:\n"
        "/start - This help\n"
        "/status - Check connection\n"
        "/cwd - Show working directory\n"
        "/new - Start a fresh session\n"
        "/sessions - List recent sessions\n"
        "/resume <id> - Resume a specific session\n"
        "/recent [N] - Resend last N messages (splits long ones)\n"
        "/stop - Interrupt running Claude task\n"
        "/restart - Restart the bot",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_status(update: Update, context) -> None:
    """Handle /status command."""
    log(f"[CMD] /status from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return

    # Check if claude is available
    try:
        result = subprocess.run([CLAUDE_PATH, "--version"], capture_output=True, text=True, timeout=5)
        version = result.stdout.strip()
        log(f"[CMD] /status - claude version: {version}")
        msg = f"✅ Connected\n📁 Working dir: `{WORKING_DIR}`\n🔧 {version}"
        log(f"[CMD] /status - sending response")
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        log(f"[CMD] /status - response sent")
    except Exception as e:
        log(f"[CMD] /status - error: {e}")
        await update.message.reply_text(f"❌ Claude Code not available: {e}")


async def cmd_cwd(update: Update, context) -> None:
    """Handle /cwd command."""
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return
    await update.message.reply_text(f"📁 Working directory: `{WORKING_DIR}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_new(update: Update, context) -> None:
    """Handle /new command - start a fresh session."""
    log(f"[CMD] /new from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return
    NEW_SESSION_FLAG.add(user_id)
    await update.message.reply_text("🆕 Next message will start a fresh session.")


async def cmd_sessions(update: Update, context) -> None:
    """Handle /sessions command - list recent sessions."""
    log(f"[CMD] /sessions from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return

    sessions = load_sessions()
    user_sessions = sessions.get(str(user_id), [])
    if not user_sessions:
        await update.message.reply_text("No saved sessions. Send a message to start one.")
        return

    lines = ["📂 *Recent Sessions*:\n"]
    # Show newest first
    for s in reversed(user_sessions[-10:]):
        ts = time.strftime("%b %d %H:%M", time.localtime(s["timestamp"]))
        sid_short = s["session_id"][:8]
        preview = s["preview"]
        lines.append(f"`{sid_short}` ({ts})\n  {preview}")

    lines.append(f"\nUse /resume <id> to resume a session.")
    await safe_reply(update.message, "\n".join(lines))


async def cmd_resume(update: Update, context) -> None:
    """Handle /resume command - resume a specific session."""
    log(f"[CMD] /resume from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return

    if not context.args:
        await update.message.reply_text("Usage: /resume <session\\_id>\nUse /sessions to list available sessions.", parse_mode=ParseMode.MARKDOWN)
        return

    partial_id = context.args[0]
    sessions = load_sessions()
    user_sessions = sessions.get(str(user_id), [])

    # Match by prefix
    matches = [s for s in user_sessions if s["session_id"].startswith(partial_id)]
    if not matches:
        await update.message.reply_text(f"No session found matching `{partial_id}`", parse_mode=ParseMode.MARKDOWN)
        return
    if len(matches) > 1:
        await update.message.reply_text(f"Multiple sessions match `{partial_id}` — be more specific.", parse_mode=ParseMode.MARKDOWN)
        return

    full_id = matches[0]["session_id"]
    RESUME_SESSION[user_id] = full_id
    await update.message.reply_text(f"🔄 Next message will resume session `{full_id[:8]}...`", parse_mode=ParseMode.MARKDOWN)


def get_session_jsonl_path(session_id: str) -> Path | None:
    """Find the JSONL file for a session ID.

    Claude Code stores sessions as JSONL in ~/.claude/projects/{encoded_cwd}/{session_id}.jsonl
    where the cwd has / replaced with - (e.g. /home/user -> -home-user).
    """
    encoded_cwd = WORKING_DIR.replace("/", "-")
    if not encoded_cwd.startswith("-"):
        encoded_cwd = "-" + encoded_cwd
    jsonl_path = Path.home() / ".claude" / "projects" / encoded_cwd / f"{session_id}.jsonl"
    if jsonl_path.exists():
        return jsonl_path
    return None


def extract_assistant_messages(jsonl_path: Path, n: int) -> list[str]:
    """Extract the last N assistant text messages from a session JSONL file."""
    messages = []
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            messages.append(text)
    except OSError as e:
        log(f"[WARN] Failed to read session JSONL: {e}")
        return []
    # Return last N
    return messages[-n:]


async def cmd_recent(update: Update, context) -> None:
    """Handle /recent command - resend last N messages from current session."""
    log(f"[CMD] /recent from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return

    # Parse N (default 1)
    n = 1
    if context.args:
        try:
            n = int(context.args[0])
            n = max(1, min(n, 100))  # Clamp to 1-100
        except ValueError:
            await update.message.reply_text("Usage: /recent [N] — resend last N messages (default 1, max 100)")
            return

    # Find current session
    session_id = USER_SESSIONS.get(user_id)
    if not session_id:
        await update.message.reply_text("No active session. Send a message first.")
        return

    jsonl_path = get_session_jsonl_path(session_id)
    if not jsonl_path:
        await update.message.reply_text(f"Session file not found for `{session_id[:8]}...`", parse_mode=ParseMode.MARKDOWN)
        return

    messages = extract_assistant_messages(jsonl_path, n)
    if not messages:
        await update.message.reply_text("No assistant messages found in this session.")
        return

    for i, msg in enumerate(messages):
        # Split long messages into chunks that fit Telegram's 4096 char limit
        chunks = []
        while msg:
            if len(msg) <= 4000:
                chunks.append(msg)
                break
            # Find a good split point (newline near the limit)
            split_at = msg.rfind("\n", 0, 4000)
            if split_at < 2000:
                # No good newline, just split at limit
                split_at = 4000
            chunks.append(msg[:split_at])
            msg = msg[split_at:].lstrip("\n")

        for j, chunk in enumerate(chunks):
            # Label if multiple messages or chunks
            if len(messages) > 1 and j == 0:
                chunk = f"📨 *Message {i+1}/{len(messages)}*\n\n{chunk}"
            elif len(chunks) > 1 and j > 0:
                chunk = f"_(continued)_\n\n{chunk}"
            await safe_reply(update.message, chunk)


async def cmd_stop(update: Update, context) -> None:
    """Handle /stop command - interrupt running Claude subprocess and clear queue."""
    log(f"[CMD] /stop from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return

    proc = RUNNING_PROCS.get(user_id)
    queue = USER_QUEUES.get(user_id)
    has_proc = proc and proc.returncode is None
    queued_count = queue.qsize() if queue else 0

    if not has_proc and queued_count == 0:
        await update.message.reply_text("Nothing running to stop.")
        return

    # Clear the queue so no more items get processed after current one finishes.
    # Don't cancel the processor task — let it finish naturally so _process_claude
    # can clean up the subprocess properly.
    if queue:
        while not queue.empty():
            try:
                _, _, _, status_msg, _ = queue.get_nowait()
                # Clean up the "Queued..." status message so it doesn't hang
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    # Interrupt the running subprocess
    if has_proc:
        try:
            proc.send_signal(signal.SIGINT)
            log(f"[CMD] /stop - sent SIGINT to PID {proc.pid}")
        except ProcessLookupError:
            pass

    parts = []
    if has_proc:
        parts.append("interrupted current task")
    if queued_count > 0:
        parts.append(f"cleared {queued_count} queued message(s)")
    await update.message.reply_text(f"🛑 Stopped — {', '.join(parts)}.")


async def cmd_restart(update: Update, context) -> None:
    """Handle /restart command - restart the bot service.

    Assumes the bot runs as a systemd user service named 'carcin'.
    Adjust the service name via CARCIN_SERVICE_NAME env var if needed.
    """
    log(f"[CMD] /restart from {update.effective_user.id}")
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Not authorized")
        return
    await update.message.reply_text("🔄 Restarting...")
    service_name = os.environ.get("CARCIN_SERVICE_NAME", "carcin")
    log(f"[CMD] /restart - triggering systemctl restart {service_name}")
    subprocess.Popen(["systemctl", "--user", "restart", service_name])


def main():
    """Start the bot."""
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable")
        print("  1. Talk to @BotFather on Telegram")
        print("  2. Create a new bot with /newbot")
        print("  3. Copy the token and set it:")
        print("     export TELEGRAM_BOT_TOKEN='your-token-here'")
        return

    if not ALLOWED_USER_IDS:
        print("WARNING: ALLOWED_USER_IDS not set - bot will accept messages from anyone!")
        print("  To restrict access, set your Telegram user ID:")
        print("  export ALLOWED_USER_IDS='123456789'")
        print("  (Get your ID by messaging @userinfobot on Telegram)")
        print()

    print(f"🦀 Starting Carcin...")
    print(f"Working directory: {WORKING_DIR}")
    if ALLOWED_USER_IDS:
        print(f"Allowed users: {ALLOWED_USER_IDS}")

    app = Application.builder().token(BOT_TOKEN).build()

    # Error handler
    async def error_handler(update, context):
        log(f"[ERROR] {context.error}")
    app.add_error_handler(error_handler)

    # Catch-all handler for debugging
    async def catch_all(update: Update, context):
        log(f"[CATCH-ALL] Update type: {type(update)}, has message: {update.message is not None}")
        if update.message:
            log(f"[CATCH-ALL] Message: {update.message.text}")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO,
        handle_file
    ))
    app.add_handler(MessageHandler(filters.ALL, catch_all))

    log("Bot running. Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=5,  # Retry on startup network errors
    )


if __name__ == "__main__":
    main()
