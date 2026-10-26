import csv
import io
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from src.config import BASE_DIR, BKK_TZ
from src.db import get_db, init_db
from src.events import add_custom_event, delete_event, fetch_forexfactory_events, get_events
from src.scheduler import init_scheduler, trigger_manual
from src.settlement_analytics import (
    analyze_news_sentiment,
    analyze_pinning_and_gex_flow,
    compute_settlement_flow_stats,
    compute_stats_for_subset,
    compute_weekly_horizon_stats,
    get_expiry_evolution_milestones,
    get_full_settlement_history,
)

app = FastAPI(title="BTC Options Pinning Dashboard", description="Deribit BTC Options Pinning & Statistics")

# Mount templates and static
TEMPLATES_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR = BASE_DIR / "web" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def on_startup():
    from src.gdrive_backup import restore_database_if_missing
    try:
        restore_res = restore_database_if_missing()
        print(f"[STARTUP] Google Drive restore check: {restore_res}")
    except Exception as e:
        print(f"[STARTUP WARNING] Google Drive restore check failed: {e}")
    init_db()
    init_scheduler()


# ---------------- API Endpoints ----------------

@app.get("/ping")
@app.get("/health")
def api_ping():
    """Ultra-lightweight keep-alive endpoint for cron-job.org and uptime monitors (<20 bytes)."""
    return {"status": "ok"}


@app.post("/api/backup/gdrive")
def api_backup_gdrive():
    """Manually trigger immediate backup of btc_pinning.db to user's Google Drive."""
    from src.gdrive_backup import backup_database
    res = backup_database()
    if not res.get("success"):
        raise HTTPException(status_code=500, detail=res.get("error", "Backup failed"))
    return res


@app.post("/api/restore/gdrive")
def api_restore_gdrive():
    """Manually trigger restoration of btc_pinning.db from Google Drive."""
    from src.gdrive_backup import restore_database_if_missing
    res = restore_database_if_missing(force=True)
    if not res.get("restored"):
        raise HTTPException(status_code=500, detail=res.get("error", "Restore failed"))
    return res


