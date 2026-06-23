"""
RKZ Lead Hunter — SQLite storage layer
Persistent dedup, lead history, and stats. Zero external dependencies.

Usage:
    from db import init_db, is_duplicate, save_lead, get_stats
    init_db()                      # call once at startup
    if not is_duplicate(dedup_key):
        save_lead({...})
"""

import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "rkz_leads.db"


# ── Connection helper ─────────────────────────────────────────────────────────
@contextmanager
def get_conn():
    """Context-managed SQLite connection. Auto-commits or rolls back."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")        # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist. Idempotent — safe to call every startup."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key       TEXT    UNIQUE NOT NULL,
                platform        TEXT    NOT NULL,
                business_name   TEXT,
                owner_name      TEXT,
                profile_url     TEXT,
                website         TEXT,
                category        TEXT,
                address         TEXT,
                post_text       TEXT,
                lead_score      INTEGER DEFAULT 0,
                signals         TEXT,                    -- JSON array
                ai_payload      TEXT,                    -- JSON blob from Qwen
                sent_to_sheet   INTEGER DEFAULT 0,
                disqualified    INTEGER DEFAULT 0,
                disqualify_reason TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_leads_dedup    ON leads(dedup_key);
            CREATE INDEX IF NOT EXISTS idx_leads_platform ON leads(platform);
            CREATE INDEX IF NOT EXISTS idx_leads_created  ON leads(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leads_score    ON leads(lead_score DESC);
        """)
    print(f"[DB] ✅ SQLite initialized at {DB_PATH}")


# ── Dedup key generation ──────────────────────────────────────────────────────
def make_dedup_key(platform: str, profile_url: str, business_name: str, post_text: str = "") -> str:
    """
    Generate a stable dedup hash. Order of preference:
      1. platform + profile_url (most reliable)
      2. platform + business_name + first 40 chars of post_text
    """
    if profile_url and profile_url.startswith("http"):
        raw = f"{platform}|{profile_url.lower().strip()}"
    else:
        raw = f"{platform}|{(business_name or '').lower().strip()}|{(post_text or '')[:40].strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── Dedup check ───────────────────────────────────────────────────────────────
def is_duplicate(dedup_key: str) -> bool:
    """Return True if this lead was already processed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE dedup_key = ? LIMIT 1",
            (dedup_key,)
        ).fetchone()
        return row is not None


# ── Save lead ─────────────────────────────────────────────────────────────────
def save_lead(lead: dict) -> int | None:
    """
    Insert lead. Returns row ID, or None if duplicate.
    Required keys: dedup_key, platform.
    Optional everything else.
    """
    with get_conn() as conn:
        try:
            cur = conn.execute("""
                INSERT INTO leads (
                    dedup_key, platform, business_name, owner_name, profile_url,
                    website, category, address, post_text, lead_score,
                    signals, ai_payload, sent_to_sheet, disqualified, disqualify_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                lead["dedup_key"],
                lead["platform"],
                lead.get("business_name", ""),
                lead.get("owner_name", ""),
                lead.get("profile_url", ""),
                lead.get("website", ""),
                lead.get("category", ""),
                lead.get("address", ""),
                lead.get("post_text", ""),
                lead.get("lead_score", 0),
                json.dumps(lead.get("signals", [])),
                json.dumps(lead.get("ai_payload", {})),
                1 if lead.get("sent_to_sheet") else 0,
                1 if lead.get("disqualified") else 0,
                lead.get("disqualify_reason", ""),
            ))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # dedup_key collision — already exists
            return None


# ── Mark sheet write status ───────────────────────────────────────────────────
def mark_sent_to_sheet(dedup_key: str, success: bool = True):
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET sent_to_sheet = ? WHERE dedup_key = ?",
            (1 if success else 0, dedup_key)
        )


# ── Stats for dashboard ───────────────────────────────────────────────────────
def get_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads WHERE disqualified=0").fetchone()[0]

        by_platform = {}
        for row in conn.execute("""
            SELECT platform, COUNT(*) as c
            FROM leads WHERE disqualified=0
            GROUP BY platform
        """):
            by_platform[row["platform"]] = row["c"]

        score_row = conn.execute("""
            SELECT AVG(lead_score) as avg, MAX(lead_score) as max
            FROM leads WHERE disqualified=0 AND lead_score > 0
        """).fetchone()

        last_24h = conn.execute("""
            SELECT COUNT(*) FROM leads
            WHERE disqualified=0 AND created_at > datetime('now','-1 day')
        """).fetchone()[0]

        return {
            "total":       total,
            "last_24h":    last_24h,
            "avg_score":   round(score_row["avg"] or 0, 1),
            "max_score":   score_row["max"] or 0,
            "by_platform": by_platform,
        }


def get_recent_leads(limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT platform, business_name, owner_name, lead_score,
                   post_text, profile_url, created_at
            FROM leads
            WHERE disqualified=0
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def purge_old_leads(days: int = 90):
    """Optional housekeeping — call from a cron or manual endpoint."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM leads WHERE created_at < datetime('now', ?)",
            (f'-{days} days',)
        )
        return cur.rowcount


if __name__ == "__main__":
    # Smoke test
    init_db()
    print("Stats:", get_stats())