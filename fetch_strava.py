#!/usr/bin/env python3
"""
Pull recent Strava activities and write data.json for the training page.

Runs on GitHub Actions. Reads three secrets from the environment:
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN

Writes data.json in the repo root. Nothing secret is ever written to it.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"

# How many recent activities to pull, and how many to fetch full detail for.
ACTIVITY_COUNT = 10
DETAIL_COUNT = 6


def http(url, data=None, headers=None):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode() if data else None,
        headers=headers or {},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    for key in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        if not os.environ.get(key):
            sys.exit(f"Missing required secret: {key}")

    res = http(TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    })
    return res["access_token"]


def main():
    token = get_access_token()
    auth = {"Authorization": f"Bearer {token}"}

    summaries = http(
        f"{API}/athlete/activities?per_page={ACTIVITY_COUNT}", headers=auth
    )

    activities = []
    best_efforts = {}
    segments = {}

    for i, a in enumerate(summaries):
        start = a.get("start_date_local", "")[:10]
        dist_m = a.get("distance") or 0

        entry = {
            "date": start,
            "name": a.get("name") or "Activity",
            "sport": a.get("sport_type") or a.get("type"),
            "distance_km": round(dist_m / 1000, 2) if dist_m else None,
            "moving_time_sec": a.get("moving_time"),
            "elevation_m": round(a.get("total_elevation_gain") or 0, 1),
            "avg_cadence": a.get("average_cadence"),
            "calories": a.get("calories"),
            "pr_count": a.get("pr_count"),
        }
        activities.append(entry)

        # Detailed call gives best efforts and segment efforts, but costs an
        # API request each, so only do it for the most recent few.
        if i >= DETAIL_COUNT:
            continue
        try:
            detail = http(f"{API}/activities/{a['id']}", headers=auth)
        except Exception:
            continue

        if entry["calories"] is None:
            entry["calories"] = detail.get("calories")

        for be in detail.get("best_efforts") or []:
            key = "fastest_" + (be.get("name") or "").lower().replace(" ", "").replace("/", "")
            t = be.get("moving_time") or be.get("elapsed_time")
            if t and (key not in best_efforts or t < best_efforts[key]):
                best_efforts[key] = t

        for se in detail.get("segment_efforts") or []:
            seg = se.get("segment") or {}
            sid = str(seg.get("id"))
            if not sid or sid == "None":
                continue
            segments.setdefault(sid, {
                "name": seg.get("name") or "Segment",
                "distance_m": seg.get("distance"),
                "efforts": [],
            })["efforts"].append({
                "date": (se.get("start_date_local") or "")[:10],
                "time_sec": se.get("moving_time") or se.get("elapsed_time"),
            })

    # Normalise best-effort keys to what the page expects.
    alias = {
        "fastest_400m": ["fastest_400m"],
        "fastest_1k": ["fastest_1k"],
        "fastest_mile": ["fastest_1mile", "fastest_mile"],
        "fastest_2mile": ["fastest_2mile"],
        "fastest_5k": ["fastest_5k"],
        "fastest_10k": ["fastest_10k"],
    }
    bests = {}
    for out_key, candidates in alias.items():
        for c in candidates:
            if c in best_efforts:
                bests[out_key] = best_efforts[c]
                break

    # Keep only segments run more than once, plus the longest few — those are
    # the ones worth tracking progress on.
    seg_list = sorted(
        segments.values(),
        key=lambda s: (-len(s["efforts"]), -(s.get("distance_m") or 0)),
    )
    seg_list = [s for s in seg_list if len(s["efforts"]) > 1][:3] or seg_list[:4]
    for s in seg_list:
        s["efforts"].sort(key=lambda e: e["date"])

    out = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "activities": activities,
        "bests": bests,
        "segments": seg_list,
    }

    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote data.json — {len(activities)} activities, "
          f"{len(bests)} best efforts, {len(seg_list)} segments.")


if __name__ == "__main__":
    main()
