"""
commands/stock.py — !stock TICKER command.

Shows a full analysis embed for a single ticker:
  • Current score + breakdown
  • Active signals
  • Recent congressional trades
  • Market data snapshot
  • Score history (last 7 days)

Usage:
    !stock AAPL
    !score AAPL    (alias — shows detailed score breakdown)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord.ext import commands
from loguru import logger
from sqlalchemy import and_, desc, select

from config import DEFAULT_EMBED_COLOUR, SECTOR_COLOURS
from database import get_session
from models import Company, Score, Trade
from services.analysis_engine import get_analysis_engine
from services.market_data import format_market_cap, format_volume, get_market_data_service
from services.scoring_engine import get_scoring_engine


# Signal name → display label mapping
SIGNAL_LABELS: dict[str, str] = {
    "BUY_CLUSTER":      "🔔 Buy Cluster",
    "NET_BUY_ACTIVITY": "📈 Net Buying",
    "SECTOR_SURGE":     "📊 Sector Surge",
    "REPEAT_BUYING":    "🔁 Repeat Buying",
    "FREQUENCY_RISE":   "⚡ Frequency Rising",
    "LARGE_TRANSACTION":"💰 Large Transactions",
}


class StockCommand(commands.Cog, name="Stock"):
    """Cog providing the !stock and !score commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # !stock TICKER
    # ------------------------------------------------------------------ #

    @commands.command(name="stock", help="Full congressional analysis for a ticker.")
    @commands.cooldown(rate=1, per=15, type=commands.BucketType.user)
    async def stock(self, ctx: commands.Context, ticker: str) -> None:
        """
        Display a full analysis embed for the given ticker.

        Parameters
        ----------
        ticker : str
            The stock ticker symbol (e.g. AAPL, NVDA, LMT).
        """
        ticker = ticker.upper().strip()
        if not ticker.isalpha() or len(ticker) > 10:
            await ctx.send("❌ Please provide a valid ticker symbol, e.g. `!stock AAPL`")
            return

        async with ctx.typing():
            embed = await self._build_stock_embed(ticker)

        if embed is None:
            await ctx.send(
                f"❌ No data found for **${ticker}**. "
                "It may not have been traded by any politician yet, "
                "or the ticker may be incorrect."
            )
            return

        await ctx.send(embed=embed)
        logger.info("[StockCommand] Served !stock {} to user {}.", ticker, ctx.author)

    # ------------------------------------------------------------------ #
    # !score TICKER (detailed breakdown alias)
    # ------------------------------------------------------------------ #

    @commands.command(name="score", help="Detailed score breakdown for a ticker.")
    @commands.cooldown(rate=1, per=15, type=commands.BucketType.user)
    async def score(self, ctx: commands.Context, ticker: str) -> None:
        """
        Display the score breakdown for the given ticker.

        Alias for !stock with an emphasis on the scoring section.
        """
        ticker = ticker.upper().strip()
        if not ticker.isalpha() or len(ticker) > 10:
            await ctx.send("❌ Please provide a valid ticker symbol, e.g. `!score AAPL`")
            return

        async with ctx.typing():
            embed = await self._build_score_embed(ticker)

        if embed is None:
            await ctx.send(f"❌ No score data found for **${ticker}**.")
            return

        await ctx.send(embed=embed)
        logger.info("[StockCommand] Served !score {} to user {}.", ticker, ctx.author)

    # ------------------------------------------------------------------ #
    # Embed builders
    # ------------------------------------------------------------------ #

    async def _build_stock_embed(self, ticker: str) -> discord.Embed | None:
        """Build the full !stock embed. Returns None if no data exists."""
        company = _get_company(ticker)
        recent_trades = _get_recent_trades(ticker, days=90)

        if not recent_trades and company is None:
            return None

        # Run live analysis
        try:
            engine = get_analysis_engine()
            signals = engine.analyse_ticker(ticker, lookback_days=90)
            scoring = get_scoring_engine()
            score = scoring.score_ticker(ticker, signals=signals, persist=False)
            latest_db_score = scoring.get_latest_score(ticker)
        except Exception as exc:
            logger.error("[StockCommand] Analysis failed for {}: {}", ticker, exc)
            score = 0.0
            signals = None
            latest_db_score = None

        sector = company.sector if company else "Unknown"
        colour = SECTOR_COLOURS.get(sector, DEFAULT_EMBED_COLOUR)
        company_name = company.company_name if company else ticker

        embed = discord.Embed(
            title=f"${ticker} — {company_name}",
            colour=colour,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Capitol Gains  •  Full Stock Analysis")

        # ── Score overview ─────────────────────────────────────────────
        score_bar = _score_bar(score)
        embed.add_field(
            name="📊 Activity Score",
            value=f"{score_bar} **{score:.1f}**/100",
            inline=False,
        )

        # ── Market data ────────────────────────────────────────────────
        if company:
            price_str = f"${company.current_price:,.2f}" if company.current_price else "N/A"
            w52_str = "N/A"
            if company.week_52_return is not None:
                icon = "📈" if company.week_52_return >= 0 else "📉"
                w52_str = f"{icon} {company.week_52_return:+.1f}%"

            embed.add_field(name="Sector", value=sector, inline=True)
            embed.add_field(name="Industry", value=company.industry or "N/A", inline=True)
            embed.add_field(name="Price", value=price_str, inline=True)
            embed.add_field(name="Market Cap", value=format_market_cap(company.market_cap), inline=True)
            embed.add_field(name="P/E Ratio", value=f"{company.pe_ratio:.1f}" if company.pe_ratio else "N/A", inline=True)
            embed.add_field(name="52-Week Return", value=w52_str, inline=True)
            embed.add_field(name="Avg Volume", value=format_volume(company.avg_volume), inline=True)

            if company.revenue_growth is not None:
                embed.add_field(
                    name="Revenue Growth",
                    value=f"{company.revenue_growth * 100:+.1f}% YoY",
                    inline=True,
                )

        # ── Active signals ─────────────────────────────────────────────
        if signals and signals.active_signals:
            signal_lines = []
            for sig in signals.active_signals:
                label = SIGNAL_LABELS.get(sig.signal_name, sig.signal_name)
                signal_lines.append(f"• {label}: {sig.summary}")
            embed.add_field(
                name=f"🔥 Active Signals ({len(signals.active_signals)})",
                value="\n".join(signal_lines[:6]),
                inline=False,
            )
        else:
            embed.add_field(
                name="Signals",
                value="No active signals in the last 90 days.",
                inline=False,
            )

        # ── Recent trades ──────────────────────────────────────────────
        if recent_trades:
            lines = []
            for t in recent_trades[:8]:
                icon = "🟢" if t.is_buy else "🔴"
                action = "BUY " if t.is_buy else "SELL"
                date_str = t.trade_date.strftime("%Y-%m-%d")
                amount = t.amount_range or "N/A"
                lines.append(
                    f"{icon} **{action}** {t.politician_name} — {date_str} — {amount}"
                )
            embed.add_field(
                name=f"Recent Trades ({len(recent_trades)} in 90 days)",
                value="\n".join(lines),
                inline=False,
            )

        # ── Score history ──────────────────────────────────────────────
        history = scoring.get_score_history(ticker, days=30) if scoring else []
        if len(history) >= 2:
            trend = history[-1].score - history[0].score
            trend_icon = "📈" if trend > 0 else ("📉" if trend < 0 else "➡️")
            embed.add_field(
                name="Score Trend (30 days)",
                value=(
                    f"{trend_icon} {history[0].score:.1f} → {history[-1].score:.1f} "
                    f"({trend:+.1f} pts)"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"!score {ticker} for breakdown  •  "
                f"!recent for latest trades  •  "
                f"Data updated daily"
            )
        )
        return embed

    async def _build_score_embed(self, ticker: str) -> discord.Embed | None:
        """Build the !score embed showing detailed point breakdown."""
        try:
            scoring = get_scoring_engine()
            latest = scoring.get_latest_score(ticker)
        except Exception as exc:
            logger.error("[StockCommand] Score fetch failed for {}: {}", ticker, exc)
            return None

        if latest is None:
            return None

        company = _get_company(ticker)
        sector = company.sector if company else "Unknown"
        colour = SECTOR_COLOURS.get(sector, DEFAULT_EMBED_COLOUR)
        company_name = company.company_name if company else ticker
        bd = latest.breakdown

        embed = discord.Embed(
            title=f"${ticker} — Score Breakdown",
            description=f"**{company_name}** · {sector}",
            colour=colour,
            timestamp=latest.calculated_at,
        )
        embed.set_author(name="Capitol Gains  •  Score Detail")

        # Overall score bar
        embed.add_field(
            name="Overall Score",
            value=f"{_score_bar(latest.score)} **{latest.score:.1f}**/100",
            inline=False,
        )

        # Component breakdown
        components = [
            ("🔔 Unique Buyer Points",    bd.get("unique_buyer_points", 0)),
            ("📈 Net Buy Points",         bd.get("net_buy_points", 0)),
            ("📊 Sector Momentum",        bd.get("sector_momentum_points", 0)),
            ("🔁 Repeat Purchase Bonus",  bd.get("repeat_purchase_points", 0)),
            ("💰 Large Transaction Bonus",bd.get("large_transaction_points", 0)),
        ]

        component_lines = []
        for label, pts in components:
            bar = _mini_bar(pts, max_pts=25)
            component_lines.append(f"{label}\n  {bar} {pts:.1f} pts")

        embed.add_field(
            name="Point Breakdown",
            value="\n".join(component_lines),
            inline=False,
        )

        freq_active = bd.get("frequency_multiplier_active", 0)
        if freq_active:
            embed.add_field(
                name="⚡ Frequency Multiplier",
                value="Active — score multiplied by 1.25×",
                inline=False,
            )

        active_signals = bd.get("active_signals", [])
        if active_signals:
            embed.add_field(
                name="Active Signals",
                value="\n".join(
                    f"• {SIGNAL_LABELS.get(s, s)}" for s in active_signals
                ),
                inline=False,
            )

        embed.add_field(
            name="Raw Score",
            value=f"{bd.get('raw_score', 0):.2f} / 100.0",
            inline=True,
        )
        embed.add_field(
            name="Calculated At",
            value=latest.calculated_at.strftime("%Y-%m-%d %H:%M UTC"),
            inline=True,
        )

        embed.set_footer(text=f"!stock {ticker} for full analysis")
        return embed

    # ------------------------------------------------------------------ #
    # Error handlers
    # ------------------------------------------------------------------ #

    @stock.error
    async def stock_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Please provide a ticker symbol, e.g. `!stock AAPL`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[StockCommand] !stock error: {}", error)
            await ctx.send("❌ An unexpected error occurred.")

    @score.error
    async def score_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Please provide a ticker symbol, e.g. `!score AAPL`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[StockCommand] !score error: {}", error)
            await ctx.send("❌ An unexpected error occurred.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_company(ticker: str) -> Company | None:
    with get_session() as session:
        return session.get(Company, ticker)


def _get_recent_trades(ticker: str, days: int = 90) -> list[Trade]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_session() as session:
        stmt = (
            select(Trade)
            .where(and_(Trade.ticker == ticker, Trade.trade_date >= cutoff))
            .order_by(desc(Trade.trade_date))
            .limit(20)
        )
        return list(session.scalars(stmt).all())


def _score_bar(score: float, width: int = 10) -> str:
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _mini_bar(pts: float, max_pts: float = 25, width: int = 6) -> str:
    filled = round(min(pts / max_pts, 1.0) * width)
    return "▓" * filled + "░" * (width - filled)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StockCommand(bot))
