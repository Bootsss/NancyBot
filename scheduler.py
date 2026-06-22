"""
scheduler.py — Automated job scheduler for Capitol Gains.

Uses APScheduler (BackgroundScheduler) so all jobs run in daemon threads
alongside the Discord bot's async event loop without blocking it.

Job schedule
------------
Daily (configurable hour, UTC):
    1. collect_trades        — pull new congressional disclosures
    2. resolve_tickers       — map unresolved company names → tickers
    3. run_scoring           — score all active tickers
    4. generate_alerts       — evaluate thresholds, create Alert rows
    5. post_alerts           — push pending Alert embeds to Discord
    6. daily_report          — post digest embed to report channel

Weekly (Monday 08:00 UTC by default):
    1. update_market_data    — refresh all Company financial metrics
    2. weekly_report         — post sector trends + win rates

Monthly (1st of month, 09:00 UTC):
    1. run_backtests         — calculate 30/90/180-day returns on alerts
    2. monthly_report        — post full analytics summary

All jobs are wrapped in a try/except so one failure never kills the
scheduler process.  Job execution is logged with start time, duration,
and outcome.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import settings
from services.alert_service import get_alert_service
from services.analysis_engine import get_analysis_engine
from services.market_data import get_market_data_service
from services.scoring_engine import get_scoring_engine
from services.ticker_mapper import get_ticker_mapper
from services.trade_collector import run_all_collectors

if TYPE_CHECKING:
    import discord


# ---------------------------------------------------------------------------
# Job wrappers
# ---------------------------------------------------------------------------

def _timed_job(name: str):
    """Decorator: log start/end/duration and catch all exceptions."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            logger.info("[Scheduler] Starting job: {}", name)
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed = time.monotonic() - start
                logger.info(
                    "[Scheduler] Job '{}' completed in {:.1f}s.", name, elapsed
                )
                return result
            except Exception as exc:
                elapsed = time.monotonic() - start
                logger.exception(
                    "[Scheduler] Job '{}' FAILED after {:.1f}s: {}", name, elapsed, exc
                )
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Individual job functions
# ---------------------------------------------------------------------------

@_timed_job("collect_trades")
def job_collect_trades() -> None:
    """Pull new congressional trade disclosures from all registered collectors."""
    results = run_all_collectors()
    total = sum(results.values())
    logger.info("[Scheduler] Trade collection: {} total new records. Detail: {}", total, results)


@_timed_job("seed_companies")
def job_seed_companies() -> None:
    """
    Ensure every ticker in the trades table has a Company row.
    Inserts one row at a time to avoid bulk constraint errors.
    """
    from database import get_session
    from models import Company, Trade
    from sqlalchemy import select

    # Get all distinct tickers first
    with get_session() as session:
        traded = session.execute(
            select(Trade.ticker, Trade.company_name)
            .where(Trade.ticker.isnot(None))
            .distinct(Trade.ticker)
        ).all()

    created = 0
    for ticker, company_name in traded:
        with get_session() as session:
            if session.get(Company, ticker) is None:
                try:
                    session.add(Company(
                        ticker=ticker,
                        company_name=company_name or ticker,
                        ticker_verified=False,
                        manually_overridden=False,
                    ))
                    created += 1
                except Exception as exc:
                    logger.debug("[Scheduler] Skipping company {}: {}", ticker, exc)

    logger.info("[Scheduler] Company seeding: {} new Company rows created.", created)


@_timed_job("resolve_tickers")
def job_resolve_tickers() -> None:
    """Attempt to map any Trade rows that still have ticker=NULL."""
    mapper = get_ticker_mapper()
    resolved = mapper.enrich_unresolved_trades()
    logger.info("[Scheduler] Ticker resolution: {} trades resolved.", resolved)


@_timed_job("run_scoring")
def job_run_scoring() -> None:
    """Score all tickers that have trades in the last 90 days."""
    engine = get_scoring_engine()
    scores = engine.score_all(lookback_days=90)
    if scores:
        top = list(scores.items())[:5]
        logger.info("[Scheduler] Scoring complete. Top 5: {}", top)
    else:
        logger.info("[Scheduler] Scoring complete. No tickers scored.")


@_timed_job("generate_alerts")
def job_generate_alerts() -> None:
    """Evaluate thresholds and insert new Alert rows into the database."""
    svc = get_alert_service()
    alerts = svc.generate_alerts()
    logger.info("[Scheduler] Alert generation: {} new alerts created.", len(alerts))


