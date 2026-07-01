"""
Meta Ads data collector — fetches all ad accounts from Business Managers,
then pulls campaign insights from the Meta Marketing API.
"""
import os
import json
import logging
from datetime import datetime, timedelta, date

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.business import Business
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign

from storage.database import upsert_campaign, upsert_daily_metrics, init_db

logger = logging.getLogger(__name__)

INSIGHT_FIELDS = [
    "campaign_id", "campaign_name",
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

ACCOUNT_FIELDS = ["id", "name", "account_status", "currency", "timezone_name"]


def _init_api():
    FacebookAdsApi.init(access_token=os.environ["META_ACCESS_TOKEN"])


def get_ad_accounts_from_bm(bm_id: str) -> list[dict]:
    """Return all ad accounts under a Business Manager (owned + client)."""
    result = []
    seen = set()
    business = Business(bm_id)

    # Owned accounts
    try:
        for acc in business.get_owned_ad_accounts(fields=ACCOUNT_FIELDS):
            d = dict(acc)
            if d.get("id") not in seen:
                d["account_source"] = "owned"
                result.append(d)
                seen.add(d["id"])
        logger.info(f"BM {bm_id}: owned accounts: {len(result)}")
    except Exception as e:
        logger.warning(f"BM {bm_id}: owned accounts error — {e}")

    # Client accounts
    try:
        for acc in business.get_client_ad_accounts(fields=ACCOUNT_FIELDS):
            d = dict(acc)
            if d.get("id") not in seen:
                d["account_source"] = "client"
                result.append(d)
                seen.add(d["id"])
        logger.info(f"BM {bm_id}: total accounts (incl. client): {len(result)}")
    except Exception as e:
        logger.warning(f"BM {bm_id}: client accounts error — {e}")

    return result


def get_all_ad_accounts() -> list[dict]:
    """
    Collect ad accounts from all configured BM IDs.
    Returns list of dicts with keys: id, name, bm_id, bm_label
    """
    bm_configs = [
        (os.environ.get("META_BM_ID_MAIN", ""), "main"),
        (os.environ.get("META_BM_ID_ZEEKR", ""), "zeekr"),
    ]
    all_accounts = []
    seen = set()
    for bm_id, label in bm_configs:
        if not bm_id:
            continue
        for acc in get_ad_accounts_from_bm(bm_id):
            acc_id = acc.get("id", "")
            if acc_id and acc_id not in seen:
                acc["bm_id"] = bm_id
                acc["bm_label"] = label
                all_accounts.append(acc)
                seen.add(acc_id)
    return all_accounts


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

    leads = (
        _extract_action(actions, "lead")
        or _extract_action(actions, "onsite_conversion.lead_grouped")
    )
    link_clicks = _extract_action(actions, "link_click")
    page_likes = _extract_action(actions, "like")
    post_engagements = int(float(row.get("post_engagement", 0) or 0))
    video_views = _extract_action(actions, "video_view")
    purchases = (
        _extract_action(actions, "purchase")
        or _extract_action(actions, "omni_purchase")
    )
    purchase_value = (
        _extract_action_value(action_values, "purchase")
        or _extract_action_value(action_values, "omni_purchase")
    )
    cost_per_lead = (
        _extract_cost_per(cost_per, "lead")
        or _extract_cost_per(cost_per, "onsite_conversion.lead_grouped")
    )
    cost_per_link_click = _extract_cost_per(cost_per, "link_click")

    spend = float(row.get("spend", 0) or 0)
    purchase_roas = round(purchase_value / spend, 2) if spend > 0 else 0

    return {
        "date": row.get("date_start"),
        "campaign_id": row.get("campaign_id", ""),
        "campaign_name": row.get("campaign_name", ""),
        "adset_id": row.get("adset_id", "campaign_level"),
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


def fetch_campaigns_for_account(account_id: str) -> list[dict]:
    account_id = account_id.lstrip("act_")
    account = AdAccount(f"act_{account_id}")
    campaigns = account.get_campaigns(fields=CAMPAIGN_FIELDS)
    result = []
    for c in campaigns:
        data = dict(c)
        upsert_campaign(data)
        result.append(data)
    return result


def fetch_insights_for_account(account_id: str, date_from: date, date_to: date) -> int:
    account_id = account_id.lstrip("act_")
    account = AdAccount(f"act_{account_id}")

    params = {
        "level": "campaign",
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
    return count


def run_daily_collection() -> int:
    """Fetch yesterday's data for all ad accounts in all BMs."""
    _init_api()
    init_db()

    yesterday = date.today() - timedelta(days=1)
    accounts = get_all_ad_accounts()

    if not accounts:
        logger.warning("No ad accounts found — check BM IDs and token permissions")
        return 0

    total = 0
    for acc in accounts:
        acc_id = acc["id"]
        acc_name = acc.get("name", acc_id)
        logger.info(f"Processing account: {acc_name} ({acc_id}) [{acc['bm_label']}]")
        try:
            fetch_campaigns_for_account(acc_id)
            count = fetch_insights_for_account(acc_id, yesterday, yesterday)
            total += count
            logger.info(f"  → {count} rows saved")
        except Exception as e:
            logger.error(f"  → Failed: {e}")

    logger.info(f"Daily collection done: {total} total rows for {yesterday}")
    return total


def run_historical_backfill(days: int = 30) -> int:
    """One-time backfill for the last N days across all accounts."""
    _init_api()
    init_db()

    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days - 1)
    accounts = get_all_ad_accounts()

    if not accounts:
        logger.warning("No ad accounts found")
        return 0

    total = 0
    for acc in accounts:
        acc_id = acc["id"]
        acc_name = acc.get("name", acc_id)
        logger.info(f"Backfilling: {acc_name} ({acc_id})")
        try:
            fetch_campaigns_for_account(acc_id)
            count = fetch_insights_for_account(acc_id, date_from, date_to)
            total += count
            logger.info(f"  → {count} rows")
        except Exception as e:
            logger.error(f"  → Failed: {e}")

    logger.info(f"Backfill done: {total} rows for last {days} days")
    return total


def list_accounts() -> list[dict]:
    """CLI helper — print all found ad accounts."""
    _init_api()
    return get_all_ad_accounts()
