"""
commands/help.py — Custom !help command.

Overrides discord.py's default help with a rich branded embed
showing all available commands with usage examples and descriptions.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from loguru import logger

from config import DEFAULT_EMBED_COLOUR


class HelpCommand(commands.Cog, name="Help"):
    """Cog providing the custom !help command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Disable the default help command so ours takes over
        self.bot.remove_command("help")

    @commands.command(name="help", help="Show all Capitol Gains commands.")
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def help(self, ctx: commands.Context, *, command_name: str = "") -> None:
        """
        Display command help.

        With no argument: full command list.
        With a command name: detail for that specific command.
        """
        if command_name.strip():
            embed = self._build_command_help(command_name.strip().lower())
        else:
            embed = self._build_full_help()

        await ctx.send(embed=embed)
        logger.debug("[HelpCommand] Served help to user {}.", ctx.author)

    def _build_full_help(self) -> discord.Embed:
        """Build the full command reference embed."""
        embed = discord.Embed(
            title="📖 Capitol Gains — Command Reference",
            description=(
                "Track congressional stock trades, spot buy clusters, and get "
                "scored alerts delivered to Discord.\n\n"
                "**Prefix:** `!`  •  **Cooldowns apply per channel/user**"
            ),
            colour=DEFAULT_EMBED_COLOUR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name="Capitol Gains Bot",
            icon_url="https://cdn.discordapp.com/embed/avatars/0.png",
        )

        # ── Research commands ──────────────────────────────────────────
        embed.add_field(
            name="🔍 Research",
            value=(
                "`!top [N]`\n"
                "  Top N stocks by congressional activity score (default 10).\n\n"
                "`!stock TICKER`\n"
                "  Full analysis: score, signals, trades, market data.\n\n"
                "`!score TICKER`\n"
                "  Detailed score breakdown showing point sources.\n\n"
                "`!sector [name]`\n"
                "  Sector activity overview, or detail for a named sector.\n"
                "  e.g. `!sector Tech` · `!sector Health` · `!sector Energy`"
            ),
            inline=False,
        )

        # ── Activity commands ──────────────────────────────────────────
        embed.add_field(
            name="📋 Activity",
            value=(
                "`!recent [days]`\n"
                "  Most recent congressional trades (default 7 days).\n\n"
                "`!alerts [days]`\n"
                "  Recent Capitol Gains alerts (default 7 days).\n\n"
                "`!watchlist`\n"
                "  All five ranked watchlists:\n"
                "  Top Picks · Most Bought · Hot Sectors · Rising · Improved"
            ),
            inline=False,
        )

        # ── Analytics commands ─────────────────────────────────────────
        embed.add_field(
            name="📊 Analytics",
            value=(
                "`!stats`\n"
                "  Database statistics, backtest win rates, and performance metrics."
            ),
            inline=False,
        )

        # ── Alert types ────────────────────────────────────────────────
        embed.add_field(
            name="🔔 Automated Alert Types",
            value=(
                "🔔 **BUY_CLUSTER** — 3+ politicians buy same stock ≤30 days\n"
                "📊 **SECTOR_SURGE** — Unusual sector concentration\n"
                "⭐ **HIGH_SCORE** — Score crossed alert threshold\n"
                "🔁 **REPEAT_BUYING** — Same politician buys repeatedly\n"
                "🚨 **UNUSUAL_ACTIVITY** — 3+ signals firing simultaneously"
            ),
            inline=False,
        )

        # ── Scoring overview ───────────────────────────────────────────
        embed.add_field(
            name="📐 How Scores Work (0–100)",
            value=(
                "Each stock is scored daily based on:\n"
                "• 🔔 Unique buyers × 5 pts\n"
                "• 📈 Net buy count × 3 pts\n"
                "• 🔁 Repeat purchases +5 pts\n"
                "• 📊 Sector momentum +10 pts\n"
                "• 💰 Large transactions +5 pts\n"
                "• ⚡ Frequency rising → 1.25× multiplier"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚠️ Disclaimer",
            value=(
                "Capitol Gains tracks congressional disclosures as a **research signal only**.\n"
                "This is **not** financial advice. Always do your own due diligence."
            ),
            inline=False,
        )

        embed.set_footer(
            text="!help COMMAND for detail on any command  •  Data updates daily"
        )
        return embed

    def _build_command_help(self, command_name: str) -> discord.Embed:
        """Build a detailed help embed for a single command."""
        details: dict[str, dict] = {
            "top": {
                "title": "!top [N]",
                "description": "Show the top N stocks ranked by congressional activity score.",
                "usage": [
                    "`!top` — top 10 stocks",
                    "`!top 5` — top 5 stocks",
                    "`!top 25` — top 25 stocks (maximum)",
                ],
                "notes": "Scores are composite metrics updated once daily. "
                         "Higher = more congressional buying interest.",
                "cooldown": "10 seconds per channel",
            },
            "stock": {
                "title": "!stock TICKER",
                "description": "Full congressional analysis for a ticker.",
                "usage": [
                    "`!stock AAPL` — Apple Inc analysis",
                    "`!stock NVDA` — NVIDIA analysis",
                    "`!stock LMT` — Lockheed Martin analysis",
                ],
                "notes": "Shows: score, active signals, last 90 days of trades, "
                         "market data snapshot, and score trend.",
                "cooldown": "15 seconds per user",
            },
            "score": {
                "title": "!score TICKER",
                "description": "Detailed point-by-point score breakdown for a ticker.",
                "usage": ["`!score AAPL`", "`!score MSFT`"],
                "notes": "Shows exactly how many points came from each signal, "
                         "plus the frequency multiplier if active.",
                "cooldown": "15 seconds per user",
            },
            "sector": {
                "title": "!sector [name]",
                "description": "Congressional sector activity overview or sector detail.",
                "usage": [
                    "`!sector` — ranked overview of all sectors",
                    "`!sector Tech` — Technology sector detail",
                    "`!sector Health` — Healthcare sector detail",
                    "`!sector Energy` · `!sector Financials` · `!sector Industrial`",
                ],
                "notes": "Partial names work: 'tech', 'health', 'pharma', 'oil', "
                         "'telecom', 'reit'.",
                "cooldown": "10 seconds per channel",
            },
            "recent": {
                "title": "!recent [days]",
                "description": "Most recent congressional stock trade disclosures.",
                "usage": [
                    "`!recent` — last 7 days",
                    "`!recent 14` — last 14 days",
                    "`!recent 30` — last 30 days (maximum)",
                ],
                "notes": "Shows buy/sell breakdown, politician count, and a "
                         "formatted trade list with amounts.",
                "cooldown": "10 seconds per channel",
            },
            "alerts": {
                "title": "!alerts [days]",
                "description": "Recently generated Capitol Gains trading alerts.",
                "usage": [
                    "`!alerts` — last 7 days",
                    "`!alerts 30` — last 30 days",
                ],
                "notes": "Shows alert type, ticker, score, and whether the alert "
                         "has been posted to Discord. ✅=posted ⏳=pending.",
                "cooldown": "10 seconds per channel",
            },
            "watchlist": {
                "title": "!watchlist",
                "description": "All five automatically maintained Capitol Gains watchlists.",
                "usage": ["`!watchlist`"],
                "notes": (
                    "Lists shown:\n"
                    "• Top Congressional Picks (highest scores)\n"
                    "• Most Bought (30-day purchase count)\n"
                    "• Hottest Sectors (by aggregate score)\n"
                    "• Fastest Rising (score velocity)\n"
                    "• Most Improved (7-day absolute gain)"
                ),
                "cooldown": "30 seconds per channel",
            },
            "stats": {
                "title": "!stats",
                "description": "Bot statistics, database summary, and backtest performance.",
                "usage": ["`!stats`"],
                "notes": "Includes: total trades, politicians tracked, alert count, "
                         "30/90-day backtest win rates, and S&P 500 comparison.",
                "cooldown": "30 seconds per channel",
            },
        }

        info = details.get(command_name)
        if info is None:
            return discord.Embed(
                title="❌ Command Not Found",
                description=(
                    f"No command named **{command_name!r}**.\n\n"
                    "Available commands: "
                    + " · ".join(f"`!{c}`" for c in details)
                ),
                colour=0xFF0000,
            )

        embed = discord.Embed(
            title=f"📖 Command: {info['title']}",
            description=info["description"],
            colour=DEFAULT_EMBED_COLOUR,
        )

        embed.add_field(
            name="Usage",
            value="\n".join(info["usage"]),
            inline=False,
        )
        embed.add_field(
            name="Notes",
            value=info["notes"],
            inline=False,
        )
        embed.add_field(
            name="Cooldown",
            value=info.get("cooldown", "Standard"),
            inline=True,
        )
        embed.set_footer(text="!help for full command list")
        return embed

    @help.error
    async def help_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[HelpCommand] Unhandled error: {}", error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCommand(bot))
