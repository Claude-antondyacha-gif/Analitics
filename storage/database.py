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

        CREATE TABLE IF NOT EXISTS daily_ad_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ad_id TEXT NOT NULL,
            ad_name TEXT,
            adset_id TEXT,
            adset_name TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            spend REAL DEFAULT 0,
            reach INTEGER DEFAULT 0,
            ctr REAL DEFAULT 0,
            cpc REAL DEFAULT 0,
            cpm REAL DEFAULT 0,
            leads INTEGER DEFAULT 0,
            link_clicks INTEGER DEFAULT 0,
            cost_per_lead REAL DEFAULT 0,
            UNIQUE(date, ad_id)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date);
        CREATE INDEX IF NOT EXISTS idx_daily_metrics_campaign ON daily_metrics(campaign_id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_date ON ai_recommendations(created_at);
        CREATE INDEX IF NOT EXISTS idx_ad_metrics_date ON daily_ad_metrics(date);
        CREATE INDEX IF NOT EXISTS idx_ad_metrics_ad ON daily_ad_metrics(ad_id);
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


def upsert_ad_metrics(row: dict):
    """Insert or update daily ad-level metrics (one row per ad per day)."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO daily_ad_metrics (
            date, ad_id, ad_name, adset_id, adset_name, campaign_id, campaign_name,
            impressions, clicks, spend, reach, ctr, cpc, cpm,
            leads, link_clicks, cost_per_lead
        ) VALUES (
            :date, :ad_id, :ad_name, :adset_id, :adset_name, :campaign_id, :campaign_name,
            :impressions, :clicks, :spend, :reach, :ctr, :cpc, :cpm,
            :leads, :link_clicks, :cost_per_lead
        )
        ON CONFLICT(date, ad_id) DO UPDATE SET
            impressions=excluded.impressions, clicks=excluded.clicks,
            spend=excluded.spend, reach=excluded.reach,
            ctr=excluded.ctr, cpc=excluded.cpc, cpm=excluded.cpm,
            leads=excluded.leads, link_clicks=excluded.link_clicks,
            cost_per_lead=excluded.cost_per_lead
    """, {
        "date": row.get("date"),
        "ad_id": row.get("ad_id", ""),
        "ad_name": row.get("ad_name", ""),
        "adset_id": row.get("adset_id", ""),
        "adset_name": row.get("adset_name", ""),
        "campaign_id": row.get("campaign_id", ""),
        "campaign_name": row.get("campaign_name", ""),
        "impressions": row.get("impressions", 0),
        "clicks": row.get("clicks", 0),
        "spend": row.get("spend", 0),
        "reach": row.get("reach", 0),
        "ctr": row.get("ctr", 0),
        "cpc": row.get("cpc", 0),
        "cpm": row.get("cpm", 0),
        "leads": row.get("leads", 0),
        "link_clicks": row.get("link_clicks", 0),
        "cost_per_lead": row.get("cost_per_lead", 0),
    })
    conn.commit()
    conn.close()


def get_ad_metrics_by_period(days: int = 30, only_leadgen: bool = True) -> list[dict]:
    """
    Returns daily ad-level rows (one row per ad per day) for last N days.
    If only_leadgen=True, filters to OUTCOME_LEADS campaigns.
    Sorted by date desc, then spend desc.
    """
    conn = get_connection()
    query = """
        SELECT
            a.date,
            a.ad_id,
            a.ad_name,
            a.adset_id,
            a.adset_name,
            a.campaign_id,
            a.campaign_name,
            a.spend,
            a.impressions,
            a.clicks,
            a.leads,
            a.link_clicks,
            a.ctr,
            a.cpc,
            a.cpm,
            a.cost_per_lead,
            COALESCE(c.objective, '') as campaign_objective
        FROM daily_ad_metrics a
        LEFT JOIN campaigns c ON a.campaign_id = c.id
        WHERE a.date >= date('now', :offset)
    """
    params: dict = {"offset": f"-{days} days"}

    if only_leadgen:
        query += """
            AND (
                c.objective = 'OUTCOME_LEADS'
                OR LOWER(a.campaign_name) LIKE '%snap%'
            )
        """

    query += " ORDER BY a.date DESC, a.spend DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ad_metrics_summary(days: int = 30, only_leadgen: bool = True) -> list[dict]:
    """
    Returns aggregated stats per ad (creative) for last N days.
    Sorted by spend desc. If only_leadgen=True, filters SNAP OUTCOME_LEADS campaigns.
    """
    conn = get_connection()
    query = """
        SELECT
            a.ad_id,
            a.ad_name,
            a.campaign_name,
            MIN(a.date) as first_date,
            MAX(a.date) as last_date,
            COUNT(DISTINCT a.date) as active_days,
            SUM(a.spend) as total_spend,
            SUM(a.impressions) as total_impressions,
            SUM(a.leads) as total_leads,
            SUM(a.clicks) as total_clicks,
            SUM(a.link_clicks) as total_link_clicks,
            CASE WHEN SUM(a.leads) > 0
                 THEN ROUND(SUM(a.spend) / SUM(a.leads), 2)
                 ELSE 0 END as cpl,
            CASE WHEN SUM(a.clicks) > 0
                 THEN ROUND(SUM(a.impressions) * 1.0 / SUM(a.clicks), 2)
                 ELSE 0 END as avg_ctr
        FROM daily_ad_metrics a
        LEFT JOIN campaigns c ON a.campaign_id = c.id
        WHERE a.date >= date('now', :offset)
    """
    params: dict = {"offset": f"-{days} days"}

    if only_leadgen:
        query += """
            AND (
                c.objective = 'OUTCOME_LEADS'
                OR LOWER(a.campaign_name) LIKE '%snap%'
            )
        """

    query += " GROUP BY a.ad_id, a.ad_name ORDER BY total_spend DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_metrics_by_period(days: int = 7, campaign_id: str = None,
                          objective: str = None) -> list[dict]:
    """Returns daily_metrics joined with campaign objective for correct classification."""
    conn = get_connection()
    # Always join campaigns to include objective in result
    query = """
        SELECT m.*, COALESCE(c.objective, '') as campaign_objective
        FROM daily_metrics m
        LEFT JOIN campaigns c ON m.campaign_id = c.id
        WHERE m.date >= date('now', :offset)
    """
    params = {"offset": f"-{days} days"}
    if objective:
        query += " AND c.objective = :objective"
        params["objective"] = objective
    if campaign_id:
        query += " AND m.campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id
    query += " ORDER BY m.date DESC"
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
