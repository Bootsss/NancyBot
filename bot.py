"""
bot.py — Capitol Gains Discord Bot entry point.

Startup sequence
----------------
1. Load .env and configure loguru
2. Validate database connection
3. Initialise all tables (idempotent)
4. Create discord.ext.commands.Bot with intents
5. Load all command cogs
6. On ready: start APScheduler, initialise services
7. Run the bot (blocks until Ctrl-C / SIGTERM)
8. On shutdown: stop scheduler, dispose DB connections

Run with:
    python bot.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from loguru import logger

from config import configure_logging, settings
from database import async_dispose_engines, async_init_db, check_db_connection, dispose_engines
from services.alert_service import get_alert_service
from scheduler import CapitolGainsScheduler


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMAND_PREFIX: str = "!"

# All cog modules to load at startup (order matters for dependencies)
COGS: list[str] = [
    "commands.help",
    "commands.top",
    "commands.stock",
    "commands.sector",
    "commands.recent",
]


# ---------------------------------------------------------------------------
# Bot subclass
# ---------------------------------------------------------------------------

class CapitolGainsBot(commands.Bot):
    """
    Subclass of commands.Bot that owns the scheduler and handles
    the full async lifecycle (setup_hook, on_ready, close).
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # required for prefix commands in 2.0+

        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            description="Capitol Gains — Congressional stock trade tracker",
            help_command=None,   # We use our own custom help cog
            case_insensitive=True,
        )

        self._scheduler: CapitolGainsScheduler | None = None

    # ------------------------------------------------------------------ #
    # Async setup (runs before the bot connects to the gateway)
    # ------------------------------------------------------------------ #

    async def setup_hook(self) -> None:
        """
        Called by discord.py once before login.  Use for:
          - DB initialisation
          - Loading cogs
        The scheduler is started in on_ready (needs the event loop running).
        """
        logger.info("[Bot] Running setup_hook...")

        # Initialise database tables
        try:
            await async_init_db()
        except Exception as exc:
            logger.critical("[Bot] Database initialisation failed: {}", exc)
            raise SystemExit(1) from exc

        # Load cogs
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("[Bot] Loaded cog: {}", cog)
            except Exception as exc:
                logger.error("[Bot] Failed to load cog {}: {}", cog, exc)
                # Non-fatal — bot continues with remaining cogs

        logger.info("[Bot] setup_hook complete.")

    # ------------------------------------------------------------------ #
    # on_ready
    # ------------------------------------------------------------------ #

    async def on_ready(self) -> None:
        """
        Called when the bot has connected to Discord and all guilds are cached.
        Safe to start background tasks here.
        """
        logger.info(
            "[Bot] Logged in as {} (id={}) on {} guild(s).",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
        )

        # Validate the alert channel exists
        channel = self.get_channel(settings.discord_channel_id)
        if channel is None:
            logger.warning(
                "[Bot] Alert channel {} not found. "
                "Check DISCORD_CHANNEL_ID and that the bot is in the server.",
                settings.discord_channel_id,
            )
        else:
            logger.info("[Bot] Alert channel: #{} ({})", channel.name, channel.id)

        # Wire the Discord client into the alert service
        get_alert_service(discord_client=self)

        # Start the scheduler
        self._scheduler = CapitolGainsScheduler(discord_client=self)
        self._scheduler.start()

        #self._scheduler.trigger_daily_now()  # ← ADD BACK TEMPORARILY

        # Set bot presence
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Congress trade stocks | !help",
            )
        )

        logger.info("[Bot] Capitol Gains is ready. Prefix: {}", COMMAND_PREFIX)

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        """Global error handler for unhandled command errors."""
        # Ignore errors already handled by cog-level handlers
        if hasattr(ctx.command, "on_error"):
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return

        if isinstance(error, commands.CommandNotFound):
            # Silently ignore unknown commands to avoid spam
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏳ Slow down! Try again in **{error.retry_after:.1f}s**.",
                delete_after=8,
            )
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"❌ Missing argument: `{error.param.name}`. "
                f"Use `!help {ctx.command.name}` for usage.",
                delete_after=15,
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(
                f"❌ Invalid argument. Use `!help {ctx.command.name}` for usage.",
                delete_after=15,
            )
            return
        if isinstance(error, commands.DisabledCommand):
            await ctx.send("🚫 This command is currently disabled.")
            return

        # Unexpected errors — log fully, send generic message
        logger.exception(
            "[Bot] Unhandled command error in !{}: {}",
            ctx.command.name if ctx.command else "unknown",
            error,
        )
        await ctx.send(
            "❌ An unexpected error occurred. The issue has been logged.",
            delete_after=15,
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("[Bot] Joined guild: {} (id={})", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("[Bot] Removed from guild: {} (id={})", guild.name, guild.id)

    # ------------------------------------------------------------------ #
    # Graceful shutdown
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Cleanly shut down the scheduler and DB connections before exit."""
        logger.info("[Bot] Shutting down...")

        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            logger.info("[Bot] Scheduler stopped.")

        await async_dispose_engines()
        dispose_engines()

        await super().close()
        logger.info("[Bot] Shutdown complete.")


# ---------------------------------------------------------------------------
# Signal handling for clean shutdown on Ctrl-C / SIGTERM
# ---------------------------------------------------------------------------

def _install_signal_handlers(bot: CapitolGainsBot) -> None:
    """Register SIGTERM handler so Railway/Render terminate cleanly."""

    def _handle_sigterm(*_):
        logger.info("[Bot] SIGTERM received — initiating shutdown.")
        asyncio.get_event_loop().create_task(bot.close())

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        # Windows doesn't support all signals; skip silently
        pass


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. Configure logging first so every subsequent log is captured
    configure_logging()
    logger.info("[Bot] Starting Capitol Gains...")

    # 2. Pre-flight: synchronous DB connectivity check
    if not check_db_connection():
        logger.critical(
            "[Bot] Cannot connect to database: {}\n"
            "Set DATABASE_URL in .env and ensure the database is reachable.",
            settings.database_url,
        )
        sys.exit(1)

    logger.info("[Bot] Database connection OK: {}", settings.database_url)

    # 3. Create bot
    bot = CapitolGainsBot()
    _install_signal_handlers(bot)

    # 4. Run — blocks until bot.close() is called
    try:
        bot.run(
            settings.discord_token,
            log_handler=None,   # loguru handles all logging; suppress discord.py's default
            log_level="DEBUG" if settings.debug_mode else "INFO",
        )
    except discord.LoginFailure:
        logger.critical(
            "[Bot] Discord login failed — invalid token. "
            "Check DISCORD_TOKEN in .env"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("[Bot] Keyboard interrupt received.")
    except Exception as exc:
        logger.critical("[Bot] Fatal error: {}", exc)
        sys.exit(1)
    finally:
        dispose_engines()
        logger.info("[Bot] Process exiting.")


if __name__ == "__main__":
    main()
