"""
commands/top.py — !top command.

Shows the highest-scoring stocks by congressional activity score.
Supports an optional limit argument (default 10, max 25).

Usage:
    !top          — top 10 stocks
    !top 5        — top 5 stocks
"""

from __future__ import annotations

import discord
from discord.ext import commands
from loguru import logger

from config import DEFAULT_EMBED_COLOUR, SECTOR_COLOURS
from services.scoring_engine import get_scoring_engine


class TopCommand(commands.Cog, name="Top"):
    """Cog providing the !top command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="top", help="Show highest-scoring congressional stocks.")
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.channel)
    async def top(self, ctx: commands.Context, limit: int = 10) -> None:
        """
        Display the top N stocks by congressional activity score.

        Parameters
        ----------
        limit : int, optional
            Number of results to show (1–25, default 10).
        """
        limit = max(1, min(limit, 25))

        async with ctx.typing():
            try:
                engine = get_scoring_engine()
                entries = engine.get_watchlist("TOP_CONGRESSIONAL", limit=limit)
            except Exception as exc:
                logger.error("[TopCommand] Failed to fetch watchlist: {}", exc)
                await ctx.send("❌ Failed to retrieve scores. Please try again later.")
                return

        if not entries:
            await ctx.send(
                "📭 No scored stocks found yet. The daily scoring job may not have run yet."
            )
            return

        embed = discord.Embed(
            title=f"🏆 Top {len(entries)} Congressional Stock Picks",
            description=(
                "Ranked by composite congressional activity score (0–100).\n"
                "Score reflects buy clustering, volume, sector trends, and more."
            ),
            colour=DEFAULT_EMBED_COLOUR,
        )

        # Build the ranked table as a single field for clean formatting
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for entry in entries:
            prefix = medals[entry.rank - 1] if entry.rank <= 3 else f"`{entry.rank:2d}.`"
            sector_tag = f" *({entry.sector})*" if entry.sector else ""
            signals = entry.metadata.get("active_signals", [])
            signal_count = f" — {len(signals)} signals" if signals else ""
            score_bar = _score_bar(entry.score)
            lines.append(
                f"{prefix} **${entry.ticker}** — {entry.company_name}{sector_tag}\n"
                f"     {score_bar} **{entry.score:.1f}**/100{signal_count}"
            )

        embed.add_field(
            name="Rankings",
            value="\n".join(lines),
            inline=False,
        )

        embed.set_footer(
            text=(
                "Use !stock TICKER for full analysis  •  "
                "!watchlist for all lists  •  "
                "Scores update daily"
            )
        )
        embed.timestamp = discord.utils.utcnow()

        await ctx.send(embed=embed)
        logger.info(
            "[TopCommand] Served top {} results to channel {}.",
            len(entries), ctx.channel.id,
        )

    @top.error
    async def top_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ Please provide a valid number, e.g. `!top 10`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s before using this command again.")
        else:
            logger.error("[TopCommand] Unhandled error: {}", error)
            await ctx.send("❌ An unexpected error occurred.")


def _score_bar(score: float, width: int = 10) -> str:
    """Return a compact Unicode progress bar for a 0–100 score."""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TopCommand(bot))
