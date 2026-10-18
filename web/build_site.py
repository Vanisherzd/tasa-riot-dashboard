#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ─── How to run ───
# python3 web/build_site.py
from __future__ import annotations

import csv
import json
import math
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template
from typing import Dict, Final, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

ROOT: Final = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.parse_fossa import FossaRecord, load_records  # noqa: E402

WEB_DIR: Final = ROOT / "web"
PUBLIC_DIR: Final = WEB_DIR / "public"
DATA_DIR: Final = ROOT / "data" / "rx_logs"
PASS_DIR: Final = DATA_DIR / "m9_passes"
FILES_JSON: Final = PASS_DIR / "files.json"
FOSSA_DIR: Final = ROOT / "data" / "fossa"
FOSSA_MESSAGES: Final = FOSSA_DIR / "messages.jsonl"
FOSSA_DEVICES: Final = FOSSA_DIR / "devices.json"
FOSSA_SNAPSHOT: Final = FOSSA_DIR / "hub_snapshot.json"


def _norm_mac(value: str) -> str:
    """Compare MACs regardless of separators and case (06:50:4E:00:42:00 == 06504e004200)."""
    return "".join(ch for ch in value if ch.isalnum()).casefold()
TAIPEI: Final = timezone(timedelta(hours=8))
KNOWN_DEVICES: Final = (
    ("cc000001", "蚵棚"),
    ("cc000003", "登山改裝"),
    ("cc000005", "蚵棚"),
    ("cc000006", "本組第一塊 Node"),
    ("cc000007", "不明"),
)


@dataclass(frozen=True)
class DashboardDataError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class SourceFile:
    file: str
    local: str


@dataclass(frozen=True)
class PassRow:
    file: str
    pass_local: str
    tca_ele: Optional[float]
    tca_azi: Optional[float]
    n_packets: int
    n_crc_ok: int
    snr_min: Optional[float]
    snr_max: Optional[float]
    macs_known: Tuple[str, ...]


@dataclass(frozen=True)
class PacketRow:
    file: str
    pass_local: str
    mac: str
    lat: float
    lng: float
    unix: int
    local: str
    bat_v: float
    crc_ok: bool
    rssi: float
    snr: float
    size: int


@dataclass(frozen=True)
class DeviceStats:
    key: str
    label: str
    n_packets: int
    n_crc_ok: int
    last_correct_local: Optional[str]
    last_mac: Optional[str]
    last_lat: Optional[float]
    last_lng: Optional[float]
    latest_voltage: Optional[float]


@dataclass(frozen=True)
class FossaDevice:
    serial: str
    mac: str
    node_id: str
    nexus_mac: str = ""


@dataclass(frozen=True)
class FossaDeviceStats:
    serial: str
    mac: str
    node_id: str
    last_received_local: Optional[str]
    n_messages: int


@dataclass(frozen=True)
class DailyComparison:
    date: str
    riot_total: int
    riot_valid: int
    fossa_total: int
    fossa_valid: int


def optional_float(value: str) -> Optional[float]:
    return float(value) if value.strip() else None


def load_source_files(path: Path) -> List[SourceFile]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    if not isinstance(raw, list):
        raise DashboardDataError(f"{path} 頂層必須是陣列")
    files = []
    for item in raw:
        if not isinstance(item, dict) or "file" not in item or "local" not in item:
            raise DashboardDataError(f"{path} 含缺少 file/local 的項目")
        name = str(item["file"])
        if name != Path(name).name or not name.endswith(".bin"):
            raise DashboardDataError(f"拒絕不安全的原始檔名: {name}")
        files.append(SourceFile(file=name, local=str(item["local"])))
    return files


def load_passes(path: Path) -> List[PassRow]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    passes = [
        PassRow(
            file=row.get("file", ""),
            pass_local=row.get("pass_local", ""),
            tca_ele=optional_float(row.get("tca_ele", "")),
            tca_azi=optional_float(row.get("tca_azi", "")),
            n_packets=int(row.get("n_packets", "0") or 0),
            n_crc_ok=int(row.get("n_crc_ok", "0") or 0),
            snr_min=optional_float(row.get("snr_min", "")),
            snr_max=optional_float(row.get("snr_max", "")),
            macs_known=tuple(row.get("macs_known", "").split()),
        )
        for row in rows
    ]
    return sorted(passes, key=lambda item: item.pass_local, reverse=True)


def load_packets(path: Path) -> List[PacketRow]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    return [
        PacketRow(
            file=row.get("file", ""),
            pass_local=row.get("pass_local", ""),
            mac=row.get("mac", ""),
            lat=float(row.get("lat", "0") or 0),
            lng=float(row.get("lng", "0") or 0),
            unix=int(row.get("unix", "0") or 0),
            local=row.get("local", ""),
            bat_v=float(row.get("bat_v", "0") or 0),
            crc_ok=row.get("crc_ok", "").strip().lower() in {"1", "true", "yes"},
            rssi=float(row.get("rssi", "0") or 0),
            snr=float(row.get("snr", "0") or 0),
            size=int(row.get("size", "0") or 0),
        )
        for row in rows
    ]


def latest_csv_pair() -> Tuple[Path, Path]:
    summaries = sorted(DATA_DIR.glob("*_M9_passes_summary.csv"))
    if not summaries:
        raise DashboardDataError(f"{DATA_DIR} 找不到 *_M9_passes_summary.csv")
    summary = summaries[-1]
    packets = summary.with_name(summary.name.replace("_summary.csv", "_packets.csv"))
    if not packets.exists():
        raise DashboardDataError(f"找不到與 {summary.name} 配對的 {packets.name}")
    return summary, packets


def device_statistics(packets: Sequence[PacketRow]) -> List[DeviceStats]:
    known_macs = {mac for mac, _ in KNOWN_DEVICES}
    groups = [(mac, label, [row for row in packets if row.mac == mac]) for mac, label in KNOWN_DEVICES]
    groups.append(("other", "其他（地址無法解碼）", [row for row in packets if row.mac not in known_macs]))
    stats = []
    for key, label, rows in groups:
        correct = [row for row in rows if row.crc_ok]
        latest = max(correct, key=lambda row: (row.unix, row.pass_local), default=None)
        has_coordinate = latest is not None and -90 <= latest.lat <= 90 and -180 <= latest.lng <= 180
        stats.append(
            DeviceStats(
                key=key,
                label=label,
                n_packets=len(rows),
                n_crc_ok=len(correct),
                last_correct_local=latest.local if latest and latest.local else None,
                last_mac=latest.mac if latest else None,
                last_lat=latest.lat if has_coordinate else None,
                last_lng=latest.lng if has_coordinate else None,
                latest_voltage=latest.bat_v if latest else None,
            )
        )
    return stats


def load_fossa_devices(path: Path) -> List[FossaDevice]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("devices"), list):
        raise DashboardDataError(f"{path} 必須含 devices 陣列")
    devices = []
    for item in raw["devices"]:
        if not isinstance(item, dict) or not isinstance(item.get("serial"), str):
            raise DashboardDataError(f"{path} devices 項目缺少 serial")
        devices.append(
            FossaDevice(
                serial=item["serial"],
                mac=str(item.get("mac", "")),
                node_id=str(item.get("nodeId", "")),
                nexus_mac=str(item.get("nexusMac", "")),
            )
        )
    return devices


