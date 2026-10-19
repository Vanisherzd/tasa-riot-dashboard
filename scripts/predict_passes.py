#!/usr/bin/env python3
"""Predict BLACK KITE-2 passes over the Node site with SGP4 from the TLE the 鐳洋 platform uses.

TLE source order: 鐳洋 platform /api/tle (same elements as their pass list), CelesTrak, cached file.

  python3 scripts/predict_passes.py                 # fetch TLE, write data/tle/black_kite_2.tle + passes.json
  python3 scripts/predict_passes.py --offline       # reuse the cached TLE (no network)
  python3 scripts/predict_passes.py --start 2026-10-16T00:00 --hours 72   # back-test a past window

Needs the `sgp4` package (pip install sgp4); everything else is stdlib. Only the two element lines are parsed, never executed. TEME -> ECEF uses GMST only
(no polar motion / nutation): a few arc-seconds, far below the TLE's own error.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

NORAD = 68474  # BLACK KITE-2 (CelesTrak name), 2026-067BL
NAME = "BLACK KITE-2"
TLE_SOURCES = (
    ("鐳洋平台 /api/tle", "https://210.61.153.16/api/tle"),
    ("CelesTrak", f"https://celestrak.org/NORAD/elements/gp.php?CATNR={NORAD}&FORMAT=tle"),
)
TLE_PATH = Path("data/tle/black_kite_2.tle")
OUT_PATH = Path("data/tle/passes.json")
TAIPEI = timezone(timedelta(hours=8))
# Node site: 清大校園 (cc000006 last fix). Override with --lat/--lng for the 鐳洋 test point (24.7607, 121.0655).
SITE = {"name": "清大校園", "lat": 24.79385, "lng": 120.99454, "alt_m": 60.0}
UPLINK_WINDOWS = ((1, 4), (13, 16))  # firmware TX hours, UTC+8, [start, end)
MIN_MAX_EL = 5.0  # passes whose peak stays below this are not listed
STEP_S = 5
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563


def pick_tle(text: str) -> Optional[Tuple[str, str]]:
    """Find our satellite's two lines in a multi-satellite TLE text (by NORAD number)."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    for i in range(len(lines) - 1):
        if lines[i].startswith(f"1 {NORAD:05d}") and lines[i + 1].startswith(f"2 {NORAD:05d}"):
            return lines[i], lines[i + 1]
    return None


def fetch_tle(path: Path, offline: bool) -> Tuple[str, str, str]:
    """Returns (line1, line2, source); the cached file keeps the source on its first line."""
    if not offline:
        for source, url in TLE_SOURCES:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    found = pick_tle(resp.read().decode("ascii", "replace"))
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"warn: {source} fetch failed: {exc}", file=sys.stderr)
                continue
            if found:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{NAME} ({source})\n{found[0]}\n{found[1]}\n", encoding="utf-8")
                return found[0], found[1], source
            print(f"warn: {source} reply has no TLE for {NORAD}", file=sys.stderr)
    if not path.exists():
        raise SystemExit("no TLE available (fetch failed and no cache)")
    lines = path.read_text(encoding="utf-8").splitlines()
    source = lines[0][lines[0].find("(") + 1:lines[0].rfind(")")] if "(" in lines[0] else "快取"
    return lines[1], lines[2], f"{source}，快取"


def geodetic_to_ecef(lat_deg: float, lng_deg: float, alt_km: float) -> Tuple[float, float, float]:
    lat, lng = math.radians(lat_deg), math.radians(lng_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + alt_km) * math.cos(lat) * math.cos(lng),
            (n + alt_km) * math.cos(lat) * math.sin(lng),
            (n * (1 - e2) + alt_km) * math.sin(lat))


def gmst_rad(jd_ut1: float) -> float:
    t = (jd_ut1 - 2451545.0) / 36525.0
    deg = 280.46061837 + 360.98564736629 * (jd_ut1 - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return math.radians(deg % 360.0)


def look_angles(sat, when: datetime, site_ecef, lat_deg: float, lng_deg: float) -> Tuple[float, float, float]:
    """Elevation, azimuth (deg) and range (km) of the satellite from the site at `when` (UTC)."""
    from sgp4.api import jday
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute, when.second + when.microsecond / 1e6)
    err, r_teme, _ = sat.sgp4(jd, fr)
    if err != 0:
        raise RuntimeError(f"sgp4 error {err}")
    g = gmst_rad(jd + fr)
    x = math.cos(g) * r_teme[0] + math.sin(g) * r_teme[1]
    y = -math.sin(g) * r_teme[0] + math.cos(g) * r_teme[1]
    z = r_teme[2]
    dx, dy, dz = x - site_ecef[0], y - site_ecef[1], z - site_ecef[2]
    lat, lng = math.radians(lat_deg), math.radians(lng_deg)
    east = -math.sin(lng) * dx + math.cos(lng) * dy
    north = -math.sin(lat) * math.cos(lng) * dx - math.sin(lat) * math.sin(lng) * dy + math.cos(lat) * dz
    up = math.cos(lat) * math.cos(lng) * dx + math.cos(lat) * math.sin(lng) * dy + math.sin(lat) * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return math.degrees(math.asin(up / rng)), math.degrees(math.atan2(east, north)) % 360.0, rng