def job_post_alerts(discord_client: "discord.Client") -> None:
    """Async bridge: schedule posting of pending alerts onto the event loop."""
    @_timed_job("post_alerts")
    def _inner():
        svc = get_alert_service(discord_client)
        loop = discord_client.loop
        if loop is None or not loop.is_running():
            logger.error("[Scheduler] Discord event loop is not running — cannot post alerts.")
            return
        future = asyncio.run_coroutine_threadsafe(
            svc.post_pending_alerts(), loop
        )
        try:
            posted = future.result(timeout=120)
            logger.info("[Scheduler] Alert posting: {} alerts sent to Discord.", posted)
        except TimeoutError:
            logger.error("[Scheduler] Alert posting timed out after 120s.")
        except Exception as exc:
            logger.error("[Scheduler] Alert posting failed: {}", exc)

    _inner()


def job_daily_report(discord_client: "discord.Client") -> None:
    """Async bridge: post daily digest embed to the report channel."""
    @_timed_job("daily_report")
    def _inner():
        svc = get_alert_service(discord_client)
        loop = discord_client.loop
        if loop is None or not loop.is_running():
            logger.error("[Scheduler] Discord event loop is not running — cannot send report.")
            return
        future = asyncio.run_coroutine_threadsafe(
            svc.send_daily_report(), loop
        )
        try:
            future.result(timeout=60)
        except TimeoutError:
            logger.error("[Scheduler] Daily report timed out after 60s.")
        except Exception as exc:
            logger.error("[Scheduler] Daily report failed: {}", exc)

    _inner()


@_timed_job("update_market_data")
def job_update_market_data() -> None:
    """Refresh financial metrics for all tracked companies."""
    svc = get_market_data_service()
    results = svc.update_all()
    success = sum(1 for v in results.values() if v)
    logger.info(
        "[Scheduler] Market data update: {}/{} tickers succeeded.",
        success, len(results),
    )


def job_weekly_report(discord_client: "discord.Client") -> None:
    """Post a weekly sector trends and win-rate summary to Discord."""
    @_timed_job("weekly_report")
    def _inner():
        loop = discord_client.loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            _send_weekly_report(discord_client), loop
        )
        try:
            future.result(timeout=120)
        except Exception as exc:
            logger.error("[Scheduler] Weekly report failed: {}", exc)

    _inner()


async def _send_weekly_report(discord_client: "discord.Client") -> None:
    """Build and send the weekly report embed."""
    import discord as _discord
    from services.scoring_engine import get_scoring_engine

    channel = discord_client.get_channel(settings.discord_report_channel_id)
    if channel is None:
        logger.error("[Scheduler] Report channel not found for weekly report.")
        return

    scoring = get_scoring_engine()

    try:
        top_congressional = scoring.get_watchlist("TOP_CONGRESSIONAL", limit=5)
        top_sector = scoring.get_watchlist("TOP_SECTOR", limit=5)
        most_bought = scoring.get_watchlist("MOST_BOUGHT", limit=5)
    except Exception as exc:
        logger.error("[Scheduler] Failed to build weekly watchlists: {}", exc)
        return

    embed = _discord.Embed(
        title="📅 Capitol Gains — Weekly Report",
        description=f"Week ending {datetime.utcnow().strftime('%B %d, %Y')}",
        colour=0x5865F2,
        timestamp=datetime.utcnow(),
    )

    if top_congressional:
        lines = [
            f"{e.rank}. **${e.ticker}** ({e.company_name}) — {e.score:.1f}/100"
            for e in top_congressional
        ]
        embed.add_field(
            name="🏆 Top Congressional Picks",
            value="\n".join(lines),
            inline=False,
        )

    if top_sector:
        lines = [
            f"{e.rank}. **{e.sector}** — avg score {e.score:.1f}"
            for e in top_sector
        ]
        embed.add_field(
            name="📊 Hottest Sectors",
            value="\n".join(lines),
            inline=False,
        )

    if most_bought:
        lines = [
            f"{e.rank}. **${e.ticker}** — {e.metadata.get('buy_count', 0)} buys "
            f"by {e.metadata.get('unique_buyers', 0)} politicians"
            for e in most_bought
        ]
        embed.add_field(
            name="📈 Most Bought (30 days)",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="Capitol Gains Bot • Weekly Digest")
    await channel.send(embed=embed)
    logger.info("[Scheduler] Weekly report posted.")


