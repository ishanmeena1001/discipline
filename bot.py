
from __future__ import annotations

import os
import asyncio
import logging
from typing import Any

import requests
import discord
from discord import app_commands
from dotenv import load_dotenv


# ============================================================
# 1. CONFIGURATION
# ============================================================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()

HTTP_TIMEOUT = 30
MAX_TASK_LENGTH = 100
MAX_GOAL_LENGTH = 500
MAX_PLAN_MINUTES = 1440
DISCORD_MESSAGE_LIMIT = 1900
MAX_TASKS_DISPLAYED = 100

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

if not N8N_WEBHOOK_URL:
    raise RuntimeError("N8N_WEBHOOK_URL is missing from .env")

if not N8N_WEBHOOK_URL.startswith(("http://", "https://")):
    raise RuntimeError("N8N_WEBHOOK_URL must be an HTTP(S) URL")


# ============================================================
# 2. LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("discipline_bot")


# ============================================================
# 3. DISCORD CLIENT
# ============================================================

class DisciplineBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default()
        )

        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced successfully.")

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        logger.info("Discipline Bot is online.")


bot = DisciplineBot()


# ============================================================
# 4. TEXT AND MESSAGE HELPERS
# ============================================================

def safe_text(value: Any, default: str = "Unknown") -> str:
    """Convert external text into safe Discord display text."""

    if value is None:
        value = default

    text = str(value)

    # Prevent unintended user/role/everyone mentions.
    text = discord.utils.escape_mentions(text)

    # Prevent supplied text from changing Discord formatting.
    text = discord.utils.escape_markdown(text)

    return text


