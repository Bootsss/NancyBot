"""
services/alert_service.py — Alert generation and Discord posting service.

Responsibilities
----------------
1. Evaluate scored tickers against alert thresholds
2. Deduplicate: don't re-alert the same ticker+type within 7 days
3. Build rich Discord embeds for each alert type
4. Post embeds to the configured Discord channel
5. Mark alerts as posted in the database

Alert types
-----------
BUY_CLUSTER      — Signal A fired (3+ politicians, same stock, ≤30 days)
SECTOR_SURGE     — Signal C fired (sector concentration ≥1.5×)
HIGH_SCORE       — Ticker score crossed ALERT_SCORE_THRESHOLD
REPEAT_BUYING    — Signal D fired (same politician, multiple purchases)
UNUSUAL_ACTIVITY — Multiple signals firing simultaneously (≥3 active)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from loguru import logger
from sqlalchemy import and_, desc, select

from config import SECTOR_COLOURS, DEFAULT_EMBED_COLOUR, settings
from database import get_session
from models import Alert, Company, Score, Trade
from services.analysis_engine import TickerSignals, get_analysis_engine
from services.market_data import format_market_cap, format_volume, get_market_data_service
from services.scoring_engine import get_scoring_engine

if TYPE_CHECKING:
    from discord import TextChannel


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEDUP_WINDOW_DAYS: int = 7          # don't re-alert same ticker+type within N days
UNUSUAL_ACTIVITY_SIGNAL_COUNT: int = 3   # signals needed for UNUSUAL_ACTIVITY alert


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------

class AlertService:
    """
    Generates, persists, and posts trading alerts.

    Usage (sync — from scheduler)::

        svc = AlertService(discord_client)
        new_alerts = svc.generate_alerts()   # returns list[Alert]

    Usage (async — post pending alerts)::

        await svc.post_pending_alerts()
    """

    def __init__(self, discord_client: discord.Client | None = None) -> None:
        self._client = discord_client
        self._analysis = get_analysis_engine()
        self._scoring = get_scoring_engine()
        self._market = get_market_data_service()

    # ------------------------------------------------------------------ #
    # Alert generation (sync — runs in scheduler thread)
    # ------------------------------------------------------------------ #

    def generate_alerts(self, lookback_days: int = 90) -> list[Alert]:
        """
        Evaluate all active tickers and generate new alerts.

        Runs analysis + scoring, then checks each ticker against every
        alert type.  Deduplicates before inserting.

        Returns the list of newly created (unpublished) Alert rows.
        """
        logger.info("[AlertService] Starting alert generation run.")
        all_signals = self._analysis.analyse_all(lookback_days=lookback_days)
        new_alerts: list[Alert] = []

        for ticker_signals in all_signals:
            ticker = ticker_signals.ticker
            score = self._scoring.score_ticker(ticker, signals=ticker_signals, persist=False)

            try:
                alerts = self._evaluate_ticker(ticker, ticker_signals, score)
                for alert in alerts:
                    if self._is_duplicate(alert):
                        logger.debug(
                            "[AlertService] Dedup skip: {} / {}", ticker, alert.alert_type
                        )
                        continue
                    self._persist_alert(alert)
                    new_alerts.append(alert)
                    logger.info(
                        "[AlertService] New alert: {} {} score={:.1f}",
                        alert.alert_type, ticker, alert.score,
                    )
            except Exception as exc:
                logger.error(
                    "[AlertService] Failed to evaluate {}: {}", ticker, exc
                )

        logger.info("[AlertService] Generated {} new alerts.", len(new_alerts))
        return new_alerts

    def _evaluate_ticker(
        self,
        ticker: str,
        signals: TickerSignals,
        score: float,
    ) -> list[Alert]:
        """Run all alert checks for a single ticker. Returns candidate alerts."""
        alerts: list[Alert] = []
        active = signals.active_signal_names

        # BUY_CLUSTER
        cluster = signals.get("BUY_CLUSTER")
        if cluster and cluster.active:
            alerts.append(self._make_alert(
                ticker=ticker,
                alert_type="BUY_CLUSTER",
                score=score,
                summary=cluster.summary,
                signal_data={
                    "signal": cluster.data,
                    "active_signals": active,
                },
            ))

        # SECTOR_SURGE
        sector_sig = signals.get("SECTOR_SURGE")
        if sector_sig and sector_sig.active:
            alerts.append(self._make_alert(
                ticker=ticker,
                alert_type="SECTOR_SURGE",
                score=score,
                summary=sector_sig.summary,
                signal_data={
                    "signal": sector_sig.data,
                    "active_signals": active,
                },
            ))

        # HIGH_SCORE
        if score >= settings.alert_score_threshold:
            alerts.append(self._make_alert(
                ticker=ticker,
                alert_type="HIGH_SCORE",
                score=score,
                summary=f"Score {score:.1f} crossed threshold {settings.alert_score_threshold}.",
                signal_data={"active_signals": active},
            ))

        # REPEAT_BUYING
        repeat = signals.get("REPEAT_BUYING")
        if repeat and repeat.active:
            alerts.append(self._make_alert(
                ticker=ticker,
                alert_type="REPEAT_BUYING",
                score=score,
                summary=repeat.summary,
                signal_data={
                    "signal": repeat.data,
                    "active_signals": active,
                },
            ))

        # UNUSUAL_ACTIVITY — 3+ signals firing simultaneously
        if len(active) >= UNUSUAL_ACTIVITY_SIGNAL_COUNT:
            alerts.append(self._make_alert(
                ticker=ticker,
                alert_type="UNUSUAL_ACTIVITY",
                score=score,
                summary=(
                    f"{len(active)} signals firing simultaneously: "
                    f"{', '.join(active)}."
                ),
                signal_data={"active_signals": active},
            ))

        return alerts

    @staticmethod
    def _make_alert(
        ticker: str,
        alert_type: str,
        score: float,
        summary: str,
        signal_data: dict,
    ) -> Alert:
        alert = Alert(
            ticker=ticker,
            alert_type=alert_type,
            score=score,
            summary=summary,
            alert_date=datetime.utcnow(),
            posted=False,
        )
        alert.signal_data = signal_data
        return alert

    def _is_duplicate(self, alert: Alert) -> bool:
        """Return True if an identical ticker+type alert exists within DEDUP_WINDOW_DAYS."""
        cutoff = datetime.utcnow() - timedelta(days=DEDUP_WINDOW_DAYS)
        with get_session() as session:
            stmt = select(Alert).where(
                and_(
                    Alert.ticker == alert.ticker,
                    Alert.alert_type == alert.alert_type,
                    Alert.alert_date >= cutoff,
                )
            ).limit(1)
            existing = session.scalars(stmt).first()
            return existing is not None

    def _persist_alert(self, alert: Alert) -> None:
        """Insert an Alert row into the database, ensuring Company row exists first."""
        from models import Company, Trade
        from sqlalchemy import select
        with get_session() as session:
            if session.get(Company, alert.ticker) is None:
                trade = session.scalars(
                    select(Trade).where(Trade.ticker == alert.ticker).limit(1)
                ).first()
                company_name = (trade.company_name if trade else None) or alert.ticker
                session.add(Company(
                    ticker=alert.ticker,
                    company_name=company_name,
                    ticker_verified=False,
                    manually_overridden=False,
                ))
                session.flush()
            session.add(alert)

    # ------------------------------------------------------------------ #
    # Discord posting (async — runs inside the bot event loop)
    # ------------------------------------------------------------------ #

    async def post_pending_alerts(self) -> int:
        """
        Fetch all unposted alerts and send them to Discord.

        Returns the count of successfully posted alerts.
        """
        if self._client is None:
            logger.warning("[AlertService] No Discord client — cannot post alerts.")
            return 0

        channel = self._get_channel()
        if channel is None:
            return 0

        with get_session() as session:
            stmt = (
                select(Alert)
                .where(Alert.posted.is_(False))
                .order_by(Alert.alert_date.asc())
            )
            pending = list(session.scalars(stmt).all())

        logger.info("[AlertService] {} pending alerts to post.", len(pending))
        posted_count = 0

        for alert in pending:
            try:
                embed = await self._build_embed(alert)
                await channel.send(embed=embed)
                self._mark_posted(alert.id)
                posted_count += 1
                await asyncio.sleep(1)   # rate limit: 1 msg/sec
            except discord.HTTPException as exc:
                logger.error(
                    "[AlertService] Discord HTTP error posting alert {}: {}", alert.id, exc
                )
            except Exception as exc:
                logger.error(
                    "[AlertService] Failed to post alert {}: {}", alert.id, exc
                )

        logger.info("[AlertService] Posted {}/{} alerts.", posted_count, len(pending))
        return posted_count

    async def post_alert_immediately(self, alert: Alert) -> bool:
        """Post a single alert embed to Discord right now. Returns success."""
        if self._client is None:
            return False
        channel = self._get_channel()
        if channel is None:
            return False
        try:
            embed = await self._build_embed(alert)
            await channel.send(embed=embed)
            self._mark_posted(alert.id)
            return True
        except Exception as exc:
            logger.error("[AlertService] Failed to post alert {}: {}", alert.id, exc)
            return False

    def _get_channel(self) -> "TextChannel | None":
        channel = self._client.get_channel(settings.discord_channel_id)
        if channel is None:
            logger.error(
                "[AlertService] Channel {} not found. Check DISCORD_CHANNEL_ID.",
                settings.discord_channel_id,
            )
        return channel

    def _mark_posted(self, alert_id: int) -> None:
        with get_session() as session:
            alert = session.get(Alert, alert_id)
            if alert:
                alert.posted = True
                alert.posted_at = datetime.utcnow()

    # ------------------------------------------------------------------ #
    # Embed builders
    # ------------------------------------------------------------------ #

    async def _build_embed(self, alert: Alert) -> discord.Embed:
        """Route to the correct embed builder by alert type."""
        company = self._get_company(alert.ticker)
        recent_trades = self._get_recent_trades(alert.ticker, days=30)

        builders = {
            "BUY_CLUSTER":      self._embed_buy_cluster,
            "SECTOR_SURGE":     self._embed_sector_surge,
            "HIGH_SCORE":       self._embed_high_score,
            "REPEAT_BUYING":    self._embed_repeat_buying,
            "UNUSUAL_ACTIVITY": self._embed_unusual_activity,
        }
        builder = builders.get(alert.alert_type, self._embed_generic)
        return builder(alert, company, recent_trades)

    def _base_embed(
        self, alert: Alert, company: Company | None, title: str, description: str
    ) -> discord.Embed:
        """Create a styled base embed shared by all alert types."""
        sector = company.sector if company else "Unknown"
        colour = SECTOR_COLOURS.get(sector, DEFAULT_EMBED_COLOUR)

        embed = discord.Embed(
            title=title,
            description=description,
            colour=colour,
            timestamp=alert.alert_date,
        )

        company_name = company.company_name if company else alert.ticker
        embed.set_author(name=f"Capitol Gains Alert  •  {alert.alert_type}")
        embed.set_footer(text=f"Score: {alert.score:.1f}/100  •  {sector}")

        # Core fields always present
        embed.add_field(name="Ticker", value=f"**${alert.ticker}**", inline=True)
        embed.add_field(name="Company", value=company_name, inline=True)
        embed.add_field(name="Sector", value=sector, inline=True)

        if company:
            embed.add_field(
                name="Market Cap",
                value=format_market_cap(company.market_cap),
                inline=True,
            )
            embed.add_field(
                name="Avg Volume",
                value=format_volume(company.avg_volume),
                inline=True,
            )
            if company.week_52_return is not None:
                direction = "📈" if company.week_52_return >= 0 else "📉"
                embed.add_field(
                    name="52-Week Return",
                    value=f"{direction} {company.week_52_return:+.1f}%",
                    inline=True,
                )

        return embed

    def _embed_buy_cluster(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        signal = alert.signal_data.get("signal", {})
        buyers = signal.get("buyers", [])
        buyer_count = signal.get("max_unique_buyers", len(buyers))
        window_start = signal.get("window_start", "")[:10] if signal.get("window_start") else "N/A"

        embed = self._base_embed(
            alert, company,
            title=f"🔔 Buy Cluster — ${alert.ticker}",
            description=(
                f"**{buyer_count} politicians** bought **${alert.ticker}** "
                f"within {settings.buy_cluster_window_days} days.\n"
                f"Cluster start: {window_start}"
            ),
        )

        if buyers:
            embed.add_field(
                name=f"Buyers ({len(buyers)})",
                value="\n".join(f"• {b}" for b in buyers[:8]),
                inline=False,
            )

        self._add_recent_activity(embed, trades)
        return embed

    def _embed_sector_surge(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        signal = alert.signal_data.get("signal", {})
        sector = signal.get("sector", company.sector if company else "Unknown")
        ratio = signal.get("concentration_ratio", 0)
        sector_buys = signal.get("sector_buys", 0)
        total_buys = signal.get("total_buys", 0)

        embed = self._base_embed(
            alert, company,
            title=f"📊 Sector Surge — {sector}",
            description=(
                f"**{sector}** is receiving **{ratio:.1f}×** its expected share "
                f"of congressional buying.\n"
                f"{sector_buys} of {total_buys} total buys in this sector."
            ),
        )

        all_sectors = signal.get("all_sectors", {})
        if all_sectors:
            top_sectors = sorted(all_sectors.items(), key=lambda x: x[1], reverse=True)[:5]
            embed.add_field(
                name="Top Sectors by Buy Count",
                value="\n".join(f"• {s}: {c}" for s, c in top_sectors),
                inline=False,
            )

        self._add_recent_activity(embed, trades)
        return embed

    def _embed_high_score(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        active_signals = alert.signal_data.get("active_signals", [])

        embed = self._base_embed(
            alert, company,
            title=f"⭐ High Score — ${alert.ticker}",
            description=(
                f"**${alert.ticker}** reached a congressional activity score of "
                f"**{alert.score:.1f}/100**, crossing the alert threshold of "
                f"{settings.alert_score_threshold}."
            ),
        )

        if active_signals:
            signal_labels = {
                "BUY_CLUSTER": "🔔 Buy Cluster",
                "NET_BUY_ACTIVITY": "📈 Net Buying",
                "SECTOR_SURGE": "📊 Sector Surge",
                "REPEAT_BUYING": "🔁 Repeat Buying",
                "FREQUENCY_RISE": "⚡ Frequency Rising",
                "LARGE_TRANSACTION": "💰 Large Transactions",
            }
            lines = [signal_labels.get(s, s) for s in active_signals]
            embed.add_field(
                name="Active Signals",
                value="\n".join(f"• {l}" for l in lines),
                inline=False,
            )

        self._add_recent_activity(embed, trades)
        return embed

    def _embed_repeat_buying(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        signal = alert.signal_data.get("signal", {})
        repeat_buyers = signal.get("repeat_buyers", {})

        embed = self._base_embed(
            alert, company,
            title=f"🔁 Repeat Buying — ${alert.ticker}",
            description=(
                f"**{len(repeat_buyers)} politician(s)** have made multiple "
                f"purchases of **${alert.ticker}**."
            ),
        )

        if repeat_buyers:
            lines = [
                f"• {name}: {count}× purchases"
                for name, count in sorted(
                    repeat_buyers.items(), key=lambda x: x[1], reverse=True
                )[:8]
            ]
            embed.add_field(
                name="Repeat Buyers",
                value="\n".join(lines),
                inline=False,
            )

        self._add_recent_activity(embed, trades)
        return embed

    def _embed_unusual_activity(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        active_signals = alert.signal_data.get("active_signals", [])

        embed = self._base_embed(
            alert, company,
            title=f"🚨 Unusual Activity — ${alert.ticker}",
            description=(
                f"**{len(active_signals)} signals** firing simultaneously for "
                f"**${alert.ticker}**. This is a high-conviction setup."
            ),
        )

        signal_descriptions = {
            "BUY_CLUSTER":      "🔔 Multiple politicians buying",
            "NET_BUY_ACTIVITY": "📈 Buys outpacing sells",
            "SECTOR_SURGE":     "📊 Sector concentration spike",
            "REPEAT_BUYING":    "🔁 Repeat purchases detected",
            "FREQUENCY_RISE":   "⚡ Purchase frequency accelerating",
            "LARGE_TRANSACTION":"💰 High-value transactions",
        }
        lines = [signal_descriptions.get(s, s) for s in active_signals]
        embed.add_field(
            name="Firing Signals",
            value="\n".join(f"• {l}" for l in lines),
            inline=False,
        )

        self._add_recent_activity(embed, trades)
        return embed

    def _embed_generic(
        self, alert: Alert, company: Company | None, trades: list[Trade]
    ) -> discord.Embed:
        embed = self._base_embed(
            alert, company,
            title=f"📌 Alert — ${alert.ticker}",
            description=alert.summary or "Congressional trading activity detected.",
        )
        self._add_recent_activity(embed, trades)
        return embed

    @staticmethod
    def _add_recent_activity(embed: discord.Embed, trades: list[Trade]) -> None:
        """Append a Recent Activity field to the embed."""
        if not trades:
            return
        lines = []
        for t in sorted(trades, key=lambda x: x.trade_date, reverse=True)[:6]:
            action = "BUY" if t.is_buy else "SELL"
            icon = "🟢" if t.is_buy else "🔴"
            date_str = t.trade_date.strftime("%Y-%m-%d")
            amount = t.amount_range or "N/A"
            lines.append(f"{icon} **{action}** — {t.politician_name} — {date_str} — {amount}")
        embed.add_field(
            name="Recent Activity",
            value="\n".join(lines),
            inline=False,
        )

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_company(ticker: str) -> Company | None:
        with get_session() as session:
            return session.get(Company, ticker)

    @staticmethod
    def _get_recent_trades(ticker: str, days: int = 30) -> list[Trade]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_session() as session:
            stmt = (
                select(Trade)
                .where(Trade.ticker == ticker, Trade.trade_date >= cutoff)
                .order_by(desc(Trade.trade_date))
                .limit(10)
            )
            return list(session.scalars(stmt).all())

    # ------------------------------------------------------------------ #
    # Reporting helpers (used by scheduler report jobs)
    # ------------------------------------------------------------------ #

    def get_recent_alerts(self, days: int = 7, limit: int = 20) -> list[Alert]:
        """Return recent alerts for the daily/weekly report."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_session() as session:
            stmt = (
                select(Alert)
                .where(Alert.alert_date >= cutoff)
                .order_by(desc(Alert.alert_date))
                .limit(limit)
            )
            return list(session.scalars(stmt).all())

    async def send_daily_report(self) -> None:
        """Build and post a daily summary embed to the report channel."""
        if self._client is None:
            return

        channel = self._client.get_channel(settings.discord_report_channel_id)
        if channel is None:
            logger.error("[AlertService] Report channel not found.")
            return

        recent_alerts = self.get_recent_alerts(days=1)
        top_scores = self._get_top_scores(limit=5)
        new_trades_count = self._count_new_trades(hours=24)

        embed = discord.Embed(
            title="📋 Capitol Gains — Daily Report",
            description=f"Summary for {datetime.utcnow().strftime('%A, %B %d %Y')}",
            colour=0x2F3136,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(
            name="New Trades (24h)",
            value=str(new_trades_count),
            inline=True,
        )
        embed.add_field(
            name="New Alerts (24h)",
            value=str(len(recent_alerts)),
            inline=True,
        )

        if top_scores:
            lines = [
                f"{i}. **${s.ticker}** — {s.score:.1f}/100"
                for i, s in enumerate(top_scores, 1)
            ]
            embed.add_field(
                name="🏆 Top Scores",
                value="\n".join(lines),
                inline=False,
            )

        if recent_alerts:
            lines = [
                f"• {a.alert_type} **${a.ticker}** — {a.alert_date.strftime('%H:%M UTC')}"
                for a in recent_alerts[:5]
            ]
            embed.add_field(
                name="🔔 Recent Alerts",
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(text="Capitol Gains Bot • Daily Digest")
        await channel.send(embed=embed)
        logger.info("[AlertService] Daily report posted.")

    @staticmethod
    def _get_top_scores(limit: int = 5) -> list[Score]:
        with get_session() as session:
            stmt = (
                select(Score)
                .distinct(Score.ticker)
                .order_by(Score.ticker, desc(Score.calculated_at))
                .subquery()
            )
            outer = (
                select(stmt)
                .order_by(desc(stmt.c.score))
                .limit(limit)
            )
            return list(session.execute(outer).all())

    @staticmethod
    def _count_new_trades(hours: int = 24) -> int:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with get_session() as session:
            from sqlalchemy import func as sqlfunc
            stmt = select(sqlfunc.count(Trade.id)).where(Trade.created_at >= cutoff)
            return session.scalar(stmt) or 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_alert_service_instance: AlertService | None = None


def get_alert_service(discord_client: discord.Client | None = None) -> AlertService:
    """
    Return the shared AlertService singleton.

    Pass discord_client on first call (bot startup).  Subsequent calls
    without a client return the already-initialised instance.
    """
    global _alert_service_instance
    if _alert_service_instance is None:
        _alert_service_instance = AlertService(discord_client)
    elif discord_client is not None and _alert_service_instance._client is None:
        _alert_service_instance._client = discord_client
    return _alert_service_instance
