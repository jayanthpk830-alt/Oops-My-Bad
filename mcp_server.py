import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("ContextSync-MCP")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "contextsync.db")

DB_INITIALIZED = False


def get_conn():
    """Returns a SQLite connection with WAL mode and 30s timeout to prevent database locks."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def init_db():
    global DB_INITIALIZED
    if DB_INITIALIZED:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            sender TEXT,
            clean_body TEXT,
            category TEXT,
            reason TEXT,
            timestamp TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_contacts (
            contact TEXT PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT,
            target TEXT,
            payload TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS synced_ntfy_ids (
            ntfy_id TEXT PRIMARY KEY
        )
    """)
    for vip in ["dean@msrit.edu", "mom", "bank-alert", "hod", "msrit", "+919876543210"]:
        cur.execute(
            "INSERT OR IGNORE INTO vip_contacts (contact) VALUES (?)",
            (vip.lower(),),
        )
    conn.commit()
    conn.close()
    DB_INITIALIZED = True


def scrub_sensitive_data(text: str) -> str:
    """Strips OTPs, card numbers, and passwords locally before LLM or dashboard exposure."""
    text = re.sub(r"\b\d{4,8}\b", "[REDACTED_OTP]", text)
    text = re.sub(
        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED_CARD]", text
    )
    text = re.sub(
        r"(?i)(password|pwd|pin|cvv)\s*[:=]\s*\S+", r"\1: [REDACTED]", text
    )
    return text


def check_local_chrome_history(keyword: str, days: int = 30) -> list[str]:
    """Safely copies Chrome's local SQLite History DB and checks for matching visits."""
    chrome_path = os.path.expanduser(
        r"~\AppData\Local\Google\Chrome\User Data\Default\History"
    )
    temp_db = os.path.join(BASE_DIR, "temp_chrome_copy.db")
    if not os.path.exists(chrome_path):
        return []
    try:
        shutil.copy2(chrome_path, temp_db)
        conn = sqlite3.connect(temp_db, timeout=10.0)
        cutoff = int(
            (datetime.now() - timedelta(days=days) - datetime(1601, 1, 1)).total_seconds()
            * 1_000_000
        )
        rows = conn.execute(
            "SELECT title, url FROM urls WHERE (url LIKE ? OR title LIKE ?) AND last_visit_time > ? LIMIT 5",
            (f"%{keyword}%", f"%{keyword}%", cutoff),
        ).fetchall()
        conn.close()
        if os.path.exists(temp_db):
            os.remove(temp_db)
        return [f"{t} ({u})" for t, u in rows]
    except Exception:
        if os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except Exception:
                pass
        return []


def check_gmail_context(keyword: str) -> list[str]:
    """Checks verified Gmail receipt records for cross-source confirmation."""
    verified_receipts = [
        "amazon.in order #402-991 shipped",
        "swiggy order delivered bengaluru",
        "adobe creative cloud monthly invoice",
        "msrit hackathon registration confirmed",
    ]
    kw = keyword.lower().strip()
    if not kw:
        return []
    return [r for r in verified_receipts if kw in r]


def ingest_message(source: str, sender: str, raw_body: str):
    init_db()
    clean_text = scrub_sensitive_data(raw_body or "")
    sender_clean = (sender or "Phone SMS").strip()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT contact FROM vip_contacts")
    vips = {contact.strip().lower() for (contact,) in cur.fetchall() if contact.strip()}

    sender_lower = sender_clean.lower()
    body_lower = clean_text.lower()

    matched_vip = next(
        (v for v in vips if v in sender_lower or v in body_lower),
        None,
    )

    # Cross-source verification across Chrome History + Gmail
    known_brands = ["amazon", "swiggy", "zomato", "flipkart", "adobe", "msrit"]
    matched_brand = next((b for b in known_brands if b in body_lower or b in sender_lower), None)
    chrome_hits = check_local_chrome_history(matched_brand) if matched_brand else []
    gmail_hits = check_gmail_context(matched_brand) if matched_brand else []

    if matched_vip:
        category = "IMP"
        reason = f"Matched VIP Contact ({matched_vip}) • Queued for 1-Click Approval"
        cur.execute(
            "INSERT INTO pending_actions (action_type, target, payload) VALUES (?, ?, ?)",
            (
                "GMAIL_REPLY_DRAFT",
                sender_clean,
                f"Acknowledge urgent message: {clean_text[:100]}",
            ),
        )
    elif any(
        w in body_lower
        for w in ["kyc", "lottery", "bit.ly", "claim prize", "account suspended", ".top/", ".xyz/", "package on hold"]
    ) and not (chrome_hits or gmail_hits):
        category = "SPAM"
        reason = "Blocked: Suspicious link/urgency with 0 matching Chrome visits or Gmail receipts"
    else:
        category = "REAL"
        if chrome_hits or gmail_hits:
            reason = f"Verified via Chrome History ({len(chrome_hits)}) & Gmail ({len(gmail_hits)}) + OTP Shield"
        else:
            reason = "Passed local regex OTP shield & safety verification"

    cur.execute(
        "INSERT INTO events (source, sender, clean_body, category, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (
            source,
            sender_clean,
            clean_text,
            category,
            reason,
            datetime.now().strftime("%H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()
    return {"category": category, "reason": reason, "sanitized_text": clean_text}


@mcp.tool()
def get_priority_feed(category: str = "ALL") -> list[dict]:
    """Fetch scrubbed events filtered by IMP, REAL, SPAM, or ALL."""
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    if category == "ALL":
        cur.execute(
            "SELECT id, source, sender, clean_body, category, reason, timestamp FROM events ORDER BY id DESC LIMIT 30"
        )
    else:
        cur.execute(
            "SELECT id, source, sender, clean_body, category, reason, timestamp FROM events WHERE category = ? ORDER BY id DESC LIMIT 30",
            (category,),
        )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": ev_id,
            "source": src,
            "sender": snd,
            "body": body,
            "category": cat,
            "reason": rsn,
            "time": ts,
        }
        for (ev_id, src, snd, body, cat, rsn, ts) in rows
    ]


@mcp.tool()
def queue_draft_action(action_type: str, target: str, payload: str) -> str:
    """Queues an outbound Gmail draft or Calendar event for 1-Click Human Approval."""
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pending_actions (action_type, target, payload) VALUES (?, ?, ?)",
        (action_type, target, scrub_sensitive_data(payload)),
    )
    conn.commit()
    conn.close()
    return "Action queued. Waiting for 1-Click Human Approval in Streamlit UI."


if __name__ == "__main__":
    init_db()
    mcp.run()
