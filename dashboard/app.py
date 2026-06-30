"""
Flask dashboard — serves the analytics UI and REST API endpoints.
Run: python dashboard/app.py
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

from storage.database import (
    init_db, get_aggregated_metrics, get_metrics_by_period,
    get_campaigns_list, get_latest_recommendations,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "dev-secret-change-me")
CORS(app)


# ─── HTML ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─── API ─────────────────────────────────────────────────────────────────────

@app.route("/api/accounts")
def api_accounts():
    campaigns = get_campaigns_list()
    return jsonify(campaigns)


@app.route("/api/summary")
def api_summary():
    campaign_id = request.args.get("campaign_id") or None
    periods = [
        ("yesterday", 1),
        ("3d", 3),
        ("7d", 7),
        ("14d", 14),
        ("30d", 30),
    ]
    result = {}
    for label, days in periods:
        result[label] = get_aggregated_metrics(days=days, campaign_id=campaign_id)
    return jsonify(result)


@app.route("/api/campaigns")
def api_campaigns():
    days = int(request.args.get("days", 7))
    campaigns = get_campaigns_list()
    rows = get_metrics_by_period(days=days)

    # aggregate per campaign
    stats: dict[str, dict] = {}
    for r in rows:
        cid = r["campaign_id"]
        if cid not in stats:
            stats[cid] = {
                "campaign_id": cid, "campaign_name": r["campaign_name"],
                "spend": 0, "impressions": 0, "clicks": 0, "leads": 0,
                "purchases": 0, "purchase_value": 0, "link_clicks": 0,
                "page_likes": 0, "video_views": 0, "reach": 0,
                "ctr_vals": [], "cpc_vals": [], "cpm_vals": [],
            }
        s = stats[cid]
        s["spend"] += r["spend"]
        s["impressions"] += r["impressions"]
        s["clicks"] += r["clicks"]
        s["leads"] += r["leads"]
        s["purchases"] += r["purchases"]
        s["purchase_value"] += r["purchase_value"]
        s["link_clicks"] += r["link_clicks"]
        s["page_likes"] += r["page_likes"]
        s["video_views"] += r["video_views"]
        s["reach"] = max(s["reach"], r["reach"])
        if r["ctr"]: s["ctr_vals"].append(r["ctr"])
        if r["cpc"]: s["cpc_vals"].append(r["cpc"])
        if r["cpm"]: s["cpm_vals"].append(r["cpm"])

    result = []
    for cid, s in stats.items():
        camp_info = next((c for c in campaigns if c["id"] == cid), {})
        avg = lambda lst: round(sum(lst) / len(lst), 2) if lst else 0
        cpl = round(s["spend"] / s["leads"], 2) if s["leads"] > 0 else 0
        cpp = round(s["spend"] / s["purchases"], 2) if s["purchases"] > 0 else 0
        roas = round(s["purchase_value"] / s["spend"], 2) if s["spend"] > 0 else 0
        result.append({
            **s,
            "status": camp_info.get("status", "UNKNOWN"),
            "objective": camp_info.get("objective", ""),
            "daily_budget": camp_info.get("daily_budget", 0),
            "avg_ctr": avg(s.pop("ctr_vals")),
            "avg_cpc": avg(s.pop("cpc_vals")),
            "avg_cpm": avg(s.pop("cpm_vals")),
            "cost_per_lead": cpl,
            "cost_per_purchase": cpp,
            "roas": roas,
        })

    result.sort(key=lambda x: x["spend"], reverse=True)
    return jsonify(result)


@app.route("/api/timeseries")
def api_timeseries():
    days = int(request.args.get("days", 14))
    campaign_id = request.args.get("campaign_id")
    rows = get_metrics_by_period(days=days, campaign_id=campaign_id)

    # group by date
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "spend": 0, "leads": 0, "clicks": 0,
                          "impressions": 0, "purchases": 0, "page_likes": 0,
                          "link_clicks": 0, "ctr": [], "cpc": []}
        by_date[d]["spend"] += r["spend"]
        by_date[d]["leads"] += r["leads"]
        by_date[d]["clicks"] += r["clicks"]
        by_date[d]["impressions"] += r["impressions"]
        by_date[d]["purchases"] += r["purchases"]
        by_date[d]["page_likes"] += r["page_likes"]
        by_date[d]["link_clicks"] += r["link_clicks"]
        if r["ctr"]: by_date[d]["ctr"].append(r["ctr"])
        if r["cpc"]: by_date[d]["cpc"].append(r["cpc"])

    result = []
    for d in sorted(by_date.keys()):
        s = by_date[d]
        ctr_vals = s.pop("ctr", [])
        cpc_vals = s.pop("cpc", [])
        s["avg_ctr"] = round(sum(ctr_vals) / len(ctr_vals), 2) if ctr_vals else 0
        s["avg_cpc"] = round(sum(cpc_vals) / len(cpc_vals), 2) if cpc_vals else 0
        s["cost_per_lead"] = round(s["spend"] / s["leads"], 2) if s["leads"] > 0 else 0
        result.append(s)

    return jsonify(result)


@app.route("/api/recommendations")
def api_recommendations():
    limit = int(request.args.get("limit", 15))
    return jsonify(get_latest_recommendations(limit=limit))


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.json or {}
    days = int(data.get("days", 7))
    campaign_id = data.get("campaign_id")
    question = data.get("question")

    from agent.ai_agent import analyze
    result = analyze(
        period_label=f"{days}d",
        days=days,
        campaign_id=campaign_id,
        custom_question=question,
    )
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    message = data.get("message", "").strip()
    days = int(data.get("days", 7))
    if not message:
        return jsonify({"error": "message is required"}), 400

    from agent.ai_agent import chat
    response = chat(user_message=message, days_context=days)
    return jsonify({"response": response})


@app.route("/api/action", methods=["POST"])
def api_action():
    """Execute a Meta Ads action (pause/enable campaign)."""
    data = request.json or {}
    action_type = data.get("action_type")
    target_id = data.get("target_id")
    target_name = data.get("target_name", "")
    reason = data.get("reason", "Manual action via dashboard")

    if not action_type or not target_id:
        return jsonify({"error": "action_type and target_id required"}), 400

    from storage.database import log_action, update_action_status
    from collector.meta_collector import _init_api
    from facebook_business.adobjects.campaign import Campaign

    action_id = log_action(action_type, target_id, target_name, reason)

    try:
        _init_api()
        if action_type == "pause_campaign":
            Campaign(target_id).api_update(fields=[], params={"status": Campaign.Status.paused})
        elif action_type == "enable_campaign":
            Campaign(target_id).api_update(fields=[], params={"status": Campaign.Status.active})
        else:
            return jsonify({"error": f"Unknown action: {action_type}"}), 400

        update_action_status(action_id, "executed")
        return jsonify({"success": True, "action_id": action_id})
    except Exception as e:
        update_action_status(action_id, "failed", str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/collect", methods=["POST"])
def api_collect():
    """Trigger manual data collection."""
    from collector.meta_collector import run_daily_collection
    try:
        count = run_daily_collection()
        return jsonify({"success": True, "rows": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("DASHBOARD_PORT", 5000))
    logger.info(f"Dashboard running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
