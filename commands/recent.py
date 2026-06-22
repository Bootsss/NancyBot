"""
commands/recent.py — !recent, !alerts, !watchlist, and !stats commands.

!recent    — Latest congressional trades (last 7 days)
!alerts    — Recent generated alerts
!watchlist — All five ranked watchlists
!stats     — Bot performance statistics and backtest results
"""

from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord.ext import commands
from loguru import logger
from sqlalchemy import desc, func, select

from config import DEFAULT_EMBED_COLOUR
from database import get_session
from models import Alert, Backtest, Company, Score, Trade
from services.alert_service import get_alert_service
from services.scoring_engine import build_watchlist_summary, get_scoring_engine


class RecentCommand(commands.Cog, name="Recent"):
    """Cog providing !recent, !alerts, !watchlist, and !stats."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # !recent
    # ------------------------------------------------------------------ #

    @commands.command(name="recent", help="Show the most recent congressional trades.")
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.channel)
    async def recent(self, ctx: commands.Context, days: int = 7) -> None:
        """
        Display the most recent congressional stock trades.

        Parameters
        ----------
        days : int, optional
            Look-back window in days (1–30, default 7).
        """
        days = max(1, min(days, 30))
        cutoff = datetime.utcnow() - timedelta(days=days)

        async with ctx.typing():
            with get_session() as session:
                stmt = (
                    select(Trade)
                    .where(Trade.trade_date >= cutoff)
                    .order_by(desc(Trade.trade_date))
                    .limit(20)
                )
                trades = list(session.scalars(stmt).all())

                # Total count for the window
                count_stmt = select(func.count(Trade.id)).where(
                    Trade.trade_date >= cutoff
                )
                total = session.scalar(count_stmt) or 0

        if not trades:
            await ctx.send(
                f"📭 No trades found in the last {days} day(s). "
                "Disclosures may not have been collected yet."
            )
            return

        embed = discord.Embed(
            title=f"📋 Recent Congressional Trades (Last {days} Day{'s' if days != 1 else ''})",
            description=f"**{total}** total trades. Showing the {len(trades)} most recent.",
            colour=DEFAULT_EMBED_COLOUR,
            timestamp=discord.utils.utcnow(),
        )

        buy_count = sum(1 for t in trades if t.is_buy)
        sell_count = sum(1 for t in trades if t.is_sell)
        embed.add_field(name="🟢 Buys", value=str(buy_count), inline=True)
        embed.add_field(name="🔴 Sells", value=str(sell_count), inline=True)
        unique_pols = len({t.politician_name for t in trades})
        embed.add_field(name="👤 Politicians", value=str(unique_pols), inline=True)

        lines = []
        for t in trades[:15]:
            icon = "🟢" if t.is_buy else "🔴"
            ticker_str = f"**${t.ticker}**" if t.ticker else f"_{t.company_name[:20]}_"
            date_str = t.trade_date.strftime("%m/%d")
            amount = t.amount_range or "N/A"
            pol_short = t.politician_name.split()[-1]   # last name only to save space
            lines.append(
                f"{icon} {ticker_str} · {pol_short} · {date_str} · {amount}"
            )

        embed.add_field(
            name="Trades",
            value="\n".join(lines),
            inline=False,
        )

        embed.set_footer(
            text=(
                f"!recent {days} to adjust window  •  "
                "!stock TICKER for analysis  •  "
                "!alerts for alerts"
            )
        )
        await ctx.send(embed=embed)
        logger.info("[RecentCommand] Served !recent ({} days) to channel {}.", days, ctx.channel.id)

    # ------------------------------------------------------------------ #
    # !alerts
    # ------------------------------------------------------------------ #

    @commands.command(name="alerts", help="Show recent Capitol Gains alerts.")
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.channel)
    async def alerts(self, ctx: commands.Context, days: int = 7) -> None:
        """
        Display recently generated trading alerts.

        Parameters
        ----------
        days : int, optional
            Look-back window in days (1–30, default 7).
        """
        days = max(1, min(days, 30))

        async with ctx.typing():
            svc = get_alert_service()
            recent = svc.get_recent_alerts(days=days, limit=15)

        if not recent:
            await ctx.send(
                f"📭 No alerts in the last {days} day(s). "
                "Alerts are generated after the daily scoring run."
            )
            return

        embed = discord.Embed(
            title=f"🔔 Recent Alerts (Last {days} Day{'s' if days != 1 else ''})",
            description=f"**{len(recent)}** alerts. Use `!stock TICKER` for full analysis.",
            colour=0xFFA500,
            timestamp=discord.utils.utcnow(),
        )

        type_icons = {
            "BUY_CLUSTER":      "🔔",
            "SECTOR_SURGE":     "📊",
            "HIGH_SCORE":       "⭐",
            "REPEAT_BUYING":    "🔁",
            "UNUSUAL_ACTIVITY": "🚨",
        }

        lines = []
        for alert in recent:
            icon = type_icons.get(alert.alert_type, "📌")
            date_str = alert.alert_date.strftime("%m/%d %H:%M")
            posted_mark = "✅" if alert.posted else "⏳"
            lines.append(
                f"{icon} **${alert.ticker}** · {alert.alert_type} · "
                f"score {alert.score:.0f} · {date_str} {posted_mark}"
            )

        embed.add_field(
            name="Alerts",
            value="\n".join(lines),
            inline=False,
        )

        embed.add_field(
            name="Legend",
            value="✅ Posted to Discord  ⏳ Pending",
            inline=False,
        )

        embed.set_footer(text="!stock TICKER for detail  •  !watchlist for rankings")
        await ctx.send(embed=embed)
        logger.info("[RecentCommand] Served !alerts ({} days) to channel {}.", days, ctx.channel.id)

    # ------------------------------------------------------------------ #
    # !watchlist
    # ------------------------------------------------------------------ #

    @commands.command(name="watchlist", help="Show all five Capitol Gains watchlists.")
    @commands.cooldown(rate=1, per=30, type=commands.BucketType.channel)
    async def watchlist(self, ctx: commands.Context) -> None:
        """Display all five automated watchlists as a single embed."""
        async with ctx.typing():
            try:
                scoring = get_scoring_engine()
                summary = build_watchlist_summary(scoring)
            except Exception as exc:
                logger.error("[RecentCommand] Watchlist build failed: {}", exc)
                await ctx.send("❌ Failed to build watchlists. Please try again later.")
                return

        embed = discord.Embed(
            title="📋 Capitol Gains Watchlists",
            description="Five automatically maintained stock lists.",
            colour=DEFAULT_EMBED_COLOUR,
            timestamp=discord.utils.utcnow(),
        )

        # Top Congressional
        top = summary.get("TOP_CONGRESSIONAL", [])[:5]
        if top:
            lines = [
                f"`{e.rank}.` **${e.ticker}** — {e.score:.0f}/100 ({e.sector or 'N/A'})"
                for e in top
            ]
            embed.add_field(
                name="🏆 Top Congressional Picks",
                value="\n".join(lines),
                inline=False,
            )

        # Most Bought
        bought = summary.get("MOST_BOUGHT", [])[:5]
        if bought:
            lines = [
                f"`{e.rank}.` **${e.ticker}** — {e.metadata.get('buy_count', 0)} buys "
                f"({e.metadata.get('unique_buyers', 0)} politicians)"
                for e in bought
            ]
            embed.add_field(
                name="📈 Most Bought (30 days)",
                value="\n".join(lines),
                inline=False,
            )

        # Top Sector
        sectors = summary.get("TOP_SECTOR", [])[:5]
        if sectors:
            lines = [
                f"`{e.rank}.` **{e.sector}** — avg score {e.score:.0f} "
                f"({e.metadata.get('ticker_count', 0)} stocks)"
                for e in sectors
            ]
            embed.add_field(
                name="📊 Hottest Sectors",
                value="\n".join(lines),
                inline=False,
            )

        # Fastest Rising
        rising = summary.get("FASTEST_RISING", [])[:5]
        if rising:
            lines = [
                f"`{e.rank}.` **${e.ticker}** — {e.score:.0f}/100 "
                f"(+{e.metadata.get('score_delta', 0):.1f} pts)"
                for e in rising
            ]
            embed.add_field(
                name="⚡ Fastest Rising Scores",
                value="\n".join(lines),
                inline=False,
            )

        # Most Improved
        improved = summary.get("MOST_IMPROVED", [])[:5]
        if improved:
            lines = [
                f"`{e.rank}.` **${e.ticker}** — {e.score:.0f}/100 "
                f"(was {e.metadata.get('prior_score', 0):.0f})"
                for e in improved
            ]
            embed.add_field(
                name="📉→📈 Most Improved",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(
            text="!top for expanded rankings  •  !stock TICKER for analysis  •  Updates daily"
        )
        await ctx.send(embed=embed)
        logger.info("[RecentCommand] Served !watchlist to channel {}.", ctx.channel.id)

    # ------------------------------------------------------------------ #
    # !stats
    # ------------------------------------------------------------------ #

    @commands.command(name="stats", help="Bot performance statistics and backtest results.")
    @commands.cooldown(rate=1, per=30, type=commands.BucketType.channel)
    async def stats(self, ctx: commands.Context) -> None:
        """Display database statistics and backtest performance metrics."""
        async with ctx.typing():
            stats_data = _collect_stats()

        embed = discord.Embed(
            title="📊 Capitol Gains — Statistics",
            colour=DEFAULT_EMBED_COLOUR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Capitol Gains Bot")

        # Database summary
        embed.add_field(
            name="📁 Database",
            value=(
                f"Total trades: **{stats_data['total_trades']:,}**\n"
                f"Unique tickers: **{stats_data['unique_tickers']:,}**\n"
                f"Politicians tracked: **{stats_data['unique_politicians']:,}**\n"
                f"Companies in DB: **{stats_data['companies']:,}**\n"
                f"Alerts generated: **{stats_data['total_alerts']:,}**"
            ),
            inline=True,
        )

        # Backtest performance
        bt = stats_data["backtest"]
        if bt["count"] > 0:
            embed.add_field(
                name="🎯 Backtest (30-Day)",
                value=(
                    f"Signals tracked: **{bt['count']}**\n"
                    f"Win rate: **{bt['win_rate_30']:.1f}%**\n"
                    f"Avg return: **{bt['avg_return_30']:+.2f}%**\n"
                    f"Best: **{bt['best_30']:+.2f}%**\n"
                    f"Worst: **{bt['worst_30']:+.2f}%**\n"
                    f"Beat S&P 500: **{bt['beat_market_30']:.1f}%**"
                ),
                inline=True,
            )
        else:
            embed.add_field(
                name="🎯 Backtest",
                value="Not enough data yet.\n(Need 30+ days of alerts.)",
                inline=True,
            )

        # Recent activity
        embed.add_field(
            name="📅 Recent Activity",
            value=(
                f"Trades (7d): **{stats_data['trades_7d']:,}**\n"
                f"Trades (30d): **{stats_data['trades_30d']:,}**\n"
                f"Alerts (7d): **{stats_data['alerts_7d']:,}**\n"
                f"Scored tickers: **{stats_data['scored_tickers']:,}**"
            ),
            inline=False,
        )

        embed.set_footer(
            text="Backtest data grows with each passing month  •  !help for commands"
        )
        await ctx.send(embed=embed)
        logger.info("[RecentCommand] Served !stats to channel {}.", ctx.channel.id)

    # ------------------------------------------------------------------ #
    # Error handlers
    # ------------------------------------------------------------------ #

    @recent.error
    async def recent_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ Please provide a valid number of days, e.g. `!recent 14`")
        else:
            logger.error("[RecentCommand] !recent error: {}", error)

    @alerts.error
    async def alerts_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[RecentCommand] !alerts error: {}", error)

    @watchlist.error
    async def watchlist_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[RecentCommand] !watchlist error: {}", error)

    @stats.error
    async def stats_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s.")
        else:
            logger.error("[RecentCommand] !stats error: {}", error)


# ---------------------------------------------------------------------------
# Stats data collector
# ---------------------------------------------------------------------------

def _collect_stats() -> dict:
    """Query all statistics in one session for the !stats embed."""
    now = datetime.utcnow()
    cutoff_7 = now - timedelta(days=7)
    cutoff_30 = now - timedelta(days=30)

    with get_session() as session:
        total_trades = session.scalar(select(func.count(Trade.id))) or 0
        unique_tickers = session.scalar(
            select(func.count(Trade.ticker.distinct())).where(Trade.ticker.isnot(None))
        ) or 0
        unique_politicians = session.scalar(
            select(func.count(Trade.politician_name.distinct()))
        ) or 0
        companies = session.scalar(select(func.count(Company.ticker))) or 0
        total_alerts = session.scalar(select(func.count(Alert.id))) or 0
        trades_7d = session.scalar(
            select(func.count(Trade.id)).where(Trade.trade_date >= cutoff_7)
        ) or 0
        trades_30d = session.scalar(
            select(func.count(Trade.id)).where(Trade.trade_date >= cutoff_30)
        ) or 0
        alerts_7d = session.scalar(
            select(func.count(Alert.id)).where(Alert.alert_date >= cutoff_7)
        ) or 0
        scored_tickers = session.scalar(
            select(func.count(Score.ticker.distinct()))
        ) or 0

        # Backtest stats
        bt_rows = list(
            session.scalars(
                select(Backtest).where(Backtest.return_30d.isnot(None))
            ).all()
        )

    bt: dict = {"count": 0}
    if bt_rows:
        r30 = [b.return_30d for b in bt_rows if b.return_30d is not None]
        beat_30 = [b for b in bt_rows if b.beat_market_30d is True]
        bt = {
            "count": len(bt_rows),
            "win_rate_30": sum(1 for r in r30 if r > 0) / len(r30) * 100 if r30 else 0,
            "avg_return_30": sum(r30) / len(r30) if r30 else 0,
            "best_30": max(r30) if r30 else 0,
            "worst_30": min(r30) if r30 else 0,
            "beat_market_30": len(beat_30) / len(bt_rows) * 100 if bt_rows else 0,
        }

    return {
        "total_trades": total_trades,
        "unique_tickers": unique_tickers,
        "unique_politicians": unique_politicians,
        "companies": companies,
        "total_alerts": total_alerts,
        "trades_7d": trades_7d,
        "trades_30d": trades_30d,
        "alerts_7d": alerts_7d,
        "scored_tickers": scored_tickers,
        "backtest": bt,
    }


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecentCommand(bot))
