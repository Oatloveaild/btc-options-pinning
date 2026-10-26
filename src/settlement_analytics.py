"""
Settlement Evolution, Pinning Flow, and GEX Sweep Analytics.
Analyzes how Spot Price, Max Pain, Top OI, and GEX Walls (Call 1 / Put 1)
evolved from tracking inception (1 month, 1 week, 24h prior) to final settlement,
determining pinning magnet pull, GEX sweeps, and news sentiment correlation.
"""
from datetime import datetime, timedelta
import sqlite3
from typing import Any, Dict, List, Optional
from src.config import BKK_TZ


def parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def get_expiry_evolution_milestones(expiry_date: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Finds snapshot milestones for a specific expiry date:
    - Inception (earliest recorded snapshot)
    - ~1 Month Ago (~30 days prior, if available)
    - ~1 Week Ago (~7 days prior, if available)
    - 24h Benchmark (06:00 BKK or last pre-expiry snapshot)
    - Pre-Settle snapshot (latest available before 15:00 on expiry date)
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT snapshot_time_bkk, expiry_date, label, index_price, max_pain,
               top_oi_strike, call1_strike, put1_strike, net_gex_usd, gamma_flip,
               call1_oi, put1_oi
        FROM snapshots
        WHERE expiry_date = ?
        ORDER BY snapshot_time_bkk ASC
        """,
        (expiry_date,),
    )
    snaps = [dict(r) for r in cur.fetchall()]

    if not snaps:
        return {
            "snapshots_count": 0,
            "milestones": [],
            "inception": None,
            "one_month": None,
            "one_week": None,
            "benchmark": None,
        }

    exp_d = parse_date(expiry_date)
    inception = snaps[0]

    # Helper to calculate day distance
    def snap_days_before(s):
        st_date = datetime.strptime(s["snapshot_time_bkk"][:10], "%Y-%m-%d").date()
        return (exp_d - st_date).days

    # 1 Month out (~25 to 35 days before expiry)
    one_month = None
    month_candidates = [s for s in snaps if 25 <= snap_days_before(s) <= 35]
    if month_candidates:
        one_month = min(month_candidates, key=lambda s: abs(snap_days_before(s) - 30))

    # 1 Week out (~5 to 9 days before expiry)
    one_week = None
    week_candidates = [s for s in snaps if 5 <= snap_days_before(s) <= 9]
    if week_candidates:
        one_week = min(week_candidates, key=lambda s: abs(snap_days_before(s) - 7))

    # 24h Benchmark (Priority: 06:00 BKK on expiry date, else day before 21:00 / 15:30)
    benchmark = None
    b0600 = [s for s in snaps if s["snapshot_time_bkk"][:10] == expiry_date and s.get("label") == "0600"]
    if b0600:
        benchmark = b0600[0]
    else:
        # Day before
        day_before = (exp_d - timedelta(days=1)).strftime("%Y-%m-%d")
        b_prev = [s for s in snaps if s["snapshot_time_bkk"][:10] == day_before]
        if b_prev:
            benchmark = b_prev[-1]
        else:
            benchmark = snaps[0]

    # Pre-settle snapshot (latest snapshot on expiry date before 15:00, or latest before settle)
    pre_settle = None
    same_day_snaps = [s for s in snaps if s["snapshot_time_bkk"][:10] == expiry_date]
    if same_day_snaps:
        # Choose the latest snapshot of that day
        latest_same_day = same_day_snaps[-1]
        if benchmark and latest_same_day["snapshot_time_bkk"] != benchmark["snapshot_time_bkk"]:
            pre_settle = latest_same_day

    # Build milestones list
    milestones = []
    seen_times = set()

    def add_milestone(label: str, period_tag: str, snap: Optional[Dict[str, Any]]):
        if not snap:
            return
        t = snap["snapshot_time_bkk"]
        if t in seen_times:
            return
        milestones.append({
            "label": label,
            "period_tag": period_tag,
            "time_bkk": t,
            "days_before": snap_days_before(snap),
            "spot": snap["index_price"],
            "max_pain": snap["max_pain"],
            "top_oi": snap["top_oi_strike"],
            "call1_strike": snap["call1_strike"],
            "put1_strike": snap["put1_strike"],
            "gamma_flip": snap["gamma_flip"],
            "net_gex_usd": snap["net_gex_usd"],
        })
        seen_times.add(t)

    # 1. Inception (First collected)
    if inception:
        days_in = snap_days_before(inception)
        in_label = f"จุดเริ่มเก็บข้อมูล ({days_in} วันก่อน)" if days_in > 0 else "จุดเริ่มเก็บข้อมูล"
        add_milestone(in_label, "Inception", inception)

    # 2. 1 Month prior
    if one_month:
        add_milestone("1 เดือนก่อน (30 วัน)", "1M", one_month)

    # 3. 1 Week prior
    if one_week:
        add_milestone("1 สัปดาห์ก่อน (7 วัน)", "1W", one_week)

    # 4. 24h / Morning Benchmark
    if benchmark:
        b_label = "24 ชม. ก่อนหน้า (Benchmark 06:00)" if benchmark.get("label") == "0600" else f"Benchmark ก่อนหน้า ({benchmark.get('label', 'Snap')})"
        add_milestone(b_label, "24h", benchmark)

    # 5. Pre-Settle (if exists and different from benchmark)
    if pre_settle:
        t_short = pre_settle["snapshot_time_bkk"][11:16]
        add_milestone(f"ก่อน Settle ({t_short} น.)", "Pre-Settle", pre_settle)

    return {
        "snapshots_count": len(snaps),
        "milestones": milestones,
        "inception": inception,
        "one_month": one_month,
        "one_week": one_week,
        "benchmark": benchmark,
        "pre_settle": pre_settle,
    }


def analyze_news_sentiment(expiry_date: str, conn: sqlite3.Connection, days_back: int = 7) -> Dict[str, Any]:
    """
    Summarizes news and macroeconomic events in the period leading to expiry date.
    Determines overall market sentiment (Bullish 🐂, Bearish 🐻, or Neutral ⚖️).
    """
    exp_d = parse_date(expiry_date)
    start_d = (exp_d - timedelta(days=days_back)).strftime("%Y-%m-%d")

    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM events
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC, time_bkk ASC
        """,
        (start_d, expiry_date),
    )
    raw_events = [dict(r) for r in cur.fetchall()]

    if not raw_events:
        return {
            "has_news": False,
            "sentiment": "neutral",
            "sentiment_label": "⚖️ เป็นกลาง (ไม่มีข่าวสำคัญ)",
            "sentiment_badge": "⚖️ เป็นกลาง",
            "sentiment_class": "text-slate-400 bg-slate-800 border-slate-700",
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "high_impact_count": 0,
            "events": [],
            "summary_th": "ไม่มีข่าวหรือรายงานเศรษฐกิจระดับ High-Impact บันทึกไว้ในช่วงเวลาของ Expiry นี้",
        }

    bullish_score = 0
    bearish_score = 0
    high_count = 0
    enriched_events = []

    # Keywords for sentiment detection
    bull_keywords = ["rate cut", "dovish", "etf", "approval", "lower cpi", "cooling", "inflow", "stimulus", "gain", "surplus", "rebound"]
    bear_keywords = ["rate hike", "hawkish", "higher cpi", "hotter", "outflow", "ban", "tariff", "lawsuit", "dump", "unlock", "war", "recession"]

    for ev in raw_events:
        impact = ev.get("impact", "Low")
        weight = 3 if impact == "High" else (2 if impact == "Medium" else 1)
        if impact == "High":
            high_count += 1

        title_lower = (ev.get("event") or "").lower()
        ev_sentiment = "neutral"
        sentiment_tag = "⚖️ เป็นกลาง"

        # Check title keywords
        if any(kw in title_lower for kw in bull_keywords):
            ev_sentiment = "bullish"
            sentiment_tag = "🐂 กระทิง (ปัจจัยบวก)"
            bullish_score += weight
        elif any(kw in title_lower for kw in bear_keywords):
            ev_sentiment = "bearish"
            sentiment_tag = "🐻 หมี (ปัจจัยลบ)"
            bearish_score += weight
        else:
            # Check economic indicators
            if "cpi" in title_lower or "pce" in title_lower or "inflation" in title_lower:
                ev_sentiment = "bearish"
                sentiment_tag = "🐻 หมี (เงินเฟ้อกดดัน)"
                bearish_score += weight
            elif "fomc" in title_lower or "federal funds" in title_lower:
                sentiment_tag = "⚡ ผันผวนสูง (ดอกเบี้ย)"
            elif "unemployment" in title_lower or "claims" in title_lower:
                sentiment_tag = "📊 ตลาดแรงงาน"

        enriched_events.append({
            "date": ev.get("date"),
            "time_bkk": ev.get("time_bkk"),
            "event": ev.get("event"),
            "impact": impact,
            "country": ev.get("country", "USD"),
            "forecast": ev.get("forecast"),
            "previous": ev.get("previous"),
            "sentiment": ev_sentiment,
            "sentiment_tag": sentiment_tag,
        })

    # Overall sentiment determination
    if bullish_score > bearish_score + 1:
        sentiment = "bullish"
        sentiment_label = "🐂 กระทิง (Bullish Sentiment)"
        sentiment_badge = "🐂 กระทิง"
        sentiment_class = "text-emerald-400 bg-emerald-500/10 border-emerald-500/30"
        summary_th = f"บรรยากาศข่าวสารมหภาคเป็นเชิงบวกต่อตลาด คริปโตมีแรงหนุนและแรงซื้อผลักดัน (Bull Score: {bullish_score})"
    elif bearish_score > bullish_score + 1:
        sentiment = "bearish"
        sentiment_label = "🐻 หมี (Bearish Sentiment)"
        sentiment_badge = "🐻 หมี"
        sentiment_class = "text-rose-400 bg-rose-500/10 border-rose-500/30"
        summary_th = f"ข่าวสารและปัจจัยเศรษฐกิจมหภาคกดดันตลาด มีแรงเทขายจากความกังวล (Bear Score: {bearish_score})"
    else:
        sentiment = "neutral"
        sentiment_label = "⚖️ เป็นกลาง (Neutral Sentiment)"
        sentiment_badge = "⚖️ เป็นกลาง"
        sentiment_class = "text-slate-300 bg-slate-800 border-slate-700"
        summary_th = f"ปัจจัยข่าวบวกและลบหักล้างกัน หรือตลาดซึมซับข่าวก่อนหน้าเรียบร้อยแล้ว (มี {high_count} ข่าวระดับ High)"

    return {
        "has_news": len(raw_events) > 0,
        "sentiment": sentiment,
        "sentiment_label": sentiment_label,
        "sentiment_badge": sentiment_badge,
        "sentiment_class": sentiment_class,
        "bullish_count": bullish_score,
        "bearish_count": bearish_score,
        "neutral_count": len(raw_events) - (bullish_score + bearish_score),
        "high_impact_count": high_count,
        "events": enriched_events,
        "summary_th": summary_th,
    }


