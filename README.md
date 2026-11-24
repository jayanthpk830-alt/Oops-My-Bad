# Oops-My-Bad
# 🛡️ ContextSync MCP — Local-First Cross-Source Context & Safety Hub
> **wHACKiest 2026 (MSRIT) • Track 1: Make MCP Matter**

## 📌 Overview
**ContextSync MCP** is a local-first Python web application (`Streamlit` at `localhost:8501`) built on the official **Model Context Protocol (MCP) Python SDK**. It unifies fragmented personal signals across **Gmail**, **Local Chrome SQLite History**, and **Bi-Directional Mobile Alerts (`ntfy.sh`)**—while protecting sensitive credentials locally via regex scrubbing and enforcing a **1-Click Human Approval Gate** before executing external actions.

---

## ✨ Key Features Implemented in This MVP
1. **Local Regex Privacy Shield:** Automatically strips 4–8 digit OTPs (`[REDACTED_OTP]`), 16-digit card numbers (`[REDACTED_CARD]`), and passwords/PINs on-device before storage or AI reasoning.
2. **Cross-Source Verification & Auto-Triage (`IMP` / `REAL` / `SPAM`):**
   * **🚨 IMP (Top Priority):** Matches VIP senders/keywords across email and mobile alerts and queues outbound actions for approval.
   * **✅ REAL (Verified):** Cross-checks merchant/order alerts against local Chrome SQLite History and Gmail receipt patterns.
   * **🛡️ SPAM (Blocked):** Flags phishing URLs and urgent KYC/lottery lures that lack matching email or browser history.
3. **2-Way Phone ↔ Laptop Sync (`ntfy.sh`):**
   * Pulls incoming mobile alerts from `ntfy.sh` with message-ID deduplication.
   * Pushes urgent VIP escalations, approval confirmations, and custom replies directly back to the user's phone (`ntfy` app).
4. **1-Click Human Approval Gate:** Blocks all outbound Gmail reply drafts or calendar updates in a local SQLite queue (`WAL` mode) until the user explicitly clicks **Approve** in the dashboard.

---

## 🛠️ Tech Stack
* **Protocol:** Official MCP Python SDK (`mcp`)
* **Backend & Database:** Python + Local SQLite (`PRAGMA journal_mode=WAL`)
* **Mobile Relay:** 2-Way `ntfy.sh` Webhook
* **Frontend Command Center:** Streamlit (`localhost:8501`)

---

## 🚀 How to Run Locally

1. **Clone the repository:**
   ```bash
   git clone https://github.com<your-username>/contextsync-mcp.git
   cd contextsync-mcp
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Launch the Streamlit Command Center:**
   ```bash
   streamlit run app.py
   ```
   Open `http://localhost:8501` in your browser.
