"""
Paper Trading Engine for BTC Systematic Option Selling.
Manages 4 Portfolios ($10,000 initial capital each, 50% BTC + 50% USD):
- Port 1: Daily C1 / P1 (Covered Strangle)
- Port 2: Daily C2 / P2 (Covered Strangle)
- Port 3: Weekly C1 / P1 (Covered Strangle)
- Port 4: Weekly C2 / P2 (Covered Strangle)
"""
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from src.config import BKK_TZ, DB_PATH
from src.db import get_db
from src.deribit_client import DeribitClient
from src.options_math import bs_gamma, bs_price, classify_expiry

logger = logging.getLogger("paper_trade")

INITIAL_CAPITAL = 10000.0

PORTFOLIO_CONFIGS = [
    {
        "id": 1,
        "name": "พอร์ต 1: Daily C1/P1",
        "cycle_type": "daily",
        "target_strikes": "c1_p1",
        "desc": "ขาย Call Wall (+GEX) และ Put Wall (-GEX) ประจำวัน (ยึดยอด Gamma ของวันนั้น)",
    },
    {
        "id": 2,
        "name": "พอร์ต 2: Daily C2/P2",
        "cycle_type": "daily",
        "target_strikes": "c2_p2",
        "desc": "ขาย Call Wall #2 และ Put Wall #2 ประจำวัน OTM กว้างขึ้น (ยึดยอด Gamma ของวันนั้น)",
    },
    {
        "id": 3,
        "name": "พอร์ต 3: Weekly C1/P1",
        "cycle_type": "weekly",
        "target_strikes": "c1_p1",
        "desc": "ขาย Call Wall (+GEX) และ Put Wall (-GEX) ประจำสัปดาห์ (ยึดยอด Gamma ของสัปดาห์นั้น)",
    },
    {
        "id": 4,
        "name": "พอร์ต 4: Weekly C2/P2",
        "cycle_type": "weekly",
        "target_strikes": "c2_p2",
        "desc": "ขาย Call Wall #2 และ Put Wall #2 ประจำสัปดาห์ OTM กว้างขึ้น (ยึดยอด Gamma ของสัปดาห์นั้น)",
    },
]


def init_paper_trade_tables():
    """Initializes tables for paper trade portfolio tracking."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_portfolios (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                cycle_type TEXT NOT NULL,
                target_strikes TEXT NOT NULL,
                initial_capital_usd REAL NOT NULL DEFAULT 10000.0,
                current_cash_usd REAL NOT NULL DEFAULT 5000.0,
                current_btc REAL NOT NULL DEFAULT 0.0,
                total_equity_usd REAL NOT NULL DEFAULT 10000.0,
                total_pnl_usd REAL NOT NULL DEFAULT 0.0,
                total_pnl_pct REAL NOT NULL DEFAULT 0.0,
                total_trades INTEGER NOT NULL DEFAULT 0,
                winning_trades INTEGER NOT NULL DEFAULT 0,
                losing_trades INTEGER NOT NULL DEFAULT 0,
                win_rate_pct REAL NOT NULL DEFAULT 0.0,
                total_premium_collected_usd REAL NOT NULL DEFAULT 0.0,
                max_drawdown_pct REAL NOT NULL DEFAULT 0.0,
                sharpe_ratio REAL NOT NULL DEFAULT 0.0,
                updated_at_bkk TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL,
                trade_ref TEXT UNIQUE NOT NULL,
                cycle_type TEXT NOT NULL,
                status TEXT NOT NULL, -- ACTIVE, SETTLED_WIN, EXERCISED_CALL, EXERCISED_PUT
                opened_at_bkk TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                expiry_time_bkk TEXT NOT NULL,
                spot_at_open REAL NOT NULL,
                settlement_price REAL,
                call_strike REAL NOT NULL,
                call_size_btc REAL NOT NULL,
                call_premium_btc REAL NOT NULL,
                call_premium_usd REAL NOT NULL,
                call_iv REAL,
                call_delta REAL,
                put_strike REAL NOT NULL,
                put_size_btc REAL NOT NULL,
                put_premium_btc REAL NOT NULL,
                put_premium_usd REAL NOT NULL,
                put_iv REAL,
                put_delta REAL,
                total_premium_collected_usd REAL NOT NULL,
                fees_usd REAL NOT NULL DEFAULT 0.0,
                net_pnl_usd REAL,
                net_pnl_pct REAL,
                equity_before_usd REAL NOT NULL,
                equity_after_usd REAL,
                settled_at_bkk TEXT,
                outcome_note TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL,
                record_date TEXT NOT NULL,
                equity_usd REAL NOT NULL,
                btc_price REAL NOT NULL,
                btc_amount REAL NOT NULL,
                cash_usd REAL NOT NULL,
                cum_pnl_usd REAL NOT NULL,
                cum_pnl_pct REAL NOT NULL,
                daily_pnl_usd REAL NOT NULL DEFAULT 0.0,
                daily_pnl_pct REAL NOT NULL DEFAULT 0.0,
                UNIQUE(portfolio_id, record_date)
            )
            """
        )

        # Seed initial portfolios if not exists
        for cfg in PORTFOLIO_CONFIGS:
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_portfolios (
                    id, name, cycle_type, target_strikes, initial_capital_usd,
                    current_cash_usd, current_btc, total_equity_usd, total_pnl_usd, total_pnl_pct,
                    total_trades, winning_trades, losing_trades, win_rate_pct,
                    total_premium_collected_usd, max_drawdown_pct, sharpe_ratio, updated_at_bkk
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cfg["id"], cfg["name"], cfg["cycle_type"], cfg["target_strikes"],
                    INITIAL_CAPITAL, 5000.0, 0.0, INITIAL_CAPITAL, 0.0, 0.0,
                    0, 0, 0, 0.0, 0.0, 0.0, 0.0, datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S")
                )
            )
        conn.commit()