@app.post("/api/scheduler/trigger")
def api_trigger(action: str = Query("snapshot")):
    try:
        res = trigger_manual(action=action)
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/scheduler/status")
def api_scheduler_status():
    from src.scheduler import scheduler
    jobs = []
    if scheduler.running:
        for j in scheduler.get_jobs():
            jobs.append({
                "id": j.id,
                "name": j.name,
                "next_run_time": j.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if j.next_run_time else None
            })
    return {
        "running": scheduler.running,
        "jobs": jobs,
        "current_time_bkk": datetime.now(BKK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    }


@app.post("/api/events/add")
async def api_add_event(request: Request):
    form = await request.form()
    date_str = form.get("date")
    time_bkk = form.get("time_bkk")
    event_text = form.get("event")
    impact = form.get("impact", "Medium")

    if not date_str or not time_bkk or not event_text:
        raise HTTPException(status_code=400, detail="Missing required event fields")

    ok = add_custom_event(str(date_str), str(time_bkk), str(event_text), str(impact))
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save custom event")
    return {"status": "success"}


@app.post("/api/events/delete/{event_id}")
def api_delete_event(event_id: int):
    ok = delete_event(event_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete event")
    return {"status": "success"}


@app.post("/api/events/refresh_forex")
def api_refresh_forex():
    evs = fetch_forexfactory_events()
    return {"status": "success", "count": len(evs)}


@app.get("/api/settlement/detail/{expiry_date}")
def api_settlement_detail(expiry_date: str):
    """Returns detailed historical milestones, pinning flow, GEX sweep status, and news sentiment for an expiry."""
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM expiry_results WHERE expiry_date = ?", (expiry_date,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Expiry result for {expiry_date} not found")
        result = dict(row)
        milestones = get_expiry_evolution_milestones(expiry_date, conn)
        news = analyze_news_sentiment(expiry_date, conn)
        flow = analyze_pinning_and_gex_flow(result, milestones, news)
        return {
            "expiry_date": expiry_date,
            "result": result,
            "milestones_info": milestones,
            "news_info": news,
            "flow_info": flow,
        }


def is_expiry_active(exp_date_str: str) -> bool:
    """Check if an expiry date is still active (unexpired) in Bangkok time (UTC+7)."""
    now_bkk = datetime.now(BKK_TZ)
    today_str = now_bkk.strftime("%Y-%m-%d")
    if exp_date_str < today_str:
        return False
    if exp_date_str == today_str and now_bkk.hour >= 15:
        return False
    return True


@app.get("/api/timeline")
def api_timeline(expiry_date: Optional[str] = None, scope: str = "active"):
    """
    Timeline API for interactive time-slider scrubbing (like oi.iyortz.com).
    Returns list of chronological snapshots and their per-strike data.
    scope: 'active' (default for main dashboard), 'expired' (for archive), 'all'
    """
    with get_db() as conn:
        # Get distinct expiries with counts and latest total_oi and total_volume sorted chronologically (ascending)
        cur_exp = conn.execute("""
            SELECT s.expiry_date, s.expiry_type, COUNT(*) as snap_count,
                   MAX(s.snapshot_time_bkk) as last_snap,
                   (
                       SELECT s2.total_oi
                       FROM snapshots s2
                       WHERE s2.expiry_date = s.expiry_date
                       ORDER BY s2.snapshot_time_bkk DESC
                       LIMIT 1
                   ) as latest_total_oi,
                   (
                       SELECT s2.total_volume
                       FROM snapshots s2
                       WHERE s2.expiry_date = s.expiry_date
                       ORDER BY s2.snapshot_time_bkk DESC
                       LIMIT 1
                   ) as latest_total_volume
            FROM snapshots s
            GROUP BY s.expiry_date
            ORDER BY s.expiry_date ASC
        """)
        all_exp = [dict(r) for r in cur_exp.fetchall()]

        # Populate latest volume from oi_by_strike for expiries where snapshots.total_volume is null
        cur_latest_vol = conn.execute("""
            SELECT expiry_date, ROUND(SUM(COALESCE(call_volume_24h, 0) + COALESCE(put_volume_24h, 0)), 2) as vol
            FROM oi_by_strike o
            WHERE snapshot_time_bkk = (
                SELECT MAX(o2.snapshot_time_bkk) FROM oi_by_strike o2 WHERE o2.expiry_date = o.expiry_date
            )
            GROUP BY expiry_date
        """)
        vol_map = {r["expiry_date"]: r["vol"] for r in cur_latest_vol.fetchall()}
        for e in all_exp:
            if not e.get("latest_total_volume"):
                e["latest_total_volume"] = vol_map.get(e["expiry_date"], 0.0)

        if scope == "active":
            expiries = [e for e in all_exp if is_expiry_active(e["expiry_date"])]
            # If all upcoming are somehow empty, fallback to last 3 expiries
            if not expiries and all_exp:
                expiries = all_exp[-3:]
        elif scope == "expired":
            expiries = [e for e in all_exp if not is_expiry_active(e["expiry_date"])]
        else:
            expiries = all_exp

        # Compute dynamic group aggregations
        from datetime import datetime, timezone, timedelta
        tz_bkk = timezone(timedelta(hours=7))
        now_bkk = datetime.now(tz_bkk)
        today_str = now_bkk.strftime("%Y-%m-%d")
        tomorrow_str = (now_bkk + timedelta(days=1)).strftime("%Y-%m-%d")
        weekday = now_bkk.weekday()
        days_to_friday = (4 - weekday) % 7
        friday_str = (now_bkk + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")
        month_prefix = now_bkk.strftime("%Y-%m")

        active_dates = [e["expiry_date"] for e in expiries]

        # 1. Today / Tomorrow Group
        today_tmrw_dates = [d for d in active_dates if d <= tomorrow_str]
        if not today_tmrw_dates and active_dates:
            today_tmrw_dates = active_dates[:1]

        # 2. This Week Group (up to this Friday)
        weekly_dates = [d for d in active_dates if d <= friday_str]
        if not weekly_dates and active_dates:
            weekly_dates = active_dates[:min(4, len(active_dates))]

        # 3. This Month Group (current month)
        monthly_dates = [d for d in active_dates if d.startswith(month_prefix)]
        if not monthly_dates and active_dates:
            monthly_dates = weekly_dates

        # 4. All Group
        all_dates = active_dates

        def sum_metrics_for_dates(date_list):
            oi_sum = sum(e.get("latest_total_oi") or 0.0 for e in expiries if e["expiry_date"] in date_list)
            vol_sum = sum(e.get("latest_total_volume") or 0.0 for e in expiries if e["expiry_date"] in date_list)
            return round(oi_sum, 2), round(vol_sum, 2)

        g_today_oi, g_today_vol = sum_metrics_for_dates(today_tmrw_dates)
        g_week_oi, g_week_vol = sum_metrics_for_dates(weekly_dates)
        g_month_oi, g_month_vol = sum_metrics_for_dates(monthly_dates)
        g_all_oi, g_all_vol = sum_metrics_for_dates(all_dates)

        groups_meta = {
            "group_today": {
                "id": "group_today",
                "label": "วันนี้/พรุ่งนี้",
                "tag": "รวบ 2 วัน",
                "dates": today_tmrw_dates,
                "latest_total_oi": g_today_oi,
                "latest_total_volume": g_today_vol,
            },
            "group_weekly": {
                "id": "group_weekly",
                "label": "สัปดาห์นี้",
                "tag": f"รวบถึงศุกร์ {friday_str}",
                "dates": weekly_dates,
                "latest_total_oi": g_week_oi,
                "latest_total_volume": g_week_vol,
            },
            "group_monthly": {
                "id": "group_monthly",
                "label": "เดือนนี้",
                "tag": f"รวบทั้งเดือน {month_prefix}",
                "dates": monthly_dates,
                "latest_total_oi": g_month_oi,
                "latest_total_volume": g_month_vol,
            },
            "all": {
                "id": "all",
                "label": "รวมทุก Expiry",
                "tag": "All Aggregated",
                "dates": all_dates,
                "latest_total_oi": g_all_oi,
                "latest_total_volume": g_all_vol,
            }
        }

        # Determine target dates and grouping mode
        is_group = False
        target_dates = []
        group_display_name = ""
        group_type_tag = ""

        if expiry_date in groups_meta:
            is_group = True
            grp = groups_meta[expiry_date]
            target_dates = grp["dates"]
            group_display_name = f"รวบกลุ่ม: {grp['label']} ({grp['tag']})"
            group_type_tag = expiry_date
        elif expiry_date and ("," in expiry_date):
            is_group = True
            target_dates = [d.strip() for d in expiry_date.split(",") if d.strip()]
            group_display_name = f"รวบกลุ่ม ({len(target_dates)} สัญญา)"
            group_type_tag = "custom_group"
        elif expiry_date and expiry_date not in ("all", "all_expiries"):
            # Single individual expiry date
            is_group = False
            target_dates = [expiry_date]
        else:
            # Default to all active expiries
            is_group = True
            target_dates = all_dates
            group_display_name = "รวมทุก Expiry ในอนาคต (All Aggregated)"
            group_type_tag = "all"

        # Query snapshots
        if not is_group and target_dates:
            cur_s = conn.execute(
                "SELECT * FROM snapshots WHERE expiry_date = ? ORDER BY snapshot_time_bkk ASC",
                (target_dates[0],),
            )
            snaps = [dict(r) for r in cur_s.fetchall()]

            # Fetch strikes for each snapshot
            for s in snaps:
                cur_st = conn.execute(
                    """
                    SELECT strike, call_oi, put_oi, total_oi, call_oi_usd, put_oi_usd,
                           call_volume_24h, put_volume_24h,
                           ROUND(COALESCE(call_volume_24h, 0) + COALESCE(put_volume_24h, 0), 2) as total_volume_24h,
                           call_gex_usd, put_gex_usd, net_gex_usd, call_iv, put_iv
                    FROM oi_by_strike
                    WHERE snapshot_time_bkk = ? AND expiry_date = ?
                    ORDER BY strike ASC
                    """,
                    (s["snapshot_time_bkk"], s["expiry_date"]),
                )
                s["strikes"] = [dict(r) for r in cur_st.fetchall()]
                tot_c_vol = round(sum(st.get("call_volume_24h") or 0.0 for st in s["strikes"]), 2)
                tot_p_vol = round(sum(st.get("put_volume_24h") or 0.0 for st in s["strikes"]), 2)
                s["total_call_volume"] = s.get("total_call_volume") or tot_c_vol
                s["total_put_volume"] = s.get("total_put_volume") or tot_p_vol
                s["total_volume"] = s.get("total_volume") or round(tot_c_vol + tot_p_vol, 2)
                s["put_call_volume_ratio"] = s.get("put_call_volume_ratio") or (round(tot_p_vol / tot_c_vol, 2) if tot_c_vol > 0 else 0.0)
        else:
            # Aggregated timeline across target_dates at each distinct snapshot time
            from src.options_math import calculate_max_pain

            placeholders = ",".join("?" for _ in target_dates) if target_dates else "'__none__'"
            cur_times = conn.execute(f"""
                SELECT snapshot_time_bkk, MAX(index_price) as index_price,
                       label, MAX(dvol) as dvol, MAX(atm_iv) as atm_iv
                FROM snapshots
                WHERE expiry_date IN ({placeholders})
                GROUP BY snapshot_time_bkk
                ORDER BY snapshot_time_bkk ASC
            """, tuple(target_dates) if target_dates else ())
            times_rows = [dict(r) for r in cur_times.fetchall()]
            snaps = []
            for t_row in times_rows:
                t_bkk = t_row["snapshot_time_bkk"]
                cur_agg_st = conn.execute(
                    f"""
                    SELECT strike,
                           ROUND(SUM(call_oi), 2) as call_oi,
                           ROUND(SUM(put_oi), 2) as put_oi,
                           ROUND(SUM(total_oi), 2) as total_oi,
                           ROUND(SUM(call_oi_usd), 2) as call_oi_usd,
                           ROUND(SUM(put_oi_usd), 2) as put_oi_usd,
                           ROUND(SUM(call_volume_24h), 2) as call_volume_24h,
                           ROUND(SUM(put_volume_24h), 2) as put_volume_24h,
                           ROUND(SUM(COALESCE(call_volume_24h, 0) + COALESCE(put_volume_24h, 0)), 2) as total_volume_24h,
                           ROUND(SUM(call_gex_usd), 2) as call_gex_usd,
                           ROUND(SUM(put_gex_usd), 2) as put_gex_usd,
                           ROUND(SUM(net_gex_usd), 2) as net_gex_usd,
                           ROUND(AVG(call_iv), 1) as call_iv,
                           ROUND(AVG(put_iv), 1) as put_iv
                    FROM oi_by_strike
                    WHERE snapshot_time_bkk = ?
                      AND expiry_date IN ({placeholders})
                    GROUP BY strike
                    ORDER BY strike ASC
                    """,
                    (t_bkk, *target_dates),
                )
                agg_strikes = [dict(r) for r in cur_agg_st.fetchall()]
                if not agg_strikes:
                    continue

                tot_call = sum(st["call_oi"] for st in agg_strikes)
                tot_put = sum(st["put_oi"] for st in agg_strikes)
                tot_oi = sum(st["total_oi"] for st in agg_strikes)
                tot_net_gex = sum(st["net_gex_usd"] or 0 for st in agg_strikes)

                tot_c_vol = round(sum(st.get("call_volume_24h") or 0.0 for st in agg_strikes), 2)
                tot_p_vol = round(sum(st.get("put_volume_24h") or 0.0 for st in agg_strikes), 2)
                tot_vol = round(tot_c_vol + tot_p_vol, 2)
                pc_vol = round(tot_p_vol / tot_c_vol, 2) if tot_c_vol > 0 else 0.0

                c1_strike = max(agg_strikes, key=lambda x: x["call_oi"])["strike"] if agg_strikes else 0
                p1_strike = max(agg_strikes, key=lambda x: x["put_oi"])["strike"] if agg_strikes else 0
                mp = calculate_max_pain(agg_strikes)

                snaps.append({
                    "snapshot_time_bkk": t_bkk,
                    "label": t_row["label"] or "group",
                    "expiry_date": group_display_name,
                    "expiry_type": group_type_tag,
                    "index_price": t_row["index_price"] or 0.0,
                    "total_call_oi": tot_call,
                    "total_put_oi": tot_put,
                    "total_oi": tot_oi,
                    "put_call_oi_ratio": round(tot_put / tot_call, 2) if tot_call > 0 else 0.0,
                    "total_call_volume": tot_c_vol,
                    "total_put_volume": tot_p_vol,
                    "total_volume": tot_vol,
                    "put_call_volume_ratio": pc_vol,
                    "net_gex_usd": tot_net_gex,
                    "call1_strike": c1_strike,
                    "put1_strike": p1_strike,
                    "max_pain": mp,
                    "hours_to_expiry": 0.0,
                    "atm_iv": t_row["atm_iv"],
                    "dvol": t_row["dvol"],
                    "strikes": agg_strikes,
                })

    return {"expiries": expiries, "groups": groups_meta, "snapshots": snaps}


# ---------------- CSV Exports ----------------

@app.get("/export/snapshots.csv")
def export_snapshots():
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM snapshots ORDER BY snapshot_time_bkk DESC")
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return StreamingResponse(io.StringIO("no data"), media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=btc_snapshots.csv"},
    )


@app.get("/export/oi_by_strike.csv")
def export_oi_by_strike(expiry_date: Optional[str] = None):
    with get_db() as conn:
        if expiry_date:
            cursor = conn.execute(
                "SELECT * FROM oi_by_strike WHERE expiry_date = ? ORDER BY snapshot_time_bkk DESC, strike ASC",
                (expiry_date,),
            )
        else:
            cursor = conn.execute("SELECT * FROM oi_by_strike ORDER BY snapshot_time_bkk DESC, strike ASC")
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return StreamingResponse(io.StringIO("no data"), media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=btc_oi_by_strike_{expiry_date or 'all'}.csv"},
    )


@app.get("/export/results.csv")
def export_results():
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM expiry_results ORDER BY expiry_date DESC")
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return StreamingResponse(io.StringIO("no data"), media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=btc_expiry_results.csv"},
    )


@app.get("/export/big_expiries.csv")
def export_big_expiries():
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM big_expiries ORDER BY snapshot_time_bkk DESC, kind ASC")
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return StreamingResponse(io.StringIO("no data"), media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=btc_big_expiries.csv"},
    )


@app.get("/export/events.csv")
def export_events():
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM events ORDER BY date DESC, time_bkk DESC")
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        return StreamingResponse(io.StringIO("no data"), media_type="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=btc_events.csv"},
    )


# ---------------- Web Pages ----------------

@app.get("/", response_class=HTMLResponse)
def page_latest(request: Request, expiry_date: Optional[str] = None):
    """
    Single-Page Comprehensive Dashboard:
    - Executive Directional Analysis (Daily / Weekly / Monthly Outlook)
    - oi.iyortz Style Header & Controls attached directly to Charts
    - Dual Connected Charts (Top: OI with Green Calls / Red Puts / IV; Bottom: GEX aligned underneath)
    - OI Change comparison
    - Cumulative Hit-rate statistics
    - Historical Settlement Results
    - Big Expiries (Weekly, Monthly, Quarterly)
    - ForexFactory & Custom Events Feed
    """
    with get_db() as conn:
        now_bkk = datetime.now(BKK_TZ)
        today_str = now_bkk.strftime("%Y-%m-%d")

        # 1. Latest snapshot (nearest active unexpired target by default, or specific expiry if requested)
        if expiry_date and expiry_date != "all":
            cur = conn.execute(
                "SELECT * FROM snapshots WHERE expiry_date = ? ORDER BY snapshot_time_bkk DESC LIMIT 1",
                (expiry_date,),
            )
            latest_row = cur.fetchone()
        else:
            # Pick nearest active (unexpired) future expiry first
            cur = conn.execute(
                """
                SELECT * FROM snapshots 
                WHERE (expiry_date > ? OR (expiry_date = ? AND ? < 15))
                ORDER BY snapshot_time_bkk DESC, hours_to_expiry ASC LIMIT 1
                """,
                (today_str, today_str, now_bkk.hour),
            )
            latest_row = cur.fetchone()
            if not latest_row:
                cur = conn.execute(
                    "SELECT * FROM snapshots ORDER BY snapshot_time_bkk DESC, hours_to_expiry ASC LIMIT 1"
                )
                latest_row = cur.fetchone()

        if not latest_row:
            cur = conn.execute("SELECT * FROM snapshots ORDER BY snapshot_time_bkk DESC LIMIT 1")
            latest_row = cur.fetchone()
        snap = dict(latest_row) if latest_row else None

        strikes = []
        snaps_same_expiry = []
        if snap:
            # Strikes for latest snapshot
            cur_strikes = conn.execute(
                """
                SELECT * FROM oi_by_strike
                WHERE snapshot_time_bkk = ? AND expiry_date = ?
                ORDER BY strike ASC
                """,
                (snap["snapshot_time_bkk"], snap["expiry_date"]),
            )
            strikes = [dict(r) for r in cur_strikes.fetchall()]

            # Snapshots of the same expiry for OI change
            cur_same = conn.execute(
                "SELECT * FROM snapshots WHERE expiry_date = ? ORDER BY snapshot_time_bkk ASC",
                (snap["expiry_date"],),
            )
            snaps_same_expiry = [dict(r) for r in cur_same.fetchall()]

        # 2. Big Expiries
        cur_big_time = conn.execute("SELECT snapshot_time_bkk FROM big_expiries ORDER BY snapshot_time_bkk DESC LIMIT 1")
        row_bt = cur_big_time.fetchone()
        big_time = row_bt["snapshot_time_bkk"] if row_bt else ""
        cur_big = conn.execute(
            "SELECT * FROM big_expiries WHERE snapshot_time_bkk = ? ORDER BY hours_to_expiry ASC",
            (big_time,),
        )
        big_expiries = [dict(r) for r in cur_big.fetchall()]

        # 3. Settlement Results with Evolution & GEX Flow Analytics
        all_results = get_full_settlement_history(conn)

        # 4. Events & Logs
        cur_ev = conn.execute("SELECT * FROM events ORDER BY date DESC, time_bkk DESC LIMIT 20")
        events_list = [dict(r) for r in cur_ev.fetchall()]

        cur_logs = conn.execute("SELECT * FROM execution_logs ORDER BY run_time_bkk DESC LIMIT 15")
        logs = [dict(r) for r in cur_logs.fetchall()]

        # 5. Distinct Expiry Dates
        cur_dates = conn.execute("SELECT DISTINCT expiry_date FROM snapshots ORDER BY expiry_date DESC")
        all_dates = [r["expiry_date"] for r in cur_dates.fetchall()]

    overall_stats = compute_stats_for_subset(all_results)
    daily_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "daily"])
    weekly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "weekly"])
    monthly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") in ("monthly", "quarterly")])
    with_events_stats = compute_stats_for_subset([r for r in all_results if r.get("has_high_impact_event") == 1])
    no_events_stats = compute_stats_for_subset([r for r in all_results if not r.get("has_high_impact_event")])

    mp_distances = [
        round(r["dist_max_pain_pct"], 2) for r in all_results if r.get("dist_max_pain_pct") is not None
    ]

    from src.scheduler import scheduler
    sched_info = {
        "running": scheduler.running,
        "next_jobs": [
            {
                "id": j.id,
                "label": "15:30 (Settle วันนี้ & Snapshot พรุ่งนี้)" if "1530" in j.id else ("21:00 (Snapshot พรุ่งนี้)" if "2100" in j.id else ("06:00 (Snapshot วันนี้)" if "0600" in j.id else "Snapshot รายชั่วโมง")),
                "next_run": j.next_run_time.strftime("%d/%m %H:%M น.") if j.next_run_time else "-"
            }
            for j in sorted(scheduler.get_jobs(), key=lambda x: x.next_run_time or datetime.max)
        ]
    }

    from src.settlement_analytics import compute_weekly_horizon_stats
    weekly_horizon = compute_weekly_horizon_stats(conn)

    return templates.TemplateResponse(
        request=request,
        name="latest.html",
        context={
            "page": "latest",
            "snap": snap,
            "strikes": strikes,
            "snaps_same_expiry": snaps_same_expiry,
            "big_expiries": big_expiries,
            "results": all_results[:5],
            "total_settled_count": len(all_results),
            "events": events_list,
            "logs": logs,
            "all_dates": all_dates,
            "overall_stats": overall_stats,
            "daily_stats": daily_stats,
            "weekly_stats": weekly_stats,
            "monthly_stats": monthly_stats,
            "with_events_stats": with_events_stats,
            "no_events_stats": no_events_stats,
            "weekly_horizon": weekly_horizon,
            "mp_distances": mp_distances,
            "sched_info": sched_info,
        },
    )


