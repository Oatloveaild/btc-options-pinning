import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import dateutil.parser
import httpx
from src.config import BKK_TZ, FOREX_FACTORY_URL
from src.db import get_db

logger = logging.getLogger("events")


def fetch_forexfactory_events() -> List[Dict[str, Any]]:
    """
    Fetch ForexFactory this week JSON and return USD events with High / Medium impact.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = httpx.get(FOREX_FACTORY_URL, headers=headers, timeout=12.0)
        response.raise_for_status()
        raw_events = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch ForexFactory events: {e}")
        return []

    saved_events = []
    now_bkk_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M")

    with get_db() as conn:
        for item in raw_events:
            country = item.get("country", "")
            impact = item.get("impact", "")
            if country == "USD" and impact in ("High", "Medium"):
                raw_date = item.get("date")
                if not raw_date:
                    continue
                try:
                    dt = dateutil.parser.parse(raw_date)
                    dt_bkk = dt.astimezone(BKK_TZ)
                    date_str = dt_bkk.strftime("%Y-%m-%d")
                    time_str = dt_bkk.strftime("%H:%M")
                except Exception:
                    continue

                event_title = item.get("title", "").strip()
                forecast = str(item.get("forecast", ""))
                previous = str(item.get("previous", ""))

                try:
                    conn.execute(
                        """
                        INSERT INTO events (date, time_bkk, event, impact, source, country, forecast, previous, added_bkk)
                        VALUES (?, ?, ?, ?, 'forexfactory', ?, ?, ?, ?)
                        ON CONFLICT(date, time_bkk, event) DO UPDATE SET
                            impact=excluded.impact,
                            forecast=excluded.forecast,
                            previous=excluded.previous
                        """,
                        (date_str, time_str, event_title, impact, country, forecast, previous, now_bkk_str),
                    )
                    saved_events.append({
                        "date": date_str,
                        "time_bkk": time_str,
                        "event": event_title,
                        "impact": impact,
                        "forecast": forecast,
                        "previous": previous,
                    })
                except Exception as e:
                    logger.warning(f"Error inserting event {event_title}: {e}")
        conn.commit()

    return saved_events


def add_custom_event(date_str: str, time_bkk_str: str, event_text: str, impact: str) -> bool:
    """Add manual custom event (e.g. crypto token unlock, SEC deadline)."""
    now_bkk_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M")
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO events (date, time_bkk, event, impact, source, country, forecast, previous, added_bkk)
                VALUES (?, ?, ?, ?, 'manual', 'CRYPTO', '', '', ?)
                ON CONFLICT(date, time_bkk, event) DO UPDATE SET
                    impact=excluded.impact,
                    added_bkk=excluded.added_bkk
                """,
                (date_str, time_bkk_str, event_text, impact, now_bkk_str),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to add custom event: {e}")
        return False


def delete_event(event_id: int) -> bool:
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to delete event {event_id}: {e}")
        return False


def get_events(limit: int = 200) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT id, date, time_bkk, event, impact, source, country, forecast, previous, added_bkk
            FROM events
            ORDER BY date DESC, time_bkk DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def check_high_impact_between(date_str: str, start_time: str = "06:00", end_time: str = "15:00") -> Tuple[int, str]:
    """
    Check if there is a High impact event on date_str between start_time and end_time.
    Returns (1 if yes else 0, summary_string)
    """
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT event, impact, time_bkk FROM events
            WHERE date = ? AND impact = 'High' AND time_bkk >= ? AND time_bkk <= ?
            ORDER BY time_bkk ASC
            """,
            (date_str, start_time, end_time),
        )
        rows = cursor.fetchall()
        if not rows:
            return 0, ""
        summaries = [f"{r['time_bkk']} {r['event']}" for r in rows]
        return 1, "; ".join(summaries)
