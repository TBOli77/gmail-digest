#!/usr/bin/env python3
"""
gmail_digest.py  —  v3.1 stable

Key changes vs v3:
• Credentials loader prefers TOKEN_JSON env (no file required), then token.json
• Robust refresh with explicit messages for invalid_grant / invalid_client
• No interactive OAuth in CI (GITHUB_ACTIONS or NO_OAUTH_LOCAL=1)
• Otherwise functionality preserved: GPT-4o summaries, categories, follow-ups,
  clean HTML digest + Notion logging.
"""

from __future__ import annotations
import base64
import datetime as dt
import html
import json
import os
import re
import textwrap
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Dict, List, Tuple, Any

import openai
openai.api_key = os.getenv("OPENAI_API_KEY")

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError

from notion_client import Client

# ─── CONFIG ──────────────────────────────────────────────────────────────
CLIENT_ID: str     = os.getenv("GMAIL_CLIENT_ID", "").strip()
CLIENT_SECRET: str = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
SEND_TO: str       = os.getenv("SEND_TO", "thiago.oliveira77@gmail.com").strip()

MODEL           = "gpt-4o"
SUMMARY_TOKENS  = 120
CHUNK_SIZE      = 1900          # Notion block limit ≈2 k
WINDOW_SECONDS  = 24 * 3600

NOTION_SECRET = os.getenv("NOTION_SECRET", "").strip()
NOTION_DB_ID  = os.getenv("NOTION_DB_ID", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# ─── CATEGORY RULES (first match wins) ───────────────────────────────────
CATEGORY_RULES: List[Tuple[str, re.Pattern[str]]] = [
    ("Family", re.compile(r"\b(gilmara|lucas|jo[aã]o ?pedro|alvaro|sonia|m[ãa]e|pai)\b", re.I)),
    ("School", re.compile(r"(highlands|naperville203|talk203|kennedy\s+junior\s+high|elementary"
                          r"|teacher|district\s*203|infinitecampus|screening results|language acquisition)", re.I)),
    ("Activities", re.compile(r"(soccer|\bnsa\b|ice cream social|tour|camp|clinic)", re.I)),
    ("Market Update", re.compile(r"(usiminas|analises@bb|\bbb-bi\b|@valor\.com|valor\b|market\s+update)", re.I)),
    ("Bills & Finance", re.compile(r"(invoice|bill|payment|transfer|investment|statement|funded"
                                   r"|usage limits|cartola|boleto|fatura|openai)", re.I)),
    ("Housing", re.compile(r"(rental|lease|property|realt(y|or)|zillow|redfin|mls listing)", re.I)),
    ("Purchases & Offers", re.compile(r"(order|receipt|reward|promo|offer|shopping|amazon)", re.I)),
    ("Meetings & Invites", re.compile(r"(invitation|event|meet|reuni[ãa]o|\.ics|calendar)", re.I)),
    ("Newsletters", re.compile(r"(mckinsey\.com|emails?\.hbr\.org|hbr\.org|@interactive\.wsj\.com"
                               r"|newsletter|weekly digest|digest update)", re.I)),
    ("Work", re.compile(r"@arcelormittal", re.I)),   # ONLY ArcelorMittal
    ("Personal", re.compile(r"", re.I)),             # fallback
    ("Other", re.compile(r".*", re.S)),
]

# ─── FOLLOW-UP PATTERNS ──────────────────────────────────────────────────
NEED_ACTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("Send reply",        re.compile(r"(please\s+reply|need\s+response|awaiting\s+your\s+reply)", re.I)),
    ("Provide document",  re.compile(r"(send|provide|need).+?(lease|photo|headshot|picture|bc|birth certificate"
                                     r"|invoice|attachment|document)", re.I)),
    ("Schedule meeting",  re.compile(r"(schedule|book|arrange).+?(call|meeting|appointment)", re.I)),
    ("Confirm attendance",re.compile(r"(rsvp|confirm).+?(attendance|presence)", re.I)),
]

