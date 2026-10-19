#!/usr/bin/env python3
"""Predict satellite passes over the two node sites with SGP4, for both networks.

  riot  : BLACK KITE-2 over 清大校園, TLE from the 鐳洋 platform /api/tle (CelesTrak fallback)
  fossa : every FOSSASAT-2E on CelesTrak over the 鐳洋 rooftop where the online Nexus sits

  python3 scripts/predict_passes.py                 # fetch TLEs, write data/tle/*.tle + data/tle/passes.json
  python3 scripts/predict_passes.py --offline       # reuse the cached TLEs (no network)
  python3 scripts/predict_passes.py --start 2026-10-16T00:00 --hours 72   # back-test a past window

Needs the `sgp4` package (pip install sgp4); everything else is stdlib. Only element lines are parsed,
never executed. TEME -> ECEF uses GMST only (no polar motion / nutation): a few arc-seconds, far below
the TLE's own error. Back-test against 25 platform passes (10/01-10/16): TCA within 9 s, peak el within 2.9°.
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
from typing import Dict, List, Optional, Tuple

OUT_PATH = Path("data/tle/passes.json")
TAIPEI = timezone(timedelta(hours=8))
UPLINK_WINDOWS = ((1, 4), (13, 16))  # Node firmware TX hours, UTC+8, [start, end)
MIN_MAX_EL = 5.0  # passes whose peak stays below this are not listed
STEP_S = 5
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563

# ponytail: fixed sites; move to a config file once nodes are deployed at the field sites.
TARGETS = {
    "riot": {
        "label": "BLACK KITE-2",
        "match": lambda name, norad: norad == 68474,
        "sources": (("鐳洋平台 /api/tle", "https://210.61.153.16/api/tle"),
                    ("CelesTrak", "https://celestrak.org/NORAD/elements/gp.php?CATNR=68474&FORMAT=tle")),
        "cache": Path("data/tle/black_kite_2.tle"),
        "site": {"name": "清大校園", "lat": 24.79385, "lng": 120.99454, "alt_m": 60.0},  # cc000006 last fix
        "uplink_windows": UPLINK_WINDOWS,
    },
    "fossa": {
        "label": "FOSSASAT-2E 星系",
        "match": lambda name, norad: name.upper().startswith("FOSSASA"),  # CelesTrak spells one "FOSSASA-2E23"
        "sources": (("CelesTrak", "https://celestrak.org/NORAD/elements/gp.php?NAME=FOSSA&FORMAT=tle"),),
        "cache": Path("data/tle/fossasat.tle"),
        "site": {"name": "鐳洋測試點", "lat": 24.76069, "lng": 121.06559, "alt_m": 50.0},  # online Nexus Hub position
        "uplink_windows": None,
    },
}


def parse_tles(text: str) -> List[Tuple[str, str, str]]:
    """(name, line1, line2) triplets from a 2- or 3-line TLE text."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out, name = [], ""
    for i, ln in enumerate(lines):
        if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append((name.strip() or ln[2:7], ln, lines[i + 1]))
        elif not ln.startswith("2 "):
            name = ln
    return out


def norad_of(line1: str) -> int:
    return int(line1[2:7])


def fetch_tles(target: dict, offline: bool) -> Tuple[List[Tuple[str, str, str]], str]:
    """Returns ([(name, l1, l2)], source). The cache file keeps the source on its first line."""
    cache: Path = target["cache"]
    if not offline:
        for source, url in target["sources"]:
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    sats = [s for s in parse_tles(resp.read().decode("ascii", "replace")) if target["match"](s[0], norad_of(s[1]))]
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"warn: {source} fetch failed: {exc}", file=sys.stderr)
                continue
            if sats:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(f"# {source}\n" + "".join(f"{n}\n{l1}\n{l2}\n" for n, l1, l2 in sats), encoding="utf-8")
                return sats, source
            print(f"warn: {source} reply has no matching TLE", file=sys.stderr)
    if not cache.exists():
        return [], "無"
    text = cache.read_text(encoding="utf-8").splitlines()
    source = text[0][2:] if text and text[0].startswith("# ") else "快取"
    return parse_tles("\n".join(text[1:] if text and text[0].startswith("# ") else text)), f"{source}，快取"


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


