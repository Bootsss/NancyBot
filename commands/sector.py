"""
commands/sector.py — !sector command.

Shows the most active sectors by congressional buying activity,
with top tickers per sector and aggregate score.

Usage:
    !sector           — all tracked sectors ranked by activity
    !sector Tech      — detail view for a specific sector
"""

from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord.ext import commands
from loguru import logger
from sqlalchemy import and_, desc, func, select

from config import DEFAULT_EMBED_COLOUR, SECTOR_COLOURS
from database import get_session
from models import Company, Trade
from services.scoring_engine import get_scoring_engine


class SectorCommand(commands.Cog, name="Sector"):
    """Cog providing the !sector command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="sector", help="Most active sectors by congressional buying.")
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.channel)
    async def sector(self, ctx: commands.Context, *, sector_name: str = "") -> None:
        """
        Show sector-level congressional activity.

        With no argument: ranked overview of all sectors.
        With a sector name: detailed breakdown for that sector.

        Parameters
        ----------
        sector_name : str, optional
            Partial sector name to filter (e.g. "Tech", "Health", "Energy").
        """
        async with ctx.typing():
            if sector_name.strip():
                embed = self._build_sector_detail(sector_name.strip())
            else:
                embed = self._build_sector_overview()

        if embed is None:
            await ctx.send(
                "📭 No sector data available yet. "
                "Ensure the daily collection and scoring jobs have run."
            )
            return

        await ctx.send(embed=embed)
        logger.info(
            "[SectorCommand] Served !sector '{}' to channel {}.",
            sector_name, ctx.channel.id,
        )

    # ------------------------------------------------------------------ #
    # Overview embed — all sectors ranked
    # ------------------------------------------------------------------ #

    def _build_sector_overview(self) -> discord.Embed | None:
        """Ranked overview of all sectors by congressional buy activity."""
        cutoff = datetime.utcnow() - timedelta(days=30)

        with get_session() as session:
            # Buy counts per sector (last 30 days)
            buy_stmt = (
                select(
                    Company.sector,
                    func.count(Trade.id).label("buy_count"),
                    func.count(Trade.politician_name.distinct()).label("unique_buyers"),
                    func.count(Trade.ticker.distinct()).label("unique_tickers"),
                )
                .join(Company, Trade.ticker == Company.ticker)
                .where(
                    and_(
                        Trade.trade_type == "purchase",
                        Trade.trade_date >= cutoff,
                        Company.sector.isnot(None),
                    )
                )
                .group_by(Company.sector)
                .order_by(desc("buy_count"))
            )
            sector_rows = session.execute(buy_stmt).all()

        if not sector_rows:
            return None

        # Get sector scores from watchlist
        try:
            scoring = get_scoring_engine()
            sector_watchlist = scoring.get_watchlist("TOP_SECTOR", limit=20)
            sector_scores = {e.sector: e.score for e in sector_watchlist}
        except Exception:
            sector_scores = {}

        total_buys = sum(r.buy_count for r in sector_rows)

        embed = discord.Embed(
            title="📊 Congressional Sector Activity (30 Days)",
            description=(
                f"**{total_buys}** total purchase events across "
                f"**{len(sector_rows)}** sectors.\n"
                "Ranked by buy count. Use `!sector <name>` for detail."
            ),
            colour=DEFAULT_EMBED_COLOUR,
            timestamp=discord.utils.utcnow(),
        )

        lines = []
        for rank, row in enumerate(sector_rows[:12], start=1):
            sector = row.sector
            colour_dot = _sector_dot(sector)
            share = row.buy_count / total_buys * 100
            score = sector_scores.get(sector)
            score_str = f" · score {score:.0f}" if score else ""
            lines.append(
                f"`{rank:2d}.` {colour_dot} **{sector}**{score_str}\n"
                f"      {row.buy_count} buys · {row.unique_buyers} politicians "
                f"· {row.unique_tickers} stocks · {share:.0f}% share"
            )

        embed.add_field(
            name="Sector Rankings",
            value="\n".join(lines),
            inline=False,
        )

        embed.set_footer(
            text="!sector <name> for detail  •  !top for top stocks  •  Updates daily"
        )
        return embed

    # ------------------------------------------------------------------ #
    # Detail embed — one sector
    # ------------------------------------------------------------------ #

    def _build_sector_detail(self, query: str) -> discord.Embed | None:
        """
        Detailed view for a single sector matching the query string.

        Resolves partial names: "tech" → "Technology", "health" → "Healthcare".
        """
        sector_name = _resolve_sector(query)
        if sector_name is None:
            return _sector_not_found_embed(query)

        cutoff_30 = datetime.utcnow() - timedelta(days=30)
        cutoff_90 = datetime.utcnow() - timedelta(days=90)

        with get_session() as session:
            # Top tickers in this sector by buy count
            ticker_stmt = (
                select(
                    Trade.ticker,
                    Company.company_name,
                    func.count(Trade.id).label("buy_count"),
                    func.count(Trade.politician_name.distinct()).label("unique_buyers"),
                )
                .join(Company, Trade.ticker == Company.ticker)
                .where(
                    and_(
                        Company.sector == sector_name,
                        Trade.trade_type == "purchase",
                        Trade.trade_date >= cutoff_90,
                        Trade.ticker.isnot(None),
                    )
                )
                .group_by(Trade.ticker, Company.company_name)
                .order_by(desc("buy_count"))
                .limit(10)
            )
            ticker_rows = session.execute(ticker_stmt).all()

            # Buy / sell summary for the sector
            summary_stmt = (
                select(
                    Trade.trade_type,
                    func.count(Trade.id).label("count"),
                )
                .join(Company, Trade.ticker == Company.ticker)
                .where(
                    and_(
                        Company.sector == sector_name,
                        Trade.trade_date >= cutoff_30,
                    )
                )
                .group_by(Trade.trade_type)
            )
            type_rows = {r.trade_type: r.count for r in session.execute(summary_stmt).all()}

        buy_count = type_rows.get("purchase", 0)
        sell_count = type_rows.get("sale", 0)
        total = buy_count + sell_count

        colour = SECTOR_COLOURS.get(sector_name, DEFAULT_EMBED_COLOUR)

        embed = discord.Embed(
            title=f"📊 {sector_name} — Sector Detail",
            colour=colour,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Capitol Gains  •  Sector Analysis")

        # Buy/sell summary
        ratio = buy_count / total * 100 if total > 0 else 0
        net_icon = "🟢" if buy_count >= sell_count else "🔴"
        embed.add_field(
            name="30-Day Activity",
            value=(
                f"{net_icon} **{buy_count}** buys · **{sell_count}** sells\n"
                f"Buy ratio: **{ratio:.0f}%**"
            ),
            inline=True,
        )

        # Score from watchlist
        try:
            scoring = get_scoring_engine()
            sector_wl = scoring.get_watchlist("TOP_SECTOR", limit=20)
            sector_entry = next((e for e in sector_wl if e.sector == sector_name), None)
            if sector_entry:
                embed.add_field(
                    name="Sector Score",
                    value=f"**{sector_entry.score:.1f}**/100",
                    inline=True,
                )
        except Exception:
            pass

        embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Top tickers table
        if ticker_rows:
            lines = []
            for rank, row in enumerate(ticker_rows, start=1):
                lines.append(
                    f"`{rank:2d}.` **${row.ticker}** — {row.company_name}\n"
                    f"      {row.buy_count} buys · {row.unique_buyers} politicians (90d)"
                )
            embed.add_field(
                name=f"Top Stocks in {sector_name} (90 days)",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Top Stocks",
                value="No trades found for this sector in the last 90 days.",
                inline=False,
            )

        embed.set_footer(
            text="!stock TICKER for full analysis  •  !sector for overview"
        )
        return embed

    @sector.error
    async def sector_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[SectorCommand] Unhandled error: {}", error)
            await ctx.send("❌ An unexpected error occurred.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECTOR_ALIASES: dict[str, str] = {
    "tech":          "Technology",
    "technology":    "Technology",
    "health":        "Healthcare",
    "healthcare":    "Healthcare",
    "pharma":        "Healthcare",
    "financial":     "Financials",
    "financials":    "Financials",
    "finance":       "Financials",
    "energy":        "Energy",
    "oil":           "Energy",
    "consumer":      "Consumer Discretionary",
    "discretionary": "Consumer Discretionary",
    "staples":       "Consumer Staples",
    "industrial":    "Industrials",
    "industrials":   "Industrials",
    "material":      "Materials",
    "materials":     "Materials",
    "real estate":   "Real Estate",
    "realestate":    "Real Estate",
    "reit":          "Real Estate",
    "utilities":     "Utilities",
    "utility":       "Utilities",
    "communication": "Communication Services",
    "telecom":       "Communication Services",
    "comms":         "Communication Services",
}


def _resolve_sector(query: str) -> str | None:
    """Resolve a partial/alias sector name to the canonical GICS name."""
    q = query.lower().strip()
    # Direct alias match
    if q in _SECTOR_ALIASES:
        return _SECTOR_ALIASES[q]
    # Substring match against canonical names
    canonical = list(SECTOR_COLOURS.keys())
    for name in canonical:
        if q in name.lower():
            return name
    return None


def _sector_dot(sector: str) -> str:
    """Return a coloured circle emoji for the sector."""
    dots = {
        "Technology":             "🔵",
        "Healthcare":             "🟢",
        "Financials":             "🟡",
        "Energy":                 "🟠",
        "Consumer Discretionary": "🩷",
        "Consumer Staples":       "🟣",
        "Industrials":            "🔷",
        "Materials":              "🟩",
        "Real Estate":            "🔴",
        "Utilities":              "⚪",
        "Communication Services": "💜",
    }
    return dots.get(sector, "⬜")


def _sector_not_found_embed(query: str) -> discord.Embed:
    embed = discord.Embed(
        title="❌ Sector Not Found",
        description=(
            f"Could not find a sector matching **{query!r}**.\n\n"
            "Available sectors:\n"
            + "\n".join(f"• {s}" for s in SECTOR_COLOURS if s != "Unknown")
        ),
        colour=0xFF0000,
    )
    embed.set_footer(text="Try: !sector Tech · !sector Health · !sector Energy")
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SectorCommand(bot))