def calculate_portfolio_metrics(portfolio_id: int):
    """Calculates win rate, total pnl, max drawdown, and sharpe ratio from equity history."""
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Trades stats
        cur.execute(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status = 'SETTLED_WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN status != 'SETTLED_WIN' AND status != 'ACTIVE' THEN 1 ELSE 0 END) as losses,
                   SUM(total_premium_collected_usd) as total_prem,
                   SUM(net_pnl_usd) as total_net_pnl
            FROM paper_trades
            WHERE portfolio_id = ? AND status != 'ACTIVE'
            """,
            (portfolio_id,)
        )
        t_row = cur.fetchone()
        total_t = t_row["total"] or 0
        wins = t_row["wins"] or 0
        losses = t_row["losses"] or 0
        win_rate = round(wins / total_t * 100.0, 1) if total_t > 0 else 0.0
        total_prem = round(t_row["total_prem"] or 0.0, 2)

        # Equity history for Drawdown and Sharpe
        cur.execute(
            """
            SELECT equity_usd, btc_amount, cash_usd, daily_pnl_pct
            FROM paper_equity_history
            WHERE portfolio_id = ?
            ORDER BY record_date ASC
            """,
            (portfolio_id,)
        )
        eq_rows = cur.fetchall()

        max_dd = 0.0
        peak = INITIAL_CAPITAL
        daily_returns = []
        last_equity = INITIAL_CAPITAL
        last_btc = 5000.0 / 81000.0
        last_cash = 5000.0

        for r in eq_rows:
            eq = r["equity_usd"]
            last_equity = eq
            if r["btc_amount"] is not None:
                last_btc = r["btc_amount"]
            if r["cash_usd"] is not None:
                last_cash = r["cash_usd"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            if r["daily_pnl_pct"] is not None:
                daily_returns.append(r["daily_pnl_pct"])

        # Sharpe ratio (assuming risk-free rate 4% annual = ~0.011% daily)
        sharpe = 0.0
        if len(daily_returns) > 3:
            avg_r = sum(daily_returns) / len(daily_returns)
            variance = sum((x - avg_r) ** 2 for x in daily_returns) / (len(daily_returns) - 1)
            std_r = math.sqrt(variance) if variance > 0 else 0.0
            if std_r > 1e-6:
                sharpe = round((avg_r - 0.011) / std_r * math.sqrt(365), 2)

        pnl_usd = round(last_equity - INITIAL_CAPITAL, 2)
        pnl_pct = round(pnl_usd / INITIAL_CAPITAL * 100.0, 2)

        conn.execute(
            """
            UPDATE paper_portfolios
            SET total_equity_usd = ?,
                total_pnl_usd = ?,
                total_pnl_pct = ?,
                current_cash_usd = ?,
                current_btc = ?,
                total_trades = ?,
                winning_trades = ?,
                losing_trades = ?,
                win_rate_pct = ?,
                total_premium_collected_usd = ?,
                max_drawdown_pct = ?,
                sharpe_ratio = ?,
                updated_at_bkk = ?
            WHERE id = ?
            """,
            (
                round(last_equity, 2), pnl_usd, pnl_pct,
                round(last_cash, 2), round(last_btc, 4),
                total_t, wins, losses,
                win_rate, total_prem, round(max_dd, 2), sharpe,
                datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                portfolio_id
            )
        )
        conn.commit()


def backfill_paper_trades_from_history():
    """
    Backfills paper trade history using existing records in expiry_results
    so that user immediately has authentic historical curves.
    """
    init_paper_trade_tables()

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM paper_trades")
        if cur.fetchone()[0] > 0:
            return  # Already backfilled

        # Get historical expiry_results sorted ascending
        cur.execute(
            """
            SELECT * FROM expiry_results
            ORDER BY expiry_date ASC
            """
        )
        exp_rows = cur.fetchall()

    if not exp_rows:
        logger.info("No expiry_results to backfill.")
        return

    logger.info(f"Backfilling paper trades across {len(exp_rows)} historical expiries...")

    # State per portfolio:
    port_state = {
        1: {"equity": INITIAL_CAPITAL, "cash": 5000.0, "btc": 0.0},
        2: {"equity": INITIAL_CAPITAL, "cash": 5000.0, "btc": 0.0},
        3: {"equity": INITIAL_CAPITAL, "cash": 5000.0, "btc": 0.0},
        4: {"equity": INITIAL_CAPITAL, "cash": 5000.0, "btc": 0.0},
    }

    # Initialize BTC balances based on first spot
    first_spot = exp_rows[0]["spot_at_snapshot"] or 80000.0
    for pid in (1, 2, 3, 4):
        port_state[pid]["btc"] = 5000.0 / first_spot

    with get_db() as conn:
        for r in exp_rows:
            exp_date = r["expiry_date"]
            spot = r["spot_at_snapshot"] or 80000.0
            settle = r["settlement_price"] or spot
            iv = (r["snap_atm_iv"] or r["snap_dvol"] or 50.0) / 100.0
            exp_type = r["expiry_type"] or "daily"

            # 1 Day T
            t_daily = 1.0 / 365.0
            # Weekly T
            t_weekly = 7.0 / 365.0

            # Daily trades for Port 1 (C1/P1) and Port 2 (C2/P2)
            c1 = r["call1_strike"] or (spot * 1.01)
            p1 = r["put1_strike"] or (spot * 0.99)
            c2 = r["call2_strike"] or (spot * 1.03)
            p2 = r["put2_strike"] or (spot * 0.97)

            # --- Port 1: Daily C1/P1 ---
            _simulate_trade_cycle(
                conn, portfolio_id=1, exp_date=exp_date, exp_type="daily",
                spot=spot, settle=settle, iv=iv, t_years=t_daily,
                call_k=c1, put_k=p1, state=port_state[1]
            )

            # --- Port 2: Daily C2/P2 ---
            _simulate_trade_cycle(
                conn, portfolio_id=2, exp_date=exp_date, exp_type="daily",
                spot=spot, settle=settle, iv=iv, t_years=t_daily,
                call_k=c2, put_k=p2, state=port_state[2]
            )

            # --- Port 3 & 4: Weekly C1/P1 and C2/P2 (Only on Friday weekly expiries) ---
            if exp_type in ("weekly", "monthly", "quarterly") or datetime.strptime(exp_date, "%Y-%m-%d").weekday() == 4:
                _simulate_trade_cycle(
                    conn, portfolio_id=3, exp_date=exp_date, exp_type="weekly",
                    spot=spot, settle=settle, iv=iv, t_years=t_weekly,
                    call_k=c1, put_k=p1, state=port_state[3]
                )
                _simulate_trade_cycle(
                    conn, portfolio_id=4, exp_date=exp_date, exp_type="weekly",
                    spot=spot, settle=settle, iv=iv, t_years=t_weekly,
                    call_k=c2, put_k=p2, state=port_state[4]
                )

        conn.commit()

    with get_db() as conn:
        for pid in (1, 2, 3, 4):
            conn.execute(
                "UPDATE paper_portfolios SET current_cash_usd = ?, current_btc = ?, total_equity_usd = ? WHERE id = ?",
                (round(port_state[pid]["cash"], 2), round(port_state[pid]["btc"], 4), round(port_state[pid]["equity"], 2), pid)
            )
        conn.commit()

    for pid in (1, 2, 3, 4):
        calculate_portfolio_metrics(pid)

    logger.info("Backfill completed successfully.")


def _simulate_trade_cycle(
    conn: sqlite3.Connection,
    portfolio_id: int,
    exp_date: str,
    exp_type: str,
    spot: float,
    settle: float,
    iv: float,
    t_years: float,
    call_k: float,
    put_k: float,
    state: Dict[str, float],
):
    """Simulates one round of covered call + cash-secured put settlement."""
    trade_ref = f"PT{portfolio_id}-{exp_date}-{exp_type[:3].upper()}"

    # Sizing
    btc_held = state["btc"]
    cash_held = state["cash"]

    call_size = btc_held
    put_size = cash_held / put_k if put_k > 0 else (cash_held / spot)

    # Black-Scholes premium
    c_prem_usd = bs_price(spot, call_k, t_years, iv, is_call=True)
    p_prem_usd = bs_price(spot, put_k, t_years, iv, is_call=False)

    tot_call_prem = c_prem_usd * call_size
    tot_put_prem = p_prem_usd * put_size
    tot_prem = tot_call_prem + tot_put_prem

    # Deribit fee ~0.03% of underlying notional
    fee_usd = (call_size + put_size) * spot * 0.0003

    eq_before = cash_held + (btc_held * spot)

    # Settlement outcome
    status = "SETTLED_WIN"
    note = "ราคาจบในกรอบ เก็บพรีเมียมเต็ม 100%"

    if settle > call_k:
        status = "EXERCISED_CALL"
        note = f"ราคาปิด (${settle:,.1f}) สูงกว่า Call Strike (${call_k:,.1f}) ขาย BTC ได้ Cap กำไร"
        # BTC sold at call_k
        cash_received = btc_held * call_k
        cash_held = cash_held + cash_received + tot_prem - fee_usd
        btc_held = 0.0
        # Rebalance 50/50 at settle price
        tot_eq = cash_held
        target_btc_usd = tot_eq * 0.5
        btc_held = target_btc_usd / settle
        cash_held = tot_eq * 0.5
    elif settle < put_k:
        status = "EXERCISED_PUT"
        note = f"ราคาปิด (${settle:,.1f}) ต่ำกว่า Put Strike (${put_k:,.1f}) บังคับซื้อ BTC ที่ Strike"
        # Cash buys BTC at put_k
        cost = put_size * put_k
        cash_held = max(0.0, cash_held - cost + tot_prem - fee_usd)
        btc_held = btc_held + put_size
        # Total equity at settle price
        tot_eq = cash_held + (btc_held * settle)
        target_btc_usd = tot_eq * 0.5
        btc_held = target_btc_usd / settle
        cash_held = tot_eq * 0.5
    else:
        # Full win in range
        cash_held = cash_held + tot_prem - fee_usd
        tot_eq = cash_held + (btc_held * settle)
        target_btc_usd = tot_eq * 0.5
        btc_held = target_btc_usd / settle
        cash_held = tot_eq * 0.5

    eq_after = tot_eq
    net_pnl = eq_after - eq_before
    net_pct = (net_pnl / eq_before) * 100.0 if eq_before > 0 else 0.0

    state["equity"] = eq_after
    state["cash"] = cash_held
    state["btc"] = btc_held

    conn.execute(
        """
        INSERT OR REPLACE INTO paper_trades (
            portfolio_id, trade_ref, cycle_type, status, opened_at_bkk, expiry_date, expiry_time_bkk,
            spot_at_open, settlement_price, call_strike, call_size_btc, call_premium_btc, call_premium_usd,
            put_strike, put_size_btc, put_premium_btc, put_premium_usd,
            total_premium_collected_usd, fees_usd, net_pnl_usd, net_pnl_pct,
            equity_before_usd, equity_after_usd, settled_at_bkk, outcome_note
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        """,
        (
            portfolio_id, trade_ref, exp_type, status, f"{exp_date} 06:00:00", exp_date, f"{exp_date} 15:00:00",
            spot, settle, call_k, round(call_size, 4), round(c_prem_usd / spot, 5), round(tot_call_prem, 2),
            put_k, round(put_size, 4), round(p_prem_usd / spot, 5), round(tot_put_prem, 2),
            round(tot_prem, 2), round(fee_usd, 2), round(net_pnl, 2), round(net_pct, 2),
            round(eq_before, 2), round(eq_after, 2), f"{exp_date} 15:30:00", note
        )
    )

    cum_pnl_usd = eq_after - INITIAL_CAPITAL
    cum_pnl_pct = (cum_pnl_usd / INITIAL_CAPITAL) * 100.0

    conn.execute(
        """
        INSERT OR REPLACE INTO paper_equity_history (
            portfolio_id, record_date, equity_usd, btc_price, btc_amount, cash_usd,
            cum_pnl_usd, cum_pnl_pct, daily_pnl_usd, daily_pnl_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            portfolio_id, exp_date, round(eq_after, 2), settle, round(btc_held, 4), round(cash_held, 2),
            round(cum_pnl_usd, 2), round(cum_pnl_pct, 2), round(net_pnl, 2), round(net_pct, 2)
        )
    )


def open_active_paper_trades():
    """
    Checks if active trades exist for upcoming Daily and Weekly expiries.
    If not, fetches live Deribit orderbook to open real active positions.
    """
    init_paper_trade_tables()

    client = DeribitClient()
    now_bkk = datetime.now(BKK_TZ)

    # 1. Fetch live book summary
    try:
        book_items = client.get_book_summary_by_currency("BTC", kind="option")
        insts = client.get_instruments("BTC", kind="option", expired=False)
        spot = float(book_items[0].get("underlying_price", 81000.0))
    except Exception as e:
        logger.error(f"Failed to fetch live Deribit data for active paper trades: {e}")
        return

    inst_map = {x["instrument_name"]: x for x in insts}
    now_ms = int(now_bkk.timestamp() * 1000)

    # 2. Collect instruments by expiry, calculate GEX for each contract, and aggregate market total OI
    from collections import defaultdict
    by_exp = defaultdict(list)
    agg_oi = defaultdict(float)

    for it in book_items:
        iname = it["instrument_name"]
        meta = inst_map.get(iname, {})
        exp_ms = meta.get("expiration_timestamp", 0)
        if exp_ms <= now_ms:
            continue
        k = float(meta.get("strike", 0.0))
        otype = meta.get("option_type", "")
        oi = float(it.get("open_interest", 0.0) or 0.0)
        mark_iv = float(it.get("mark_iv", 0.0) or 0.0)

        agg_oi[k] += oi

        t_years = max((exp_ms - now_ms) / (1000.0 * 86400.0 * 365.0), 1e-5)
        gamma = bs_gamma(spot, k, t_years, mark_iv / 100.0) if mark_iv > 0 else 0.0
        gex_usd = gamma * spot * (spot * 0.01) * oi

        by_exp[exp_ms].append({
            "name": iname,
            "strike": k,
            "type": otype,
            "mark_btc": float(it.get("mark_price", 0.0) or 0.0),
            "mark_usd": float(it.get("mark_price", 0.0) or 0.0) * spot,
            "bid_btc": float(it.get("bid_price", 0.0) or 0.0),
            "mark_iv": mark_iv,
            "call_gex": gex_usd if otype == "call" else 0.0,
            "put_gex": -gex_usd if otype == "put" else 0.0,
            "oi": oi,
        })

    sorted_exp_ms = sorted(by_exp.keys())
    if not sorted_exp_ms:
        return

    # Target Daily (closest future expiry)
    daily_ms = sorted_exp_ms[0]
    daily_dt = datetime.fromtimestamp(daily_ms / 1000.0, tz=BKK_TZ)
    daily_date_str = daily_dt.strftime("%Y-%m-%d")

    # Target Weekly (next Friday expiry)
    weekly_ms = None
    for ms in sorted_exp_ms:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=BKK_TZ)
        if dt.weekday() == 4 and dt.date() > now_bkk.date():
            weekly_ms = ms
            break
    if not weekly_ms and len(sorted_exp_ms) > 1:
        weekly_ms = sorted_exp_ms[1]
    weekly_dt = datetime.fromtimestamp(weekly_ms / 1000.0, tz=BKK_TZ)
    weekly_date_str = weekly_dt.strftime("%Y-%m-%d")

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check existing active trades
        cur.execute("SELECT portfolio_id, trade_ref FROM paper_trades WHERE status = 'ACTIVE'")
        active_map = {r["portfolio_id"]: r["trade_ref"] for r in cur.fetchall()}

        # -------------------------------------------------------------
        # Open for Port 1 & 2: DAILY CYCLE (ใช้ GEX ของวันนั้นเดี่ยวๆ)
        # -------------------------------------------------------------
        daily_items = by_exp[daily_ms]
        d_otm_calls = [x for x in daily_items if x["type"] == "call" and x["strike"] >= spot]
        d_otm_puts = [x for x in daily_items if x["type"] == "put" and x["strike"] <= spot]

        # Call Wall: Positive Gamma (+GEX สูงสุดของวันนั้น)
        d_calls_by_gex = sorted(d_otm_calls, key=lambda x: x["call_gex"], reverse=True)
        c1 = d_calls_by_gex[0] if d_calls_by_gex else None
        c2_candidates = [x for x in d_calls_by_gex if x["strike"] > (c1["strike"] if c1 else 0)]
        c2 = c2_candidates[0] if c2_candidates else (d_calls_by_gex[1] if len(d_calls_by_gex) > 1 else c1)

        # Put Wall: Negative Gamma (-GEX magnitude สูงสุดของวันนั้น, most negative first)
        d_puts_by_gex = sorted(d_otm_puts, key=lambda x: x["put_gex"])
        p1 = d_puts_by_gex[0] if d_puts_by_gex else None
        p2_candidates = [x for x in d_puts_by_gex if x["strike"] < (p1["strike"] if p1 else 0)]
        p2 = p2_candidates[0] if p2_candidates else (d_puts_by_gex[1] if len(d_puts_by_gex) > 1 else p1)

        if c1 and p1:
            logger.info(
                f"[Daily {daily_date_str}] GEX วันนั้น -> "
                f"C1: ${c1['strike']:,.0f} (+${c1['call_gex']/1e6:,.2f}M) | P1: ${p1['strike']:,.0f} (-${abs(p1['put_gex'])/1e6:,.2f}M) | "
                f"C2: ${c2['strike']:,.0f} | P2: ${p2['strike']:,.0f}"
            )
            if 1 not in active_map:
                _create_active_trade(conn, 1, "daily", daily_date_str, daily_dt, spot, c1, p1)
            if 2 not in active_map and c2 and p2:
                _create_active_trade(conn, 2, "daily", daily_date_str, daily_dt, spot, c2, p2)

        # -------------------------------------------------------------
        # Open for Port 3 & 4: WEEKLY CYCLE (ใช้ GEX ของสัปดาห์นั้นเดี่ยวๆ)
        # -------------------------------------------------------------
        if weekly_ms and weekly_ms in by_exp:
            weekly_items = by_exp[weekly_ms]
            w_otm_calls = [x for x in weekly_items if x["type"] == "call" and x["strike"] >= spot]
            w_otm_puts = [x for x in weekly_items if x["type"] == "put" and x["strike"] <= spot]

            # Call Wall: Positive Gamma (+GEX สูงสุดของสัปดาห์นั้น)
            w_calls_by_gex = sorted(w_otm_calls, key=lambda x: x["call_gex"], reverse=True)
            wc1 = w_calls_by_gex[0] if w_calls_by_gex else None
            wc2_candidates = [x for x in w_calls_by_gex if x["strike"] > (wc1["strike"] if wc1 else 0)]
            wc2 = wc2_candidates[0] if wc2_candidates else (w_calls_by_gex[1] if len(w_calls_by_gex) > 1 else wc1)

            # Put Wall: Negative Gamma (-GEX magnitude สูงสุดของสัปดาห์นั้น, most negative first)
            w_puts_by_gex = sorted(w_otm_puts, key=lambda x: x["put_gex"])
            wp1 = w_puts_by_gex[0] if w_puts_by_gex else None
            wp2_candidates = [x for x in w_puts_by_gex if x["strike"] < (wp1["strike"] if wp1 else 0)]
            wp2 = wp2_candidates[0] if wp2_candidates else (w_puts_by_gex[1] if len(w_puts_by_gex) > 1 else wp1)

            if wc1 and wp1:
                logger.info(
                    f"[Weekly {weekly_date_str}] GEX สัปดาห์นั้น -> "
                    f"C1: ${wc1['strike']:,.0f} (+${wc1['call_gex']/1e6:,.2f}M) | P1: ${wp1['strike']:,.0f} (-${abs(wp1['put_gex'])/1e6:,.2f}M) | "
                    f"C2: ${wc2['strike']:,.0f} | P2: ${wp2['strike']:,.0f}"
                )
                if 3 not in active_map:
                    _create_active_trade(conn, 3, "weekly", weekly_date_str, weekly_dt, spot, wc1, wp1)
                if 4 not in active_map and wc2 and wp2:
                    _create_active_trade(conn, 4, "weekly", weekly_date_str, weekly_dt, spot, wc2, wp2)

        conn.commit()


