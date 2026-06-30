"""
Meta Ads data collector — fetches campaigns, ad sets, and insights from the Meta Marketing API.
"""
import os
import json
import logging
from datetime import datetime, timedelta, date

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign

from storage.database import upsert_campaign, upsert_daily_metrics, init_db

logger = logging.getLogger(__name__)

INSIGHT_FIELDS = [
    "campaign_id", "campaign_name", "adset_id", "adset_name",
    "impressions", "clicks", "spend", "reach", "frequency",
    "ctr", "cpc", "cpm", "cpp",
    "actions", "action_values", "cost_per_action_type",
    "video_30_sec_watched_actions", "post_engagement",
    "date_start", "date_stop",
]

CAMPAIGN_FIELDS = [
    "id", "name", "status", "objective",
    "daily_budget", "lifetime_budget", "created_time",
]


def _init_api():
    FacebookAdsApi.init(
        app_id=os.environ.get("META_APP_ID"),
        app_secret=os.environ.get("META_APP_SECRET"),
        access_token=os.environ["META_ACCESS_TOKEN"],
    )


def _extract_action(actions: list, action_type: str) -> int:
    if not actions:
        return 0
    for a in actions:
        if a.get("action_type") == action_type:
            return int(float(a.get("value", 0)))
    return 0


def _extract_action_value(action_values: list, action_type: str) -> float:
    if not action_values:
        return 0.0
    for a in action_values:
        if a.get("action_type") == action_type:
            return float(a.get("value", 0))
    return 0.0


def _extract_cost_per(cost_per: list, action_type: str) -> float:
    if not cost_per:
        return 0.0
    for a in cost_per:
        if a.get("action_type") == action_type:
            return float(a.get("value", 0))
    return 0.0


def _parse_insight_row(row: dict) -> dict:
    actions = row.get("actions", [])
    action_values = row.get("action_values", [])
    cost_per = row.get("cost_per_action_type", [])

    leads = _extract_action(actions, "lead") or _extract_action(actions, "onsite_conversion.lead_grouped")
    link_clicks = _extract_action(actions, "link_click")
    page_likes = _extract_action(actions, "like")
    post_engagements = int(float(row.get("post_engagement", 0) or 0))
    video_views = _extract_action(actions, "video_view")
    purchases = _extract_action(actions, "purchase") or _extract_action(actions, "omni_purchase")
    purchase_value = _extract_action_value(action_values, "purchase") or _extract_action_value(action_values, "omni_purchase")
    cost_per_lead = _extract_cost_per(cost_per, "lead") or _extract_cost_per(cost_per, "onsite_conversion.lead_grouped")
    cost_per_link_click = _extract_cost_per(cost_per, "link_click")

    spend = float(row.get("spend", 0) or 0)
    purchase_roas = round(purchase_value / spend, 2) if spend > 0 else 0

    return {
        "date": row.get("date_start"),
        "campaign_id": row.get("campaign_id", ""),
        "campaign_name": row.get("campaign_name", ""),
        "adset_id": row.get("adset_id", ""),
        "adset_name": row.get("adset_name", ""),
        "impressions": int(row.get("impressions", 0) or 0),
        "clicks": int(row.get("clicks", 0) or 0),
        "spend": spend,
        "reach": int(row.get("reach", 0) or 0),
        "frequency": float(row.get("frequency", 0) or 0),
        "ctr": float(row.get("ctr", 0) or 0),
        "cpc": float(row.get("cpc", 0) or 0),
        "cpm": float(row.get("cpm", 0) or 0),
        "cpp": float(row.get("cpp", 0) or 0),
        "leads": leads,
        "link_clicks": link_clicks,
        "post_engagements": post_engagements,
        "video_views": video_views,
        "page_likes": page_likes,
        "cost_per_lead": cost_per_lead,
        "cost_per_link_click": cost_per_link_click,
        "purchase_roas": purchase_roas,
        "purchases": purchases,
        "purchase_value": purchase_value,
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def fetch_campaigns() -> list[dict]:
    account = AdAccount(f"act_{os.environ['META_AD_ACCOUNT_ID'].lstrip('act_')}")
    campaigns = account.get_campaigns(fields=CAMPAIGN_FIELDS)
    result = []
    for c in campaigns:
        data = dict(c)
        upsert_campaign(data)
        result.append(data)
    logger.info(f"Fetched {len(result)} campaigns")
    return result


def fetch_insights_for_period(date_from: date, date_to: date) -> int:
    account = AdAccount(f"act_{os.environ['META_AD_ACCOUNT_ID'].lstrip('act_')}")

    params = {
        "level": "adset",
        "time_range": {
            "since": date_from.isoformat(),
            "until": date_to.isoformat(),
        },
        "time_increment": 1,
        "fields": INSIGHT_FIELDS,
        "limit": 500,
    }

    insights = account.get_insights(params=params)
    count = 0
    for row in insights:
        parsed = _parse_insight_row(dict(row))
        upsert_daily_metrics(parsed)
        count += 1

    logger.info(f"Saved {count} insight rows for {date_from} → {date_to}")
    return count


def run_daily_collection():
    """Main entry point called by scheduler — fetches yesterday's data."""
    _init_api()
    init_db()

    yesterday = date.today() - timedelta(days=1)
    fetch_campaigns()
    count = fetch_insights_for_period(yesterday, yesterday)
    logger.info(f"Daily collection complete: {count} rows saved for {yesterday}")
    return count


def run_historical_backfill(days: int = 30):
    """One-time backfill for the last N days."""
    _init_api()
    init_db()

    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days - 1)
    fetch_campaigns()
    count = fetch_insights_for_period(date_from, date_to)
    logger.info(f"Backfill complete: {count} rows for last {days} days")
    return count