@app.get("/oi_change", response_class=HTMLResponse)
def page_oi_change(request: Request, expiry_date: Optional[str] = None):
    with get_db() as conn:
        # Get all distinct expiry dates
        cur_dates = conn.execute("SELECT DISTINCT expiry_date FROM snapshots ORDER BY expiry_date DESC")
        all_dates = [r["expiry_date"] for r in cur_dates.fetchall()]

        selected_date = expiry_date or (all_dates[0] if all_dates else None)

        snaps = []
        if selected_date:
            cur_s = conn.execute(
                "SELECT * FROM snapshots WHERE expiry_date = ? ORDER BY snapshot_time_bkk ASC",
                (selected_date,),
            )
            snaps = [dict(r) for r in cur_s.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="oi_change.html",
        context={
            "page": "oi_change",
            "all_dates": all_dates,
            "selected_date": selected_date,
            "snaps": snaps,
        },
    )


@app.get("/gex_aggregate", response_class=HTMLResponse)
def page_gex_aggregate(request: Request):
    """Aggregate GEX across expiries (this Friday, end of month, target expiry)."""
    with get_db() as conn:
        # Latest snapshot
        cur = conn.execute("SELECT snapshot_time_bkk FROM snapshots ORDER BY snapshot_time_bkk DESC LIMIT 1")
        latest_row = cur.fetchone()
        latest_time = latest_row["snapshot_time_bkk"] if latest_row else None

        gex_data = []
        if latest_time:
            # Group GEX by strike for latest snapshot
            cur_gex = conn.execute(
                """
                SELECT strike,
                       SUM(call_gex_usd) as call_gex,
                       SUM(put_gex_usd) as put_gex,
                       SUM(net_gex_usd) as net_gex,
                       SUM(call_oi) as total_call_oi,
                       SUM(put_oi) as total_put_oi
                FROM oi_by_strike
                WHERE snapshot_time_bkk = ?
                GROUP BY strike
                ORDER BY strike ASC
                """,
                (latest_time,),
            )
            gex_data = [dict(r) for r in cur_gex.fetchall()]

        # Latest big expiries info
        cur_big = conn.execute(
            """
            SELECT * FROM big_expiries
            WHERE snapshot_time_bkk = ?
            ORDER BY kind ASC
            """,
            (latest_time,) if latest_time else ("",),
        )
        bigs = [dict(r) for r in cur_big.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="gex_aggregate.html",
        context={
            "page": "gex_aggregate",
            "latest_time": latest_time,
            "gex_data": gex_data,
            "bigs": bigs,
        },
    )