@_timed_job("run_backtests")
def job_run_backtests() -> None:
    """
    Calculate 30/90/180-day returns for all alerts old enough to measure.

    An alert is eligible when alert_date + N days <= today.
    """
    from database import get_session
    from models import Alert, Backtest
    from services.market_data import get_market_data_service
    from sqlalchemy import select

    market = get_market_data_service()
    now = datetime.utcnow()

    with get_session() as session:
        # Find alerts without a backtest row
        stmt = (
            select(Alert)
            .outerjoin(Backtest, Backtest.alert_id == Alert.id)
            .where(Backtest.id.is_(None))
            .order_by(Alert.alert_date.asc())
        )
        eligible = list(session.scalars(stmt).all())

    logger.info("[Scheduler] Backtesting {} eligible alerts.", len(eligible))
    computed = 0

    for alert in eligible:
        try:
            entry_price = market.get_price(alert.ticker)
            if entry_price is None:
                continue

            r30 = r90 = r180 = None
            sp30 = sp90 = sp180 = None

            days_elapsed = (now - alert.alert_date).days

            if days_elapsed >= 30:
                r30 = market.get_return(alert.ticker, alert.alert_date,
                                        alert.alert_date + __import__('datetime').timedelta(days=30))
                sp30 = market.get_spy_return(alert.alert_date,
                                             alert.alert_date + __import__('datetime').timedelta(days=30))

            if days_elapsed >= 90:
                r90 = market.get_return(alert.ticker, alert.alert_date,
                                        alert.alert_date + __import__('datetime').timedelta(days=90))
                sp90 = market.get_spy_return(alert.alert_date,
                                             alert.alert_date + __import__('datetime').timedelta(days=90))

            if days_elapsed >= 180:
                r180 = market.get_return(alert.ticker, alert.alert_date,
                                         alert.alert_date + __import__('datetime').timedelta(days=180))
                sp180 = market.get_spy_return(alert.alert_date,
                                              alert.alert_date + __import__('datetime').timedelta(days=180))

            # Only write a row if we have at least 30-day data
            if r30 is None and days_elapsed < 30:
                continue

            backtest = Backtest(
                alert_id=alert.id,
                ticker=alert.ticker,
                entry_price=entry_price,
                return_30d=r30,
                return_90d=r90,
                return_180d=r180,
                sp500_return_30d=sp30,
                sp500_return_90d=sp90,
                sp500_return_180d=sp180,
                beat_market_30d=(r30 > sp30) if r30 is not None and sp30 is not None else None,
                beat_market_90d=(r90 > sp90) if r90 is not None and sp90 is not None else None,
                beat_market_180d=(r180 > sp180) if r180 is not None and sp180 is not None else None,
                calculated_at=now,
            )

            with get_session() as session:
                session.add(backtest)

            computed += 1
            logger.debug(
                "[Scheduler] Backtest for alert {}: 30d={} 90d={} 180d={}",
                alert.id, r30, r90, r180,
            )

        except Exception as exc:
            logger.error(
                "[Scheduler] Backtest failed for alert {}: {}", alert.id, exc
            )

    logger.info("[Scheduler] Backtests complete: {} computed.", computed)


def job_monthly_report(discord_client: "discord.Client") -> None:
    """Post a monthly analytics summary to Discord."""
    @_timed_job("monthly_report")
    def _inner():
        loop = discord_client.loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            _send_monthly_report(discord_client), loop
        )
        try:
            future.result(timeout=180)
        except Exception as exc:
            logger.error("[Scheduler] Monthly report failed: {}", exc)

    _inner()


async def _send_monthly_report(discord_client: "discord.Client") -> None:
    """Build and send the monthly analytics embed."""
    import discord as _discord
    from database import get_session
    from models import Backtest
    from sqlalchemy import func, select

    channel = discord_client.get_channel(settings.discord_report_channel_id)
    if channel is None:
        return

    embed = _discord.Embed(
        title="📆 Capitol Gains — Monthly Analytics",
        description=f"Report for {datetime.utcnow().strftime('%B %Y')}",
        colour=0x57F287,
        timestamp=datetime.utcnow(),
    )

    # Backtest summary stats
    with get_session() as session:
        stmt = select(Backtest).where(Backtest.return_30d.isnot(None))
        backtests = list(session.scalars(stmt).all())

    if backtests:
        returns_30 = [b.return_30d for b in backtests if b.return_30d is not None]
        returns_90 = [b.return_90d for b in backtests if b.return_90d is not None]

        win_rate_30 = (
            sum(1 for r in returns_30 if r > 0) / len(returns_30) * 100
            if returns_30 else 0
        )
        avg_return_30 = sum(returns_30) / len(returns_30) if returns_30 else 0

        beat_30 = [b for b in backtests if b.beat_market_30d is True]
        beat_rate_30 = len(beat_30) / len(backtests) * 100 if backtests else 0

        embed.add_field(
            name="📊 30-Day Backtest Results",
            value=(
                f"Signals tracked: **{len(backtests)}**\n"
                f"Win rate (>0%): **{win_rate_30:.1f}%**\n"
                f"Avg return: **{avg_return_30:+.2f}%**\n"
                f"Beat S&P 500: **{beat_rate_30:.1f}%**"
            ),
            inline=False,
        )

        if returns_90:
            avg_return_90 = sum(returns_90) / len(returns_90)
            win_rate_90 = sum(1 for r in returns_90 if r > 0) / len(returns_90) * 100
            embed.add_field(
                name="📊 90-Day Backtest Results",
                value=(
                    f"Signals tracked: **{len(returns_90)}**\n"
                    f"Win rate (>0%): **{win_rate_90:.1f}%**\n"
                    f"Avg return: **{avg_return_90:+.2f}%**"
                ),
                inline=False,
            )
    else:
        embed.add_field(
            name="📊 Backtest Results",
            value="Not enough historical data yet (need 30+ days of alerts).",
            inline=False,
        )

    embed.set_footer(text="Capitol Gains Bot • Monthly Analytics")
    await channel.send(embed=embed)
    logger.info("[Scheduler] Monthly report posted.")


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