def load_hub_snapshot(path: Path) -> List[Dict[str, object]]:
    """Device status pulled from the FOSSA IoT Hub (scripts/fetch_fossa_hub.py); absent file = no panel."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    links = raw.get("links") if isinstance(raw, dict) else None
    return [item for item in (links or []) if isinstance(item, dict)]


def fossa_record_order(record: FossaRecord) -> Tuple[int, str]:
    timestamp = record.sat_unix or record.received_unix or record.queue_unix or 0
    return timestamp, record.key


def fossa_device_statistics(
    records: Sequence[FossaRecord], devices: Sequence[FossaDevice]
) -> List[FossaDeviceStats]:
    assigned = set()
    stats = []
    for device in devices:
        matches = []
        for index, record in enumerate(records):
            known = {_norm_mac(value) for value in (device.mac, device.nexus_mac) if value}
            mac_matches = bool(known) and _norm_mac(record.mac) in known
            node_matches = bool(device.node_id) and record.node_id == device.node_id
            if mac_matches or node_matches:
                assigned.add(index)
                matches.append(record)
        latest = max(matches, key=fossa_record_order, default=None)
        stats.append(
            FossaDeviceStats(
                serial=device.serial,
                mac=device.mac,
                node_id=device.node_id,
                last_received_local=(latest.received_at_local or latest.sat_local) if latest else None,
                n_messages=len(matches),
            )
        )

    unregistered: Dict[Tuple[str, str], List[FossaRecord]] = {}
    for index, record in enumerate(records):
        if index not in assigned:
            unregistered.setdefault((record.mac, record.node_id), []).append(record)
    for (mac, node_id), matches in sorted(unregistered.items()):
        latest = max(matches, key=fossa_record_order)
        stats.append(
            FossaDeviceStats(
                serial="未登錄",
                mac=mac,
                node_id=node_id,
                last_received_local=latest.received_at_local or latest.sat_local,
                n_messages=len(matches),
            )
        )
    return stats


def daily_comparison(packets: Sequence[PacketRow], records: Sequence[FossaRecord]) -> List[DailyComparison]:
    riot: Dict[str, Tuple[int, int]] = {}
    for packet in packets:
        date = packet.pass_local[:10]
        if date:
            total, valid = riot.get(date, (0, 0))
            riot[date] = total + 1, valid + int(packet.crc_ok)

    fossa: Dict[str, Tuple[int, int]] = {}
    for record in records:
        date = (record.sat_local or record.received_at_local or record.queue_local or "")[:10]
        if date:
            total, valid = fossa.get(date, (0, 0))
            fossa[date] = total + 1, valid + int(record.lat is not None and record.lng is not None)

    return [
        DailyComparison(
            date=date,
            riot_total=riot.get(date, (0, 0))[0],
            riot_valid=riot.get(date, (0, 0))[1],
            fossa_total=fossa.get(date, (0, 0))[0],
            fossa_valid=fossa.get(date, (0, 0))[1],
        )
        for date in sorted(set(riot) | set(fossa))
    ]


def render_table(headers: Sequence[str], rows: Iterable[Sequence[str]], caption: str) -> str:
    body = "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="empty">目前沒有資料；請執行 <code>sh web/update.sh</code>。</td></tr>'
    head = "".join(f'<th scope="col">{escape(header)}</th>' for header in headers)
    return (
        f'<div class="table-wrap" tabindex="0" role="region" aria-label="{escape(caption)}">'
        f'<table><caption>{escape(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def chart_ticks(peak: int) -> List[int]:
    return sorted({round(peak * step / 4) for step in range(5)})


def grouped_bar_svg(
    chart_id: str,
    title: str,
    description: str,
    labels: Sequence[str],
    totals: Sequence[int],
    correct: Sequence[int],
    annotations: Optional[Sequence[str]] = None,
    series_names: Tuple[str, str] = ("封包", "CRC 正確"),
    bar_classes: Tuple[str, str] = ("bar-total", "bar-ok"),
) -> str:
    if not labels:
        return '<div class="empty">沒有可繪製的資料。</div>'
    width, height = 1200, 390
    left, right, top, bottom = 54, 18, 44, 92
    plot_width, plot_height = width - left - right, height - top - bottom
    peak = max(max(totals, default=0), max(correct, default=0), 1)
    slot = plot_width / len(labels)
    bar_width = min(18.0, slot * 0.31)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{chart_id}-title {chart_id}-desc">',
        f'<title id="{chart_id}-title">{escape(title)}</title>',
        f'<desc id="{chart_id}-desc">{escape(description)}</desc>',
    ]
    for value in chart_ticks(peak):
        y = top + plot_height - plot_height * value / peak
        parts.append(f'<line class="chart-grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="chart-axis" x="{left-8}" y="{y+4:.1f}" text-anchor="end">{value}</text>')
    label_every = max(1, math.ceil(len(labels) / 10))
    for index, (label, total, ok) in enumerate(zip(labels, totals, correct)):
        center = left + slot * (index + 0.5)
        total_height = plot_height * total / peak
        ok_height = plot_height * ok / peak
        base_y = top + plot_height
        parts.append(
            f'<rect class="{bar_classes[0]}" x="{center-bar_width-1:.1f}" y="{base_y-total_height:.1f}" '
            f'width="{bar_width:.1f}" height="{total_height:.1f}">'
            f'<title>{escape(label)}：{escape(series_names[0])} {total}</title></rect>'
        )
        parts.append(
            f'<rect class="{bar_classes[1]}" x="{center+1:.1f}" y="{base_y-ok_height:.1f}" '
            f'width="{bar_width:.1f}" height="{ok_height:.1f}">'
            f'<title>{escape(label)}：{escape(series_names[1])} {ok}</title></rect>'
        )
        if annotations:
            note_y = min(base_y - max(total_height, ok_height) - 7, base_y - 7)
            parts.append(f'<text class="chart-note" x="{center:.1f}" y="{note_y:.1f}" text-anchor="middle">{escape(annotations[index])}</text>')
        if index % label_every == 0 or index == len(labels) - 1:
            parts.append(
                f'<text class="chart-label" x="{center:.1f}" y="{base_y+18:.1f}" '
                f'text-anchor="middle">{escape(label)}</text>'
            )
    parts.append("</svg>")
    return (
        f'<div class="chart-scroll" tabindex="0" role="region" aria-label="{escape(title)}圖表">'
        + "".join(parts)
        + "</div>"
    )


def pass_chart(passes: Sequence[PassRow]) -> str:
    ordered = list(reversed(passes))
    labels = [item.pass_local[5:] for item in ordered]
    notes = [f"{item.tca_ele:.0f}°" if item.tca_ele is not None else "—" for item in ordered]
    return grouped_bar_svg(
        "passes-chart",
        "每場次封包數與 CRC 正確數",
        "每組兩柱分別代表總封包與 CRC 正確封包；柱頂文字是 TCA 仰角。",
        labels,
        [item.n_packets for item in ordered],
        [item.n_crc_ok for item in ordered],
        notes,
    )


def snr_chart(packets: Sequence[PacketRow]) -> str:
    bins = {}
    for packet in packets:
        lower = math.floor(packet.snr / 4) * 4
        total, correct = bins.get(lower, (0, 0))
        bins[lower] = (total + 1, correct + int(packet.crc_ok))
    ordered = sorted(bins)
    return grouped_bar_svg(
        "snr-chart",
        "SNR 分箱與 CRC 正確數",
        "SNR 以 4 dB 分箱；每組兩柱分別代表總封包與 CRC 正確封包。",
        [f"{lower}～{lower + 4}" for lower in ordered],
        [bins[lower][0] for lower in ordered],
        [bins[lower][1] for lower in ordered],
    )


def fossa_daily_chart(comparison: Sequence[DailyComparison]) -> str:
    fossa_days = [item for item in comparison if item.fossa_total]
    return grouped_bar_svg(
        "fossa-daily-chart",
        "FOSSA 每日訊息數與暫定有效座標數",
        "每組兩柱分別代表 FOSSA 訊息與依暫定 payload 格式解出合理座標的訊息。",
        [item.date[5:] for item in fossa_days],
        [item.fossa_total for item in fossa_days],
        [item.fossa_valid for item in fossa_days],
        series_names=("訊息", "暫定有效"),
        bar_classes=("bar-fossa", "bar-fossa-valid"),
    )


def comparison_chart(comparison: Sequence[DailyComparison]) -> str:
    if not comparison:
        return '<div class="empty">沒有可繪製的資料。</div>'
    width, height = 1200, 390
    left, right, top, bottom = 54, 18, 44, 92
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [
        max(item.riot_total, item.riot_valid, item.fossa_total, item.fossa_valid)
        for item in comparison
    ]
    peak = max(values, default=1) or 1
    slot = plot_width / len(comparison)
    bar_width = min(14.0, slot * 0.18)
    series = (
        ("bar-total", "鐳洋收到", [item.riot_total for item in comparison]),
        ("bar-ok", "鐳洋 CRC 正確", [item.riot_valid for item in comparison]),
        ("bar-fossa", "FOSSA 收到", [item.fossa_total for item in comparison]),
        ("bar-fossa-valid", "FOSSA 暫定有效", [item.fossa_valid for item in comparison]),
    )
    parts = [
        '<svg viewBox="0 0 1200 390" role="img" aria-labelledby="comparison-chart-title comparison-chart-desc">',
        '<title id="comparison-chart-title">鐳洋與 FOSSA 每日接收對照</title>',
        '<desc id="comparison-chart-desc">每日四柱依序是鐳洋收到、鐳洋 CRC 正確、FOSSA 收到與 FOSSA 暫定有效。</desc>',
    ]
    for value in chart_ticks(peak):
        y = top + plot_height - plot_height * value / peak
        parts.append(f'<line class="chart-grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="chart-axis" x="{left-8}" y="{y+4:.1f}" text-anchor="end">{value}</text>')
    label_every = max(1, math.ceil(len(comparison) / 10))
    multiple_years = len({item.date[:4] for item in comparison}) > 1
    for index, item in enumerate(comparison):
        center = left + slot * (index + 0.5)
        for series_index, (class_name, series_name, series_values) in enumerate(series):
            value = series_values[index]
            bar_height = plot_height * value / peak
            x = center + (series_index - 1.5) * (bar_width + 2) - bar_width / 2
            parts.append(
                f'<rect class="{class_name}" x="{x:.1f}" y="{top+plot_height-bar_height:.1f}" '
                f'width="{bar_width:.1f}" height="{bar_height:.1f}">'
                f'<title>{escape(item.date)}：{escape(series_name)} {value}</title></rect>'
            )
        if index % label_every == 0 or index == len(comparison) - 1:
            date_label = item.date[2:] if multiple_years else item.date[5:]
            parts.append(
                f'<text class="chart-label" x="{center:.1f}" y="{top+plot_height+18:.1f}" '
                f'text-anchor="middle">{escape(date_label)}</text>'
            )
    parts.append("</svg>")
    return (
        '<div class="chart-scroll" tabindex="0" role="region" aria-label="鐳洋與 FOSSA 每日接收對照圖表">'
        + "".join(parts)
        + "</div>"
    )


def fmt_number(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


# ---------------------------------------------------------------- Hub-style building blocks
TAIWAN_BBOX: Final = (119.3, 21.8, 122.2, 25.5)  # lng_min, lat_min, lng_max, lat_max
TAIWAN_OUTLINE: Final = (
    (121.0, 25.3), (121.6, 25.3), (121.95, 25.0), (121.75, 24.5), (121.45, 23.6), (121.1, 22.6),
    (120.85, 21.9), (120.7, 21.95), (120.45, 22.4), (120.15, 23.1), (120.1, 23.7), (120.55, 24.5), (121.0, 25.3),
)


def render_cards(items: Sequence[Tuple[str, str, str]]) -> str:
    """Hub-style metric cards: (value_html, label, modifier)."""
    cards = "".join(
        f'<div class="card{(" card--" + mod) if mod else ""}"><span class="card__value">{value}</span><span class="card__label">{escape(label)}</span></div>'
        for value, label, mod in items
    )
    return f'<div class="cards">{cards}</div>'


def render_tiles(pairs: Sequence[Tuple[str, str]]) -> str:
    tiles = "".join(f'<div class="tile"><dd>{value}</dd><dt>{escape(label)}</dt></div>' for label, value in pairs)
    return f'<dl class="tiles">{tiles}</dl>'


def hubcard(title: str, subtitle: str, body: str, wide: bool = False) -> str:
    cls = "hubcard hubcard--wide" if wide else "hubcard"
    return (
        f'<article class="{cls}"><div class="hubcard__head"><h3>{escape(title)}</h3><p>{escape(subtitle)}</p></div>'
        f'<div class="hubcard__body">{body}</div></article>'
    )


def details_panel(summary: str, note: str, inner: str, open_: bool = False) -> str:
    return (
        f'<details class="panel"{" open" if open_ else ""}><summary>{escape(summary)} <span class="muted">{escape(note)}</span></summary>'
        + inner
        + "</details>"
    )


def osm_link(lat: float, lng: float, digits: int = 5) -> str:
    url = f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=16/{lat:.6f}/{lng:.6f}"
    return f'<a href="{escape(url)}" rel="noopener">{lat:.{digits}f}, {lng:.{digits}f}</a>'


def mini_map_svg(points: Sequence[Tuple[float, float, str, str]]) -> str:
    """Inline SVG of Taiwan with device dots: (lat, lng, label, css_modifier)."""
    lng0, lat0, lng1, lat1 = TAIWAN_BBOX
    width, height = 320, 360
    def sx(lng: float) -> float:
        return (lng - lng0) / (lng1 - lng0) * width
    def sy(lat: float) -> float:
        return height - (lat - lat0) / (lat1 - lat0) * height
    parts = [f'<svg class="minimap" viewBox="0 0 {width} {height}" role="img" aria-label="裝置最後位置示意圖">']
    for lng in range(120, 123):
        parts.append(f'<line class="grat" x1="{sx(lng):.1f}" y1="0" x2="{sx(lng):.1f}" y2="{height}"/>'
                     f'<text class="axis" x="{sx(lng) + 2:.1f}" y="{height - 4}">{lng}°E</text>')
    for lat in range(22, 26):
        parts.append(f'<line class="grat" x1="0" y1="{sy(lat):.1f}" x2="{width}" y2="{sy(lat):.1f}"/>'
                     f'<text class="axis" x="3" y="{sy(lat) - 3:.1f}">{lat}°N</text>')
    outline = " ".join(f"{sx(lng):.1f},{sy(lat):.1f}" for lng, lat in TAIWAN_OUTLINE)
    parts.append(f'<polygon class="land" points="{outline}"/>')
    # ponytail: greedy pixel clustering (dots within 18 px merge); fine for a handful of devices
    clusters: List[List[Tuple[float, float, str, str]]] = []
    for pt in points:
        if not (lat0 <= pt[0] <= lat1 and lng0 <= pt[1] <= lng1):
            continue
        for members in clusters:
            if abs(sx(pt[1]) - sx(members[0][1])) < 18 and abs(sy(pt[0]) - sy(members[0][0])) < 18:
                members.append(pt)
                break
        else:
            clusters.append([pt])
    for members in clusters:
        lat = sum(m[0] for m in members) / len(members)
        lng = sum(m[1] for m in members) / len(members)
        mod = members[0][3]
        label = members[0][2] if len(members) == 1 else f"{len(members)} 台（{members[0][2]} 等）"
        title = "、".join(m[2] for m in members)
        x, y = sx(lng), sy(lat)
        parts.append(f'<circle class="dot{(" dot--" + mod) if mod else ""}" cx="{x:.1f}" cy="{y:.1f}" r="{5 + min(len(members), 4)}"><title>{escape(title)}</title></circle>')
        parts.append(f'<text class="lbl" x="{min(x + 10, width - 110):.1f}" y="{y + 4:.1f}">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _elapsed(seconds: object) -> str:
    try:
        total = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days}d {hours}h {rem // 60}m"


def render_riot_console(passes: Sequence[PassRow], packets: Sequence[PacketRow], devices: Sequence[DeviceStats]) -> str:
    decodable = sorted((row for row in packets if row.crc_ok), key=lambda row: (row.unix, row.pass_local), reverse=True)
    last_packet = decodable[0] if decodable else None
    latest_pass = passes[0] if passes else None
    cards = render_cards([
        (f"{len(packets):,}", "IoT 封包（平台檔內 CC 封包）", ""),
        (escape(last_packet.local) if last_packet else "—", "最後可解碼封包產生時間", ""),
        (f"{len(passes):,}", "過境場次（10/01 起）", ""),
        ((escape(latest_pass.pass_local) + f'<br><small>TCA {fmt_number(latest_pass.tca_ele)}°，{latest_pass.n_packets} 包</small>') if latest_pass else "—", "最新一場過境", ""),
    ])
    known = [d for d in devices if d.key != "other"]
    points = [(d.last_lat, d.last_lng, d.label or d.key, "") for d in known if d.last_lat is not None and d.last_lng is not None]
    poslist = "".join(
        f'<li><span class="mono">{escape(d.key)}</span><span>{osm_link(d.last_lat, d.last_lng) if d.last_lat is not None and d.last_lng is not None else "<span class=muted>—</span>"}</span></li>'
        for d in known
    )
    position_card = hubcard("位置", "各裝置最後一筆可解碼封包之座標", mini_map_svg(points) + f'<ul class="poslist">{poslist}</ul>')
    passlist = "".join(
        f'<li><span class="when">{escape(row.pass_local)}</span><span>TCA {fmt_number(row.tca_ele)}°／方位 {fmt_number(row.tca_azi)}°</span><span class="number">{row.n_packets} 包</span></li>'
        for row in passes[:8]
    ) or '<li><span class="muted">尚無過境紀錄</span></li>'
    passes_card = hubcard("最近過境", "鐳洋平台檔案清單；過境預報待接入 TLE 計算", f'<ul class="passlist">{passlist}</ul>')
    device_cards = []
    for d in devices:
        tiles = render_tiles([
            ("封包數", f"{d.n_packets:,}"),
            ("最後可解碼封包", escape(d.last_correct_local or "—")),
            ("最後座標", osm_link(d.last_lat, d.last_lng) if d.last_lat is not None and d.last_lng is not None else "—"),
            ("最近電壓", f"{fmt_number(d.latest_voltage)} V"),
        ])
        mac = "其他" if d.key == "other" else d.key
        device_cards.append(f'<div class="device-card"><h4>{escape(d.label or mac)} <span class="mono">{escape(mac)}</span></h4>{tiles}</div>')
    devices_card = hubcard("裝置狀態", "依 MAC 彙整；時間、座標與電壓採最新可解碼封包", f'<div class="device-list">{"".join(device_cards)}</div>', wide=True)
    grid = f'<div class="grid-2">{position_card}{passes_card}</div>{devices_card}'
    rows = []
    for item in decodable[:50]:
        rows.append((
            f'<th scope="row" class="mono">{escape(item.local or "—")}</th>',
            f'<td class="mono">{escape(item.pass_local)}</td>',
            f'<td class="mono"><a href="raw/{quote(item.file)}" download>{escape(item.file)}</a></td>',
            f'<td class="mono">{escape(item.mac)}</td>',
            f"<td>{osm_link(item.lat, item.lng, 6)}</td>",
            f'<td class="number">{item.bat_v:.1f} V</td>',
            f'<td class="number">{item.rssi:.1f}</td>',
            f'<td class="number">{item.snr:.1f}</td>',
            f'<td class="number">{item.size} B</td>',
        ))
    messages = render_table(
        ("Queue 時間（封包產生）", "衛星 RX 時窗（過境場次）", "雲端 RX（平台檔）", "MAC", "座標", "電壓", "RSSI", "SNR", "長度"),
        rows,
        "鐳洋 IoT 訊息（最近 50 筆可解碼封包）",
    )
    messages_panel = (
        f'<section class="panel"><div class="panel__head"><h3>IoT 訊息（{len(decodable):,} 筆可解碼，顯示最近 50 筆）</h3>'
        '<p>三個時間欄位與 FOSSA Hub 對齊：封包內 UnixTime 為產生時間，過境場次為衛星接收時窗，平台檔為雲端取得。</p></div>'
        + messages + "</section>"
    )
    head = (
        '<div class="console__head"><div><p class="section-kicker">RIOT-2 M9 · 鐳洋 NODE</p><h2>鐳洋 Node</h2></div>'
        '<p>自製節點經 BLACK KITE-2 上行，鐳洋平台每場過境提供一個 UART 紀錄檔；本區每小時自動補抓並解析。</p></div>'
    )
    return head + cards + grid + messages_panel


def render_fossa_console(records: Sequence[FossaRecord], hub_links: Sequence[Dict[str, object]], devices: Sequence[FossaDeviceStats]) -> str:
    latest = max(records, key=fossa_record_order, default=None)
    online = [link for link in hub_links if link.get("active")]
    last_cell = max((float(link.get("last_cell_connection_ms") or 0) for link in hub_links), default=0)
    hub_packets = sum(int(link.get("total_packets") or 0) for link in hub_links)
    hub_last_packet = max((float(link.get("last_packet_ms") or 0) for link in hub_links), default=0)
    cards = render_cards([
        (f"{hub_packets:,}", "IoT packages received（IoT Hub 累計）", ""),
        (escape(_hub_time(hub_last_packet, millis=True)), "最後 IoT package（IoT Hub）", ""),
        (f'{len(online)}<small> / {len(hub_links) or len(devices)}</small>', "上線裝置（IoT Hub）", "signal" if online else "warn"),
        (escape(_hub_time(last_cell, millis=True)), "最後行動網路連線", ""),
    ])
    _ = latest
    points = []
    poslist = []
    for link in hub_links:
        pos = link.get("position") if isinstance(link.get("position"), dict) else {}
        lat, lng = pos.get("lat"), pos.get("lng")
        serial = str(link.get("serial") or "—")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and (lat or lng):
            in_tw = TAIWAN_BBOX[1] <= lat <= TAIWAN_BBOX[3] and TAIWAN_BBOX[0] <= lng <= TAIWAN_BBOX[2]
            points.append((float(lat), float(lng), serial[-4:], "fossa"))
            note = "" if in_tw else ' <span class="muted">（出廠測試位置）</span>'
            poslist.append(f'<li><span class="mono">{escape(serial)}</span><span>{osm_link(float(lat), float(lng))}{note}</span></li>')
        else:
            poslist.append(f'<li><span class="mono">{escape(serial)}</span><span class="muted">—</span></li>')
    position_card = hubcard("位置", "IoT Hub 最後回報位置（行動網路狀態）", mini_map_svg(points) + f'<ul class="poslist">{"".join(poslist)}</ul>')
    passlist = "".join(
        f'<li><span class="when">{escape(_hub_time(link.get("next_pass_s")))}</span><span>{escape(str(link.get("serial") or "—"))}</span>'
        f'<span class="number">{"最後衛星封包 " + _hub_time(link.get("last_sat_packet_s")) if link.get("last_sat_packet_s") else "尚無衛星封包"}</span></li>'
        for link in hub_links
    ) or '<li><span class="muted">尚未拉取 IoT Hub 狀態</span></li>'
    passes_card = hubcard("下一次過境估計", "IoT Hub 依裝置最後位置計算", f'<ul class="passlist">{passlist}</ul>')
    device_cards = []
    for link in hub_links:
        pill = '<span class="status-pill">上線</span>' if link.get("active") else '<span class="status-pill status-pill--off">未上線</span>'
        temp = link.get("temperature_c"); hum = link.get("humidity_pct")
        tiles = render_tiles([
            ("裝置時間", escape(_hub_time(link.get("status_time_s")))),
            ("最後行動網路連線", escape(_hub_time(link.get("last_cell_connection_ms"), millis=True))),
            ("運行時間", _elapsed(link.get("elapsed_s"))),
            ("溫度", f"{fmt_number(temp if isinstance(temp, (int, float)) else None, 1)} °C"),
            ("濕度", f"{fmt_number(hum if isinstance(hum, (int, float)) else None, 0)} %"),
            ("重置 Link／Nexus", f'{link.get("link_resets") if link.get("link_resets") is not None else "—"}／{link.get("nexus_resets") if link.get("nexus_resets") is not None else "—"}'),
            ("韌體 Link／Nexus", escape(_hub_version(link.get("link_fw")) + "／" + _hub_version(link.get("nexus_fw")))),
            ("IoT 封包累計", escape(str(link.get("total_packets") if link.get("total_packets") is not None else "—"))),
            ("最後 IoT 封包", escape(_hub_time(link.get("last_packet_ms"), millis=True))),
            ("下次過境估計", escape(_hub_time(link.get("next_pass_s")))),
            ("最後衛星接收", escape(_hub_time(link.get("last_sat_packet_s")))),
        ])
        device_cards.append(
            f'<div class="device-card"><h4>{escape(str(link.get("serial") or "—"))} {pill} <span class="mono">{escape(str(link.get("link_mac") or ""))}</span></h4>{tiles}</div>'
        )
    if not device_cards:
        device_cards.append('<p class="muted">尚未拉取 IoT Hub 狀態；設定 FOSSA_HUB_EMAIL／FOSSA_HUB_PASSWORD 後由排程自動更新。</p>')
    devices_card = hubcard("裝置狀態", "來自 IoT Hub 之最後狀態回報（行動網路，每 300 s）", f'<div class="device-list">{"".join(device_cards)}</div>', wide=True)
    grid = f'<div class="grid-2">{position_card}{passes_card}</div>{devices_card}'
    rows = []
    for record in sorted(records, key=fossa_record_order, reverse=True)[:50]:
        coord = osm_link(record.lat, record.lng, 6) if record.lat is not None and record.lng is not None else '<span class="muted">—</span>'
        rows.append((
            f'<th scope="row" class="mono">{escape(record.queue_local or "—")}</th>',
            f'<td class="mono">{escape(record.sat_local or "—")}</td>',
            f'<td class="mono">{escape(record.received_at_local or "—")}</td>',
            f'<td>{escape(record.source or "—")}</td>',
            f'<td class="mono">{escape(record.mac or "—")}</td>',
            f'<td class="number">{fmt_number(record.rssi, 0)}</td>',
            f"<td>{coord}</td>",
            f'<td class="mono payload">{escape(record.payload_hex or "—")}</td>',
        ))
    messages = render_table(("Queue 時間", "衛星 RX 時間", "雲端 RX 時間", "來源", "MAC", "RSSI", "暫定座標", "Payload hex"), rows, "FOSSA IoT 訊息")
    if not rows:
        messages = '<div class="empty"><p>尚未收到 FOSSA IoT 訊息；裝置狀態見上方卡片。</p></div>'
    messages_panel = (
        f'<section class="panel"><div class="panel__head"><h3>IoT 訊息（{len(records):,} 筆，顯示最近 50 筆）</h3>'
        '<p>欄位與 FOSSA Hub 一致：Queue（裝置排入）、衛星 RX、雲端 RX（Worker／Hub 取得）。座標為暫定解碼。</p></div>'
        + messages + "</section>"
    )
    return cards + grid + messages_panel


def _hub_time(value: object, millis: bool = False) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    if millis:
        number /= 1000
    moment = datetime.fromtimestamp(number, tz=timezone(timedelta(hours=8)))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _hub_version(value: object) -> str:
    if isinstance(value, dict):
        return ".".join(str(value.get(key, 0)) for key in ("major", "minor", "patch"))
    return "—"


def render_hub_status_panel(links: Sequence[Dict[str, object]]) -> str:
    """FOSSA IoT Hub device status (cellular status reports), mirroring the Hub's own dashboard cards."""
    if not links:
        return ""
    rows = []
    for link in links:
        position = link.get("position") if isinstance(link.get("position"), dict) else {}
        lat, lng = position.get("lat"), position.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and (lat or lng):
            osm = f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=16/{lat:.6f}/{lng:.6f}"
            where = f'<a href="{escape(osm)}" rel="noopener">{lat:.5f}, {lng:.5f}</a>'
        else:
            where = '<span class="muted">—</span>'
        active = '<span class="good">上線</span>' if link.get("active") else '<span class="muted">未上線</span>'
        temp = link.get("temperature_c"); hum = link.get("humidity_pct")
        climate = f"{fmt_number(temp if isinstance(temp, (int, float)) else None, 1)} °C／{fmt_number(hum if isinstance(hum, (int, float)) else None, 0)} %"
        rows.append(
            (
                f'<th scope="row" class="mono">{escape(str(link.get("serial") or "—"))}</th>',
                f"<td>{active}</td>",
                f'<td class="mono">{escape(_hub_time(link.get("last_cell_connection_ms"), millis=True))}</td>',
                f"<td>{where}</td>",
                f"<td>{climate}</td>",
                f'<td class="number">{escape(str(link.get("link_resets") if link.get("link_resets") is not None else "—"))}／{escape(str(link.get("nexus_resets") if link.get("nexus_resets") is not None else "—"))}</td>',
                f'<td class="mono">{escape(_hub_version(link.get("link_fw")))}／{escape(_hub_version(link.get("nexus_fw")))}</td>',
                f'<td class="mono">{escape(_hub_time(link.get("next_pass_s")))}</td>',
                f'<td class="mono">{escape(_hub_time(link.get("last_sat_packet_s")))}</td>',
                f'<td class="number">{escape(str(link.get("total_packets") if link.get("total_packets") is not None else "—"))}</td>',
            )
        )
    table = render_table(
        ("S/N", "狀態", "最後行動網路連線", "位置（最後回報）", "溫度／濕度", "重置 Link／Nexus", "韌體 Link／Nexus", "下次過境估計", "最後衛星封包", "IoT 封包累計"),
        rows,
        "FOSSA IoT Hub 裝置狀態",
    )
    return (
        '<section id="fossa-hub-status" class="panel"><div class="panel__head"><h3>裝置狀態（IoT Hub）</h3>'
        '<p>由 <code>scripts/fetch_fossa_hub.py</code> 以帳號權杖拉取 IoT Hub 之裝置狀態；狀態經行動網路回報，衛星 IoT 封包另列於下方訊息表。</p></div>'
        + table
        + "</section>"
    )