@app.get("/results", response_class=HTMLResponse)
def page_results(
    request: Request,
    expiry_type: Optional[str] = Query(None),
    event_filter: Optional[str] = Query(None),
):
    with get_db() as conn:
        results = get_full_settlement_history(conn, expiry_type=expiry_type, event_filter=event_filter)
        flow_stats = compute_settlement_flow_stats(results)

    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "page": "results",
            "results": results,
            "flow_stats": flow_stats,
            "expiry_type": expiry_type or "all",
            "event_filter": event_filter or "all",
        },
    )


@app.get("/stats", response_class=HTMLResponse)
def page_stats(request: Request):
    """Cumulative hit-rate statistics breakdown."""
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM expiry_results WHERE settlement_price IS NOT NULL")
        all_results = [dict(r) for r in cursor.fetchall()]

    overall_stats = compute_stats_for_subset(all_results)
    daily_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "daily"])
    weekly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "weekly"])
    monthly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") in ("monthly", "quarterly")])

    with_events_stats = compute_stats_for_subset([r for r in all_results if r.get("has_high_impact_event") == 1])
    no_events_stats = compute_stats_for_subset([r for r in all_results if not r.get("has_high_impact_event")])

    weekly_horizon = compute_weekly_horizon_stats(conn)

    # Distribution data for chart
    mp_distances = [
        round(r["dist_max_pain_pct"], 2) for r in all_results if r.get("dist_max_pain_pct") is not None
    ]
    top_distances = [
        round(r["dist_top_oi_pct"], 2) for r in all_results if r.get("dist_top_oi_pct") is not None
    ]

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={
            "page": "stats",
            "overall": overall_stats,
            "daily": daily_stats,
            "weekly": weekly_stats,
            "monthly": monthly_stats,
            "with_events": with_events_stats,
            "no_events": no_events_stats,
            "weekly_horizon": weekly_horizon,
            "mp_distances": mp_distances,
            "top_distances": top_distances,
            "total_settled": len(all_results),
        },
    )


@app.get("/big_expiries", response_class=HTMLResponse)
def page_big_expiries(request: Request):
    with get_db() as conn:
        # Get latest snapshot time
        cur_time = conn.execute("SELECT snapshot_time_bkk FROM big_expiries ORDER BY snapshot_time_bkk DESC LIMIT 1")
        latest_t = cur_time.fetchone()
        snap_time = latest_t["snapshot_time_bkk"] if latest_t else None

        records = []
        if snap_time:
            cur = conn.execute(
                "SELECT * FROM big_expiries WHERE snapshot_time_bkk = ? ORDER BY hours_to_expiry ASC",
                (snap_time,),
            )
            records = [dict(r) for r in cur.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="big_expiries.html",
        context={
            "page": "big_expiries",
            "snap_time": snap_time,
            "records": records,
        },
    )


@app.get("/events", response_class=HTMLResponse)
def page_events(request: Request):
    events_list = get_events(limit=250)
    with get_db() as conn:
        cur_logs = conn.execute("SELECT * FROM execution_logs ORDER BY run_time_bkk DESC LIMIT 30")
        logs = [dict(r) for r in cur_logs.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="events.html",
        context={
            "page": "events",
            "events": events_list,
            "logs": logs,
        },
    )