def analyze_pinning_and_gex_flow(expiry_result: Dict[str, Any], milestones_info: Dict[str, Any], news_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Computes precise Pinning Flow, GEX Sweep status, and Wall Distances.
    """
    settle = expiry_result.get("settlement_price")
    max_pain = expiry_result.get("max_pain")
    top_oi = expiry_result.get("top_oi_strike")
    c1 = expiry_result.get("call1_strike")
    p1 = expiry_result.get("put1_strike")
    gamma_flip = expiry_result.get("snap_gamma_flip")

    # 1. Benchmark Spot Price
    benchmark_snap = milestones_info.get("benchmark") or milestones_info.get("inception")
    spot_base = expiry_result.get("spot_at_snapshot")
    if spot_base is None and benchmark_snap:
        spot_base = benchmark_snap.get("spot")

    # 2. Pinning Flow Direction Analysis
    flow_status = "unknown"
    flow_label = "-"
    flow_badge_class = "bg-slate-700 text-slate-400"
    flow_desc = ""
    dist_improved_usd = 0.0
    dist_improved_pct = 0.0

    if settle and max_pain and spot_base:
        dist_before = abs(spot_base - max_pain)
        dist_after = abs(settle - max_pain)

        if dist_after < dist_before:
            flow_status = "flow_in"
            flow_label = "🎯 ไหลเข้า Pinning"
            flow_badge_class = "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
            dist_improved_usd = round(dist_before - dist_after, 2)
            dist_improved_pct = round(dist_improved_usd / max_pain * 100.0, 2)
            flow_desc = f"ราคาดึงดูดเข้าหา Max Pain (${max_pain:,.0f}) แคบลง +${dist_improved_usd:,.0f} (+{dist_improved_pct:.2f}%)"
        else:
            flow_status = "flow_out"
            flow_label = "💨 หลุด/ไหลออกห่าง"
            flow_badge_class = "bg-amber-500/20 text-amber-300 border border-amber-500/30"
            dist_worsened_usd = round(dist_after - dist_before, 2)
            dist_worsened_pct = round(dist_worsened_usd / max_pain * 100.0, 2)
            flow_desc = f"ราคาไหลหลุดห่างจาก Max Pain (${max_pain:,.0f}) กว้างขึ้น -${dist_worsened_usd:,.0f} (-{dist_worsened_pct:.2f}%)"

    # 3. Pinning Accuracy Status
    dist_mp_pct = expiry_result.get("dist_max_pain_pct")
    dist_mp_usd = expiry_result.get("dist_max_pain_usd")
    if dist_mp_pct is not None:
        abs_mp_pct = abs(dist_mp_pct)
        if abs_mp_pct <= 0.5:
            pin_badge = "🎯 แม่นยำสูง (±0.5%)"
            pin_badge_class = "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
            pin_summary = f"Settle สำเร็จในระยะแม่นยำสูง (ห่าง Max Pain เพียง {dist_mp_pct:+.2f}%)"
        elif abs_mp_pct <= 1.0:
            pin_badge = "✓ เข้าโซน (±1.0%)"
            pin_badge_class = "bg-teal-500/20 text-teal-300 border border-teal-500/30"
            pin_summary = f"Settle สำเร็จในโซน Pinning (ห่าง Max Pain {dist_mp_pct:+.2f}%)"
        else:
            pin_badge = f"✕ หลุด ({dist_mp_pct:+.2f}%)"
            pin_badge_class = "bg-rose-500/20 text-rose-300 border border-rose-500/30"
            pin_summary = f"Settle หลุดจุด Max Pain ห่าง {dist_mp_pct:+.2f}% (${dist_mp_usd:+,.2f})"
    else:
        pin_badge = "-"
        pin_badge_class = "bg-slate-700 text-slate-400"
        pin_summary = "ไม่มีข้อมูล Max Pain"

    # 4. GEX Sweep vs Containment Analysis
    gex_status = "unknown"
    gex_label = "-"
    gex_class = "bg-slate-700 text-slate-400"
    gex_desc = ""
    dist_c1_usd = round(settle - c1, 2) if (settle and c1) else None
    dist_c1_pct = round(dist_c1_usd / c1 * 100.0, 2) if (dist_c1_usd is not None and c1) else None
    dist_p1_usd = round(settle - p1, 2) if (settle and p1) else None
    dist_p1_pct = round(dist_p1_usd / p1 * 100.0, 2) if (dist_p1_usd is not None and p1) else None

    if settle and c1 and settle > c1:
        gex_status = "call_sweep"
        gex_label = f"🐂 Call Sweep (+{dist_c1_pct:.1f}%)"
        gex_class = "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
        gex_desc = f"เกิด Call Wall Sweep! ทะลุแนวต้าน Call 1 (${c1:,.0f}) ขึ้นไป +${dist_c1_usd:,.0f} (+{dist_c1_pct:.2f}%) ทำให้ Dealer ต้องเร่งซื้อ Spot Hedging (Short Gamma Squeeze)"
    elif settle and p1 and settle < p1:
        gex_status = "put_sweep"
        gex_label = f"🐻 Put Sweep ({dist_p1_pct:.1f}%)"
        gex_class = "bg-rose-500/20 text-rose-300 border border-rose-500/40"
        gex_desc = f"เกิด Put Wall Sweep! ทุบหลุดแนวรับ Put 1 (${p1:,.0f}) ลงไป ${dist_p1_usd:,.0f} ({dist_p1_pct:.2f}%) ตลาดเกิด Gamma Squeeze ฝั่งขาย"
    elif settle and p1 and c1:
        gex_status = "contained"
        gex_label = "🛡️ ในกรอบ GEX (P1–C1)"
        gex_class = "bg-blue-500/20 text-blue-300 border border-blue-500/30"
        gex_desc = f"ราคาวิ่งอยู่ในกรอบปลอดภัยระหว่าง Put Wall (${p1:,.0f}) ถึง Call Wall (${c1:,.0f}) ไม่เกิดการ Sweep หลุดแนวรับต้านหลัก"
    else:
        gex_status = "unknown"
        gex_label = "ไม่มีข้อมูลกรอบ"
        gex_class = "bg-slate-700 text-slate-400"
        gex_desc = "-"

    # 5. Top OI Dynamics
    top_oi_pinned = False
    dist_top_oi_pct = expiry_result.get("dist_top_oi_pct")
    dist_top_oi_usd = expiry_result.get("dist_top_oi_usd")
    if dist_top_oi_pct is not None and abs(dist_top_oi_pct) <= 1.0:
        top_oi_pinned = True

    return {
        "flow_status": flow_status,
        "flow_label": flow_label,
        "flow_badge_class": flow_badge_class,
        "flow_desc": flow_desc,
        "dist_improved_usd": dist_improved_usd,
        "dist_improved_pct": dist_improved_pct,
        "pin_badge": pin_badge,
        "pin_badge_class": pin_badge_class,
        "pin_summary": pin_summary,
        "gex_status": gex_status,
        "gex_label": gex_label,
        "gex_class": gex_class,
        "gex_desc": gex_desc,
        "dist_c1_usd": dist_c1_usd,
        "dist_c1_pct": dist_c1_pct,
        "dist_p1_usd": dist_p1_usd,
        "dist_p1_pct": dist_p1_pct,
        "top_oi_pinned": top_oi_pinned,
        "dist_top_oi_pct": dist_top_oi_pct,
        "dist_top_oi_usd": dist_top_oi_usd,
        "spot_base": spot_base,
    }


def get_full_settlement_history(conn: sqlite3.Connection, expiry_type: Optional[str] = None, event_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Retrieves all settled expiries with enriched evolution milestones,
    pinning flow dynamics, GEX sweep classifications, and news sentiment.
    """
    query = "SELECT * FROM expiry_results WHERE settlement_price IS NOT NULL"
    params = []

    if expiry_type and expiry_type != "all":
        query += " AND expiry_type = ?"
        params.append(expiry_type)

    if event_filter == "with_event":
        query += " AND has_high_impact_event = 1"
    elif event_filter == "without_event":
        query += " AND (has_high_impact_event = 0 OR has_high_impact_event IS NULL)"

    query += " ORDER BY expiry_date DESC"

    cur = conn.cursor()
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]

    enriched = []
    for r in rows:
        exp_date = r["expiry_date"]
        milestones_info = get_expiry_evolution_milestones(exp_date, conn)
        news_info = analyze_news_sentiment(exp_date, conn)
        flow_info = analyze_pinning_and_gex_flow(r, milestones_info, news_info)

        item = {
            **r,
            "milestones_info": milestones_info,
            "news_info": news_info,
            "flow_info": flow_info,
        }
        enriched.append(item)

    return enriched