def render_fossa_sections(
    records: Sequence[FossaRecord],
    devices: Sequence[FossaDeviceStats],
    comparison: Sequence[DailyComparison],
    hub_links: Sequence[Dict[str, object]] = (),
) -> str:
    valid_count = sum(record.lat is not None and record.lng is not None for record in records)
    satellite_count = sum(record.source == "satellite" for record in records)
    cellular_count = sum(record.source == "cellular" for record in records)
    metrics = (
        '<section class="metrics" aria-label="FOSSA 資料總覽">'
        f'<dl class="metric"><dt>訊息數</dt><dd>{len(records):,}</dd></dl>'
        f'<dl class="metric metric--signal"><dt>暫定有效座標</dt><dd>{valid_count:,}</dd></dl>'
        f'<dl class="metric"><dt>Satellite</dt><dd>{satellite_count:,}</dd></dl>'
        f'<dl class="metric"><dt>Cellular</dt><dd>{cellular_count:,}</dd></dl>'
        "</section>"
    )

    empty_state = ""
    if not records:
        empty_state = (
            '<div class="panel empty-state"><p><strong>尚未收到 FOSSA 衛星 IoT 訊息。</strong></p>'
            '<p>裝置狀態由 IoT Hub 拉取（下表）；IoT 訊息可經 Integration webhook 或 Hub API 取得。</p></div>'
        )

    device_rows = []
    for device in devices:
        serial = escape(device.serial)
        if device.serial == "未登錄":
            serial = f'<span class="warning">{serial}</span>'
        device_rows.append(
            (
                f'<th scope="row" class="mono">{serial}</th>',
                f'<td class="mono">{escape(device.mac or "待填")}</td>',
                f'<td class="mono">{escape(device.node_id or "待填")}</td>',
                f'<td class="mono">{escape(device.last_received_local or "—")}</td>',
                f'<td class="number">{device.n_messages:,}</td>',
            )
        )
    devices_table = render_table(
        ("S/N", "MAC", "nodeId", "最後收到時間", "筆數"),
        device_rows,
        "FOSSA Nexus 設備統計",
    )
    devices_panel = (
        '<section id="fossa-devices" class="panel"><div class="panel__head"><h3>設備</h3>'
        '<p>在 <code>data/fossa/devices.json</code> 填入註冊後的 MAC／nodeId；未匹配訊息另列為未登錄。</p></div>'
        + devices_table
        + "</section>"
    )

    recent_rows = []
    for record in sorted(records, key=fossa_record_order, reverse=True)[:100]:
        coordinate = '<span class="muted">—</span>'
        if record.lat is not None and record.lng is not None:
            osm = (
                "https://www.openstreetmap.org/"
                f"?mlat={record.lat:.6f}&mlon={record.lng:.6f}#map=16/{record.lat:.6f}/{record.lng:.6f}"
            )
            coordinate = f'<a href="{escape(osm)}" rel="noopener">{record.lat:.6f}, {record.lng:.6f}</a>'
        recent_rows.append(
            (
                f'<th scope="row" class="mono">{escape(record.sat_local or "—")}</th>',
                f'<td class="mono">{escape(record.queue_local or "—")}</td>',
                f'<td class="number">{record.latency_s if record.latency_s is not None else "—"}</td>',
                f'<td class="number">{fmt_number(record.rssi, 0)}</td>',
                f'<td>{escape(record.source or "—")}</td>',
                f"<td>{coordinate}</td>",
                f'<td class="mono payload">{escape(record.payload_hex or "—")}</td>',
            )
        )
    recent_table = render_table(
        ("Sat 時間", "Queue 時間", "延遲 (s)", "RSSI", "來源", "暫定座標", "Payload hex"),
        recent_rows,
        "最近 100 筆 FOSSA Nexus 訊息",
    )
    recent_panel = (
        '<section id="fossa-messages" class="panel"><div class="panel__head"><h3>最近 100 筆訊息</h3>'
        '<p>座標暫按 little-endian float32：lat@0、lng@4 解碼；正式格式仍待 FOSSA 提供。</p></div>'
        + recent_table
        + "</section>"
    )

    daily_panel = (
        '<section id="fossa-daily" class="panel chart"><div class="panel__body"><h3>每日訊息</h3>'
        '<p class="legend"><span><i class="fossa"></i>全部訊息</span>'
        '<span><i class="fossa-valid"></i>暫定有效座標</span></p>'
        f'{fossa_daily_chart(comparison)}</div></section>'
    )
    _ = (metrics, empty_state, recent_panel)
    fossa_section = (
        '<section id="fossa-nexus" class="source-section console"><div class="panel__head section-head">'
        '<div><p class="section-kicker">FOSSA NETWORK</p><h2>FOSSA Nexus</h2></div>'
        '<p>來自 FOSSA IoT Hub 的裝置狀態與 IoT 訊息；版面對齊 Hub 裝置儀表板。'
        '<span class="nowrap">正式座標格式</span>仍待 FOSSA 確認。</p></div>'
        + render_fossa_console(records, hub_links, devices)
        + details_panel('裝置對照與完整狀態表', 'devices.json 對照、IoT Hub 狀態表', render_hub_status_panel(hub_links) + devices_panel)
        + details_panel('每日訊息圖', '全部訊息與暫定有效座標', daily_panel)
        + "</section>"
    )

    comparison_rows = [
        (
            f'<th scope="row" class="mono">{escape(item.date)}</th>',
            f'<td class="number">{item.riot_total:,}</td>',
            f'<td class="number good">{item.riot_valid:,}</td>',
            f'<td class="number">{item.fossa_total:,}</td>',
            f'<td class="number good">{item.fossa_valid:,}</td>',
        )
        for item in reversed(comparison)
    ]
    comparison_table = render_table(
        ("日期", "鐳洋收到", "鐳洋 CRC 正確", "FOSSA 收到", "FOSSA 暫定有效"),
        comparison_rows,
        "鐳洋與 FOSSA 每日接收對照",
    )
    comparison_section = (
        '<section id="source-comparison" class="source-section"><div class="panel__head section-head">'
        '<div><p class="section-kicker">CROSS-NETWORK</p><h2>鐳洋 vs FOSSA 對照</h2></div>'
        '<p>鐳洋有效代表 CRC 正確；FOSSA 有效代表暫定格式'
        '<span class="nowrap">可解出合理座標</span>，兩者定義不同。</p></div>'
        '<section id="comparison-table" class="panel">'
        + comparison_table
        + '</section><section id="comparison-chart" class="panel chart"><div class="panel__body">'
        '<h3>每日接收量</h3><p class="legend">'
        '<span><i></i>鐳洋收到</span><span><i class="ok"></i>鐳洋 CRC 正確</span>'
        '<span><i class="fossa"></i>FOSSA 收到</span><span><i class="fossa-valid"></i>FOSSA 暫定有效</span>'
        f'</p>{comparison_chart(comparison)}</div></section></section>'
    )
    return fossa_section + comparison_section


