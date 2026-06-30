"""
AI Agent powered by Claude — analyzes Meta Ads data, generates recommendations,
detects critical issues, and can execute actions via the Meta API.
"""
import os
import json
import logging
from datetime import datetime

import anthropic

from storage.database import (
    get_metrics_by_period, get_aggregated_metrics,
    save_recommendation, get_campaigns_list,
    log_action, update_action_status,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ти — експертний AI-аналітик рекламних кампаній у Meta Ads (Facebook/Instagram).
Ти аналізуєш дані рекламного кабінету і:
1. Виявляєш проблеми та можливості
2. Даєш конкретні рекомендації з пріоритетами (HIGH / MEDIUM / LOW)
3. Визначаєш критичні ситуації що потребують негайних дій
4. Пропонуєш гіпотези для покращення результатів
5. При необхідності пропонуєш конкретні дії: вимкнути кампанію, змінити бюджет тощо

Відповідай ТІЛЬКИ JSON-об'єктом наступної структури:
{
  "summary": "Короткий огляд 2-3 речення",
  "critical_alerts": [
    {"level": "CRITICAL|WARNING", "campaign_id": "...", "campaign_name": "...", "message": "...", "suggested_action": "pause|reduce_budget|none"}
  ],
  "recommendations": [
    {"priority": "HIGH|MEDIUM|LOW", "category": "budget|creative|targeting|bidding|other", "campaign_id": "...", "campaign_name": "...", "text": "..."}
  ],
  "hypotheses": [
    {"title": "...", "description": "...", "expected_impact": "..."}
  ],
  "actions_to_execute": [
    {"action_type": "pause_campaign|enable_campaign|set_budget", "target_id": "...", "target_name": "...", "reason": "...", "value": null}
  ]
}

Якщо немає даних — поверни порожні масиви, але завжди заповни summary."""

ANALYSIS_TOOL = {
    "name": "get_ads_data",
    "description": "Отримати агреговані метрики та детальні дані по кампаніях",
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Кількість днів для аналізу"},
            "campaign_id": {"type": "string", "description": "ID конкретної кампанії (опціонально)"},
        },
        "required": ["days"],
    },
}


def _build_data_summary(days: int, campaign_id: str = None) -> str:
    agg = get_aggregated_metrics(days=days, campaign_id=campaign_id)
    rows = get_metrics_by_period(days=days, campaign_id=campaign_id)
    campaigns = get_campaigns_list()

    # group by campaign
    campaign_stats: dict[str, dict] = {}
    for r in rows:
        cid = r["campaign_id"]
        if cid not in campaign_stats:
            campaign_stats[cid] = {
                "name": r["campaign_name"], "spend": 0, "impressions": 0,
                "clicks": 0, "leads": 0, "purchases": 0, "purchase_value": 0,
                "link_clicks": 0, "page_likes": 0, "video_views": 0,
                "ctr_sum": 0, "cpc_sum": 0, "rows": 0,
            }
        s = campaign_stats[cid]
        s["spend"] += r["spend"]
        s["impressions"] += r["impressions"]
        s["clicks"] += r["clicks"]
        s["leads"] += r["leads"]
        s["purchases"] += r["purchases"]
        s["purchase_value"] += r["purchase_value"]
        s["link_clicks"] += r["link_clicks"]
        s["page_likes"] += r["page_likes"]
        s["video_views"] += r["video_views"]
        s["ctr_sum"] += r["ctr"]
        s["cpc_sum"] += r["cpc"]
        s["rows"] += 1

    camp_lines = []
    for cid, s in campaign_stats.items():
        n = s["rows"]
        avg_ctr = round(s["ctr_sum"] / n, 2) if n else 0
        avg_cpc = round(s["cpc_sum"] / n, 2) if n else 0
        cpl = round(s["spend"] / s["leads"], 2) if s["leads"] > 0 else 0
        cpp_val = round(s["spend"] / s["purchases"], 2) if s["purchases"] > 0 else 0
        roas = round(s["purchase_value"] / s["spend"], 2) if s["spend"] > 0 else 0

        status = next((c["status"] for c in campaigns if c["id"] == cid), "UNKNOWN")
        objective = next((c["objective"] for c in campaigns if c["id"] == cid), "UNKNOWN")

        camp_lines.append(
            f"  Campaign: {s['name']} (id:{cid}, status:{status}, objective:{objective})\n"
            f"    Витрати: ${s['spend']:.2f} | Охоплення: {s['impressions']} | Кліки: {s['clicks']}\n"
            f"    CTR: {avg_ctr}% | CPC: ${avg_cpc} | Ліди: {s['leads']} | CPL: ${cpl}\n"
            f"    Покупки: {s['purchases']} | CPP: ${cpp_val} | ROAS: {roas}\n"
            f"    Підписники: {s['page_likes']} | Переглядів відео: {s['video_views']}"
        )

    return (
        f"=== АНАЛІЗ ЗА {days} ДНІВ ===\n"
        f"Загальні витрати: ${agg.get('total_spend', 0):.2f}\n"
        f"Загальне охоплення: {agg.get('total_impressions', 0)}\n"
        f"Загальні кліки: {agg.get('total_clicks', 0)}\n"
        f"Загальні ліди: {agg.get('total_leads', 0)}\n"
        f"Середній CTR: {agg.get('avg_ctr', 0):.2f}%\n"
        f"Середній CPC: ${agg.get('avg_cpc', 0):.2f}\n"
        f"Вартість ліда: ${agg.get('cost_per_lead', 0):.2f}\n"
        f"Активних кампаній: {agg.get('active_campaigns', 0)}\n\n"
        f"По кампаніях:\n" + "\n".join(camp_lines)
    )


def analyze(period_label: str = "7d", days: int = 7,
            campaign_id: str = None, custom_question: str = None) -> dict:
    """
    Run AI analysis for the given period.
    Returns the parsed JSON from Claude.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    data_summary = _build_data_summary(days=days, campaign_id=campaign_id)

    user_msg = (
        f"Проаналізуй рекламні кампанії за останні {days} днів.\n\n"
        f"{data_summary}\n\n"
        + (f"Додаткове питання: {custom_question}" if custom_question else "")
    )

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()

    # extract JSON even if wrapped in markdown code block
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Claude returned non-JSON, wrapping in default structure")
        result = {
            "summary": raw[:500],
            "critical_alerts": [],
            "recommendations": [],
            "hypotheses": [],
            "actions_to_execute": [],
        }

    # Save to DB
    save_recommendation(
        period=period_label,
        analysis_type="auto" if not custom_question else "query",
        summary=result.get("summary", ""),
        recommendations=result.get("recommendations", []),
        critical_alerts=result.get("critical_alerts", []),
        campaign_ids=[campaign_id] if campaign_id else [],
        raw=raw,
    )

    return result


def execute_suggested_actions(analysis_result: dict, auto_execute: bool = False):
    """
    Log suggested actions to DB. If auto_execute=True AND action is critical,
    actually call Meta API to pause campaign.
    """
    actions = analysis_result.get("actions_to_execute", [])
    if not actions:
        return []

    from collector.meta_collector import _init_api
    from facebook_business.adobjects.campaign import Campaign

    executed = []
    for action in actions:
        action_type = action.get("action_type")
        target_id = action.get("target_id", "")
        target_name = action.get("target_name", "")
        reason = action.get("reason", "")

        action_id = log_action(action_type, target_id, target_name, reason)

        if auto_execute and action_type in ("pause_campaign", "enable_campaign"):
            try:
                _init_api()
                camp = Campaign(target_id)
                new_status = (
                    Campaign.Status.paused if action_type == "pause_campaign"
                    else Campaign.Status.active
                )
                camp.api_update(fields=[], params={"status": new_status})
                update_action_status(action_id, "executed", f"Status set to {new_status}")
                executed.append({"action_id": action_id, "status": "executed", **action})
            except Exception as e:
                update_action_status(action_id, "failed", str(e))
                executed.append({"action_id": action_id, "status": "failed", "error": str(e), **action})
        else:
            executed.append({"action_id": action_id, "status": "pending", **action})

    return executed


def chat(user_message: str, days_context: int = 7) -> str:
    """
    Free-form chat with the AI agent about the current ad account.
    Returns a human-readable Ukrainian response.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    data_summary = _build_data_summary(days=days_context)

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=(
            "Ти — AI-асистент з реклами у Meta Ads. Відповідай українською мовою. "
            "Будь конкретним, лаконічним, давай практичні поради."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Поточні дані рекламного кабінету (останні {days_context} днів):\n"
                f"{data_summary}\n\n"
                f"Запит: {user_message}"
            ),
        }],
    )
    return response.content[0].text
