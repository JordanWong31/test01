#!/usr/bin/env python3
"""Fine Watch checker.

Looks up each plate in docs/config.json on the Macau PSP (FSM) online
ticket service and records the result in docs/status.json.

  * No record          -> no notification.
  * 有違例紀錄 appears  -> urgent phone push immediately (ntfy.sh),
                          then a reminder every `remindEveryMinutes`
                          until you press "Stop reminders".

Control messages (no password needed) are read from the ntfy topic
"<NTFY_TOPIC>-control":  stop | resume | check
The notification's "Stop reminders" button and the dashboard post them.

Only the Python standard library is used.

Environment:
  NTFY_TOPIC     ntfy topic to push to (required for notifications)
  NTFY_SERVER    defaults to https://ntfy.sh
  LOOP_MINUTES   keep running this long (0 = check once and exit)
  GIT_PUSH=1     commit + push docs/status.json while looping
  DRY_RUN=1      check and print only
"""

import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "docs", "config.json")
STATUS_PATH = os.path.join(ROOT, "docs", "status.json")

START_URL = "https://www.fsm.gov.mo/webticket/Webform1.aspx?carClass=L&Lang=C"
FINED_TEXT = "有違例紀錄"
HISTORY_LIMIT = 300
ERROR_ALERT_AFTER = 30      # consecutive failed checks (~30 min) before one warning
TICK_SECONDS = 20           # how often the loop wakes up to read control messages
PUSH_EVERY_MINUTES = 10     # dashboard refresh cadence when nothing changes

SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CONTROL_TOPIC = TOPIC + "-control" if TOPIC else ""


def dashboard_url():
    """https://<owner>.github.io/<repo>/ (GitHub Actions sets GITHUB_REPOSITORY)."""
    url = os.environ.get("DASHBOARD_URL", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/"
    if url and TOPIC:
        url += "#topic=" + TOPIC
    return url


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).replace(microsecond=0).isoformat()


def minutes_since(stamp):
    if not stamp:
        return float("inf")
    return (now() - datetime.fromisoformat(stamp)).total_seconds() / 60


def normalize_plate(p):
    return re.sub(r"[^A-Za-z0-9]", "", p or "").upper()


def pretty_plate(p):
    m = re.fullmatch(r"([A-Z]{1,2})(\d{2})(\d{2})", p)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else p


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- FSM lookup
def _hidden_fields(page):
    return {n: html.unescape(v) for n, v in re.findall(
        r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]*value="([^"]*)"', page)}


def _span(page, span_id):
    m = re.search(r'<span[^>]+id="%s"[^>]*>(.*?)</span>' % re.escape(span_id), page, re.S)
    return re.sub(r"<[^>]+>", "", html.unescape(m.group(1))).strip() if m else ""


def lookup(plate, attempts=4):
    """Look up a plate, retrying on network timeouts (the FSM server is
    sometimes slow to answer overseas servers)."""
    last = None
    for i in range(attempts):
        try:
            return _lookup_once(plate)
        except Exception as e:
            last = e
            log(f"  attempt {i + 1}/{attempts} failed: {type(e).__name__}: {e}")
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise last