def render_content(
    passes: Sequence[PassRow],
    packets: Sequence[PacketRow],
    devices: Sequence[DeviceStats],
    fossa_records: Sequence[FossaRecord],
    fossa_devices: Sequence[FossaDeviceStats],
    comparison: Sequence[DailyComparison],
    summary_name: str,
    packets_name: str,
    now: datetime,
) -> str:
    total_ok = sum(row.crc_ok for row in packets)
    success_rate = f"{total_ok / len(packets):.1%}" if packets else "—"
    metrics = (
        '<section class="metrics" aria-label="資料總覽">'
        f'<dl class="metric"><dt>場次數</dt><dd>{len(passes):,}</dd></dl>'
        f'<dl class="metric"><dt>封包數</dt><dd>{len(packets):,}</dd></dl>'
        f'<dl class="metric metric--signal"><dt>CRC 正確</dt><dd>{total_ok:,}</dd></dl>'
        f'<dl class="metric"><dt>正確率</dt><dd>{success_rate}</dd></dl>'
        "</section>"
    )

    latest = passes[0] if passes else None
    cutoff = now - timedelta(hours=24)
    recent_passes = [row for row in passes if datetime.strptime(row.pass_local, "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI) >= cutoff]
    recent_files = {row.file for row in recent_passes}
    recent_packets = [row for row in packets if row.file in recent_files]
    latest_text = "目前沒有場次資料。"
    if latest:
        latest_text = (
            f'<span class="latest-value">{escape(latest.pass_local)}</span>'
            f'TCA {fmt_number(latest.tca_ele)}° / {fmt_number(latest.tca_azi)}°，'
            f'{latest.n_packets:,} 包，<span class="good">{latest.n_crc_ok:,} 包 CRC 正確</span>。'
        )
    recent_text = (
        f'<span class="latest-value">{len(recent_passes):,} 場 · {len(recent_packets):,} 包</span>'
        f'<span class="good">{sum(row.crc_ok for row in recent_packets):,} 包 CRC 正確</span>；'
        f'統計起點 {cutoff.strftime("%m-%d %H:%M")}。'
    )
    latest_panel = (
        '<section class="panel panel--featured"><div class="panel__head"><h2>最新狀態</h2>'
        '<p>最近 24 小時以本次產生頁面的 UTC+8 時刻回推。</p></div><div class="panel__body latest-grid">'
        f'<div class="latest-block"><h3>最新一場</h3><p>{latest_text}</p></div>'
        f'<div class="latest-block"><h3>最近 24 小時</h3><p>{recent_text}</p></div></div></section>'
    )

    pass_rows = []
    known_labels = dict(KNOWN_DEVICES)
    for item in passes:
        snr = "—" if item.snr_min is None else f"{item.snr_min:.1f}～{item.snr_max:.1f}"
        macs = " ".join(
            f'<span class="tag" title="{escape(known_labels.get(mac, ""))}">{escape(mac)}</span>' for mac in item.macs_known
        ) or '<span class="muted">—</span>'
        raw_path = PASS_DIR / item.file
        raw_link = (
            f'<a href="raw/{quote(item.file)}" download>{escape(item.file)}</a>' if raw_path.is_file() else '<span class="muted">未下載</span>'
        )
        pass_rows.append(
            (
                f'<th scope="row" class="mono">{escape(item.pass_local)}</th>',
                f'<td class="number">{fmt_number(item.tca_ele)}°</td>',
                f'<td class="number">{fmt_number(item.tca_azi)}°</td>',
                f'<td class="number">{item.n_packets:,}</td>',
                f'<td class="number good">{item.n_crc_ok:,}</td>',
                f'<td class="number">{snr}</td>',
                f"<td>{macs}</td>",
                f'<td class="mono">{raw_link}</td>',
            )
        )
    passes_table = render_table(
        ("Local 時間", "TCA 仰角", "TCA 方位", "封包", "CRC 正確", "SNR (dB)", "已知 MAC", "原始檔"),
        pass_rows,
        "逐場次資料（最新在上）",
    )
    passes_panel = (
        '<section class="panel"><div class="panel__head"><h2>逐場次</h2>'
        '<p>原始檔可直接下載；空白 SNR 表示該場未解出 CC 封包。</p></div>' + passes_table + "</section>"
    )

    device_rows = []
    for item in devices:
        mac_cell = '<span class="mono">其他</span>' if item.key == "other" else f'<span class="mono">{escape(item.key)}</span>'
        coordinate = '<span class="muted">—</span>'
        if item.last_lat is not None and item.last_lng is not None:
            osm = (
                "https://www.openstreetmap.org/"
                f"?mlat={item.last_lat:.6f}&mlon={item.last_lng:.6f}#map=16/{item.last_lat:.6f}/{item.last_lng:.6f}"
            )
            coordinate = f'<a href="{escape(osm)}" rel="noopener">{item.last_lat:.6f}, {item.last_lng:.6f}</a>'
        device_rows.append(
            (
                f'<th scope="row">{mac_cell}<br><span class="muted">{escape(item.label)}</span></th>',
                f'<td class="number">{item.n_packets:,}</td>',
                f'<td class="number good">{item.n_crc_ok:,}</td>',
                f'<td class="mono">{escape(item.last_correct_local or "—")}</td>',
                f"<td>{coordinate}</td>",
                f'<td class="number">{fmt_number(item.latest_voltage)} V</td>',
            )
        )
    devices_table = render_table(
        ("設備", "筆數", "CRC 正確", "最後一筆正確時間", "最後座標", "最近電壓"),
        device_rows,
        "各設備統計",
    )
    devices_panel = (
        '<section class="panel"><div class="panel__head"><h2>各設備</h2>'
        '<p>筆數包含全部封包；時間、座標與電壓只採最新 CRC 正確封包，避免位元錯誤誤導。</p></div>'
        + devices_table
        + "</section>"
    )

    charts = (
        '<section><div class="panel__head"><h2>訊號與封包品質</h2><p>藍色為全部封包，綠色為 CRC 正確。</p></div>'
        '<div class="charts">'
        f'<article class="panel chart"><div class="panel__body"><h3>每場次封包</h3><p class="legend"><span><i></i>全部封包</span><span><i class="ok"></i>CRC 正確</span></p>{pass_chart(passes)}</div></article>'
        f'<article class="panel chart"><div class="panel__body"><h3>SNR 分箱</h3><p class="legend"><span><i></i>全部封包</span><span><i class="ok"></i>CRC 正確</span></p>{snr_chart(packets)}</div></article>'
        "</div></section>"
    )

    recent_ok = sorted((row for row in packets if row.crc_ok), key=lambda row: (row.unix, row.pass_local), reverse=True)[:50]
    packet_rows = []
    for item in recent_ok:
        osm = f"https://www.openstreetmap.org/?mlat={item.lat:.6f}&mlon={item.lng:.6f}#map=16/{item.lat:.6f}/{item.lng:.6f}"
        packet_rows.append(
            (
                f'<th scope="row" class="mono">{escape(item.local or "—")}</th>',
                f'<td class="mono">{escape(item.mac)}</td>',
                f'<td><a href="{escape(osm)}" rel="noopener">{item.lat:.6f}, {item.lng:.6f}</a></td>',
                f'<td class="number">{item.bat_v:.1f} V</td>',
                f'<td class="number">{item.rssi:.1f}</td>',
                f'<td class="number">{item.snr:.1f}</td>',
                f'<td class="number">{item.size} B</td>',
                f'<td class="mono">{escape(item.pass_local)}</td>',
            )
        )
    packets_table = render_table(
        ("時間", "MAC", "座標", "電壓", "RSSI", "SNR", "長度", "場次"), packet_rows, "最近 50 筆 CRC 正確封包"
    )
    packets_panel = (
        '<section class="panel"><div class="panel__head"><h2>最近 50 筆 CRC 正確封包</h2>'
        '<p>依封包內時間由新到舊；座標連至 OpenStreetMap。</p></div>' + packets_table + "</section>"
    )

    downloads = (
        '<section id="downloads" class="panel"><div class="panel__head"><h2>下載資料</h2><p>提供產生本頁所用的原始結構化資料。</p></div>'
        '<div class="downloads">'
        f'<div class="download"><strong><a href="{quote(summary_name)}" download>逐場次 CSV</a></strong><code>{escape(summary_name)}</code></div>'
        f'<div class="download"><strong><a href="{quote(packets_name)}" download>逐封包 CSV</a></strong><code>{escape(packets_name)}</code></div>'
        '<div class="download"><strong><a href="files.json" download>平台清單 JSON</a></strong><code>files.json</code></div>'
        '<div class="download"><strong><a href="data.json" download>儀表板 JSON</a></strong><code>data.json</code></div>'
        "</div></section>"
    )
    hub_links = load_hub_snapshot(FOSSA_SNAPSHOT)
    online = sum(1 for link in hub_links if link.get("active"))
    overview = (
        '<section id="overview" class="console"><div class="console__head"><div><p class="section-kicker">OVERVIEW</p><h2>總覽</h2></div>'
        '<p>兩條鏈路的最新數字；細節見各自區塊。</p></div>'
        + render_cards([
            (f"{len(passes):,}", "鐳洋過境場次", ""),
            (f"{len(packets):,}", "鐳洋 IoT 封包", ""),
            (f"{len(fossa_records):,}", "FOSSA IoT 訊息", ""),
            (f'{online}<small> / {len(hub_links)}</small>', "FOSSA 上線裝置", "signal" if online else "warn"),
        ])
        + latest_panel
        + "</section>"
    )
    riot_section = (
        '<section id="riot" class="console">'
        + render_riot_console(passes, packets, devices)
        + details_panel('逐場次完整表', f'{len(passes)} 場，含原始檔下載', passes_panel)
        + details_panel('各設備統計表', '含 CRC 正確筆數', devices_panel)
        + details_panel('訊號與封包品質圖', '每場次封包與 SNR 分箱', charts)
        + details_panel('最近 50 筆 CRC 正確封包', '原始欄位表', packets_panel)
        + "</section>"
    )
    _ = metrics
    return (
        overview
        + riot_section
        + render_fossa_sections(fossa_records, fossa_devices, comparison, hub_links)
        + downloads
    )


def prepare_public(files: Sequence[SourceFile], summary: Path, packets: Path) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir = PUBLIC_DIR / "raw"
    raw_dir.mkdir(exist_ok=True)
    for old_file in raw_dir.iterdir():
        if old_file.is_file() or old_file.is_symlink():
            old_file.unlink()
    for pattern in ("*_M9_passes_summary.csv", "*_M9_passes_packets.csv"):
        for old_csv in PUBLIC_DIR.glob(pattern):
            old_csv.unlink()
    shutil.copy2(summary, PUBLIC_DIR / summary.name)
    shutil.copy2(packets, PUBLIC_DIR / packets.name)
    shutil.copy2(FILES_JSON, PUBLIC_DIR / "files.json")
    for item in files:
        source = PASS_DIR / item.file
        if source.is_file():
            shutil.copy2(source, raw_dir / item.file)


def write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    now = datetime.now(TAIPEI)
    summary_path, packets_path = latest_csv_pair()
    source_files = load_source_files(FILES_JSON)
    passes = load_passes(summary_path)
    packets = load_packets(packets_path)
    devices = device_statistics(packets)
    fossa_records = load_records(FOSSA_MESSAGES)
    fossa_devices = fossa_device_statistics(fossa_records, load_fossa_devices(FOSSA_DEVICES))
    comparison = daily_comparison(packets, fossa_records)
    prepare_public(source_files, summary_path, packets_path)

    coverage = "尚無資料"
    if passes:
        coverage = f"{passes[-1].pass_local} – {passes[0].pass_local}"
    content = render_content(
        passes,
        packets,
        devices,
        fossa_records,
        fossa_devices,
        comparison,
        summary_path.name,
        packets_path.name,
        now,
    )
    template = Template((WEB_DIR / "template.html").read_text(encoding="utf-8"))
    html = template.substitute(
        css=(WEB_DIR / "style.css").read_text(encoding="utf-8"),
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
        coverage=coverage,
        content=content,
    )
    data = {
        "generated_at_local": now.isoformat(timespec="seconds"),
        "coverage": coverage,
        "passes": [asdict(item) for item in passes],
        "devices": [asdict(item) for item in devices],
        "fossa": {
            "n_messages": len(fossa_records),
            "n_provisional_valid": sum(
                record.lat is not None and record.lng is not None for record in fossa_records
            ),
            "devices": [asdict(item) for item in fossa_devices],
            "daily": [
                {
                    "date": item.date,
                    "n_messages": item.fossa_total,
                    "n_provisional_valid": item.fossa_valid,
                }
                for item in comparison
                if item.fossa_total
            ],
        },
        "daily_comparison": [asdict(item) for item in comparison],
    }
    write_text_atomic(PUBLIC_DIR / "data.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    write_text_atomic(PUBLIC_DIR / "index.html", html)
    print(
        f"dashboard -> {PUBLIC_DIR / 'index.html'} "
        f"({len(passes)} passes, {len(packets)} packets, {sum(row.crc_ok for row in packets)} CRC ok, "
        f"{len(fossa_records)} FOSSA messages)"
    )


if __name__ == "__main__":
    main()