def find_passes(line1: str, line2: str, start: datetime, hours: float, lat: float, lng: float, alt_m: float,
                windows=None, sat_name: str = "") -> List[dict]:
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(line1, line2)
    site = geodetic_to_ecef(lat, lng, alt_m / 1000.0)
    el_at = lambda t: look_angles(sat, t, site, lat, lng)  # noqa: E731

    def cross(lo: datetime, hi: datetime, rising: bool) -> datetime:
        for _ in range(12):  # 5 s -> ~1 ms
            mid = lo + (hi - lo) / 2
            if (el_at(mid)[0] > 0) == rising:
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
                passes.append(_pass_dict(aos, tca, los, best[0], el_at(aos)[1], best[1], el_at(los)[1], windows, sat_name))
            aos = None
        prev_above = above
        t = t2
    return passes


def _pass_dict(aos, tca, los, max_el, az_aos, az_tca, az_los, windows, sat_name) -> dict:
    local = tca.astimezone(TAIPEI)
    out = {
        "sat": sat_name,
        "aos": aos.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "tca": local.strftime("%Y-%m-%d %H:%M:%S"),
        "los": los.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "tca_unix": int(tca.timestamp()),
        "duration_s": int((los - aos).total_seconds()),
        "max_el": round(max_el, 1),
        "az_aos": round(az_aos), "az_tca": round(az_tca), "az_los": round(az_los),
    }
    if windows:
        out["in_uplink_window"] = any(a <= local.hour < b for a, b in windows)
    return out


def tle_epoch(line1: str) -> datetime:
    year = int(line1[18:20]); year += 2000 if year < 57 else 1900
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(line1[20:32]) - 1)


def predict_target(key: str, target: dict, start: datetime, hours: float, now: datetime, offline: bool) -> Dict[str, object]:
    sats, source = fetch_tles(target, offline)
    site = target["site"]
    passes: List[dict] = []
    for name, l1, l2 in sats:
        passes += find_passes(l1, l2, start, hours, site["lat"], site["lng"], site["alt_m"], target["uplink_windows"], name)
    passes.sort(key=lambda p: p["tca_unix"])
    epochs = [tle_epoch(l1) for _, l1, _ in sats]
    newest = max(epochs) if epochs else None
    out: Dict[str, object] = {
        "label": target["label"], "satellites": [n for n, _, _ in sats], "source": source,
        "tle_epoch": newest.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S") if newest else "—",
        "tle_age_h": round((now - min(epochs)).total_seconds() / 3600, 1) if epochs else None,  # oldest element set
        "site": site, "min_max_el": MIN_MAX_EL, "passes": passes,
    }
    if target["uplink_windows"]:
        out["uplink_windows"] = [f"{s:02d}:00-{e:02d}:00" for s, e in target["uplink_windows"]]
    print(f"{key}: {target['label']} {len(sats)} sat(s), TLE {source}, newest epoch {out['tle_epoch']}; {len(passes)} passes in {hours:g} h over {site['name']}")
    for p in passes[:10]:
        flag = ("上行時段" if p.get("in_uplink_window") else "") if target["uplink_windows"] else p["sat"]
        print(f"  {p['aos'][5:16]} -> {p['tca'][11:19]} -> {p['los'][11:19]}  max {p['max_el']:5.1f}°  az {p['az_aos']:3d}->{p['az_los']:3d}  {p['duration_s']:4d} s  {flag}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="以 TLE 預測 BLACK KITE-2 與 FOSSASAT-2E 過境時間")
    ap.add_argument("--offline", action="store_true", help="不連網，使用快取 TLE")
    ap.add_argument("--start", help="起算時間（本地 UTC+8，ISO），預設現在")
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--only", choices=sorted(TARGETS), help="只算一個網路")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    a = ap.parse_args()
    try:
        import sgp4  # noqa: F401
    except ImportError:
        raise SystemExit("需要 sgp4：pip install sgp4")
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(a.start).replace(tzinfo=TAIPEI).astimezone(timezone.utc) if a.start else now
    out: Dict[str, object] = {
        "generated_at": now.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "start": start.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S"), "hours": a.hours,
    }
    for key, target in TARGETS.items():
        if a.only and key != a.only:
            continue
        out[key] = predict_target(key, target, start, a.hours, now, a.offline)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