class CapitolGainsScheduler:
    """
    Wraps APScheduler and registers all Capitol Gains jobs.

    Usage::

        scheduler = CapitolGainsScheduler(discord_client)
        scheduler.start()
        # ... bot runs ...
        scheduler.shutdown()
    """

    def __init__(self, discord_client: "discord.Client") -> None:
        self._client = discord_client
        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,       # merge missed runs into one
                "max_instances": 1,     # never run the same job twice concurrently
                "misfire_grace_time": 3600,  # allow up to 1h late start
            },
            timezone="UTC",
        )

    def start(self) -> None:
        """Register all jobs and start the scheduler."""
        self._register_daily_jobs()
        self._register_weekly_jobs()
        self._register_monthly_jobs()
        self._scheduler.start()
        logger.info(
            "[Scheduler] Started. Daily jobs at {:02d}:00 UTC, "
            "weekly on day {}, monthly on 1st.",
            settings.daily_job_hour,
            settings.weekly_job_day,
        )

    def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("[Scheduler] Shutdown complete.")

    def trigger_daily_now(self) -> None:
        """Manually trigger the full daily pipeline (useful for testing)."""
        logger.info("[Scheduler] Manually triggering daily pipeline.")
        job_collect_trades()
        job_seed_companies()
        job_resolve_tickers()
        job_run_scoring()
        job_generate_alerts()
        job_post_alerts(self._client)
        job_daily_report(self._client)

    def get_job_status(self) -> list[dict]:
        """Return a list of job status dicts for the !stats command."""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs

    # ------------------------------------------------------------------ #
    # Job registration
    # ------------------------------------------------------------------ #

    def _register_daily_jobs(self) -> None:
        hour = settings.daily_job_hour
        client = self._client

        # Stagger jobs by 5 minutes each to avoid overlapping DB writes
        self._scheduler.add_job(
            job_collect_trades,
            CronTrigger(hour=hour, minute=0),
            id="collect_trades",
            name="Collect Congressional Trades",
        )
        self._scheduler.add_job(
            job_seed_companies,
            CronTrigger(hour=hour, minute=3),
            id="seed_companies",
            name="Seed Company Rows",
        )
        self._scheduler.add_job(
            job_resolve_tickers,
            CronTrigger(hour=hour, minute=5),
            id="resolve_tickers",
            name="Resolve Ticker Symbols",
        )
        self._scheduler.add_job(
            job_run_scoring,
            CronTrigger(hour=hour, minute=10),
            id="run_scoring",
            name="Score All Tickers",
        )
        self._scheduler.add_job(
            job_generate_alerts,
            CronTrigger(hour=hour, minute=20),
            id="generate_alerts",
            name="Generate Alerts",
        )
        self._scheduler.add_job(
            lambda: job_post_alerts(client),
            CronTrigger(hour=hour, minute=25),
            id="post_alerts",
            name="Post Alerts to Discord",
        )
        self._scheduler.add_job(
            lambda: job_daily_report(client),
            CronTrigger(hour=hour, minute=30),
            id="daily_report",
            name="Daily Report",
        )

    def _register_weekly_jobs(self) -> None:
        day = settings.weekly_job_day  # 0=Monday
        client = self._client

        self._scheduler.add_job(
            job_update_market_data,
            CronTrigger(day_of_week=day, hour=8, minute=0),
            id="update_market_data",
            name="Update Market Data",
        )
        self._scheduler.add_job(
            lambda: job_weekly_report(client),
            CronTrigger(day_of_week=day, hour=8, minute=45),
            id="weekly_report",
            name="Weekly Report",
        )

    def _register_monthly_jobs(self) -> None:
        client = self._client

        self._scheduler.add_job(
            job_run_backtests,
            CronTrigger(day=1, hour=9, minute=0),
            id="run_backtests",
            name="Run Backtests",
        )
        self._scheduler.add_job(
            lambda: job_monthly_report(client),
            CronTrigger(day=1, hour=9, minute=30),
            id="monthly_report",
            name="Monthly Report",
        )