# ─── HELPER FUNCTIONS ────────────────────────────────────────────────────
def _load_creds_from_json_blob(blob: str) -> Credentials:
    """
    Accepts a JSON string representing an "authorized_user" credential and
    returns a google.oauth2.credentials.Credentials with SCOPES applied.
    """
    data = json.loads(blob)
    # Allow both "scopes" (string) and "scope" (compat)
    scopes = data.get("scopes") or data.get("scope")
    if isinstance(scopes, str):
        data["scopes"] = scopes
    # Ensure required fields are present
    for k in ("client_id", "client_secret", "refresh_token"):
        if not data.get(k):
            raise ValueError(f"token.json missing required field: {k}")
    return Credentials.from_authorized_user_info(data, SCOPES)

def _maybe_refresh(creds: Credentials) -> Credentials:
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            return creds
        except RefreshError as e:
            msg = str(e)
            # Common cases explained:
            if "invalid_grant" in msg:
                raise RuntimeError(
                    "Google returned invalid_grant while refreshing.\n"
                    "Your refresh token is expired or revoked. Generate a new TOKEN_JSON via OAuth Playground "
                    "and update the GitHub Secret."
                ) from e
            if "invalid_client" in msg:
                raise RuntimeError(
                    "Google returned invalid_client while refreshing.\n"
                    "Check that client_id/client_secret in TOKEN_JSON match the *same* Google Cloud project."
                ) from e
            raise
    raise RuntimeError("Credentials are invalid and cannot be refreshed (no refresh_token).")

def get_credentials() -> Credentials:
    """
    Load credentials in this order:
      1) TOKEN_JSON env (preferred for CI)
      2) token.json file (legacy)
      3) Interactive OAuth **only when not in CI** (local dev)
    """
    token_env = os.getenv("TOKEN_JSON", "").strip()
    if token_env:
        creds = _load_creds_from_json_blob(token_env)
        return _maybe_refresh(creds)

    if os.path.exists("token.json"):
        with open("token.json", "r", encoding="utf-8") as f:
            creds = Credentials.from_authorized_user_info(json.load(f), SCOPES)
        creds = _maybe_refresh(creds)
        # write back refreshed token for local runs
        try:
            with open("token.json", "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        except Exception:
            pass
        return creds

    # Only fall back to interactive flow locally
    if os.getenv("GITHUB_ACTIONS") or os.getenv("NO_OAUTH_LOCAL") == "1":
        raise RuntimeError(
            "No TOKEN_JSON env and no token.json found; interactive OAuth is disabled in CI.\n"
            "Create a refresh token via OAuth Playground and set TOKEN_JSON secret."
        )

    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("CLIENT_ID/CLIENT_SECRET env vars are required for local interactive OAuth.")

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uris": ["http://localhost:8765/"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        SCOPES,
    )
    creds = flow.run_local_server(port=8765, prompt="consent")
    try:
        with open("token.json", "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    except Exception:
        pass
    return creds

def list_msg_ids(svc, after_ts: int) -> List[str]:
    q = f"after:{after_ts} -category:promotions -category:social -in:spam"
    ids, resp = [], svc.users().messages().list(userId="me", q=q).execute()
    ids += [m["id"] for m in resp.get("messages", [])]
    while resp.get("nextPageToken"):
        resp = svc.users().messages().list(
            userId="me", q=q, pageToken=resp["nextPageToken"]).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
    return ids

def fetch_full(svc, msg_id: str) -> Dict[str, Any]:
    return svc.users().messages().get(userId="me", id=msg_id, format="full").execute()

def meta_from_full(full: Dict[str, Any]) -> Dict[str, Any]:
    hdr = {h["name"].lower(): h["value"] for h in full["payload"]["headers"]}
    return {
        "id": full["id"],
        "subject": hdr.get("subject", "(sem assunto)"),
        "from": parseaddr(hdr.get("from", ""))[1],
        "date": hdr.get("date", ""),
        "important": "IMPORTANT" in full.get("labelIds", []),
        "snippet": html.unescape(full.get("snippet", "")),
        "attachments": collect_attachments(full),
    }

def collect_attachments(full: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    def walk(parts):
        for p in parts:
            if fname := p.get("filename"):
                files.append(fname)
            if "parts" in p:
                walk(p["parts"])
    walk(full.get("payload", {}).get("parts", []))
    return files

def extract_plain_text(full: Dict[str, Any]) -> str:
    """Walks the Gmail message parts and returns all text/plain or text/html content decoded."""
    def walk(parts):
        texts: List[str] = []
        for p in parts:
            ct = p.get("mimeType", "")
            data = p.get("body", {}).get("data")
            if data and ct in ("text/plain", "text/html"):
                txt = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
                if ct == "text/html":
                    txt = re.sub(r"<[^>]+>", " ", txt)
                texts.append(txt)
            if "parts" in p:
                texts.extend(walk(p["parts"]))
        return texts
    return "\n".join(walk(full.get("payload", {}).get("parts", [])))

# ─── SUMMARISER ──────────────────────────────────────────────────────────
def summarise(subject: str, text: str) -> str:
    if not text:
        return "Summary not available."
    sys_prompt = "Summarise the email in 1 paragraph. **Do not** repeat the subject."
    try:
        resp = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": textwrap.shorten(text, width=1200, placeholder=" …")},
            ],
            max_tokens=SUMMARY_TOKENS,
            temperature=0.2,
        )
        summary = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ summarise() failed for subject={subject!r}: {e}")
        raise

    subj_norm = re.sub(r"\W+", "", subject.lower())
    summ_norm = re.sub(r"\W+", "", summary.lower())
    if subj_norm and summ_norm.startswith(subj_norm[:30]):
        summary = textwrap.shorten(text, width=180, placeholder=" …") or "Summary not available."
    return summary

