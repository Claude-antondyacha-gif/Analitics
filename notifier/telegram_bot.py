"""
Telegram notifier — sends daily analytics report to a Telegram chat.
"""
import os
import logging
import requests
from datetime import date

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def _fmt(val, prefix="", suffix="", decimals=2) -> str:
    if val is None or val == 0:
        return "—"
    return f"{prefix}{val:,.{decimals}f}{suffix}".replace(",", " ")


def _pct_change(now, prev) -> str:
    if not prev or not now:
        return ""
    change = (now - prev) / prev * 100
    arrow = "🔺" if change > 0 else "🔻"
    return f" {arrow}{abs(change):.0f}%"


def build_daily_report() -> str:
    from storage.database import get_aggregated_metrics, get_campaigns_list, get_metrics_by_period

    today = date.today()
    periods = [
        ("Вчора", 1),
        ("3 дні", 3),
        ("7 днів", 7),
        ("14 днів", 14),
        ("Місяць", 30),
    ]

    lines = [
        f"📊 <b>Meta Ads — Щоденний звіт</b>",
        f"📅 {today.strftime('%d.%m.%Y')}",
        "",
    ]

    # ── Summary by period ──────────────────────────────────────────
    lines.append("═══ ЗАГАЛЬНІ ПОКАЗНИКИ ═══")
    for label, days in periods:
        s = get_aggregated_metrics(days=days)
        spend = s.get("total_spend") or 0
        leads = s.get("total_leads") or 0
        cpl = s.get("cost_per_lead") or 0
        ctr = s.get("avg_ctr") or 0
        cpc = s.get("avg_cpc") or 0
        impressions = s.get("total_impressions") or 0
        roas = s.get("roas") or 0
        purchases = s.get("total_purchases") or 0
        video = s.get("total_video_views") or 0
        campaigns = s.get("active_campaigns") or 0

        lines.append(f"\n📆 <b>{label}</b>")
        lines.append(f"  💰 Витрати: <b>${spend:,.2f}</b>")
        lines.append(f"  🎯 Ліди: <b>{leads}</b>  |  CPL: <b>${cpl:,.2f}</b>")
        lines.append(f"  👆 CTR: <b>{ctr:.2f}%</b>  |  CPC: <b>${cpc:.2f}</b>")
        lines.append(f"  👁 Покази: <b>{impressions:,}</b>")
        if roas > 0:
            lines.append(f"  📈 ROAS: <b>{roas}x</b>  |  🛒 Покупки: <b>{purchases}</b>")
        if video > 0:
            lines.append(f"  ▶️ Відео: <b>{video:,}</b>")
        lines.append(f"  📊 Кампаній: <b>{campaigns}</b>")

    # ── Per campaign (yesterday) ────────────────────────────────────
    rows = get_metrics_by_period(days=1)
    if rows:
        lines.append("\n═══ КАМПАНІЇ (вчора) ═══")
        # aggregate per campaign
        stats: dict = {}
        for r in rows:
            cid = r["campaign_id"]
            if cid not in stats:
                stats[cid] = {
                    "name": r["campaign_name"], "spend": 0,
                    "leads": 0, "clicks": 0, "impressions": 0,
                    "ctrs": [], "cpcs": [],
                }
            s = stats[cid]
            s["spend"] += r["spend"]
            s["leads"] += r["leads"]
            s["clicks"] += r["clicks"]
            s["impressions"] += r["impressions"]
            if r["ctr"]: s["ctrs"].append(r["ctr"])
            if r["cpc"]: s["cpcs"].append(r["cpc"])

        sorted_camps = sorted(stats.values(), key=lambda x: x["spend"], reverse=True)
        for s in sorted_camps[:10]:  # top 10
            avg_ctr = sum(s["ctrs"]) / len(s["ctrs"]) if s["ctrs"] else 0
            cpl = s["spend"] / s["leads"] if s["leads"] > 0 else 0
            name = s["name"][:35] + "…" if len(s["name"]) > 35 else s["name"]
            lines.append(f"\n▸ <b>{name}</b>")
            lines.append(f"  💸 ${s['spend']:.2f}  |  🎯 {s['leads']} лідів" + (f"  |  CPL ${cpl:.2f}" if cpl else ""))
            lines.append(f"  CTR {avg_ctr:.2f}%  |  Кліки {s['clicks']:,}")

    # ── AI Analysis (latest recommendation) ───────────────────────
    try:
        from storage.database import get_latest_recommendations
        recs = get_latest_recommendations(limit=1)
        if recs:
            r = recs[0]
            lines.append("\n═══ AI АНАЛІЗ ═══")
            lines.append(f"📝 {r['summary']}")

            alerts = r.get("critical_alerts", [])
            if alerts:
                lines.append("\n🚨 <b>Критичні алерти:</b>")
                for a in alerts[:3]:
                    lines.append(f"  • {a}")

            recommendations = r.get("recommendations", [])
            if recommendations:
                lines.append("\n💡 <b>Рекомендації:</b>")
                for rec in recommendations[:4]:
                    lines.append(f"  • {rec}")
    except Exception:
        pass

    lines.append("\n" + "─" * 30)
    lines.append("🤖 Sfero Analytics Bot | автозвіт")

    return "\n".join(lines)


def send_daily_report() -> bool:
    try:
        text = build_daily_report()
        # Telegram limit is 4096 chars
        if len(text) > 4000:
            text = text[:3950] + "\n\n<i>...звіт скорочено</i>"
        return send_message(text)
    except Exception as e:
        logger.error(f"Failed to build/send report: {e}", exc_info=True)
        return False


def send_alert(message: str) -> bool:
    return send_message(f"🚨 <b>ALERT</b>\n\n{message}")


def send_test_message() -> bool:
    return send_message("✅ <b>Meta Analytics Bot</b>\n\nПідключення успішне! Щоденні звіти будуть надходити о 07:05 за Києвом.")
