#!/usr/bin/env python3
"""
Pull a comprehensive Strava snapshot and write data.json for the training page.

Runs on GitHub Actions. Reads three secrets from the environment:
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN

PRIVACY NOTE: this repo is public, so data.json is publicly readable.
GPS traces, polylines, start/end coordinates and the athlete's name, city
and profile photo are deliberately NOT written. Distances, times, paces,
cadence, elevation, segment names and activity titles ARE written.
Secrets are never written.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"

ACTIVITY_COUNT = 60      # enough for ~12 weeks of volume history
DETAIL_COUNT = 10        # full detail (splits, laps, efforts) for the most recent
STREAM_COUNT = 3         # time-series charts for the most recent few
WEEKS_HISTORY = 12


def http(url, data=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data).encode() if data else None,
                headers=headers or {},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:                      # rate limited
                time.sleep(8 * (attempt + 1))
                continue
            if e.code in (404, 403):
                return None
            if attempt == retries - 1:
                raise
            time.sleep(2)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    return None


def get_access_token():
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


def pace_str(dist_m, sec):
    if not dist_m or not sec:
        return None
    p = sec / (dist_m / 1000.0)
    return f"{int(p // 60)}:{int(round(p % 60)):02d}"


def main():
    token = get_access_token()
    auth = {"Authorization": f"Bearer {token}"}

    out = {"synced_at": datetime.now(timezone.utc).isoformat()}

    # ---- Athlete (non-identifying fields only) ----
    me = http(f"{API}/athlete", headers=auth) or {}
    athlete_id = me.get("id")
    out["athlete"] = {
        "measurement": me.get("measurement_preference"),
        "weight_kg": me.get("weight"),
        "created_at": (me.get("created_at") or "")[:10],
    }

    # ---- Lifetime / YTD / recent totals ----
    if athlete_id:
        stats = http(f"{API}/athletes/{athlete_id}/stats", headers=auth) or {}

        def totals(key):
            t = stats.get(key) or {}
            return {
                "count": t.get("count"),
                "distance_km": round((t.get("distance") or 0) / 1000, 1),
                "moving_time_sec": t.get("moving_time"),
                "elevation_m": round(t.get("elevation_gain") or 0),
            }
        out["totals"] = {
            "all_run": totals("all_run_totals"),
            "ytd_run": totals("ytd_run_totals"),
            "recent_run": totals("recent_run_totals"),
        }

    # ---- Training zones ----
    zones = http(f"{API}/athlete/zones", headers=auth) or {}
    hr = (zones.get("heart_rate") or {}).get("zones")
    out["zones"] = {"heart_rate": hr} if hr else {}

    # ---- Gear from profile ----
    gear_ids = set()
    out["gear"] = [{
        "name": g.get("name"),
        "distance_km": round((g.get("distance") or 0) / 1000, 1),
        "primary": g.get("primary"),
        "retired": g.get("retired"),
    } for g in (me.get("shoes") or [])]

    # ---- Activities ----
    summaries = http(
        f"{API}/athlete/activities?per_page={ACTIVITY_COUNT}", headers=auth) or []

    activities = []
    best_efforts = {}
    segments = {}

    for i, a in enumerate(summaries):
        start = (a.get("start_date_local") or "")[:10]
        dist_m = a.get("distance") or 0
        entry = {
            "id": a.get("id"),
            "date": start,
            "time_of_day": (a.get("start_date_local") or "")[11:16],
            "name": a.get("name") or "Activity",
            "sport": a.get("sport_type") or a.get("type"),
            "distance_km": round(dist_m / 1000, 2) if dist_m else None,
            "moving_time_sec": a.get("moving_time"),
            "elapsed_time_sec": a.get("elapsed_time"),
            "pace": pace_str(dist_m, a.get("moving_time")),
            "elevation_m": round(a.get("total_elevation_gain") or 0, 1),
            "avg_cadence": a.get("average_cadence"),
            "max_speed_kmh": round((a.get("max_speed") or 0) * 3.6, 1) or None,
            "avg_hr": a.get("average_heartrate"),
            "max_hr": a.get("max_heartrate"),
            "calories": a.get("calories"),
            "suffer_score": a.get("suffer_score"),
            "pr_count": a.get("pr_count"),
            "achievement_count": a.get("achievement_count"),
            "kudos": a.get("kudos_count"),
        }
        if a.get("gear_id"):
            gear_ids.add(a["gear_id"])
        activities.append(entry)

        if i >= DETAIL_COUNT:
            continue
        detail = http(f"{API}/activities/{a['id']}", headers=auth)
        if not detail:
            continue

        for k_out, k_in in (("calories", "calories"), ("avg_hr", "average_heartrate"),
                            ("max_hr", "max_heartrate"), ("suffer_score", "suffer_score")):
            if entry.get(k_out) is None:
                entry[k_out] = detail.get(k_in)
        entry["device"] = detail.get("device_name")
        desc = (detail.get("description") or "").strip()
        if desc:
            entry["description"] = desc[:300]

        splits = []
        for sp in detail.get("splits_metric") or []:
            splits.append({
                "km": sp.get("split"),
                "time_sec": sp.get("moving_time") or sp.get("elapsed_time"),
                "elev_diff": round(sp.get("elevation_difference") or 0, 1),
                "distance_m": round(sp.get("distance") or 0),
            })
        if splits:
            entry["splits"] = splits

        laps = []
        for lp in detail.get("laps") or []:
            laps.append({
                "n": lp.get("lap_index"),
                "distance_m": round(lp.get("distance") or 0),
                "time_sec": lp.get("moving_time"),
                "pace": pace_str(lp.get("distance"), lp.get("moving_time")),
                "avg_cadence": lp.get("average_cadence"),
            })
        if len(laps) > 1:
            entry["laps"] = laps

        for be in detail.get("best_efforts") or []:
            name = (be.get("name") or "").lower().replace(" ", "").replace("/", "")
            t = be.get("moving_time") or be.get("elapsed_time")
            if t and (name not in best_efforts or t < best_efforts[name]["time_sec"]):
                best_efforts[name] = {"time_sec": t, "date": start}

        for se in detail.get("segment_efforts") or []:
            seg = se.get("segment") or {}
            sid = str(seg.get("id"))
            if not sid or sid == "None":
                continue
            segments.setdefault(sid, {
                "name": seg.get("name") or "Segment",
                "distance_m": round(seg.get("distance") or 0),
                "grade": seg.get("average_grade"),
                "efforts": [],
            })["efforts"].append({
                "date": (se.get("start_date_local") or "")[:10],
                "time_sec": se.get("moving_time") or se.get("elapsed_time"),
                "pr_rank": se.get("pr_rank"),
            })

    out["activities"] = activities

    alias = {
        "fastest_400m": ["400m"],
        "fastest_half_mile": ["1/2mile", "halfmile"],
        "fastest_1k": ["1k"],
        "fastest_mile": ["1mile", "mile"],
        "fastest_2mile": ["2mile"],
        "fastest_5k": ["5k"],
        "fastest_10k": ["10k"],
        "fastest_15k": ["15k"],
        "fastest_10mile": ["10mile"],
        "fastest_20k": ["20k"],
        "fastest_half": ["halfmarathon"],
    }
    bests = {}
    for out_key, cands in alias.items():
        for c in cands:
            if c in best_efforts:
                bests[out_key] = best_efforts[c]
                break
    out["bests"] = bests

    seg_list = sorted(segments.values(),
                      key=lambda s: (-len(s["efforts"]), -(s.get("distance_m") or 0)))
    seg_list = [s for s in seg_list if len(s["efforts"]) > 1][:4] or seg_list[:4]
    for s in seg_list:
        s["efforts"].sort(key=lambda e: e["date"])
    out["segments"] = seg_list

    # ---- Weekly volume (last 12 weeks, Monday-anchored) ----
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    buckets = []
    for w in range(WEEKS_HISTORY - 1, -1, -1):
        ws = monday - timedelta(weeks=w)
        we = ws + timedelta(days=6)
        km = 0.0
        cnt = 0
        secs = 0
        for a in activities:
            if not a["date"]:
                continue
            try:
                d = datetime.strptime(a["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if ws <= d <= we:
                km += a["distance_km"] or 0
                secs += a["moving_time_sec"] or 0
                cnt += 1
        buckets.append({
            "week_start": ws.isoformat(),
            "distance_km": round(km, 1),
            "runs": cnt,
            "moving_time_sec": secs,
        })
    out["weekly"] = buckets

    # ---- Streams for the most recent runs (no GPS) ----
    streams_out = {}
    for a in activities[:STREAM_COUNT]:
        if not a.get("id"):
            continue
        keys = "distance,altitude,velocity_smooth,cadence,heartrate,time"
        st = http(f"{API}/activities/{a['id']}/streams?keys={keys}&key_by_type=true",
                  headers=auth)
        if not st:
            continue
        packed = {}
        for k in ("distance", "altitude", "velocity_smooth", "cadence", "heartrate", "time"):
            series = (st.get(k) or {}).get("data")
            if not series:
                continue
            step = max(1, len(series) // 120)
            packed[k] = [round(v, 2) if isinstance(v, float) else v
                         for v in series[::step]]
        if packed:
            streams_out[str(a["id"])] = packed
    out["streams"] = streams_out

    # ---- Gear referenced by activities ----
    known = {g["name"] for g in out["gear"]}
    for gid in list(gear_ids)[:6]:
        g = http(f"{API}/gear/{gid}", headers=auth)
        if g and g.get("name") not in known:
            out["gear"].append({
                "name": g.get("name"),
                "brand": g.get("brand_name"),
                "model": g.get("model_name"),
                "distance_km": round((g.get("distance") or 0) / 1000, 1),
                "retired": g.get("retired"),
            })

    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"data.json written — {len(activities)} activities, {len(bests)} best efforts, "
          f"{len(seg_list)} segments, {len(streams_out)} stream sets, "
          f"{len(out['gear'])} gear items.")


if __name__ == "__main__":
    main()