async def send_message(
    interaction: discord.Interaction,
    content: str,
):
    """Send a Discord message without allowing unwanted mentions."""

    await interaction.followup.send(
        content,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def send_long_message(
    interaction: discord.Interaction,
    content: str,
):
    """Split output into messages within Discord's character limit."""

    if not content:
        return

    chunks: list[str] = []
    current = ""

    for line in content.splitlines():
        # Split unusually long individual lines.
        while len(line) > DISCORD_MESSAGE_LIMIT:
            if current:
                chunks.append(current)
                current = ""

            chunks.append(line[:DISCORD_MESSAGE_LIMIT])
            line = line[DISCORD_MESSAGE_LIMIT:]

        candidate = line if not current else current + "\n" + line

        if len(candidate) > DISCORD_MESSAGE_LIMIT:
            if current:
                chunks.append(current)

            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    for chunk in chunks:
        await send_message(interaction, chunk)


async def respond_to_error(
    interaction: discord.Interaction,
    message: str,
):
    """Respond correctly whether or not the interaction was deferred."""

    safe_message = f"❌ {safe_text(message, 'Something went wrong.')}"

    if interaction.response.is_done():
        await send_message(interaction, safe_message)
    else:
        await interaction.response.send_message(
            safe_message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def defer_interaction(interaction: discord.Interaction):
    """Acknowledge a command before making a potentially slow request."""

    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)


# ============================================================
# 5. N8N HTTP CLIENT
# ============================================================

async def send_to_n8n(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send JSON to n8n without blocking Discord's event loop.

    Expected response:
        {"success": true, ...}

    Errors are logged privately and returned as safe messages.
    """

    command_name = str(payload.get("command", "unknown"))

    def make_request() -> dict[str, Any]:
        response = requests.post(
            N8N_WEBHOOK_URL,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object from n8n")

        return data

    try:
        result = await asyncio.to_thread(make_request)

        if result.get("success") is not True:
            logger.warning(
                "n8n did not confirm success for command: %s",
                command_name,
            )

            return {
                "success": False,
                "message": (
                    "n8n did not confirm the operation. "
                    "Check the workflow and its response."
                ),
            }

        return result

    except requests.exceptions.Timeout:
        logger.warning("n8n timed out for command: %s", command_name)

        return {
            "success": False,
            "message": "n8n timed out. Please try again.",
        }

    except requests.exceptions.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        logger.error(
            "n8n HTTP error for %s: status=%s",
            command_name,
            status,
        )

        return {
            "success": False,
            "message": "n8n returned an HTTP error. Check its logs.",
        }

    except requests.exceptions.RequestException:
        logger.exception(
            "Network error while calling n8n for %s",
            command_name,
        )

        return {
            "success": False,
            "message": "Cannot connect to n8n. Check its availability.",
        }

    except (ValueError, TypeError):
        logger.exception(
            "Invalid response from n8n for %s",
            command_name,
        )

        return {
            "success": False,
            "message": "n8n returned an invalid response.",
        }

    except Exception:
        logger.exception(
            "Unexpected n8n client error for %s",
            command_name,
        )

        return {
            "success": False,
            "message": "An unexpected error occurred.",
        }


async def execute_command(
    interaction: discord.Interaction,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Common command execution and failure handling."""

    await defer_interaction(interaction)

    result = await send_to_n8n(payload)

    if not result.get("success"):
        await respond_to_error(
            interaction,
            result.get("message", "The operation failed."),
        )
        return None

    return result


# ============================================================
# 6. /TASK
# ============================================================

@bot.tree.command(
    name="task",
    description="Add a task for today",
)
@app_commands.describe(
    task="The task you want to add",
    priority="Task priority",
)
@app_commands.choices(
    priority=[
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Medium", value="medium"),
        app_commands.Choice(name="High", value="high"),
    ]
)
async def task(
    interaction: discord.Interaction,
    task: app_commands.Range[str, 1, MAX_TASK_LENGTH],
    priority: app_commands.Choice[str],
):
    task = task.strip()

    if not task:
        await respond_to_error(interaction, "Task cannot be empty.")
        return

    result = await execute_command(
        interaction,
        {
            "command": "task",
            "user_id": str(interaction.user.id),
            "task": task,
            "priority": priority.value,
        },
    )

    if result is None:
        return

    await send_long_message(
        interaction,
        "**Task Added**\n"
        f"📌 {safe_text(task)}\n"
        f"Priority: **{priority.value.upper()}**\n"
        "Status: ⏳ Pending",
    )


# ============================================================
# 7. /DONE
# ============================================================

@bot.tree.command(
    name="done",
    description="Mark a task as completed",
)
@app_commands.describe(
    task="The task you completed",
)
async def done(
    interaction: discord.Interaction,
    task: app_commands.Range[str, 1, MAX_TASK_LENGTH],
):
    task = task.strip()

    if not task:
        await respond_to_error(interaction, "Task cannot be empty.")
        return

    result = await execute_command(
        interaction,
        {
            "command": "done",
            "user_id": str(interaction.user.id),
            "task": task,
        },
    )

    if result is None:
        return

    await send_long_message(
        interaction,
        "**Task Completed**\n"
        f"✅ {safe_text(task)}",
    )


# ============================================================
# 8. /CHECKIN
# ============================================================

@bot.tree.command(
    name="checkin",
    description="Show today's discipline dashboard",
)
async def checkin(interaction: discord.Interaction):
    result = await execute_command(
        interaction,
        {
            "command": "checkin",
            "user_id": str(interaction.user.id),
        },
    )

    if result is None:
        return

    tasks = result.get("tasks", [])

    if (
        not isinstance(tasks, list)
        or not all(isinstance(item, dict) for item in tasks)
    ):
        await respond_to_error(
            interaction,
            "n8n returned an invalid task list.",
        )
        return

    # Avoid accidentally flooding a Discord channel.
    if len(tasks) > MAX_TASKS_DISPLAYED:
        await respond_to_error(
            interaction,
            f"Too many tasks to display at once "
            f"(maximum {MAX_TASKS_DISPLAYED}).",
        )
        return

    total = len(tasks)

    completed = sum(
        1
        for item in tasks
        if str(item.get("status", "")).strip().lower()
        == "completed"
    )

    pending = total - completed

    percentage = (
        round(completed / total * 100)
        if total
        else 0
    )

    summary = (
        "📋 **Today's Check-in**\n\n"
        f"**Progress:** {completed}/{total} completed "
        f"({percentage}%)\n"
        f"✅ Completed: {completed}\n"
        f"⏳ Pending: {pending}"
    )

    await send_long_message(interaction, summary)

    if not tasks:
        await send_message(
            interaction,
            "No tasks found for today. Add one with `/task`.",
        )
        return

    lines = []

    for index, item in enumerate(tasks, start=1):
        status = str(item.get("status", "pending")).lower()

        emoji = "✅" if status == "completed" else "⏳"

        task_name = safe_text(
            item.get("task"),
            "Unknown task",
        )

        priority = safe_text(
            item.get("priority"),
            "normal",
        ).upper()

        lines.append(
            f"{index}. {emoji} **{task_name}**\n"
            f"Priority: {priority}"
        )

    await send_long_message(
        interaction,
        "\n\n".join(lines),
    )


# ============================================================
# 9. /PLAN
# ============================================================

@bot.tree.command(
    name="plan",
    description="Create an AI-powered plan for today",
)
@app_commands.describe(
    goal="What you want to accomplish",
    available_minutes="Available time, from 1 to 1440 minutes",
)
async def plan(
    interaction: discord.Interaction,
    goal: app_commands.Range[str, 1, MAX_GOAL_LENGTH],
    available_minutes: app_commands.Range[int, 1, MAX_PLAN_MINUTES],
):
    goal = goal.strip()

    if not goal:
        await respond_to_error(interaction, "Goal cannot be empty.")
        return

    result = await execute_command(
        interaction,
        {
            "command": "plan",
            "user_id": str(interaction.user.id),
            "goal": goal,
            "available_minutes": available_minutes,
        },
    )

    if result is None:
        return

    # Preserve the response structure used by the existing workflow.
    tasks = result.get("plan")

    if not isinstance(tasks, list) or not tasks:
        await respond_to_error(
            interaction,
            "The planner returned no valid tasks.",
        )
        return

    if not all(isinstance(item, dict) for item in tasks):
        await respond_to_error(
            interaction,
            "The planner returned an invalid task format.",
        )
        return

    if len(tasks) > MAX_TASKS_DISPLAYED:
        await respond_to_error(
            interaction,
            "The planner returned too many tasks to display.",
        )
        return

    lines = []

    for item in tasks:
        task_name = safe_text(
            item.get("task"),
            "Untitled task",
        )

        priority = str(
            item.get("priority", "medium")
        ).strip().lower()

        if priority == "high":
            emoji = "🔴"
        elif priority == "medium":
            emoji = "🟡"
        else:
            emoji = "🟢"

        lines.append(
            f"{emoji} **{task_name}** — {priority.upper()}"
        )

    message = (
        "**TODAY'S PLAN**\n"
        f"Goal: {safe_text(goal)}\n"
        f"Available time: {available_minutes} minutes\n\n"
        + "\n".join(lines)
    )

    await send_long_message(interaction, message)


# ============================================================
# 10. GLOBAL SLASH-COMMAND ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    # Avoid logging user-provided task contents or credentials.
    logger.error(
        "Slash command failed: %s (%s)",
        getattr(interaction.command, "name", "unknown"),
        type(error).__name__,
    )

    if isinstance(error, app_commands.CommandOnCooldown):
        message = (
            f"Please wait {error.retry_after:.1f} seconds "
            "before trying again."
        )

    elif isinstance(error, app_commands.TransformerError):
        message = "One of the command arguments is invalid."

    elif isinstance(error, app_commands.CheckFailure):
        message = "You cannot use this command."

    else:
        message = "An unexpected command error occurred."

    try:
        await respond_to_error(interaction, message)
    except discord.HTTPException:
        logger.warning("Could not deliver the command error response.")


# ============================================================
# 11. START BOT
# ============================================================

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.critical(
            "Discord login failed. Check DISCORD_TOKEN."
        )
        raise
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