def compute_settlement_flow_stats(enriched_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes summary KPI stats on Pinning Flow In, Exact/Zone Pinning,
    Call Sweep %, Put Sweep %, and GEX Range Containment.
    """
    total = len(enriched_results)
    if total == 0:
        return {}

    valid_mp = [r for r in enriched_results if r.get("max_pain") is not None]
    n_mp = len(valid_mp)

    # Pinning Flow In rate (did price move closer to max pain?)
    flow_in_count = sum(1 for r in valid_mp if r.get("flow_info", {}).get("flow_status") == "flow_in")
    flow_in_rate = round(flow_in_count / n_mp * 100.0, 1) if n_mp > 0 else 0.0

    # Exact Pin (±0.5%) & Zone Pin (±1.0%)
    exact_pin_count = sum(1 for r in valid_mp if abs(r.get("dist_max_pain_pct") or 999) <= 0.5)
    zone_pin_count = sum(1 for r in valid_mp if abs(r.get("dist_max_pain_pct") or 999) <= 1.0)
    exact_pin_rate = round(exact_pin_count / n_mp * 100.0, 1) if n_mp > 0 else 0.0
    zone_pin_rate = round(zone_pin_count / n_mp * 100.0, 1) if n_mp > 0 else 0.0

    # GEX Dynamics
    call_sweep_count = sum(1 for r in enriched_results if r.get("flow_info", {}).get("gex_status") == "call_sweep")
    put_sweep_count = sum(1 for r in enriched_results if r.get("flow_info", {}).get("gex_status") == "put_sweep")
    contained_count = sum(1 for r in enriched_results if r.get("flow_info", {}).get("gex_status") == "contained")

    call_sweep_rate = round(call_sweep_count / total * 100.0, 1)
    put_sweep_rate = round(put_sweep_count / total * 100.0, 1)
    contained_rate = round(contained_count / total * 100.0, 1)

    # Top OI Hit rate
    top_oi_count = sum(1 for r in enriched_results if abs(r.get("dist_top_oi_pct") or 999) <= 1.0)
    top_oi_rate = round(top_oi_count / total * 100.0, 1)

    # Average absolute distance to Max Pain
    avg_dist_mp = (
        sum(abs(r["dist_max_pain_pct"]) for r in valid_mp if r.get("dist_max_pain_pct") is not None) / n_mp
        if n_mp > 0 else 0.0
    )

    return {
        "total": total,
        "n_mp": n_mp,
        "flow_in_count": flow_in_count,
        "flow_in_rate": flow_in_rate,
        "exact_pin_count": exact_pin_count,
        "exact_pin_rate": exact_pin_rate,
        "zone_pin_count": zone_pin_count,
        "zone_pin_rate": zone_pin_rate,
        "contained_count": contained_count,
        "contained_rate": contained_rate,
        "call_sweep_count": call_sweep_count,
        "call_sweep_rate": call_sweep_rate,
        "put_sweep_count": put_sweep_count,
        "put_sweep_rate": put_sweep_rate,
        "top_oi_count": top_oi_count,
        "top_oi_rate": top_oi_rate,
        "avg_dist_mp": round(avg_dist_mp, 2),
    }


def compute_stats_for_subset(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Computes hit-rate and distance metrics across reference targets:
    Max Pain, Top OI, Call #1, Call #2, Put #1, Put #2, Gamma Flip, and Range (P1-C1 & P2-C2).
    """
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

    # Call 2 & Put 2
    valid_c2 = [r for r in records if r.get("call2_strike") is not None and r.get("call2_dist_pct") is not None]
    n_c2 = len(valid_c2)
    w05_c2 = sum(r.get("[call2_within_0.5pct]") or r.get("call2_within_0.5pct") or 0 for r in valid_c2)
    w1_c2 = sum(r.get("call2_within_1pct") or 0 for r in valid_c2)
    avg_dist_c2 = sum(abs(r["call2_dist_pct"]) for r in valid_c2) / n_c2 if n_c2 > 0 else 0.0

    valid_p2 = [r for r in records if r.get("put2_strike") is not None and r.get("put2_dist_pct") is not None]
    n_p2 = len(valid_p2)
    w05_p2 = sum(r.get("[put2_within_0.5pct]") or r.get("put2_within_0.5pct") or 0 for r in valid_p2)
    w1_p2 = sum(r.get("put2_within_1pct") or 0 for r in valid_p2)
    avg_dist_p2 = sum(abs(r["put2_dist_pct"]) for r in valid_p2) / n_p2 if n_p2 > 0 else 0.0

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

    # P2-C2 range
    valid_p2_c2 = [
        r for r in records
        if r.get("put2_strike") is not None and r.get("call2_strike") is not None and r.get("settlement_price") is not None
    ]
    n_p2_c2 = len(valid_p2_c2)
    in_p2_c2_count = sum(
        1 for r in valid_p2_c2
        if min(r["put2_strike"], r["call2_strike"]) <= r["settlement_price"] <= max(r["put2_strike"], r["call2_strike"])
    )
    avg_width_p2_c2 = (
        sum(
            abs(r["call2_strike"] - r["put2_strike"]) / min(r["put2_strike"], r["call2_strike"]) * 100.0
            for r in valid_p2_c2
        ) / n_p2_c2
        if n_p2_c2 > 0 else 0.0
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
            "n": n_c1,
        },
        "call2": {
            "pct_05": round(w05_c2 / n_c2 * 100.0, 1) if n_c2 > 0 else 0.0,
            "pct_1": round(w1_c2 / n_c2 * 100.0, 1) if n_c2 > 0 else 0.0,
            "avg_dist": round(avg_dist_c2, 2),
            "n": n_c2,
        },
        "put1": {
            "pct_05": round(w05_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
            "pct_1": round(w1_p1 / n_p1 * 100.0, 1) if n_p1 > 0 else 0.0,
            "avg_dist": round(avg_dist_p1, 2),
            "n": n_p1,
        },
        "put2": {
            "pct_05": round(w05_p2 / n_p2 * 100.0, 1) if n_p2 > 0 else 0.0,
            "pct_1": round(w1_p2 / n_p2 * 100.0, 1) if n_p2 > 0 else 0.0,
            "avg_dist": round(avg_dist_p2, 2),
            "n": n_p2,
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
        "range_p2_c2": {
            "in_range_pct": round(in_p2_c2_count / n_p2_c2 * 100.0, 1) if n_p2_c2 > 0 else 0.0,
            "avg_width": round(avg_width_p2_c2, 2),
            "n_range": n_p2_c2,
        },
    }


def compute_weekly_horizon_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Computes 7-Day Weekly Expiry & Range Migration Statistics:
    - Baseline (D-7 or earliest snapshot): BTC Spot, P1, P2, C1, C2, Range Widths
    - Final (D-0 / Settlement): Settle Price, BTC % change, P1/P2 Shifts, C1/C2 Shifts
    - 7-Day Range Containment: Did price settle within the initial 7-day P1-C1 & P2-C2 range?
    - Tracks both settled weekly contracts and active weekly contracts currently underway.
    """
    cur = conn.cursor()

    # 1. Get all snapshots ordered by time
    cur.execute("""
        SELECT expiry_date, expiry_type, snapshot_time_bkk, index_price,
               call1_strike, call2_strike, put1_strike, put2_strike,
               max_pain, total_oi, call1_oi, call2_oi, put1_oi, put2_oi
        FROM snapshots
        ORDER BY snapshot_time_bkk ASC
    """)
    all_snaps = [dict(r) for r in cur.fetchall()]

    # 2. Get all settled results
    cur.execute("SELECT * FROM expiry_results WHERE settlement_price IS NOT NULL ORDER BY expiry_date DESC")
    settled_map = {r["expiry_date"]: dict(r) for r in cur.fetchall()}

    # Group snapshots by expiry_date
    by_expiry: Dict[str, List[Dict[str, Any]]] = {}
    for s in all_snaps:
        exp = s["expiry_date"]
        by_expiry.setdefault(exp, []).append(s)

    weekly_items = []
    sorted_expiries = sorted(by_expiry.keys())

    for exp in sorted_expiries:
        snaps = by_expiry[exp]
        try:
            dt = datetime.strptime(exp, "%Y-%m-%d")
        except Exception:
            continue

        is_friday = (dt.weekday() == 4)
        exp_type = snaps[0].get("expiry_type") or ("weekly" if is_friday else "daily")

        # Focus on Friday expiries or weekly/monthly contracts
        if not is_friday and exp_type not in ("weekly", "monthly"):
            continue

        base_snap = snaps[0]
        latest_snap = snaps[-1]
        settle_res = settled_map.get(exp)

        is_settled = settle_res is not None and settle_res.get("settlement_price") is not None
        settle_price = settle_res["settlement_price"] if is_settled else None

        base_spot = base_snap["index_price"]
        base_p1 = base_snap.get("put1_strike")
        base_p2 = base_snap.get("put2_strike")
        base_c1 = base_snap.get("call1_strike")
        base_c2 = base_snap.get("call2_strike")
        base_mp = base_snap.get("max_pain")

        # Inner range (P1 to C1) width %
        base_width = round((base_c1 - base_p1) / base_p1 * 100, 2) if (base_c1 and base_p1) else None

        # P2 to C2 direct width %
        base_width_p2_c2 = round((base_c2 - base_p2) / base_p2 * 100, 2) if (base_c2 and base_p2) else None

        # Outer envelope boundary: min(P1, P2) to max(C1, C2)
        lower_outer = min(p for p in [base_p1, base_p2] if p is not None) if (base_p1 or base_p2) else base_p1
        upper_outer = max(c for c in [base_c1, base_c2] if c is not None) if (base_c1 or base_c2) else base_c1
        base_outer_width = round((upper_outer - lower_outer) / lower_outer * 100, 2) if (lower_outer and upper_outer) else None

        # Final / latest values
        final_p1 = settle_res.get("put1_strike") if is_settled else latest_snap.get("put1_strike")
        final_p2 = settle_res.get("put2_strike") if is_settled else latest_snap.get("put2_strike")
        final_c1 = settle_res.get("call1_strike") if is_settled else latest_snap.get("call1_strike")
        final_c2 = settle_res.get("call2_strike") if is_settled else latest_snap.get("call2_strike")
        final_mp = settle_res.get("max_pain") if is_settled else latest_snap.get("max_pain")
        current_spot = settle_price if is_settled else latest_snap.get("index_price")

        # 7-day BTC Price Change & Wall Shifts
        btc_chg_usd = round(current_spot - base_spot, 2) if (current_spot and base_spot) else 0.0
        btc_chg_pct = round(btc_chg_usd / base_spot * 100, 2) if (btc_chg_usd and base_spot) else 0.0

        p1_shift_usd = round(final_p1 - base_p1, 2) if (final_p1 and base_p1) else 0.0
        p1_shift_pct = round(p1_shift_usd / base_p1 * 100, 2) if (p1_shift_usd and base_p1) else 0.0

        p2_shift_usd = round(final_p2 - base_p2, 2) if (final_p2 and base_p2) else 0.0
        p2_shift_pct = round(p2_shift_usd / base_p2 * 100, 2) if (p2_shift_usd and base_p2) else 0.0

        c1_shift_usd = round(final_c1 - base_c1, 2) if (final_c1 and base_c1) else 0.0
        c1_shift_pct = round(c1_shift_usd / base_c1 * 100, 2) if (c1_shift_usd and base_c1) else 0.0

        c2_shift_usd = round(final_c2 - base_c2, 2) if (final_c2 and base_c2) else 0.0
        c2_shift_pct = round(c2_shift_usd / base_c2 * 100, 2) if (c2_shift_usd and base_c2) else 0.0

        # Containment & Wall Distance Evaluation
        settled_in_7d_range = None
        settled_in_p2_c2 = None
        settled_in_outer_range = None
        status = "in_progress"
        badge = "⏳ กำลังติดตาม"
        badge_class = "bg-amber-500/20 text-amber-300 border border-amber-500/30"

        dist_to_c1_pct = round((current_spot - base_c1) / base_c1 * 100, 2) if (base_c1 and current_spot) else None
        dist_to_c2_pct = round((current_spot - base_c2) / base_c2 * 100, 2) if (base_c2 and current_spot) else None
        dist_to_p1_pct = round((current_spot - base_p1) / base_p1 * 100, 2) if (base_p1 and current_spot) else None
        dist_to_p2_pct = round((current_spot - base_p2) / base_p2 * 100, 2) if (base_p2 and current_spot) else None

        if is_settled and base_p1 and base_c1 and settle_price:
            if base_p1 <= settle_price <= base_c1:
                settled_in_7d_range = True
                status = "contained"
                badge = "🛡️ Settle ในกรอบ P1–C1"
                badge_class = "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
            elif settle_price > base_c1:
                settled_in_7d_range = False
                status = "call_breakout"
                badge = f"🐂 ทะลุ Call #1 (+{dist_to_c1_pct}%)"
                badge_class = "bg-rose-500/20 text-rose-300 border border-rose-500/40"
            else:
                settled_in_7d_range = False
                status = "put_breakdown"
                badge = f"🐻 หลุด Put #1 ({dist_to_p1_pct}%)"
                badge_class = "bg-rose-500/20 text-rose-300 border border-rose-500/40"

            if base_p2 and base_c2:
                low_p2_c2 = min(base_p2, base_c2)
                high_p2_c2 = max(base_p2, base_c2)
                settled_in_p2_c2 = (low_p2_c2 <= settle_price <= high_p2_c2)

            if lower_outer and upper_outer:
                settled_in_outer_range = (lower_outer <= settle_price <= upper_outer)
        elif not is_settled and base_p1 and base_c1 and current_spot:
            if base_p1 <= current_spot <= base_c1:
                badge = "🛡️ ปัจจุบันอยู่ในกรอบ P1–C1"
                badge_class = "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
            elif current_spot > base_c1:
                badge = f"⚠️ ปัจจุบันเหนือ Call #1 (+{dist_to_c1_pct}%)"
                badge_class = "bg-amber-500/20 text-amber-300 border border-amber-500/40"
            else:
                badge = f"⚠️ ปัจจุบันใต้ Put #1 ({dist_to_p1_pct}%)"
                badge_class = "bg-rose-500/20 text-rose-300 border border-rose-500/40"

        base_dt = datetime.strptime(base_snap["snapshot_time_bkk"][:10], "%Y-%m-%d")
        exp_dt = datetime.strptime(exp, "%Y-%m-%d")
        tracking_days = max(1, (exp_dt - base_dt).days)

        weekly_items.append({
            "expiry_date": exp,
            "expiry_type": exp_type,
            "is_settled": is_settled,
            "settle_price": settle_price,
            "current_spot": current_spot,
            "tracking_days": tracking_days,
            "base_date": base_snap["snapshot_time_bkk"],
            "base_spot": base_spot,
            "base_p1": base_p1,
            "base_p2": base_p2,
            "base_c1": base_c1,
            "base_c2": base_c2,
            "base_mp": base_mp,
            "base_width": base_width,
            "base_width_p2_c2": base_width_p2_c2,
            "base_outer_width": base_outer_width,
            "lower_outer": lower_outer,
            "upper_outer": upper_outer,
            "final_p1": final_p1,
            "final_p2": final_p2,
            "final_c1": final_c1,
            "final_c2": final_c2,
            "final_mp": final_mp,
            "btc_chg_usd": btc_chg_usd,
            "btc_chg_pct": btc_chg_pct,
            "p1_shift_usd": p1_shift_usd,
            "p1_shift_pct": p1_shift_pct,
            "p2_shift_usd": p2_shift_usd,
            "p2_shift_pct": p2_shift_pct,
            "c1_shift_usd": c1_shift_usd,
            "c1_shift_pct": c1_shift_pct,
            "c2_shift_usd": c2_shift_usd,
            "c2_shift_pct": c2_shift_pct,
            "settled_in_7d_range": settled_in_7d_range,
            "settled_in_p2_c2": settled_in_p2_c2,
            "settled_in_outer_range": settled_in_outer_range,
            "status": status,
            "badge": badge,
            "badge_class": badge_class,
            "dist_to_c1_pct": dist_to_c1_pct,
            "dist_to_c2_pct": dist_to_c2_pct,
            "dist_to_p1_pct": dist_to_p1_pct,
            "dist_to_p2_pct": dist_to_p2_pct,
        })

    # Summary metrics for settled weekly items
    settled_items = [w for w in weekly_items if w["is_settled"] and w["settled_in_7d_range"] is not None]
    n_settled = len(settled_items)
    contained_count = sum(1 for w in settled_items if w["settled_in_7d_range"] is True)
    contained_7d_rate = round(contained_count / n_settled * 100.0, 1) if n_settled > 0 else 100.0
    
    contained_p2_c2_count = sum(1 for w in settled_items if w.get("settled_in_p2_c2") is True)
    contained_p2_c2_7d_rate = round(contained_p2_c2_count / n_settled * 100.0, 1) if n_settled > 0 else 100.0
    
    contained_outer_count = sum(1 for w in settled_items if w.get("settled_in_outer_range") is True)
    contained_outer_7d_rate = round(contained_outer_count / n_settled * 100.0, 1) if n_settled > 0 else 100.0

    avg_width_7d = round(sum(w["base_width"] for w in settled_items if w["base_width"]) / n_settled, 2) if n_settled > 0 else 14.7
    avg_outer_width_7d = round(sum(w["base_outer_width"] for w in settled_items if w.get("base_outer_width")) / n_settled, 2) if n_settled > 0 else 18.5
    avg_width_p2_c2_7d = round(sum(w["base_width_p2_c2"] for w in settled_items if w.get("base_width_p2_c2")) / n_settled, 2) if n_settled > 0 else 16.0

    avg_c1_shift_pct = round(sum(abs(w["c1_shift_pct"]) for w in settled_items) / n_settled, 2) if n_settled > 0 else 0.0
    avg_c2_shift_pct = round(sum(abs(w["c2_shift_pct"]) for w in settled_items) / n_settled, 2) if n_settled > 0 else 0.0
    avg_p1_shift_pct = round(sum(abs(w["p1_shift_pct"]) for w in settled_items) / n_settled, 2) if n_settled > 0 else 0.0
    avg_p2_shift_pct = round(sum(abs(w["p2_shift_pct"]) for w in settled_items) / n_settled, 2) if n_settled > 0 else 0.0

    # Active items currently underway
    active_items = [w for w in weekly_items if not w["is_settled"]]
    current_weekly = active_items[0] if active_items else None

    return {
        "weekly_items": list(reversed(weekly_items)),
        "settled_items": settled_items,
        "active_items": active_items,
        "current_weekly": current_weekly,
        "n_settled": n_settled,
        "contained_count": contained_count,
        "contained_7d_rate": contained_7d_rate,
        "contained_p2_c2_7d_rate": contained_p2_c2_7d_rate,
        "contained_outer_7d_rate": contained_outer_7d_rate,
        "avg_width_7d": avg_width_7d,
        "avg_outer_width_7d": avg_outer_width_7d,
        "avg_width_p2_c2_7d": avg_width_p2_c2_7d,
        "avg_c1_shift_pct": avg_c1_shift_pct,
        "avg_c2_shift_pct": avg_c2_shift_pct,
        "avg_p1_shift_pct": avg_p1_shift_pct,
        "avg_p2_shift_pct": avg_p2_shift_pct,
    }
