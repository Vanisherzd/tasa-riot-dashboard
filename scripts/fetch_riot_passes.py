"""Download 鐳洋 M9 pass files (UART logs) listed at https://210.61.153.16/api/lora_file_list.

usage: python3 scripts/fetch_riot_passes.py [--since 2026-10-01] [--out data/rx_logs/m9_passes]

Writes <out>/files.json (one entry per pass: file, mission_utc, local, size, start/tca/end
elevation+azimuth) and downloads the .bin files not yet present. Then parse each with
scripts/parse_riot_log.py. The host serves a Let's Encrypt IP certificate; TLS is verified normally.
"""
import argparse
import json
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://210.61.153.16"
FILE_RE = re.compile(r"\d{10}-RIoT-[\w-]+\.bin")
LINK_PREFIX = "/api/lora_file_list/download/"
GEOM = re.compile(r"(Start|TCA|End)\s*[：:]\s*Ele\.\s*([\d.]+)°,\s*Azi\.\s*([\d.]+)°\s*[（(]\s*([\d:]+)\s*[)）]")


class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.links = [], None, None, []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.links = [], []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "a" and self.row is not None:
            self.links += [v for k, v in attrs if k == "href"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append((self.row, self.links))
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=90) as r:
        return r.read()


def listing() -> list[dict]:
    t = Table()
    t.feed(fetch(BASE + "/api/lora_file_list").decode("utf-8"))
    out = []
    for cells, links in t.rows:
        if len(cells) < 5 or not FILE_RE.fullmatch(cells[0]):
            continue
        link = links[0] if links else None  # page content is untrusted: only accept the expected download path
        if link != LINK_PREFIX + cells[0]:
            link = None
        d = {"file": cells[0], "mission_utc": cells[2], "local": cells[3], "size": cells[4], "link": link}
        for k, ele, azi, hms in GEOM.findall(cells[1]):
            d[k.lower()] = {"ele": float(ele), "azi": float(azi), "t": hms}
        out.append(d)
    return sorted(out, key=lambda d: d["file"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-01", help="mission date (UTC) lower bound")
    ap.add_argument("--out", default="data/rx_logs/m9_passes")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = [d for d in listing() if d["mission_utc"][:10] >= a.since]
    (out / "files.json").write_text(json.dumps(files, ensure_ascii=False, indent=1))
    for d in files:
        dst = (out / d["file"]).resolve()
        if dst.parent != out.resolve():
            raise ValueError(f"refusing path outside {out}: {d['file']}")
        if d["link"] and not dst.exists():
            dst.write_bytes(fetch(BASE + d["link"]))
            print("downloaded", d["file"])
        tca = d.get("tca", {})
        print(f"{d['local'][:16]}  TCA {tca.get('ele', '?'):>5}°  {d['size']:>8}  {d['file']}")
    print(f"{len(files)} passes since {a.since}")


if __name__ == "__main__":
    main()
