#!/usr/bin/env python3
"""Fine Watch checker.

Looks up each plate in docs/config.json on the Macau PSP (FSM) online
ticket service, records the result in docs/status.json, and sends a phone
push through ntfy.sh when the result is 有違例紀錄 (has violation records).

Only the Python standard library is used, so no install step is needed.

Environment:
  NTFY_TOPIC   ntfy topic to push to (required to send notifications)
  NTFY_SERVER  defaults to https://ntfy.sh
  DRY_RUN=1    check and print, but do not notify or write files
"""

import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "docs", "config.json")
STATUS_PATH = os.path.join(ROOT, "docs", "status.json")

BASE = "https://www.fsm.gov.mo/webticket/"
START_URL = BASE + "Webform1.aspx?carClass=L&Lang=C"
PUBLIC_URL = START_URL  # link opened when the notification is tapped

FINED_TEXT = "有違例紀錄"
HISTORY_LIMIT = 300
ERROR_ALERT_AFTER = 8  # consecutive failed checks (~4h at 30 min) before warning


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hours_since(iso):
    if not iso:
        return float("inf")
    then = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def normalize_plate(p):
    return re.sub(r"[^A-Za-z0-9]", "", p or "").upper()


def pretty_plate(p):
    # AA8092 -> AA-80-92 (Macau style); leave anything unusual as-is
    m = re.fullmatch(r"([A-Z]{1,2})(\d{2})(\d{2})", p)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else p


def hidden_fields(page):
    fields = {}
    for name, value in re.findall(
        r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]*value="([^"]*)"', page
    ):
        fields[name] = html.unescape(value)
    return fields


def span_text(page, span_id):
    m = re.search(r'<span[^>]+id="%s"[^>]*>(.*?)</span>' % re.escape(span_id), page, re.S)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", html.unescape(m.group(1))).strip()


def lookup(plate):
    """Return dict(result=..., text=...). result is fined | clear | unknown."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh) FineWatch/1.0 personal plate check"),
        ("Accept-Language", "zh-HK,zh;q=0.9,en;q=0.8"),
    ]

    page = opener.open(START_URL, timeout=30).read().decode("utf-8", "replace")
    form = hidden_fields(page)
    form.update({
        "resW": "1920",
        "resH": "1080",
        "Calculator": plate,
        "btnOk": "確  定",
        "Textbox1": "",
    })
    data = urllib.parse.urlencode(form).encode()
    resp = opener.open(START_URL, data=data, timeout=30)
    result_page = resp.read().decode("utf-8", "replace")

    red = span_text(result_page, "lbRed")
    if FINED_TEXT in result_page:
        return {"result": "fined", "text": red or FINED_TEXT}
    if "沒有登記" in result_page:
        return {"result": "unregistered", "text": "輸入的車牌編號沒有登記 (plate not registered)"}
    if "WebForm2.aspx" in resp.geturl() and red:
        # Result page reached and the result line says something else
        # (e.g. 沒有違例紀錄) -> no fines.
        return {"result": "clear", "text": red}
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", result_page))
    snippet = re.sub(r"function.*", "", snippet).strip()[:160]
    return {"result": "unknown", "text": snippet or "Unexpected page"}


def notify(title, message, priority="default", tags="", url=PUBLIC_URL):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("  (NTFY_TOPIC not set, skipping notification)")
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
        "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}[priority],
        "tags": [t for t in tags.split(",") if t],
        "click": url,
        "actions": [{"action": "view", "label": "Open FSM site", "url": url}],
    }).encode()
    req = urllib.request.Request(server, data=payload, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print(f"  -> notified: {title}")
            return True
        except Exception as e:  # network hiccup, retry
            print(f"  notify attempt {attempt + 1} failed: {e}")
            time.sleep(3)
    return False


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    config = load_json(CONFIG_PATH, {"plates": []})
    status = load_json(STATUS_PATH, {"plates": {}})
    remind_hours = float(config.get("remindEveryHours", 24))
    checked_at = now_iso()

    plates = [normalize_plate(p) for p in config.get("plates", []) if normalize_plate(p)]
    if not plates:
        print("No plates configured in docs/config.json")
    # Drop plates that were removed from the config
    status["plates"] = {p: v for p, v in status.get("plates", {}).items() if p in plates}

    for plate in plates:
        st = status["plates"].setdefault(plate, {
            "plate": plate, "display": pretty_plate(plate), "result": "pending",
            "since": None, "lastNotified": None, "errors": 0, "history": [],
        })
        prev = st.get("result")
        print(f"Checking {plate} ...")
        try:
            r = lookup(plate)
        except Exception as e:
            r = {"result": "error", "text": f"{type(e).__name__}: {e}"[:200]}
        print(f"  result: {r['result']} ({r['text']})")

        entry = {"at": checked_at, "result": r["result"], "text": r["text"]}
        st["history"] = ([entry] + st.get("history", []))[:HISTORY_LIMIT]
        st["lastChecked"] = checked_at
        st["lastText"] = r["text"]
        name = st["display"]

        if r["result"] == "unregistered":
            st["result"] = "unregistered"
            st["errors"] = 0
            st["lastSuccess"] = checked_at
            continue
        if r["result"] in ("error", "unknown"):
            st["errors"] = st.get("errors", 0) + 1
            if st["errors"] == ERROR_ALERT_AFTER and not dry:
                notify(f"⚠️ Fine Watch can't check {name}",
                       f"The last {ERROR_ALERT_AFTER} checks failed. Last error: {r['text']}",
                       priority="low", tags="warning")
            continue  # keep the previous known result

        st["errors"] = 0
        changed = r["result"] != prev
        if changed:
            st["since"] = checked_at
        st["result"] = r["result"]
        st["lastSuccess"] = checked_at

        if dry:
            continue
        if r["result"] == "fined":
            if changed or hours_since(st.get("lastNotified")) >= remind_hours:
                first = changed
                title = (f"🚨 {name} 有違例紀錄 — you have a traffic fine"
                         if first else f"⏰ Reminder: {name} still has a traffic fine")
                msg = ("The Macau PSP site shows 有違例紀錄 (violation record) for your car. "
                       "Tap to open the FSM site and check the details.")
                if not first:
                    msg = f"Still showing 有違例紀錄 since {st['since'][:10]}. " + msg
                if notify(title, msg, priority="urgent" if first else "high",
                          tags="rotating_light,car"):
                    st["lastNotified"] = checked_at
        elif r["result"] == "clear" and prev == "fined":
            notify(f"✅ {name} is clear", "No more violation records on the FSM site. Drive safe!",
                   tags="white_check_mark,car")
            st["lastNotified"] = None

    status["updated"] = checked_at
    if dry:
        print(json.dumps(status, ensure_ascii=False, indent=2)[:1500])
        return
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