def _lookup_once(plate):
    """Return dict(result=fined|clear|unregistered|unknown, text=...)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh) FineWatch/1.1 personal plate check"),
        ("Accept-Language", "zh-HK,zh;q=0.9,en;q=0.8"),
    ]
    page = opener.open(START_URL, timeout=45).read().decode("utf-8", "replace")
    form = _hidden_fields(page)
    form.update({"resW": "1920", "resH": "1080", "Calculator": plate,
                 "btnOk": "確  定", "Textbox1": ""})
    resp = opener.open(START_URL, data=urllib.parse.urlencode(form).encode(), timeout=45)
    result = resp.read().decode("utf-8", "replace")

    if FINED_TEXT in result:
        return {"result": "fined", "text": FINED_TEXT}
    if "沒有登記" in result:
        return {"result": "unregistered", "text": "輸入的車牌編號沒有登記 (plate not registered)"}
    red = _span(result, "lbRed")
    if "WebForm2.aspx" in resp.geturl() and red:
        return {"result": "clear", "text": red}
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", result))
    return {"result": "unknown", "text": re.sub(r"function.*", "", snippet).strip()[:160] or "Unexpected page"}


# ---------------------------------------------------------------- ntfy
def notify(title, message, priority=3, tags=(), with_stop=False):
    if not TOPIC:
        log("  (NTFY_TOPIC not set, skipping notification)")
        return False
    actions = [{"action": "view", "label": "Open FSM site", "url": START_URL}]
    dash = dashboard_url()
    if dash:
        actions.insert(0, {"action": "view", "label": "Open dashboard", "url": dash})
    if with_stop:
        actions.insert(0, {"action": "http", "label": "Stop reminders", "method": "POST",
                           "url": f"{SERVER}/{CONTROL_TOPIC}", "body": "stop", "clear": True})
    payload = json.dumps({"topic": TOPIC, "title": title, "message": message,
                          "priority": priority, "tags": list(tags),
                          "click": dash or START_URL, "actions": actions}).encode()
    req = urllib.request.Request(SERVER, data=payload, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            log(f"  -> notified: {title}")
            return True
        except Exception as e:
            log(f"  notify attempt {attempt + 1} failed: {e}")
            time.sleep(3)
    return False


def read_control(status):
    """Return list of new control commands (stop/resume/check)."""
    if not CONTROL_TOPIC:
        return []
    since = status.get("controlSince") or str(int(time.time()) - 60)
    url = f"{SERVER}/{CONTROL_TOPIC}/json?poll=1&since={since}"
    try:
        lines = urllib.request.urlopen(url, timeout=15).read().decode().splitlines()
    except Exception as e:
        log(f"  control read failed: {e}")
        return []
    cmds = []
    for line in lines:
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") != "message":
            continue
        status["controlSince"] = m["id"]
        word = (m.get("message") or "").strip().lower()
        if word in ("stop", "resume", "check"):
            cmds.append(word)
    return cmds


# ---------------------------------------------------------------- core
def reminder_message(st):
    return (f"Still showing {FINED_TEXT} (since {st['since'][:16].replace('T', ' ')} UTC). "
            "Tap 'Stop reminders' once you've dealt with it.")


def check_plate(plate, st, cfg, dry):
    """Run one FSM lookup for a plate and update its state."""
    log(f"Checking {plate} ...")
    try:
        r = lookup(plate)
    except Exception as e:
        r = {"result": "error", "text": f"{type(e).__name__}: {e}"[:200]}
    log(f"  result: {r['result']} ({r['text']})")
    t = iso()
    prev_entry = (st.get("history") or [None])[0]
    entry = {"at": t, "result": r["result"], "text": r["text"]}
    # Collapse identical consecutive results so history stays readable
    if prev_entry and prev_entry["result"] == r["result"]:
        prev_entry["last"] = t
        prev_entry["count"] = prev_entry.get("count", 1) + 1
    else:
        st["history"] = ([entry] + st.get("history", []))[:HISTORY_LIMIT]
    st["lastChecked"] = t
    st["lastText"] = r["text"]

    if r["result"] in ("error", "unknown"):
        st["errors"] = st.get("errors", 0) + 1
        if st["errors"] == ERROR_ALERT_AFTER and not dry:
            notify(f"⚠️ Fine Watch can't check {st['display']}",
                   f"The last {ERROR_ALERT_AFTER} checks failed. Last error: {r['text']}",
                   priority=2, tags=["warning"])
        return False

    st["errors"] = 0
    st["lastSuccess"] = t
    prev = st.get("result")
    changed = r["result"] != prev
    if changed:
        st["since"] = t
    st["result"] = r["result"]

    if r["result"] == "fined" and changed and not dry:
        # New fine: alert immediately and (re)start reminders.
        st["stopped"] = None
        if notify(f"🚨 {st['display']} {FINED_TEXT} — you have a traffic fine",
                  "The Macau PSP site shows a violation record for your car. "
                  "Tap to open the FSM site. You'll be reminded until you tap 'Stop reminders'.",
                  priority=5, tags=["rotating_light", "car"], with_stop=True):
            st["lastNotified"] = t
            st["reminders"] = 0
    elif r["result"] != "fined":
        st["lastNotified"] = None
        st["stopped"] = None
    return changed


def maybe_remind(st, cfg, dry):
    if dry or st.get("result") != "fined" or st.get("stopped"):
        return
    every = float(cfg.get("remindEveryMinutes", 5))
    if minutes_since(st.get("lastNotified")) >= every - 0.1:
        n = st.get("reminders", 0) + 1
        if notify(f"⏰ Reminder #{n}: {st['display']} has a traffic fine",
                  reminder_message(st), priority=4, tags=["alarm_clock", "car"], with_stop=True):
            st["lastNotified"] = iso()
            st["reminders"] = n


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def push_status():
    git("add", "docs/status.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", f"Status update {iso()}")
    for _ in range(3):
        pull = git("pull", "--rebase", "--autostash", "-q", "-X", "theirs")
        if git("push", "-q").returncode == 0:
            log("  pushed status")
            return
        log("  push failed, retrying", pull.stderr.strip()[:200])
        time.sleep(5)


def save(status):
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    loop_minutes = float(os.environ.get("LOOP_MINUTES", "0"))
    use_git = os.environ.get("GIT_PUSH") == "1" and not dry
    deadline = time.time() + loop_minutes * 60

    if use_git:  # a queued run may start from an older checkout
        git("pull", "--rebase", "--autostash", "-q", "-X", "theirs")
    status = load_json(STATUS_PATH, {"plates": {}})
    last_push = 0.0
    force_check = True

    while True:
        if use_git:  # pick up plate/interval changes made from the dashboard
            git("pull", "--rebase", "--autostash", "-q", "-X", "theirs")
        cfg = load_json(CONFIG_PATH, {"plates": []})
        plates = [normalize_plate(p) for p in cfg.get("plates", []) if normalize_plate(p)]
        check_every = float(cfg.get("checkEveryMinutes", 3))
        status["plates"] = {p: v for p, v in status.get("plates", {}).items() if p in plates}
        status["checkEveryMinutes"] = check_every
        status["remindEveryMinutes"] = float(cfg.get("remindEveryMinutes", 5))
        status["running"] = loop_minutes > 0
        dirty = False

        for cmd in read_control(status):
            log(f"  control: {cmd}")
            dirty = True
            if cmd == "check":
                force_check = True
            for st in status["plates"].values():
                if cmd == "stop" and st.get("result") == "fined" and not st.get("stopped"):
                    st["stopped"] = iso()
                    if not dry:
                        notify(f"🔕 Reminders stopped for {st['display']}",
                               "You won't be reminded again unless the record clears and a new one appears.",
                               priority=2, tags=["no_bell"])
                elif cmd == "resume" and st.get("stopped"):
                    st["stopped"] = None
                    st["lastNotified"] = None  # remind right away

        for plate in plates:
            st = status["plates"].setdefault(plate, {
                "plate": plate, "display": pretty_plate(plate), "result": "pending",
                "since": None, "lastNotified": None, "stopped": None, "errors": 0, "history": []})
            maybe_remind(st, cfg, dry)  # never let a slow FSM check delay a reminder
            wait = check_every if st.get("errors", 0) == 0 else min(1.0, check_every)
            if force_check or minutes_since(st.get("lastChecked")) >= wait - 0.1:
                dirty |= check_plate(plate, st, cfg, dry)
            maybe_remind(st, cfg, dry)

        status["updated"] = iso()
        if not dry:
            save(status)
        if use_git and (dirty or force_check or time.time() - last_push >= PUSH_EVERY_MINUTES * 60):
            push_status()
            last_push = time.time()
        force_check = False

        if time.time() + TICK_SECONDS >= deadline:
            break
        time.sleep(TICK_SECONDS)

    if dry:
        print(json.dumps(status, ensure_ascii=False, indent=2)[:1500])
    elif use_git:
        status["running"] = False
        save(status)
        push_status()


if __name__ == "__main__":
    sys.exit(main())
