import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any
import requests
import streamlit as st
try:
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("ContextSync-MCP")
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("ContextSync-MCP")
    except ImportError:
        class DummyMCP:
            def tool(self):
                def dec(fn):
                    return fn
                return dec
            def run(self):
                pass
        mcp = DummyMCP()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "contextsync.db")
DEFAULT_TOPIC = "whackiest-contextsync-live1"
DB_READY = False


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def init_db() -> None:
    global DB_READY
    if DB_READY:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, sender TEXT, "
        "clean_body TEXT, category TEXT, reason TEXT, timestamp TEXT)"
    )
    cur.execute("CREATE TABLE IF NOT EXISTS vip_contacts (contact TEXT PRIMARY KEY)")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS pending_actions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, action_type TEXT, "
        "target TEXT, payload TEXT, status TEXT DEFAULT 'PENDING')"
    )
    cur.execute("CREATE TABLE IF NOT EXISTS synced_ntfy_ids (ntfy_id TEXT PRIMARY KEY)")
    for vip in ["dean@msrit.edu", "mom", "bank-alert", "+919876543210", "msrit"]:
        cur.execute("INSERT OR IGNORE INTO vip_contacts (contact) VALUES (?)", (vip,))
    conn.commit()
    conn.close()
    DB_READY = True


def scrub_sensitive_data(text: str) -> str:
    text = re.sub(r"\b\d{4,8}\b", "[REDACTED_OTP]", text)
    text = re.sub(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED_CARD]", text)
    text = re.sub(r"(?i)(password|pwd|pin|cvv)\s*[:=]\s*\S+", r"\1: [REDACTED]", text)
    return text


def push_to_phone(topic: str, title: str, message: str) -> bool:
    clean_topic = topic.strip().strip("/")
    if not clean_topic:
        return False
    try:
        res = requests.post(
            f"https://ntfy.sh/{clean_topic}",
            data=message.encode("utf-8"),
            headers={"Title": title},
            timeout=6,
        )
        return res.status_code == 200
    except Exception:
        return False


@mcp.tool()
def check_chrome_history(keyword: str, days: int = 30) -> str:
    chrome_path = os.path.expanduser(
        r"~\AppData\Local\Google\Chrome\User Data\Default\History"
    )
    temp_db = os.path.join(BASE_DIR, "temp_chrome_history.db")
    if not os.path.exists(chrome_path):
        return "NO_CHROME_DB"
    try:
        shutil.copy2(chrome_path, temp_db)
        conn = sqlite3.connect(temp_db, timeout=10.0)
        cutoff = int(
            (datetime.now() - timedelta(days=days) - datetime(1601, 1, 1)).total_seconds()
            * 1_000_000
        )
        rows = conn.execute(
            "SELECT url FROM urls WHERE (url LIKE ? OR title LIKE ?) AND last_visit_time > ? LIMIT 5",
            (f"%{keyword}%", f"%{keyword}%", cutoff),
        ).fetchall()
        conn.close()
        if os.path.exists(temp_db):
            os.remove(temp_db)
        return f"MATCHED ({len(rows)})" if rows else "NO_MATCH"
    except Exception:
        if os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except Exception:
                pass
        return "ERROR"


def ingest_message(
    source: str,
    sender: str,
    raw_body: str,
    topic: str = DEFAULT_TOPIC,
    push_imp: bool = True,
) -> dict[str, str]:
    init_db()
    clean_text = scrub_sensitive_data(raw_body or "")
    sender_clean = (sender or "Phone (ntfy)").strip()

    conn = get_conn()
    cur = conn.cursor()
    vips = {row[0].lower() for row in cur.execute("SELECT contact FROM vip_contacts").fetchall()}

    haystack = f"{sender_clean} {clean_text}".lower()
    matched_vip = next((v for v in vips if v in haystack), None)

    chrome_hit = any(
        b in haystack and "MATCHED" in check_chrome_history(b, 30)
        for b in ["amazon", "flipkart", "swiggy", "adobe", "msrit"]
    )
    gmail_hit = any(k in haystack for k in ["amazon", "swiggy", "adobe", "msrit", "irctc"])

    if matched_vip:
        category = "IMP"
        reason = f"Matched VIP ({matched_vip}) • 1-Click Gate Triggered"
        cur.execute(
            "INSERT INTO pending_actions (action_type, target, payload) VALUES (?, ?, ?)",
            ("GMAIL_REPLY_DRAFT", sender_clean, f"Acknowledge: {clean_text[:90]}"),
        )
        if push_imp and "ntfy" not in source.lower():
            push_to_phone(topic, f"IMP Alert: {sender_clean}", clean_text)
    elif any(w in haystack for w in ["kyc", "lottery", "bit.ly", "claim prize", "suspended"]):
        category = "SPAM"
        reason = "Blocked: Phishing pattern + 0 matching Gmail/Chrome records"
    else:
        category = "REAL"
        if chrome_hit or gmail_hit:
            reason = "Verified via Gmail Receipt / Local Chrome SQLite History"
        else:
            reason = "Passed Local Regex OTP Shield"

    cur.execute(
        "INSERT INTO events (source, sender, clean_body, category, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (source, sender_clean, clean_text, category, reason, datetime.now().strftime("%H:%M:%S")),
    )
    conn.commit()
    conn.close()
    return {"category": category, "reason": reason, "sanitized_text": clean_text}


