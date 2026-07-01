import sqlite3
import json
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "analytics.db"


def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT,
            objective TEXT,
            daily_budget REAL,
            lifetime_budget REAL,
            created_time TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ad_sets (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            name TEXT NOT NULL,
            status TEXT,
            daily_budget REAL,
            targeting TEXT,
            updated_at TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            campaign_name TEXT,
            adset_id TEXT,
            adset_name TEXT,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            spend REAL DEFAULT 0,
            reach INTEGER DEFAULT 0,
            frequency REAL DEFAULT 0,
            ctr REAL DEFAULT 0,
            cpc REAL DEFAULT 0,
            cpm REAL DEFAULT 0,
            cpp REAL DEFAULT 0,
            leads INTEGER DEFAULT 0,
            link_clicks INTEGER DEFAULT 0,
            post_engagements INTEGER DEFAULT 0,
            video_views INTEGER DEFAULT 0,
            page_likes INTEGER DEFAULT 0,
            cost_per_lead REAL DEFAULT 0,
            cost_per_link_click REAL DEFAULT 0,
            purchase_roas REAL DEFAULT 0,
            purchases INTEGER DEFAULT 0,
            purchase_value REAL DEFAULT 0,
            raw_json TEXT,
            UNIQUE(date, campaign_id, adset_id)
        );

        CREATE TABLE IF NOT EXISTS ai_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            period TEXT NOT NULL,
            analysis_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            recommendations TEXT NOT NULL,
            critical_alerts TEXT,
            campaign_ids TEXT,
            raw_response TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_name TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            executed_at TEXT,
            result TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date);
        CREATE INDEX IF NOT EXISTS idx_daily_metrics_campaign ON daily_metrics(campaign_id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_date ON ai_recommendations(created_at);
    """)

    conn.commit()
    conn.close()


def upsert_campaign(campaign: dict):
    conn = get_connection()
    conn.execute("""
        INSERT INTO campaigns (id, name, status, objective, daily_budget, lifetime_budget, created_time, updated_at)
        VALUES (:id, :name, :status, :objective, :daily_budget, :lifetime_budget, :created_time, :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, status=excluded.status, objective=excluded.objective,
            daily_budget=excluded.daily_budget, updated_at=excluded.updated_at
    """, {
        "id": campaign.get("id"),
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "objective": campaign.get("objective"),
        "daily_budget": float(campaign.get("daily_budget", 0) or 0) / 100,
        "lifetime_budget": float(campaign.get("lifetime_budget", 0) or 0) / 100,
        "created_time": campaign.get("created_time"),
        "updated_at": datetime.utcnow().isoformat(),
    })
    conn.commit()
    conn.close()


def upsert_daily_metrics(row: dict):
    conn = get_connection()
    conn.execute("""
        INSERT INTO daily_metrics (
            date, campaign_id, campaign_name, adset_id, adset_name,
            impressions, clicks, spend, reach, frequency,
            ctr, cpc, cpm, cpp, leads, link_clicks, post_engagements,
            video_views, page_likes, cost_per_lead, cost_per_link_click,
            purchase_roas, purchases, purchase_value, raw_json
        ) VALUES (
            :date, :campaign_id, :campaign_name, :adset_id, :adset_name,
            :impressions, :clicks, :spend, :reach, :frequency,
            :ctr, :cpc, :cpm, :cpp, :leads, :link_clicks, :post_engagements,
            :video_views, :page_likes, :cost_per_lead, :cost_per_link_click,
            :purchase_roas, :purchases, :purchase_value, :raw_json
        )
        ON CONFLICT(date, campaign_id, adset_id) DO UPDATE SET
            impressions=excluded.impressions, clicks=excluded.clicks, spend=excluded.spend,
            reach=excluded.reach, frequency=excluded.frequency, ctr=excluded.ctr,
            cpc=excluded.cpc, cpm=excluded.cpm, leads=excluded.leads,
            link_clicks=excluded.link_clicks, post_engagements=excluded.post_engagements,
            video_views=excluded.video_views, page_likes=excluded.page_likes,
            cost_per_lead=excluded.cost_per_lead, cost_per_link_click=excluded.cost_per_link_click,
            purchase_roas=excluded.purchase_roas, purchases=excluded.purchases,
            purchase_value=excluded.purchase_value, raw_json=excluded.raw_json
    """, row)
    conn.commit()
    conn.close()


def get_metrics_by_date(date_iso: str) -> list[dict]:
    """Returns all daily_metrics rows for the given date (YYYY-MM-DD)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = ? ORDER BY campaign_name",
        (date_iso,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_metrics_by_period(days: int = 7, campaign_id: str = None,
                          objective: str = None) -> list[dict]:
    conn = get_connection()
    if objective:
        query = """
            SELECT m.* FROM daily_metrics m
            JOIN campaigns c ON m.campaign_id = c.id
            WHERE m.date >= date('now', :offset)
            AND c.objective = :objective
        """
        params = {"offset": f"-{days} days", "objective": objective}
    else:
        query = """
            SELECT * FROM daily_metrics
            WHERE date >= date('now', :offset)
        """
        params = {"offset": f"-{days} days"}
    if campaign_id:
        query += " AND campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id
    query += " ORDER BY date DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_aggregated_metrics(days: int = 7, campaign_id: str = None) -> dict:
    conn = get_connection()
    query = """
        SELECT
            SUM(spend) as total_spend,
            SUM(impressions) as total_impressions,
            SUM(clicks) as total_clicks,
            SUM(leads) as total_leads,
            SUM(reach) as total_reach,
            SUM(link_clicks) as total_link_clicks,
            SUM(video_views) as total_video_views,
            SUM(page_likes) as total_page_likes,
            SUM(purchases) as total_purchases,
            SUM(purchase_value) as total_purchase_value,
            AVG(ctr) as avg_ctr,
            AVG(cpc) as avg_cpc,
            AVG(cpm) as avg_cpm,
            AVG(frequency) as avg_frequency,
            COUNT(DISTINCT campaign_id) as active_campaigns
        FROM daily_metrics
        WHERE date >= date('now', :offset)
    """
    params = {"offset": f"-{days} days"}
    if campaign_id:
        query += " AND campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id

    row = conn.execute(query, params).fetchone()
    result = dict(row) if row else {}

    spend = result.get("total_spend") or 0
    leads = result.get("total_leads") or 0
    purchases = result.get("total_purchases") or 0

    result["cost_per_lead"] = round(spend / leads, 2) if leads > 0 else 0
    result["cost_per_purchase"] = round(spend / purchases, 2) if purchases > 0 else 0
    result["roas"] = round((result.get("total_purchase_value") or 0) / spend, 2) if spend > 0 else 0

    conn.close()
    return result


def save_recommendation(period: str, analysis_type: str, summary: str,
                         recommendations: list, critical_alerts: list = None,
                         campaign_ids: list = None, raw: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO ai_recommendations
            (created_at, period, analysis_type, summary, recommendations, critical_alerts, campaign_ids, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        period,
        analysis_type,
        summary,
        json.dumps(recommendations, ensure_ascii=False),
        json.dumps(critical_alerts or [], ensure_ascii=False),
        json.dumps(campaign_ids or [], ensure_ascii=False),
        raw,
    ))
    conn.commit()
    conn.close()


def get_latest_recommendations(limit: int = 10) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM ai_recommendations ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["recommendations"] = json.loads(d["recommendations"] or "[]")
        d["critical_alerts"] = json.loads(d["critical_alerts"] or "[]")
        d["campaign_ids"] = json.loads(d["campaign_ids"] or "[]")
        result.append(d)
    return result


def log_action(action_type: str, target_id: str, target_name: str, reason: str) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO ai_actions (created_at, action_type, target_id, target_name, reason, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (datetime.utcnow().isoformat(), action_type, target_id, target_name, reason))
    action_id = cur.lastrowid
    conn.commit()
    conn.close()
    return action_id


def update_action_status(action_id: int, status: str, result: str = None):
    conn = get_connection()
    conn.execute("""
        UPDATE ai_actions SET status=?, executed_at=?, result=? WHERE id=?
    """, (status, datetime.utcnow().isoformat(), result, action_id))
    conn.commit()
    conn.close()


def get_campaigns_list() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM campaigns ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]
