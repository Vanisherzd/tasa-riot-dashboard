"""Build per-pass and per-packet CSVs from data/rx_logs/m9_passes/ (files.json + *.bin).

usage: python3 scripts/summarize_riot_passes.py [--dir data/rx_logs/m9_passes] [--out data/rx_logs]
Writes <out>/<first>_<last>_M9_passes_summary.csv and ..._packets.csv (dates from files.json)
and prints totals. Run after scripts/fetch_riot_passes.py.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_riot_log as p  # noqa: E402

KNOWN = ("cc000001", "cc000003", "cc000005", "cc000006", "cc000007")


def packets(path: Path) -> list[dict]:
    text = path.read_bytes().decode("latin-1")
    rows = []
    for r in p.records(text):
        b = r["bytes"]
        if r["size"] < 19 or len(b) < 19 or b[0] != 0xCC:
            continue
        d = p.decode(b[:19])
        d.update(size=r["size"], line=r["line"], rssi=r["rssi"], snr=r["snr"], hex=b.hex(" "))
        rows.append(d)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/rx_logs/m9_passes")
    ap.add_argument("--out", default="data/rx_logs")
    a = ap.parse_args()
    d = Path(a.dir)
    files = sorted(json.loads((d / "files.json").read_text()), key=lambda f: f["file"])
    first, last = files[0]["local"][:10], files[-1]["local"][:10]
    stem = f"{first}_{last[5:]}_M9_passes"
    pk_rows, sm_rows = [], []
    for f in files:
        rows = packets(d / f["file"]) if (d / f["file"]).exists() else []
        ok = [r for r in rows if r["crc_ok"]]
        snr = [r["snr"] for r in rows]
        rssi = [r["rssi"] for r in rows]
        tca = f.get("tca", {})
        for r in rows:
            pk_rows.append({"file": f["file"], "pass_local": f["local"][:16], "tca_ele": tca.get("ele"), **r})
        sm_rows.append({
            "file": f["file"], "pass_local": f["local"][:16], "tca_ele": tca.get("ele"), "tca_azi": tca.get("azi"),
            "size": f["size"], "n_packets": len(rows), "n_crc_ok": len(ok),
            "snr_min": min(snr) if snr else "", "snr_max": max(snr) if snr else "",
            "rssi_min": min(rssi) if rssi else "", "rssi_max": max(rssi) if rssi else "",
            "macs_known": " ".join(sorted({r["mac"] for r in rows if r["mac"] in KNOWN})),
        })
    out = Path(a.out)
    cols = ["file", "pass_local", "tca_ele", "size", "line", "mac", "lat", "lng", "unix", "local", "bat_v", "crc_ok", "rssi", "snr", "hex"]
    with open(out / f"{stem}_packets.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(pk_rows)
    with open(out / f"{stem}_summary.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(sm_rows[0].keys()))
        w.writeheader()
        w.writerows(sm_rows)
    n_ok = sum(r["crc_ok"] for r in pk_rows)
    print(f"{len(files)} passes {first}..{last}: {len(pk_rows)} packets, {n_ok} CRC ok -> {out / stem}_*.csv")
    print("per MAC (all/ok):", {m: (c, sum(1 for r in pk_rows if r['mac'] == m and r['crc_ok']))
                                for m, c in Counter(r["mac"] for r in pk_rows).most_common() if m in KNOWN})
    for s in sm_rows[-3:]:
        print(f"  {s['pass_local']} TCA {s['tca_ele']} n={s['n_packets']} ok={s['n_crc_ok']} snr={s['snr_min']}..{s['snr_max']} macs={s['macs_known']}")


if __name__ == "__main__":
    main()