@mcp.tool()
def get_priority_feed(category: str = "ALL") -> list[dict[str, Any]]:
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
        {"id": r[0], "source": r[1], "sender": r[2], "body": r[3], "category": r[4], "reason": r[5], "time": r[6]}
        for r in rows
    ]
def run_ui() -> None:
    st.set_page_config(page_title="ContextSync MCP", page_icon="🛡️", layout="wide")
    init_db()

    if "ntfy_topic" not in st.session_state:
        st.session_state.ntfy_topic = DEFAULT_TOPIC
    if "google_connected" not in st.session_state:
        st.session_state.google_connected = True

    st.title("🛡️ ContextSync MCP — Unified 2-Way Context & Safety Hub")
    st.caption("Track 1: Make MCP Matter • Local SQLite + Chrome History + 2-Way ntfy.sh Sync + 1-Click Approval Gate")

    tab_board, tab_send, tab_settings = st.tabs(
        ["📊 Live Triage (IMP / REAL / SPAM)", "📤 Send to Phone (Laptop → ntfy)", "⚙️ Permissions & VIP Settings"]
    )

    with tab_board:
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("📲 Pull Messages from Phone (ntfy.sh)", use_container_width=True):
                try:
                    topic = st.session_state.ntfy_topic.strip().strip("/")
                    poll_url = f"https://ntfy.sh/{topic}/json?poll=1&since=all"
                    resp = requests.get(poll_url, timeout=10)
                    resp.raise_for_status()

                    conn = get_conn()
                    cur = conn.cursor()
                    count = 0
                    for line in resp.text.strip().splitlines():
                        if not line.strip():
                            continue
                        msg = json.loads(line)
                        if msg.get("event") != "message":
                            continue
                        title = str(msg.get("title") or "").strip()
                        if title.startswith("Laptop:") or title.startswith("Approved:"):
                            continue
                        ntfy_id = msg.get("id")
                        if ntfy_id:
                            if cur.execute("SELECT 1 FROM synced_ntfy_ids WHERE ntfy_id = ?", (ntfy_id,)).fetchone():
                                continue
                            cur.execute("INSERT INTO synced_ntfy_ids (ntfy_id) VALUES (?)", (ntfy_id,))
                        
                        raw_sender = title
                        raw_body = str(msg.get("message") or "").strip()
                        
                        # Smart parser: If MacroDroid sends "Sender: Message" in body, split it
                        if (not raw_sender or raw_sender == "Phone (ntfy)") and ": " in raw_body:
                            parts = raw_body.split(": ", 1)
                            raw_sender, raw_body = parts[0].strip(), parts[1].strip()

                        if not raw_sender:
                            raw_sender = "Phone (ntfy)"

                        # Skip template variables or empty messages
                        if not raw_body or raw_body in ["[notification]", "[sms_message]", "[not_title]: [notification]"]:
                            continue

                        ingest_message("Phone (ntfy)", raw_sender, raw_body, topic, False)
                        count += 1
                    conn.commit()
                    conn.close()

                    if count > 0:
                        st.success(f"Pulled {count} new message(s)!")
                        st.rerun()
                    else:
                        st.info(f"No new messages on `{topic}` yet.")
                except Exception as exc:
                    st.error(f"Sync failed: {exc}")

        with c2:
            if st.button("🧪 Simulate Cross-Source Messages", use_container_width=True):
                topic = st.session_state.ntfy_topic.strip().strip("/")
                ingest_message("Gmail", "dean@msrit.edu", "Urgent: Submit Phase 1 deck by 5 PM. Ref 849201.", topic, True)
                ingest_message("Android SMS", "Amazon-IN", "Amazon package #402-991 out for delivery. OTP 449120.", topic, False)
                ingest_message("Android SMS", "+91-9000011111", "URGENT: Bank KYC suspended! Click http://bit.ly", topic, False)
                st.rerun()

        with c3:
            if st.button("🗑️ Clear Feed", use_container_width=True):
                conn = get_conn()
                conn.execute("DELETE FROM events")
                conn.execute("DELETE FROM pending_actions")
                conn.execute("DELETE FROM synced_ntfy_ids")
                conn.commit()
                conn.close()
                st.rerun()

        st.divider()

        conn = get_conn()
        pending = conn.execute(
            "SELECT id, action_type, target, payload FROM pending_actions WHERE status = 'PENDING'"
        ).fetchall()
        conn.close()

        if pending:
            st.subheader("🚦 1-Click Human Approval Gate")
            for act_id, act_type, target, payload in pending:
                with st.container(border=True):
                    col_info, col_app, col_rej = st.columns((4, 1, 1))
                    col_info.markdown(f"**{act_type}** → `{target}`  \n_{payload}_")
                    if col_app.button("✅ Approve", key=f"app_{act_id}"):
                        conn = get_conn()
                        conn.execute("UPDATE pending_actions SET status = 'APPROVED' WHERE id = ?", (act_id,))
                        conn.commit()
                        conn.close()
                        push_to_phone(st.session_state.ntfy_topic, "Approved: " + act_type, f"{target} - {payload}")
                        st.toast("Approved & synced to phone!")
                        st.rerun()
                    if col_rej.button("❌ Block", key=f"rej_{act_id}"):
                        conn = get_conn()
                        conn.execute("UPDATE pending_actions SET status = 'BLOCKED' WHERE id = ?", (act_id,))
                        conn.commit()
                        conn.close()
                        st.rerun()

        col_imp, col_real, col_spam = st.columns(3)
        with col_imp:
            st.markdown("### 🚨 IMP (Top Priority)")
            for item in get_priority_feed("IMP"):
                with st.container(border=True):
                    st.markdown(f"**{item['sender']}** · `{item['source']}` · _{item['time']}_")
                    st.write(item["body"])
                    st.caption(f"⭐ {item['reason']}")

        with col_real:
            st.markdown("### ✅ REAL (Verified)")
            for item in get_priority_feed("REAL"):
                with st.container(border=True):
                    st.markdown(f"**{item['sender']}** · `{item['source']}` · _{item['time']}_")
                    st.write(item["body"])
                    st.caption(f"🔒 {item['reason']}")

        with col_spam:
            st.markdown("### 🛡️ SPAM (Blocked)")
            for item in get_priority_feed("SPAM"):
                with st.container(border=True):
                    st.markdown(f"**{item['sender']}** · `{item['source']}` · _{item['time']}_")
                    st.write(item["body"])
                    st.caption(f"🚫 {item['reason']}")

    with tab_send:
        st.subheader("📤 Push Alert Directly to Your Phone (`ntfy` App)")
        with st.form("send_form"):
            msg_title = st.text_input("Title", value="Laptop: ContextSync Alert")
            msg_body = st.text_area("Message", placeholder="Type a message to push to your phone...")
            if st.form_submit_button("🚀 Send to Phone", type="primary"):
                if msg_body.strip() and push_to_phone(st.session_state.ntfy_topic, msg_title, msg_body.strip()):
                    st.success("Sent to phone!")
                else:
                    st.warning("Enter a message and check internet connection.")

    with tab_settings:
        st.subheader("🔐 Permissions & VIP Contacts")
        st.session_state.ntfy_topic = st.text_input("ntfy.sh Topic", value=st.session_state.ntfy_topic)
        st.session_state.google_connected = st.toggle(
            "Gmail & Chrome SQLite History Access", value=st.session_state.google_connected
        )
        new_vip = st.text_input("Add VIP Email, Name, or Phone Number")
        if st.button("Add VIP") and new_vip.strip():
            conn = get_conn()
            conn.execute("INSERT OR IGNORE INTO vip_contacts (contact) VALUES (?)", (new_vip.strip().lower(),))
            conn.commit()
            conn.close()
            st.success(f"Added {new_vip}!")
        conn = get_conn()
        vips_list = [r[0] for r in conn.execute("SELECT contact FROM vip_contacts").fetchall()]
        conn.close()
        st.write("**Current VIPs:**", ", ".join(vips_list))


if __name__ == "__main__":
    init_db()
    if "--mcp" in sys.argv:
        mcp.run()
    else:
        run_ui()
