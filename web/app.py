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
    init_db()
    init_scheduler()


# ---------------- API Endpoints ----------------

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
        # Get distinct expiries with counts and latest total_oi sorted chronologically (ascending)
        cur_exp = conn.execute("""
            SELECT s.expiry_date, s.expiry_type, COUNT(*) as snap_count,
                   MAX(s.snapshot_time_bkk) as last_snap,
                   (
                       SELECT s2.total_oi
                       FROM snapshots s2
                       WHERE s2.expiry_date = s.expiry_date
                       ORDER BY s2.snapshot_time_bkk DESC
                       LIMIT 1
                   ) as latest_total_oi
            FROM snapshots s
            GROUP BY s.expiry_date
            ORDER BY s.expiry_date ASC
        """)
        all_exp = [dict(r) for r in cur_exp.fetchall()]

        if scope == "active":
            expiries = [e for e in all_exp if is_expiry_active(e["expiry_date"])]
            # If all upcoming are somehow empty, fallback to last 3 expiries
            if not expiries and all_exp:
                expiries = all_exp[-3:]
        elif scope == "expired":
            expiries = [e for e in all_exp if not is_expiry_active(e["expiry_date"])]
        else:
            expiries = all_exp

        # Filter query for snapshots
        if expiry_date and expiry_date != "all":
            cur_s = conn.execute(
                "SELECT * FROM snapshots WHERE expiry_date = ? ORDER BY snapshot_time_bkk ASC",
                (expiry_date,),
            )
            snaps = [dict(r) for r in cur_s.fetchall()]

            # Fetch strikes for each snapshot
            for s in snaps:
                cur_st = conn.execute(
                    """
                    SELECT strike, call_oi, put_oi, total_oi, call_oi_usd, put_oi_usd,
                           call_gex_usd, put_gex_usd, net_gex_usd, call_iv, put_iv
                    FROM oi_by_strike
                    WHERE snapshot_time_bkk = ? AND expiry_date = ?
                    ORDER BY strike ASC
                    """,
                    (s["snapshot_time_bkk"], s["expiry_date"]),
                )
                s["strikes"] = [dict(r) for r in cur_st.fetchall()]
        else:
            # Aggregated timeline across all expiries at each distinct snapshot time
            from src.options_math import calculate_max_pain

            cur_times = conn.execute("""
                SELECT snapshot_time_bkk, MAX(index_price) as index_price,
                       label, MAX(dvol) as dvol, MAX(atm_iv) as atm_iv
                FROM snapshots
                GROUP BY snapshot_time_bkk
                ORDER BY snapshot_time_bkk ASC
            """)
            times_rows = [dict(r) for r in cur_times.fetchall()]
            snaps = []
            for t_row in times_rows:
                t_bkk = t_row["snapshot_time_bkk"]
                cur_agg_st = conn.execute(
                    """
                    SELECT strike,
                           ROUND(SUM(call_oi), 2) as call_oi,
                           ROUND(SUM(put_oi), 2) as put_oi,
                           ROUND(SUM(total_oi), 2) as total_oi,
                           ROUND(SUM(call_oi_usd), 2) as call_oi_usd,
                           ROUND(SUM(put_oi_usd), 2) as put_oi_usd,
                           ROUND(SUM(call_gex_usd), 2) as call_gex_usd,
                           ROUND(SUM(put_gex_usd), 2) as put_gex_usd,
                           ROUND(SUM(net_gex_usd), 2) as net_gex_usd,
                           ROUND(AVG(call_iv), 1) as call_iv,
                           ROUND(AVG(put_iv), 1) as put_iv
                    FROM oi_by_strike
                    WHERE snapshot_time_bkk = ?
                    GROUP BY strike
                    ORDER BY strike ASC
                    """,
                    (t_bkk,),
                )
                agg_strikes = [dict(r) for r in cur_agg_st.fetchall()]
                if not agg_strikes:
                    continue

                tot_call = sum(st["call_oi"] for st in agg_strikes)
                tot_put = sum(st["put_oi"] for st in agg_strikes)
                tot_oi = sum(st["total_oi"] for st in agg_strikes)
                tot_net_gex = sum(st["net_gex_usd"] or 0 for st in agg_strikes)

                c1_strike = max(agg_strikes, key=lambda x: x["call_oi"])["strike"] if agg_strikes else 0
                p1_strike = max(agg_strikes, key=lambda x: x["put_oi"])["strike"] if agg_strikes else 0
                mp = calculate_max_pain(agg_strikes)

                snaps.append({
                    "snapshot_time_bkk": t_bkk,
                    "label": t_row["label"] or "all",
                    "expiry_date": "All Expiries",
                    "expiry_type": "all",
                    "index_price": t_row["index_price"] or 0.0,
                    "total_call_oi": tot_call,
                    "total_put_oi": tot_put,
                    "total_oi": tot_oi,
                    "put_call_oi_ratio": round(tot_put / tot_call, 2) if tot_call > 0 else 0.0,
                    "net_gex_usd": tot_net_gex,
                    "call1_strike": c1_strike,
                    "put1_strike": p1_strike,
                    "max_pain": mp,
                    "hours_to_expiry": 0.0,
                    "atm_iv": t_row["atm_iv"],
                    "dvol": t_row["dvol"],
                    "strikes": agg_strikes,
                })

    return {"expiries": expiries, "snapshots": snaps}


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

    # Helper function for stats calculation
    def compute_stats_for_subset(records):
        valid_mp = [r for r in records if r.get("max_pain") is not None]
        n_mp = len(valid_mp)
        if n_mp == 0:
            return None

        w05_mp = sum(r.get("[within_0.5pct_max_pain]") or r.get("within_0.5pct_max_pain") or 0 for r in valid_mp)
        w1_mp = sum(r.get("within_1pct_max_pain") or 0 for r in valid_mp)
        avg_dist_mp = (
            sum(abs(r["dist_max_pain_pct"]) for r in valid_mp if r.get("dist_max_pain_pct") is not None) / n_mp
            if n_mp > 0 else 0.0
        )

        valid_top = [r for r in records if r.get("top_oi_strike") is not None]
        n_top = len(valid_top)
        w05_top = sum(r.get("[within_0.5pct_top_oi]") or r.get("within_0.5pct_top_oi") or 0 for r in valid_top)
        w1_top = sum(r.get("within_1pct_top_oi") or 0 for r in valid_top)
        avg_dist_top = (
            sum(abs(r["dist_top_oi_pct"]) for r in valid_top if r.get("dist_top_oi_pct") is not None) / n_top
            if n_top > 0 else 0.0
        )

        valid_c1 = [r for r in records if r.get("call1_strike") is not None and r.get("call1_dist_pct") is not None]
        n_c1 = len(valid_c1)
        w05_c1 = sum(r.get("[call1_within_0.5pct]") or r.get("call1_within_0.5pct") or 0 for r in valid_c1)
        w1_c1 = sum(r.get("call1_within_1pct") or 0 for r in valid_c1)
        avg_dist_c1 = sum(abs(r["call1_dist_pct"]) for r in valid_c1) / n_c1 if n_c1 > 0 else 0.0

        valid_p1 = [r for r in records if r.get("put1_strike") is not None and r.get("put1_dist_pct") is not None]
        n_p1 = len(valid_p1)
        w05_p1 = sum(r.get("[put1_within_0.5pct]") or r.get("put1_within_0.5pct") or 0 for r in valid_p1)
        w1_p1 = sum(r.get("put1_within_1pct") or 0 for r in valid_p1)
        avg_dist_p1 = sum(abs(r["put1_dist_pct"]) for r in valid_p1) / n_p1 if n_p1 > 0 else 0.0

        valid_flip = [r for r in records if r.get("snap_gamma_flip") is not None and r.get("dist_gamma_flip_pct") is not None]
        n_flip = len(valid_flip)
        w05_flip = sum(1 for r in valid_flip if abs(r["dist_gamma_flip_pct"]) <= 0.5)
        w1_flip = sum(1 for r in valid_flip if abs(r["dist_gamma_flip_pct"]) <= 1.0)
        avg_dist_flip = sum(abs(r["dist_gamma_flip_pct"]) for r in valid_flip) / n_flip if n_flip > 0 else 0.0

        valid_range = [r for r in records if r.get("settled_in_put1_call1_range") is not None]
        n_range = len(valid_range)
        in_range_count = sum(r.get("settled_in_put1_call1_range") or 0 for r in valid_range)
        avg_range_width = (
            sum(r["range_width_pct"] for r in valid_range if r.get("range_width_pct") is not None) / n_range
            if n_range > 0 else 0.0
        )

        return {
            "n": n_mp,
            "max_pain": {
                "pct_05": round(w05_mp / n_mp * 100.0, 1) if n_mp > 0 else 0.0,
                "pct_1": round(w1_mp / n_mp * 100.0, 1) if n_mp > 0 else 0.0,
                "avg_dist": round(avg_dist_mp, 2),
            },
            "top_oi": {
                "pct_05": round(w05_top / n_top * 100.0, 1) if n_top > 0 else 0.0,
                "pct_1": round(w1_top / n_top * 100.0, 1) if n_top > 0 else 0.0,
                "avg_dist": round(avg_dist_top, 2),
            },
            "call1": {
                "pct_05": round(w05_c1 / n_c1 * 100.0, 1) if n_c1 > 0 else 0.0,
                "pct_1": round(w1_c1 / n_c1 * 100.0, 1) if n_c1 > 0 else 0.0,
                "avg_dist": round(avg_dist_c1, 2),
            },
            "put1": {
                "pct_05": round(w05_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
                "pct_1": round(w1_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
                "avg_dist": round(avg_dist_p1, 2),
            },
            "gamma_flip": {
                "pct_05": round(w05_flip / n_flip * 100.0, 1) if n_flip > 0 else 0.0,
                "pct_1": round(w1_flip / n_flip * 100.0, 1) if n_flip > 0 else 0.0,
                "avg_dist": round(avg_dist_flip, 2),
                "n_flip": n_flip,
            },
            "range": {
                "in_range_pct": round(in_range_count / n_range * 100.0, 1) if n_range > 0 else 0.0,
                "avg_width": round(avg_range_width, 2),
                "n_range": n_range,
            },
        }

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

    # Helper function to compute statistics
    def compute_stats_for_subset(records):
        n = len([r for r in records if r.get("max_pain") is not None])
        if n == 0:
            return None

        # Max Pain
        valid_mp = [r for r in records if r.get("max_pain") is not None]
        n_mp = len(valid_mp)
        w05_mp = sum(r.get("[within_0.5pct_max_pain]") or r.get("within_0.5pct_max_pain") or 0 for r in valid_mp)
        w1_mp = sum(r.get("within_1pct_max_pain") or 0 for r in valid_mp)
        avg_dist_mp = (
            sum(abs(r["dist_max_pain_pct"]) for r in valid_mp if r.get("dist_max_pain_pct") is not None) / n_mp
            if n_mp > 0 else 0.0
        )

        # Top OI
        valid_top = [r for r in records if r.get("top_oi_strike") is not None]
        n_top = len(valid_top)
        w05_top = sum(r.get("[within_0.5pct_top_oi]") or r.get("within_0.5pct_top_oi") or 0 for r in valid_top)
        w1_top = sum(r.get("within_1pct_top_oi") or 0 for r in valid_top)
        avg_dist_top = (
            sum(abs(r["dist_top_oi_pct"]) for r in valid_top if r.get("dist_top_oi_pct") is not None) / n_top
            if n_top > 0 else 0.0
        )

        # Call 1 & Put 1
        valid_c1 = [r for r in records if r.get("call1_strike") is not None and r.get("call1_dist_pct") is not None]
        n_c1 = len(valid_c1)
        w05_c1 = sum(r.get("[call1_within_0.5pct]") or r.get("call1_within_0.5pct") or 0 for r in valid_c1)
        w1_c1 = sum(r.get("call1_within_1pct") or 0 for r in valid_c1)
        avg_dist_c1 = sum(abs(r["call1_dist_pct"]) for r in valid_c1) / n_c1 if n_c1 > 0 else 0.0

        valid_p1 = [r for r in records if r.get("put1_strike") is not None and r.get("put1_dist_pct") is not None]
        n_p1 = len(valid_p1)
        w05_p1 = sum(r.get("[put1_within_0.5pct]") or r.get("put1_within_0.5pct") or 0 for r in valid_p1)
        w1_p1 = sum(r.get("put1_within_1pct") or 0 for r in valid_p1)
        avg_dist_p1 = sum(abs(r["put1_dist_pct"]) for r in valid_p1) / n_p1 if n_p1 > 0 else 0.0

        # Gamma flip
        valid_flip = [r for r in records if r.get("snap_gamma_flip") is not None and r.get("dist_gamma_flip_pct") is not None]
        n_flip = len(valid_flip)
        w05_flip = sum(1 for r in valid_flip if abs(r["dist_gamma_flip_pct"]) <= 0.5)
        w1_flip = sum(1 for r in valid_flip if abs(r["dist_gamma_flip_pct"]) <= 1.0)
        avg_dist_flip = sum(abs(r["dist_gamma_flip_pct"]) for r in valid_flip) / n_flip if n_flip > 0 else 0.0

        # P1-C1 range
        valid_range = [r for r in records if r.get("settled_in_put1_call1_range") is not None]
        n_range = len(valid_range)
        in_range_count = sum(r.get("settled_in_put1_call1_range") or 0 for r in valid_range)
        avg_range_width = (
            sum(r["range_width_pct"] for r in valid_range if r.get("range_width_pct") is not None) / n_range
            if n_range > 0 else 0.0
        )

        return {
            "n": n,
            "max_pain": {
                "pct_05": round(w05_mp / n_mp * 100.0, 1) if n_mp > 0 else 0.0,
                "pct_1": round(w1_mp / n_mp * 100.0, 1) if n_mp > 0 else 0.0,
                "avg_dist": round(avg_dist_mp, 2),
            },
            "top_oi": {
                "pct_05": round(w05_top / n_top * 100.0, 1) if n_top > 0 else 0.0,
                "pct_1": round(w1_top / n_top * 100.0, 1) if n_top > 0 else 0.0,
                "avg_dist": round(avg_dist_top, 2),
            },
            "call1": {
                "pct_05": round(w05_c1 / n_c1 * 100.0, 1) if n_c1 > 0 else 0.0,
                "pct_1": round(w1_c1 / n_c1 * 100.0, 1) if n_c1 > 0 else 0.0,
                "avg_dist": round(avg_dist_c1, 2),
            },
            "put1": {
                "pct_05": round(w05_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
                "pct_1": round(w1_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
                "avg_dist": round(avg_dist_p1, 2),
            },
            "gamma_flip": {
                "pct_05": round(w05_flip / n_flip * 100.0, 1) if n_flip > 0 else 0.0,
                "pct_1": round(w1_flip / n_flip * 100.0, 1) if n_flip > 0 else 0.0,
                "avg_dist": round(avg_dist_flip, 2),
                "n_flip": n_flip,
            },
            "range": {
                "in_range_pct": round(in_range_count / n_range * 100.0, 1) if n_range > 0 else 0.0,
                "avg_width": round(avg_range_width, 2),
                "n_range": n_range,
            },
        }

    overall_stats = compute_stats_for_subset(all_results)
    daily_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "daily"])
    weekly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") == "weekly"])
    monthly_stats = compute_stats_for_subset([r for r in all_results if r.get("expiry_type") in ("monthly", "quarterly")])

    with_events_stats = compute_stats_for_subset([r for r in all_results if r.get("has_high_impact_event") == 1])
    no_events_stats = compute_stats_for_subset([r for r in all_results if not r.get("has_high_impact_event")])

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