# ─── CATEGORY & FOLLOW-UP ────────────────────────────────────────────────
def categorise(meta: Dict[str, Any]) -> str:
    hay = f"{meta['subject']} {meta['from']}".lower()
    for cat, pat in CATEGORY_RULES:
        if pat.search(hay):
            return cat
    return "Other"

def detect_followup(meta: Dict[str, Any], summary: str) -> Tuple[bool, str | None]:
    hay = f"{meta['subject']} {summary} {meta['snippet']}"
    for act, pat in NEED_ACTION_PATTERNS:
        if pat.search(hay):
            return True, act
    if re.search(r"^re:\s", meta["subject"], re.I):
        return True, "Send reply"
    return False, None

# ─── HTML DIGEST BUILDER ────────────────────────────────────────────────
CARD_CSS = "margin:8px 0;padding:12px;border:1px solid #e0e0e0;border-radius:8px;"

def build_digest(groups: Dict[str, List[Dict[str, Any]]]):
    sections, attachments, followups = [], [], []
    ref_no = 1
    for cat, items in groups.items():
        if not items:
            continue
        seg = [f"<h3>{cat}</h3>"]
        for m in items:
            ref = f"[{ref_no:02d}]"
            try:
                date_fmt = dt.datetime.strptime(m["date"][:25], "%a, %d %b %Y %H:%M:%S").strftime("%d/%m/%Y")
            except Exception:
                date_fmt = ""
            header = f"{ref} {html.escape(m['subject'])} — {html.escape(m['from'])} {f'({date_fmt})' if date_fmt else ''}"
            seg.append(
                f'<div style="{CARD_CSS}"><div style="font-weight:bold;">{header}</div>'
                f'<div style="color:#555;margin-top:4px;">{html.escape(m["summary"])}</div></div>'
            )
            for f in m["attachments"]:
                attachments.append((f, ref, m["from"]))
            need_fu, act = detect_followup(m, m["summary"])
            if need_fu and act:
                followups.append({"ref": ref, "action": act, "subject": m["subject"]})
            ref_no += 1
        sections.append("\n".join(seg))
    return "\n".join(sections), attachments, followups

def build_suggestions(groups: Dict[str, List[Dict[str, Any]]], followups, attach_n: int) -> List[str]:
    s: List[str] = []
    if groups.get("Activities"):
        s.append("Mark upcoming sports / activity dates on the calendar.")
    if groups.get("Purchases & Offers"):
        s.append("Consider unsubscribing from promotional newsletters.")
    if attach_n:
        s.append("Download and file important attachments.")
    if followups:
        s.append("Schedule time today to clear pending follow-ups.")
    if not s:
        s.append("Inbox looks good today — no suggestions!")
    return s