def find_passes(line1: str, line2: str, start: datetime, hours: float, lat: float, lng: float, alt_m: float) -> List[dict]:
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(line1, line2)
    site = geodetic_to_ecef(lat, lng, alt_m / 1000.0)
    el_at = lambda t: look_angles(sat, t, site, lat, lng)  # noqa: E731

    def cross(lo: datetime, hi: datetime, rising: bool) -> datetime:
        for _ in range(12):  # 5 s -> ~1 ms
            mid = lo + (hi - lo) / 2
            above = el_at(mid)[0] > 0
            if above == rising:
                hi = mid
            else:
                lo = mid
        return hi

    passes: List[dict] = []
    t = start
    end = start + timedelta(hours=hours)
    prev_above = el_at(t)[0] > 0
    aos: Optional[datetime] = None
    while t < end:
        t2 = t + timedelta(seconds=STEP_S)
        above = el_at(t2)[0] > 0
        if above and not prev_above:
            aos = cross(t, t2, rising=True)
        elif prev_above and not above and aos is not None:
            los = cross(t, t2, rising=False)
            best = max((el_at(aos + timedelta(seconds=s)) + (s,) for s in range(0, int((los - aos).total_seconds()) + 1)), key=lambda v: v[0])
            tca = aos + timedelta(seconds=best[3])
            if best[0] >= MIN_MAX_EL:
                passes.append(_pass_dict(aos, tca, los, best[0], el_at(aos)[1], best[1], el_at(los)[1]))
            aos = None
        prev_above = above
        t = t2
    return passes


def _pass_dict(aos: datetime, tca: datetime, los: datetime, max_el: float, az_aos: float, az_tca: float, az_los: float) -> dict:
    local = tca.astimezone(TAIPEI)
    return {
        "aos": aos.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "tca": local.strftime("%Y-%m-%d %H:%M:%S"),
        "los": los.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "tca_unix": int(tca.timestamp()),
        "duration_s": int((los - aos).total_seconds()),
        "max_el": round(max_el, 1),
        "az_aos": round(az_aos), "az_tca": round(az_tca), "az_los": round(az_los),
        "in_uplink_window": any(a <= local.hour < b for a, b in UPLINK_WINDOWS),
    }


def tle_epoch(line1: str) -> datetime:
    year = int(line1[18:20]); year += 2000 if year < 57 else 1900
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(line1[20:32]) - 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="以 TLE 預測 BLACK KITE-2 過境時間")
    ap.add_argument("--offline", action="store_true", help="不連 CelesTrak，使用快取 TLE")
    ap.add_argument("--start", help="起算時間（本地 UTC+8，ISO），預設現在")
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--lat", type=float, default=SITE["lat"])
    ap.add_argument("--lng", type=float, default=SITE["lng"])
    ap.add_argument("--alt", type=float, default=SITE["alt_m"])
    ap.add_argument("--tle", type=Path, default=TLE_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    a = ap.parse_args()
    try:
        import sgp4  # noqa: F401
    except ImportError:
        raise SystemExit("需要 sgp4：pip install sgp4")
    l1, l2, source = fetch_tle(a.tle, a.offline)
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(a.start).replace(tzinfo=TAIPEI).astimezone(timezone.utc) if a.start else now
    passes = find_passes(l1, l2, start, a.hours, a.lat, a.lng, a.alt)
    epoch = tle_epoch(l1)
    out = {
        "satellite": NAME, "norad": NORAD, "source": source, "tle": [l1, l2],
        "tle_epoch": epoch.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "tle_age_h": round((now - epoch).total_seconds() / 3600, 1),
        "generated_at": now.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "start": start.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"), "hours": a.hours,
        "site": {"name": SITE["name"] if (a.lat, a.lng) == (SITE["lat"], SITE["lng"]) else "自訂", "lat": a.lat, "lng": a.lng, "alt_m": a.alt},
        "min_max_el": MIN_MAX_EL, "uplink_windows": [f"{s:02d}:00-{e:02d}:00" for s, e in UPLINK_WINDOWS],
        "passes": passes,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{NAME}: TLE epoch {out['tle_epoch']} ({out['tle_age_h']} h old); {len(passes)} passes in {a.hours:g} h from {out['start']} -> {a.out}")
    for p in passes:
        print(f"  {p['aos'][5:16]} -> {p['tca'][11:19]} -> {p['los'][11:19]}  max {p['max_el']:5.1f}°  az {p['az_aos']:3d}->{p['az_los']:3d}  {p['duration_s']:4d} s  {'上行時段' if p['in_uplink_window'] else ''}")


if __name__ == "__main__":
    main()
