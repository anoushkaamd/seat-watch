#!/usr/bin/env python3
"""
NEU Seat Watcher (GitHub Actions version)

Each scheduled run checks your courses a few times over about 4 minutes and
sends a phone alert (via the ntfy app) when a full section gets an open seat.
It saves which sections were open to state.json so you get one alert per
opening instead of one every 5 minutes.

Modes:
  watch       normal scheduled run (default)
  once        print current seat counts to the log, no alerts
  test-alert  send a test notification to your phone
  list-terms  print Banner term codes
"""

import argparse
import json
import os
import random
import string
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://nubanner.neu.edu/StudentRegistrationSsb/ssb"
REG_URL = f"{BASE}/registration"

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")
STATE_PATH = os.path.join(HERE, "state.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "4"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "60"))
FAILURE_ALERT_AFTER = 6  # failed runs in a row before you get a "watcher problem" alert
SEAT_ALERT_REPEATS = 4   # notifications sent per opening so you don't miss it
SEAT_ALERT_GAP_SECONDS = 10


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{stamp}] {msg}", flush=True)


# ----------------------------- files ----------------------------------------

def load_watchlist():
    try:
        with open(WATCHLIST_PATH) as f:
            wl = json.load(f)
    except Exception as e:
        sys.exit(f"Could not read watchlist.json: {e}")
    term = str(wl.get("term", "")).strip()
    courses = wl.get("courses", [])
    if not term or not courses:
        sys.exit("watchlist.json needs a \"term\" and at least one entry in \"courses\".")
    for c in courses:
        if "subject" not in c or "number" not in c:
            sys.exit(f"Each course needs \"subject\" and \"number\": {c}")
    return term, courses


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("open", {})
    st.setdefault("failures", 0)
    return st


def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
        f.write("\n")


# ----------------------------- alerts ---------------------------------------

def push(title, msg, priority="high"):
    log(f"ALERT: {title}: {msg}")
    if not NTFY_TOPIC:
        log("NTFY_TOPIC secret is not set, so no phone alert was sent.")
        return
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": priority,
                     "Tags": "rotating_light", "Click": REG_URL},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"Phone alert failed: {e}")


def push_repeated(title, msg):
    """Send the same seat alert several times, a few seconds apart."""
    for i in range(1, SEAT_ALERT_REPEATS + 1):
        push(f"{title} ({i}/{SEAT_ALERT_REPEATS})", msg, priority="urgent")
        if i < SEAT_ALERT_REPEATS:
            time.sleep(SEAT_ALERT_GAP_SECONDS)


# ----------------------------- Banner ---------------------------------------

def new_session(term):
    """Banner ties the selected term to a session cookie, so set it first."""
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (personal NEU seat watcher)"})
    sid = "".join(random.choices(string.ascii_lowercase, k=5)) + str(int(time.time() * 1000))
    r = s.post(
        f"{BASE}/term/search",
        params={"mode": "search"},
        data={"term": term, "studyPath": "", "studyPathText": "",
              "startDatepicker": "", "endDatepicker": "", "uniqueSessionId": sid},
        timeout=20,
    )
    r.raise_for_status()
    s.sid = sid
    return s


def search_course(s, term, subject, number):
    s.post(f"{BASE}/classSearch/resetDataForm", timeout=20)
    r = s.get(
        f"{BASE}/searchResults/searchResults",
        params={
            "txt_subject": subject.upper(),
            "txt_courseNumber": str(number),
            "txt_term": term,
            "startDatepicker": "",
            "endDatepicker": "",
            "uniqueSessionId": s.sid,
            "pageOffset": 0,
            "pageMaxSize": 100,
            "sortColumn": "subjectDescription",
            "sortDirection": "asc",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data")
    if data is None:
        raise RuntimeError("Banner returned no data (session likely expired)")
    return data


def list_terms():
    r = requests.get(f"{BASE}/classSearch/getTerms",
                     params={"searchTerm": "", "offset": 1, "max": 15}, timeout=20)
    r.raise_for_status()
    for t in r.json():
        print(f"{t['code']}  {t['description']}")


def describe(sec):
    return (f"{sec.get('subject')} {sec.get('courseNumber')}-{sec.get('sequenceNumber')} "
            f"(CRN {sec.get('courseReferenceNumber')}) {sec.get('courseTitle', '')}").strip()


def check_all(s, term, courses, state, alert, verbose):
    for item in courses:
        sections = search_course(s, term, item["subject"], item["number"])
        wanted = {str(c) for c in item.get("crns", [])}
        if wanted:
            sections = [x for x in sections if str(x.get("courseReferenceNumber")) in wanted]
            missing = wanted - {str(x.get("courseReferenceNumber")) for x in sections}
            if missing:
                log(f"! CRN(s) {', '.join(sorted(missing))} not found under "
                    f"{item['subject']} {item['number']} for term {term}")
        if not sections:
            log(f"! No sections found for {item['subject']} {item['number']}")

        for sec in sections:
            crn = str(sec.get("courseReferenceNumber"))
            seats = sec.get("seatsAvailable") or 0
            waitlisted = sec.get("waitCount") or 0
            is_open = seats > 0

            if verbose:
                log(f"{describe(sec)}: {'OPEN' if is_open else 'full'}, "
                    f"{sec.get('enrollment')}/{sec.get('maximumEnrollment')} enrolled, "
                    f"{seats} seats, {waitlisted} waitlisted")

            if alert and is_open and not state["open"].get(crn, False):
                note = f" ({waitlisted} on waitlist, seat may go to them)" if waitlisted else ""
                push_repeated("Seat open!", f"{describe(sec)}: {seats} seat(s){note}")

            state["open"][crn] = is_open

        time.sleep(random.uniform(2, 4))


# ----------------------------- main -----------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="watch",
                    choices=["watch", "once", "test-alert", "list-terms"])
    mode = ap.parse_args().mode or "watch"

    if mode == "list-terms":
        list_terms()
        return
    if mode == "test-alert":
        push_repeated("Seat watcher test", "If you see this, phone alerts are working.")
        return

    term, courses = load_watchlist()

    if mode == "once":
        s = new_session(term)
        check_all(s, term, courses, {"open": {}}, alert=False, verbose=True)
        return

    # watch mode
    state = load_state()
    deadline = time.time() + RUN_MINUTES * 60
    success = False
    s = None
    while True:
        try:
            if s is None:
                s = new_session(term)
            log(f"Checking {len(courses)} course(s) for term {term}")
            check_all(s, term, courses, state, alert=True, verbose=True)
            success = True
        except Exception as e:
            log(f"Error: {e}. Will retry with a fresh session.")
            s = None
        wait = POLL_SECONDS + random.uniform(0, 15)
        if time.time() + wait > deadline:
            break
        time.sleep(wait)

    if success:
        state["failures"] = 0
    else:
        state["failures"] += 1
        log(f"All checks failed this run ({state['failures']} run(s) in a row).")
        if state["failures"] == FAILURE_ALERT_AFTER:
            push("Seat watcher problem",
                 f"Checks have failed {FAILURE_ALERT_AFTER} runs in a row. "
                 "Open the Actions tab on GitHub to see the error.", priority="default")
    save_state(state)


if __name__ == "__main__":
    main()