# ─── NOTION LOGGER ───────────────────────────────────────────────────────
def strip_html(ht: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", ht)).strip()

def add_to_notion(html_digest: str) -> None:
    if not (NOTION_SECRET and NOTION_DB_ID):
        return
    
    plain_digest = strip_html(html_digest)

    # Also prepare Notion blocks for detailed content
    lines = [ln for ln in plain_digest.splitlines() if ln.strip()]
    blocks: List[Dict[str, Any]] = []
    for ln in lines:
        if re.match(r"^[📊📝📎🤖]|^[A-Z][a-z]+", ln):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": ln}}]}
            })
        else:
            for chunk in (ln[i:i+CHUNK_SIZE] for i in range(0, len(ln), CHUNK_SIZE)):
                blocks.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}]
                    }
                })

    Client(auth=NOTION_SECRET).pages.create(
        parent={"database_id": NOTION_DB_ID},
        properties={
            "Name": {"title": [{"text": {"content": f"Digest {dt.date.today()}"}}]},
            "Digest": {"rich_text": [{"text": {"content": plain_digest}}]}
        },
        children=blocks[:50]  # optional: limit to avoid API max
    )

# ─── MAIN ────────────────────────────────────────────────────────────────
def main() -> None:
    creds = get_credentials()
    svc = build("gmail", "v1", credentials=creds)

    after_ts = int(time.time()) - WINDOW_SECONDS
    msg_ids = list_msg_ids(svc, after_ts)

    metas, seen = [], set()
    for mid in msg_ids:
        full = fetch_full(svc, mid)
        meta = meta_from_full(full)
        # Skip the digests I send myself
        if meta["subject"].startswith("📬 Gmail Daily Digest"):
            continue
        if meta["subject"] in seen:
            continue
        seen.add(meta["subject"])
        body_text = extract_plain_text(full) or meta["snippet"] or meta["subject"]
        meta["summary"] = summarise(meta["subject"], body_text)
        metas.append(meta)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for m in metas:
        groups.setdefault(categorise(m), []).append(m)

    body_html, attachments, followups = build_digest(groups)

    overview = {
        "total": len(metas),
        "important": sum(1 for m in metas if m["important"]),
        "attach": len(attachments),
    }
    sugg = build_suggestions(groups, followups, len(attachments))

    today = dt.datetime.now().strftime("%d/%m/%Y")
    action_items_html = ("".join(f"<li>[Action: {fu['action']}] {fu['ref']} {html.escape(fu['subject'])}</li>"
                                 for fu in followups) or "<li>None</li>")
    attachments_html = ("".join(f"<li>{html.escape(f)} — {r} — {html.escape(s)}</li>"
                                for f, r, s in attachments) or "<li>None</li>")
    suggestions_html = "".join(f"<li>{html.escape(x)}</li>" for x in sugg)

    html_digest = f"""
    <html><body style="font-family:Helvetica,Arial;background:#f6f8fa;padding:24px;">
      <div style="max-width:680px;margin:auto;background:#fff;padding:24px;border-radius:12px;">
        <h2 style="margin-top:0">📬 Gmail Daily Digest <span style="font-size:14px;color:#888">— {today}</span></h2>
        <h3>📊 Overview</h3>
        <ul><li>Total: {overview['total']} | Important: {overview['important']} | Attachments: {overview['attach']}</li></ul>
        {body_html}
        <h3>📝 Action Items</h3>
        <ul>{action_items_html}</ul>
        <h3>📎 Attachments</h3>
        <ul>{attachments_html}</ul>
        <h3>🤖 Suggestions</h3>
        <ul>{suggestions_html}</ul>
      </div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📬 Gmail Daily Digest — {today}"
    msg["From"] = msg["To"] = SEND_TO
    msg.attach(MIMEText(html_digest, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()

    add_to_notion(html_digest)
    print("✅ Improved digest emailed & logged to Notion!")

if __name__ == "__main__":
    try:
        main()
    except HttpError as err:
        print("❌ Gmail API error:", err)
    except Exception as e:
        print("❌ Fatal error:", e)