def _create_active_trade(
    conn: sqlite3.Connection,
    portfolio_id: int,
    cycle_type: str,
    exp_date: str,
    exp_dt: datetime,
    spot: float,
    call_item: Dict[str, Any],
    put_item: Dict[str, Any],
):
    """Creates a new active paper trade entry."""
    cur = conn.cursor()
    cur.execute("SELECT current_cash_usd, current_btc, total_equity_usd FROM paper_portfolios WHERE id = ?", (portfolio_id,))
    p_row = cur.fetchone()

    equity = p_row["total_equity_usd"] if p_row and p_row["total_equity_usd"] > 0 else INITIAL_CAPITAL
    cash = p_row["current_cash_usd"] if p_row and p_row["current_cash_usd"] > 0 else (equity * 0.5)
    btc = p_row["current_btc"] if p_row and p_row["current_btc"] > 0 else ((equity * 0.5) / spot)

    c_k = call_item["strike"]
    p_k = put_item["strike"]

    # Sell Call on 100% of BTC held (Covered Call)
    call_size = round(btc, 4)

    # Sell Put on 100% of Cash held (Cash-Secured Put)
    put_size = round(cash / p_k, 4) if p_k > 0 else round(cash / spot, 4)

    # Realistic premium: use bid if available > 0, else mark
    c_prem_btc = call_item.get("bid_btc") or call_item["mark_btc"]
    p_prem_btc = put_item.get("bid_btc") or put_item["mark_btc"]

    c_prem_usd = c_prem_btc * spot * call_size
    p_prem_usd = p_prem_btc * spot * put_size
    tot_prem = c_prem_usd + p_prem_usd
    fee_usd = (call_size + put_size) * spot * 0.0003

    trade_ref = f"PT{portfolio_id}-{exp_date}-{cycle_type[:3].upper()}-LIVE"
    now_str = datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        INSERT OR REPLACE INTO paper_trades (
            portfolio_id, trade_ref, cycle_type, status, opened_at_bkk, expiry_date, expiry_time_bkk,
            spot_at_open, settlement_price, call_strike, call_size_btc, call_premium_btc, call_premium_usd,
            put_strike, put_size_btc, put_premium_btc, put_premium_usd,
            total_premium_collected_usd, fees_usd, net_pnl_usd, net_pnl_pct,
            equity_before_usd, equity_after_usd, settled_at_bkk, outcome_note
        ) VALUES (
            ?, ?, ?, 'ACTIVE', ?, ?, ?,
            ?, NULL, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, NULL, NULL,
            ?, NULL, NULL, 'กำลังรันสด รอ Expiry 15:00 น.'
        )
        """,
        (
            portfolio_id, trade_ref, cycle_type, now_str, exp_date, exp_dt.strftime("%Y-%m-%d %H:%M:%S"),
            spot, c_k, round(call_size, 4), round(c_prem_btc, 5), round(c_prem_usd, 2),
            p_k, round(put_size, 4), round(p_prem_btc, 5), round(p_prem_usd, 2),
            round(tot_prem, 2), round(fee_usd, 2), round(equity, 2)
        )
    )


def get_all_portfolios_data(use_cache: bool = True) -> Dict[str, Any]:
    """Retrieves full summary, equity curves, active positions, and trade history."""
    if use_cache:
        from src.cache import memory_cache
        cached_val = memory_cache.get("paper_trade:all_data")
        if cached_val is not None:
            return cached_val

    init_paper_trade_tables()
    backfill_paper_trades_from_history()
    open_active_paper_trades()

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Portfolios
        cur.execute("SELECT * FROM paper_portfolios ORDER BY id ASC")
        ports = [dict(r) for r in cur.fetchall()]

        # Compute live spot and ensure realistic 50/50 BTC and Cash allocation
        try:
            client = DeribitClient()
            book_items = client.get_book_summary_by_currency("BTC", kind="option")
            cur_spot = float(book_items[0].get("underlying_price", 81000.0))
            book_map = {x["instrument_name"]: x for x in book_items}
        except Exception:
            cur_spot = 81000.0
            book_map = {}

        for p in ports:
            if not p.get("current_btc") or p["current_btc"] <= 0:
                p["current_btc"] = round((p["total_equity_usd"] * 0.5) / cur_spot, 4)
                p["current_cash_usd"] = round(p["total_equity_usd"] * 0.5, 2)
                conn.execute(
                    "UPDATE paper_portfolios SET current_btc = ?, current_cash_usd = ? WHERE id = ?",
                    (p["current_btc"], p["current_cash_usd"], p["id"])
                )
        conn.commit()

        # 2. Active Trades
        cur.execute(
            """
            SELECT pt.*, p.name as portfolio_name
            FROM paper_trades pt
            JOIN paper_portfolios p ON pt.portfolio_id = p.id
            WHERE pt.status = 'ACTIVE'
            ORDER BY pt.portfolio_id ASC
            """
        )
        active_trades = [dict(r) for r in cur.fetchall()]

        now_bkk = datetime.now(BKK_TZ)

        for at in active_trades:
            c_k = at["call_strike"]
            p_k = at["put_strike"]
            at["current_spot"] = cur_spot
            at["dist_call_pct"] = round((c_k - cur_spot) / cur_spot * 100.0, 2)
            at["dist_put_pct"] = round((cur_spot - p_k) / cur_spot * 100.0, 2)

            # Ensure active trade sizes match full 100% portfolio capacity
            pid = at["portfolio_id"]
            p_match = next((p for p in ports if p["id"] == pid), None)
            if p_match and (at.get("call_size_btc", 0) <= 0 or at.get("put_size_btc", 0) <= 0):
                at["call_size_btc"] = p_match["current_btc"]
                at["put_size_btc"] = round(p_match["current_cash_usd"] / p_k, 4)
                conn.execute(
                    "UPDATE paper_trades SET call_size_btc = ?, put_size_btc = ? WHERE id = ?",
                    (at["call_size_btc"], at["put_size_btc"], at["id"])
                )

            at["call_notional_usd"] = round(at["call_size_btc"] * cur_spot, 2)
            at["put_notional_usd"] = round(at["put_size_btc"] * p_k, 2)
            at["total_notional_usd"] = round(at["call_notional_usd"] + at["put_notional_usd"], 2)

            # Hours left
            try:
                exp_dt = datetime.strptime(at["expiry_time_bkk"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BKK_TZ)
                hours_left = max(0.0, round((exp_dt - now_bkk).total_seconds() / 3600.0, 1))
            except Exception:
                hours_left = 0.0
            at["hours_left"] = hours_left

            # Safety status
            if p_k <= cur_spot <= c_k:
                at["safety_status"] = "SAFE"
                at["safety_label"] = "อยู่ในกรอบกำไร 100%"
            elif cur_spot > c_k:
                at["safety_status"] = "BREACH_CALL"
                at["safety_label"] = f"ทะลุ Call (+{(cur_spot - c_k)/cur_spot*100:.1f}%)"
            else:
                at["safety_status"] = "BREACH_PUT"
                at["safety_label"] = f"หลุด Put (-{(p_k - cur_spot)/cur_spot*100:.1f}%)"

        # 3. Trade History (Settled)
        cur.execute(
            """
            SELECT pt.*, p.name as portfolio_name
            FROM paper_trades pt
            JOIN paper_portfolios p ON pt.portfolio_id = p.id
            WHERE pt.status != 'ACTIVE'
            ORDER BY pt.expiry_date DESC, pt.portfolio_id ASC
            LIMIT 100
            """
        )
        history = [dict(r) for r in cur.fetchall()]

        # 4. Equity Curves for Chart
        cur.execute(
            """
            SELECT record_date, portfolio_id, equity_usd, cum_pnl_pct, btc_price
            FROM paper_equity_history
            ORDER BY record_date ASC, portfolio_id ASC
            """
        )
        eq_all = cur.fetchall()

        # Build chart series
        dates_set = sorted(list(set(r["record_date"] for r in eq_all)))
        series = {1: [], 2: [], 3: [], 4: []}
        btc_perf = []

        base_btc = eq_all[0]["btc_price"] if eq_all else 80000.0
        btc_date_map = {}
        for r in eq_all:
            btc_date_map[r["record_date"]] = r["btc_price"]

        for d in dates_set:
            b_p = btc_date_map.get(d, base_btc)
            btc_pct = round((b_p - base_btc) / base_btc * 100.0, 2)
            btc_perf.append(btc_pct)

        for pid in (1, 2, 3, 4):
            pid_map = {r["record_date"]: r for r in eq_all if r["portfolio_id"] == pid}
            last_eq = INITIAL_CAPITAL
            last_pct = 0.0
            for d in dates_set:
                if d in pid_map:
                    last_eq = pid_map[d]["equity_usd"]
                    last_pct = pid_map[d]["cum_pnl_pct"]
                series[pid].append({
                    "date": d,
                    "equity": round(last_eq, 2),
                    "pct": round(last_pct, 2),
                })

        result = {
            "portfolios": ports,
            "active_trades": active_trades,
            "history": history,
            "chart": {
                "dates": dates_set,
                "port1": series[1],
                "port2": series[2],
                "port3": series[3],
                "port4": series[4],
                "btc_perf": btc_perf,
            },
            "current_spot": cur_spot,
        }
        if use_cache:
            from src.cache import memory_cache
            memory_cache.set("paper_trade:all_data", result, ttl=25.0)
        return result


def settle_active_paper_trades(settle_date: str, settle_price: float):
    """
    Called upon settlement to finalize active trades for settle_date,
    calculate realized PnL, update equity, and open new active cycles.
    """
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM paper_trades
            WHERE status = 'ACTIVE' AND expiry_date <= ?
            """,
            (settle_date,)
        )
        active_to_settle = cur.fetchall()

        for at in active_to_settle:
            pid = at["portfolio_id"]
            c_k = at["call_strike"]
            p_k = at["put_strike"]
            tot_prem = at["total_premium_collected_usd"]
            fee_usd = at["fees_usd"]
            eq_before = at["equity_before_usd"]

            cur.execute("SELECT current_cash_usd, current_btc FROM paper_portfolios WHERE id = ?", (pid,))
            p_curr = cur.fetchone()
            cash_held = p_curr["current_cash_usd"]
            btc_held = p_curr["current_btc"]

            status = "SETTLED_WIN"
            note = f"ราคาปิด (${settle_price:,.1f}) จบในกรอบ (${p_k:,.0f} - ${c_k:,.0f}) ชนะ 100%"

            if settle_price > c_k:
                status = "EXERCISED_CALL"
                note = f"ราคาปิด (${settle_price:,.1f}) สูงกว่า Call (${c_k:,.0f}) ขาย BTC ได้ Cap กำไร"
                cash_received = btc_held * c_k
                cash_held = cash_held + cash_received + tot_prem - fee_usd
                tot_eq = cash_held
                btc_held = (tot_eq * 0.5) / settle_price
                cash_held = tot_eq * 0.5
            elif settle_price < p_k:
                status = "EXERCISED_PUT"
                note = f"ราคาปิด (${settle_price:,.1f}) ต่ำกว่า Put (${p_k:,.0f}) บังคับซื้อ BTC ที่ Strike"
                put_cost = at["put_size_btc"] * p_k
                cash_held = max(0.0, cash_held - put_cost + tot_prem - fee_usd)
                btc_held = btc_held + at["put_size_btc"]
                tot_eq = cash_held + (btc_held * settle_price)
                btc_held = (tot_eq * 0.5) / settle_price
                cash_held = tot_eq * 0.5
            else:
                cash_held = cash_held + tot_prem - fee_usd
                tot_eq = cash_held + (btc_held * settle_price)

            eq_after = tot_eq
            net_pnl = eq_after - eq_before
            net_pct = (net_pnl / eq_before) * 100.0 if eq_before > 0 else 0.0

            cur.execute(
                """
                UPDATE paper_trades
                SET status = ?,
                    settlement_price = ?,
                    net_pnl_usd = ?,
                    net_pnl_pct = ?,
                    equity_after_usd = ?,
                    settled_at_bkk = datetime('now', '+7 hours'),
                    outcome_note = ?
                WHERE id = ?
                """,
                (status, settle_price, round(net_pnl, 2), round(net_pct, 2), round(eq_after, 2), note, at["id"])
            )

            # Record equity history
            cum_pnl_usd = eq_after - INITIAL_CAPITAL
            cum_pnl_pct = (cum_pnl_usd / INITIAL_CAPITAL) * 100.0
            cur.execute(
                """
                INSERT OR REPLACE INTO paper_equity_history (
                    portfolio_id, record_date, equity_usd, btc_price, btc_amount, cash_usd,
                    cum_pnl_usd, cum_pnl_pct, daily_pnl_usd, daily_pnl_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid, settle_date, round(eq_after, 2), settle_price, round(btc_held, 4), round(cash_held, 2),
                    round(cum_pnl_usd, 2), round(cum_pnl_pct, 2), round(net_pnl, 2), round(net_pct, 2)
                )
            )

            # Update portfolio balances
            cur.execute(
                """
                UPDATE paper_portfolios
                SET current_cash_usd = ?,
                    current_btc = ?
                WHERE id = ?
                """,
                (round(cash_held, 2), round(btc_held, 4), pid)
            )

        conn.commit()

    # Re-calculate metrics
    for pid in (1, 2, 3, 4):
        calculate_portfolio_metrics(pid)

    # Open next cycle active trades
    open_active_paper_trades()
    try:
        from src.cache import memory_cache
        memory_cache.delete("paper_trade:all_data")
    except Exception:
        pass


def reset_paper_trades():
    """Resets all paper trading records back to clean initial $10,000 state."""
    init_spot = 81000.0
    try:
        client = DeribitClient()
        bk = client.get_book_summary_by_currency("BTC", kind="option")
        if bk and bk[0].get("underlying_price"):
            init_spot = float(bk[0]["underlying_price"])
    except Exception:
        pass
    init_btc = round(5000.0 / init_spot, 4)

    with get_db() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_equity_history")
        conn.execute(
            """
            UPDATE paper_portfolios
            SET initial_capital_usd = 10000.0,
                current_cash_usd = 5000.0,
                current_btc = ?,
                total_equity_usd = 10000.0,
                total_pnl_usd = 0.0,
                total_pnl_pct = 0.0,
                total_trades = 0,
                winning_trades = 0,
                losing_trades = 0,
                win_rate_pct = 0.0,
                total_premium_collected_usd = 0.0,
                max_drawdown_pct = 0.0,
                sharpe_ratio = 0.0,
                updated_at_bkk = datetime('now', '+7 hours')
            """,
            (init_btc,)
        )
        conn.commit()
    backfill_paper_trades_from_history()
    open_active_paper_trades()
    try:
        from src.cache import memory_cache
        memory_cache.delete("paper_trade:all_data")
    except Exception:
        pass
    return {"status": "success", "message": "Paper trade portfolios reset successfully"}
