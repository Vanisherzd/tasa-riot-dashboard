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
TLE_PASSES: Final = ROOT / "data" / "tle" / "passes.json"


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


def load_predicted_passes(path: Path) -> Dict[str, object]:
    """BLACK KITE-2 passes computed by scripts/predict_passes.py; absent file = no prediction panel."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DashboardDataError(f"無法讀取 {path}: {error}") from error
    return raw if isinstance(raw, dict) else {}


def upcoming_passes(pred: Dict[str, object], now: datetime) -> List[dict]:
    cutoff = now.timestamp() - 600  # keep a pass that is in progress
    return [p for p in pred.get("passes", []) if isinstance(p, dict) and float(p.get("tca_unix") or 0) > cutoff]


def _pass_flag(p: dict, pred: Dict[str, object]) -> str:
    """Right-hand label: uplink window for the Node (fixed TX hours), satellite name for a constellation."""
    if pred.get("uplink_windows"):
        return "上行時段內" if p.get("in_uplink_window") else "非上行時段"
    return escape(str(p.get("sat") or ""))


def predicted_pass_list(pred: Dict[str, object], now: datetime, generated_at: str, limit: int = 8) -> str:
    if not pred:
        return '<ul class="passlist"><li><span class="muted">尚未計算；執行 <code>scripts/predict_passes.py</code>（需 sgp4）。</span></li></ul>'
    rows = "".join(
        f'<li><span class="when">{escape(p["aos"][5:16])} → {escape(p["los"][11:16])}</span>'
        f'<span class="mono">最大仰角 {p["max_el"]:.0f}° · 方位 {p["az_aos"]}°→{p["az_los"]}° · {p["duration_s"] // 60} 分</span>'
        f'<span class="number">{_pass_flag(p, pred)}</span></li>'
        for p in upcoming_passes(pred, now)[:limit]
    ) or '<li><span class="muted">未來 48 小時沒有最大仰角 ≥ 5° 的過境。</span></li>'
    age = pred.get("tle_age_h")
    stale = isinstance(age, (int, float)) and age > 72
    sats = pred.get("satellites") or []
    who = f'{len(sats)} 顆：{escape("、".join(str(s) for s in sats))}；' if len(sats) > 1 else ""
    note = (f'<li><span class="muted">{who}TLE epoch {escape(str(pred.get("tle_epoch", "—")))}（{escape(str(pred.get("source", "")))}）'
            f'{"，最舊元素已超過 3 天，誤差增大" if stale else ""}；觀測點 {escape(str((pred.get("site") or {}).get("name", "")))}；'
            f'計算於 {escape(generated_at)}</span></li>')
    return f'<ul class="passlist">{rows}{note}</ul>'


def next_pass_tile(pred: Dict[str, object], now: datetime) -> str:
    items = upcoming_passes(pred, now)
    if not items:
        return "—"
    p = items[0]
    tail = ("" if p.get("in_uplink_window") else "（非上行時段）") if pred.get("uplink_windows") else f' · {escape(str(p.get("sat") or ""))}'
    return f'{escape(p["aos"][5:16])} · {p["max_el"]:.0f}°{tail}'


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


# ---------------------------------------------------------------- Hub-style app shell
TAIWAN_BBOX: Final = (119.3, 21.8, 122.2, 25.5)  # lng_min, lat_min, lng_max, lat_max
RIOT_COLOR: Final = "#43d6a0"
FOSSA_COLOR: Final = "#ffb454"


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


def osm_link(lat: float, lng: float, digits: int = 5) -> str:
    url = f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=16/{lat:.6f}/{lng:.6f}"
    return f'<a href="{escape(url)}" rel="noopener">{lat:.{digits}f}, {lng:.{digits}f}</a>'


def in_taiwan(lat: float, lng: float) -> bool:
    return TAIWAN_BBOX[1] <= lat <= TAIWAN_BBOX[3] and TAIWAN_BBOX[0] <= lng <= TAIWAN_BBOX[2]


def plausible(lat: Optional[float], lng: Optional[float]) -> bool:
    """GPS fix worth plotting: not null island, within range (a 0,0 fix is a cold-start default)."""
    return lat is not None and lng is not None and abs(lat) <= 90 and abs(lng) <= 180 and (abs(lat) > 0.01 or abs(lng) > 0.01)


def map_point(lat: float, lng: float, label: str, color: str, html: str = "", radius: int = 8, opacity: float = 1.0) -> dict:
    return {"lat": round(lat, 6), "lng": round(lng, 6), "label": label, "color": color, "html": html or escape(label), "r": radius, "op": opacity}


def map_div(points: Sequence[dict], zoom: int = 16) -> str:
    """Leaflet placeholder; template script draws Esri World Imagery tiles and the markers."""
    payload = escape(json.dumps(points, ensure_ascii=False, separators=(",", ":")), quote=True)
    return (
        f'<div class="map" data-points="{payload}" data-zoom="{zoom}" role="img" aria-label="裝置位置衛星影像地圖">'
        '<p class="map__fallback">衛星影像地圖需要 JavaScript 與網路連線。</p></div>'
    )


def _elapsed(seconds: object) -> str:
    try:
        total = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days}d {hours}h {rem // 60}m"


def _hub_time(value: object, millis: bool = False) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    if millis:
        number /= 1000
    moment = datetime.fromtimestamp(number, tz=TAIPEI)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _hub_next_pass(value: object) -> str:
    """Next pass from the Hub snapshot; flag it once it is in the past so a stale snapshot is visible."""
    text = _hub_time(value)
    try:
        if text != "—" and float(value) < datetime.now(TAIPEI).timestamp():  # type: ignore[arg-type]
            return text + "（已過，待 Hub 更新）"
    except (TypeError, ValueError):
        pass
    return text


def hub_snapshot_time(path: Path) -> str:
    """When scripts/fetch_fossa_hub.py last pulled the Hub; shown so a stale snapshot is obvious."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "—"
    return _hub_time(raw.get("fetched_at")) if isinstance(raw, dict) else "—"


def _hub_version(value: object) -> str:
    if isinstance(value, dict):
        return ".".join(str(value.get(key, 0)) for key in ("major", "minor", "patch"))
    return "—"


def _num(value: object, digits: int = 1, unit: str = "") -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.{digits}f}{unit}"
    return "—"


def _plain(value: object) -> str:
    return escape(str(value)) if value is not None and value != "" else "—"


def view_open(key: str, kicker: str, title_html: str, tabs: Sequence[Tuple[str, str]], lead: str = "") -> str:
    tab_html = "".join(
        f'<button class="tab" type="button" role="tab" data-tab="{escape(tid)}">{escape(name)}</button>' for tid, name in tabs
    )
    lead_html = f'<p class="view__lead">{lead}</p>' if lead else ""
    nav = f'<nav class="tabs" role="tablist" aria-label="分頁">{tab_html}</nav>' if tabs else ""
    return (
        f'<section class="view" data-view="{escape(key)}"><header class="view__head"><div><p class="kicker">{escape(kicker)}</p>'
        f"<h2>{title_html}</h2>{lead_html}</div>{nav}</header>"
    )


def tabpanel(tid: str, inner: str) -> str:
    return f'<div class="tabpanel" data-tab="{escape(tid)}" role="tabpanel">{inner}</div>'


def pill(text: str, on: bool) -> str:
    return f'<span class="status-pill{"" if on else " status-pill--off"}">{escape(text)}</span>'


def devlink(href: str, color: str, title_html: str, sub: str, status_html: str) -> str:
    return (
        f'<a class="devlink" href="{escape(href)}"><i class="devlink__dot" style="background:{color}"></i>'
        f'<span class="devlink__main"><strong>{title_html}</strong><span class="mono">{escape(sub)}</span></span>'
        f'<span class="devlink__status">{status_html}</span></a>'
    )


def grid2(*cards: str) -> str:
    return f'<div class="grid-2">{"".join(cards)}</div>'


def panel(title: str, note: str, inner: str) -> str:
    return f'<section class="panel"><div class="panel__head"><h3>{escape(title)}</h3><p>{note}</p></div>{inner}</section>'


# ---------------------------------------------------------------- 鐳洋 Node views
def riot_messages_table(items: Sequence[PacketRow], caption: str, limit: int) -> str:
    rows = []
    for item in items[:limit]:
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
    if not rows:
        return '<div class="empty"><p>尚無可解碼封包。</p></div>'
    return render_table(
        ("Queue 時間（封包產生）", "衛星 RX 時窗（過境場次）", "雲端 RX（平台檔）", "MAC", "座標", "電壓", "RSSI", "SNR", "長度"),
        rows,
        caption,
    )


def riot_passes_table(passes: Sequence[PassRow], caption: str) -> str:
    known_labels = dict(KNOWN_DEVICES)
    rows = []
    for item in passes:
        snr = "—" if item.snr_min is None else f"{item.snr_min:.1f}～{item.snr_max:.1f}"
        macs = " ".join(
            f'<a class="tag" href="#riot/{escape(mac)}" title="{escape(known_labels.get(mac, ""))}">{escape(mac)}</a>' for mac in item.macs_known
        ) or '<span class="muted">—</span>'
        raw_path = PASS_DIR / item.file
        raw_link = (
            f'<a href="raw/{quote(item.file)}" download>{escape(item.file)}</a>' if raw_path.is_file() else '<span class="muted">未下載</span>'
        )
        rows.append((
            f'<th scope="row" class="mono">{escape(item.pass_local)}</th>',
            f'<td class="number">{fmt_number(item.tca_ele)}°</td>',
            f'<td class="number">{fmt_number(item.tca_azi)}°</td>',
            f'<td class="number">{item.n_packets:,}</td>',
            f'<td class="number good">{item.n_crc_ok:,}</td>',
            f'<td class="number">{snr}</td>',
            f"<td>{macs}</td>",
            f'<td class="mono">{raw_link}</td>',
        ))
    if not rows:
        return '<div class="empty"><p>尚無過境紀錄。</p></div>'
    return render_table(("Local 時間", "TCA 仰角", "TCA 方位", "封包", "可解碼", "SNR (dB)", "已知 MAC", "原始檔"), rows, caption)


def riot_pass_list(passes: Sequence[PassRow], limit: int = 8) -> str:
    items = "".join(
        f'<li><span class="when">{escape(row.pass_local)}</span><span>TCA {fmt_number(row.tca_ele)}°／方位 {fmt_number(row.tca_azi)}°</span>'
        f'<span class="number">{row.n_packets} 包</span></li>'
        for row in passes[:limit]
    )
    return f'<ul class="passlist">{items or "<li><span class=muted>尚無過境紀錄</span></li>"}</ul>'


def riot_charts(passes: Sequence[PassRow], packets: Sequence[PacketRow]) -> str:
    return (
        '<div class="charts">'
        f'<article class="panel chart"><div class="panel__body"><h3>每場次封包</h3><p class="legend"><span><i></i>全部封包</span><span><i class="ok"></i>可解碼</span></p>{pass_chart(passes)}</div></article>'
        f'<article class="panel chart"><div class="panel__body"><h3>SNR 分箱</h3><p class="legend"><span><i></i>全部封包</span><span><i class="ok"></i>可解碼</span></p>{snr_chart(packets)}</div></article>'
        "</div>"
    )


def render_riot_views(passes: Sequence[PassRow], packets: Sequence[PacketRow], devices: Sequence[DeviceStats], predicted: Dict[str, object], now: datetime) -> str:
    decodable = sorted((r for r in packets if r.crc_ok), key=lambda r: (r.unix, r.pass_local), reverse=True)
    known = [d for d in devices if d.key != "other"]
    other = next((d for d in devices if d.key == "other"), None)
    last = decodable[0] if decodable else None
    latest_pass = passes[0] if passes else None
    cards = render_cards([
        (f"{len(decodable):,}<small> / {len(packets):,}</small>", "可解碼 IoT 封包／全部封包", ""),
        (escape(last.local) if last else "—", "最後可解碼封包時間", ""),
        (f"{len(passes):,}", "過境場次（10/01 起）", ""),
        ((escape(latest_pass.pass_local) + f"<br><small>TCA {fmt_number(latest_pass.tca_ele)}°，{latest_pass.n_packets} 包</small>") if latest_pass else "—", "最新一場過境", ""),
    ])
    points = [
        map_point(d.last_lat, d.last_lng, d.label or d.key, RIOT_COLOR,
                  f"<b>{escape(d.label or d.key)}</b><br><span class=mono>{escape(d.key)}</span><br>{escape(d.last_correct_local or '')}")
        for d in known if plausible(d.last_lat, d.last_lng)
    ]
    links = "".join(
        devlink(f"#riot/{d.key}", RIOT_COLOR, escape(d.label or d.key), d.key,
                f'<span class="mono">{escape(d.last_correct_local or "—")}</span><small>最後可解碼 · {d.n_crc_ok:,}/{d.n_packets:,} 包</small>')
        for d in known
    )
    if other:
        links += (
            f'<div class="devlink devlink--static"><i class="devlink__dot" style="background:var(--ink-muted)"></i>'
            f'<span class="devlink__main"><strong>{escape(other.label)}</strong><span class="mono">—</span></span>'
            f'<span class="devlink__status"><span class="mono">{other.n_packets:,} 包</span><small>地址無法解碼</small></span></div>'
        )
    dash = (
        cards
        + grid2(
            hubcard("位置", "各裝置最後一筆可解碼封包之座標（Esri 衛星影像）", map_div(points, 15)),
            hubcard("裝置", "點選進入各裝置儀表板", f'<div class="devlist">{links}</div>'),
        )
        + grid2(
            hubcard("下次過境預測", "BLACK KITE-2，TLE＋SGP4，未來 48 小時；韌體上行時段 01–04、13–16", predicted_pass_list(predicted.get("riot") or {}, now, str(predicted.get("generated_at", "—")))),
            hubcard("最近過境", "鐳洋平台檔案清單（實際接收場次）", riot_pass_list(passes)),
        )
        + hubcard("訊號品質", "每場次封包數與可解碼數", f'<div class="chart chart--compact">{pass_chart(passes)}</div>', wide=True)
        + hubcard("最近 IoT 訊息", "三個時間欄位與 FOSSA Hub 對齊：封包內 UnixTime、過境場次、平台檔取得時間",
                  riot_messages_table(decodable, "鐳洋最近 10 筆可解碼封包", 10), wide=True)
    )
    out = [
        view_open("riot", "鐳洋 NODE · RIOT-2 M9 · BLACK KITE-2", "鐳洋 Node 網路",
                  [("dash", "儀表板"), ("passes", "過境"), ("quality", "訊號品質"), ("msgs", "IoT 訊息")],
                  "自製節點經 BLACK KITE-2 上行，鐳洋平台每場過境提供一個 UART 紀錄檔；每小時自動補抓並解析。"),
        tabpanel("dash", dash),
        tabpanel("passes", panel("逐場次", f"{len(passes)} 場，最新在上；原始檔可直接下載。", riot_passes_table(passes, "逐場次資料"))),
        tabpanel("quality", riot_charts(passes, packets)),
        tabpanel("msgs", panel("IoT 訊息", f"{len(decodable):,} 筆可解碼封包，顯示最近 200 筆。", riot_messages_table(decodable, "鐳洋可解碼封包", 200))),
        "</section>",
    ]
    for d in known:
        mine = [r for r in decodable if r.mac == d.key]
        mine_all = [r for r in packets if r.mac == d.key]
        my_passes = [p for p in passes if d.key in p.macs_known]
        last = mine[0] if mine else None
        title = f'{escape(d.label or d.key)} <span class="mono">{escape(d.key)}</span>'
        cards = render_cards([
            (f"{len(mine):,}<small> / {len(mine_all):,}</small>", "可解碼 IoT 封包／全部", ""),
            (escape(last.local) if last else "—", "最後可解碼封包時間", ""),
            (f"{len(my_passes):,}", "出現的過境場次", ""),
            (f"{fmt_number(d.latest_voltage)} V", "最近電壓", "warn" if d.latest_voltage is not None and d.latest_voltage < 3.6 else ""),
        ])
        pts: List[dict] = []
        located = [r for r in mine if plausible(r.lat, r.lng)]
        if located:
            head = located[0]
            pts.append(map_point(head.lat, head.lng, d.label or d.key, RIOT_COLOR,
                                 f"<b>{escape(d.label or d.key)}</b><br>{escape(head.local)}<br>{head.lat:.6f}, {head.lng:.6f}", radius=9))
            seen = {(round(head.lat, 5), round(head.lng, 5))}
            for r in located[1:]:
                key = (round(r.lat, 5), round(r.lng, 5))
                if key in seen:
                    continue
                seen.add(key)
                pts.append(map_point(r.lat, r.lng, r.local, RIOT_COLOR, f"{escape(r.local)}<br>{r.lat:.6f}, {r.lng:.6f}", radius=4, opacity=0.45))
                if len(pts) > 40:
                    break
        status = render_tiles([
            ("最後座標", osm_link(last.lat, last.lng, 6) if last else "—"),
            ("最後 RSSI／SNR", f"{last.rssi:.1f}／{last.snr:.1f} dB" if last else "—"),
            ("封包長度", f"{last.size} B" if last else "—"),
            ("最後過境場次", escape(last.pass_local) if last else "—"),
            ("最後平台檔", f'<a href="raw/{quote(last.file)}" download>{escape(last.file)}</a>' if last else "—"),
            ("可解碼比例", f"{len(mine) / len(mine_all):.0%}" if mine_all else "—"),
        ])
        dash = (
            cards
            + grid2(
                hubcard("位置", "大點為最後可解碼封包位置，小點為先前位置", map_div(pts, 17)),
                hubcard("裝置狀態", "採最新可解碼封包", status),
            )
            + hubcard("最近 IoT 訊息", "最近 10 筆可解碼封包", riot_messages_table(mine, f"{d.key} 最近封包", 10), wide=True)
        )
        config = render_tiles([
            ("名稱", escape(d.label or "—")),
            ("MAC", f'<span class="mono">{escape(d.key)}</span>'),
            ("上行衛星", "BLACK KITE-2"),
            ("地面平台", "鐳洋 RIoT-2 M9"),
            ("資料來源", "鐳洋平台每場過境 UART 紀錄檔"),
            ("封包驗證", "CRC-8（poly 0x31）"),
        ])
        out += [
            view_open(f"riot/{d.key}", "鐳洋 NODE · 裝置", title, [("dash", "儀表板"), ("msgs", "IoT 訊息"), ("passes", "過境"), ("config", "設定")]),
            tabpanel("dash", dash),
            tabpanel("msgs", panel("IoT 訊息", f"{len(mine):,} 筆可解碼封包，顯示最近 200 筆。", riot_messages_table(mine, f"{d.key} 可解碼封包", 200))),
            tabpanel("passes", panel("過境", f"此裝置出現在 {len(my_passes)} 場過境。", riot_passes_table(my_passes, f"{d.key} 過境"))),
            tabpanel("config", hubcard("設定", "裝置登錄資訊（靜態）", config, wide=True)),
            "</section>",
        ]
    return "".join(out)


# ---------------------------------------------------------------- FOSSA Nexus views
def fossa_units(hub_links: Sequence[Dict[str, object]], devices: Sequence[FossaDevice], records: Sequence[FossaRecord]) -> List[dict]:
    """One entry per Nexus: Hub status joined with devices.json and the IoT records that match its MACs."""
    by_serial = {d.serial.casefold(): d for d in devices}
    units: List[dict] = []
    seen = set()
    for link in hub_links:
        serial = str(link.get("serial") or "")
        dev = by_serial.get(serial.casefold())
        units.append({"serial": serial or (dev.serial if dev else "—"), "link": link, "dev": dev})
        seen.add(serial.casefold())
    for d in devices:
        if d.serial.casefold() not in seen:
            units.append({"serial": d.serial, "link": {}, "dev": d})
    for unit in units:
        link, dev = unit["link"], unit["dev"]
        macs = {_norm_mac(str(v)) for v in (link.get("link_mac"), link.get("nexus_mac"), dev.mac if dev else "", dev.nexus_mac if dev else "") if v}
        node_id = dev.node_id if dev else ""
        unit["records"] = sorted(
            (r for r in records if _norm_mac(r.mac) in macs or (node_id and r.node_id == node_id)),
            key=fossa_record_order, reverse=True,
        )
        pos = link.get("position") if isinstance(link.get("position"), dict) else {}
        lat, lng = pos.get("lat"), pos.get("lng")
        unit["pos"] = (float(lat), float(lng)) if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and (lat or lng) else None
    return units


def fossa_messages_table(records: Sequence[FossaRecord], caption: str, limit: int) -> str:
    rows = []
    for record in records[:limit]:
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
    if not rows:
        return '<div class="empty"><p>尚未收到衛星 IoT 訊息；IoT Hub 的封包計數見上方卡片。</p></div>'
    return render_table(("Queue 時間", "衛星 RX 時間", "雲端 RX 時間", "來源", "MAC", "RSSI", "暫定座標", "Payload hex"), rows, caption)


def _power(value: object) -> str:
    if not isinstance(value, dict):
        return "—"
    def volts(key: str) -> str:
        raw = value.get(key)
        return f"{raw / 1000:.2f} V" if isinstance(raw, (int, float)) else "—"
    return f'{volts("battVolt")}／{volts("bus3Volt")}／{volts("bus5Volt")}'


def fossa_host_tiles(link: Dict[str, object]) -> str:
    return render_tiles([
        ("裝置時間", escape(_hub_time(link.get("status_time_s")))),
        ("最後行動網路連線", escape(_hub_time(link.get("last_cell_connection_ms"), millis=True))),
        ("運行時間", _elapsed(link.get("elapsed_s"))),
        ("主機溫度", _num(link.get("temperature_c"), 1, " °C")),
        ("主機濕度", _num(link.get("humidity_pct"), 0, " %")),
        ("重置 Link／Nexus", f'{_plain(link.get("link_resets"))}／{_plain(link.get("nexus_resets"))}'),
        ("韌體 Link／Nexus", escape(_hub_version(link.get("link_fw")) + "／" + _hub_version(link.get("nexus_fw")))),
        ("電源（電池／3V3／5V）", _power(link.get("power"))),
        ("衛星 RX 封包計數", _plain(link.get("rx_packet_count"))),
        ("TX 佇列", _plain(link.get("tx_queue_len"))),
        ("最後衛星接收", escape(_hub_time(link.get("last_sat_packet_s")))),
        ("下次過境估計", escape(_hub_next_pass(link.get("next_pass_s")))),
    ])


def fossa_hub_table(units: Sequence[dict]) -> str:
    rows = []
    for unit in units:
        link = unit["link"]
        where = osm_link(*unit["pos"]) if unit["pos"] else '<span class="muted">—</span>'
        if unit["pos"] and not in_taiwan(*unit["pos"]):
            where += ' <span class="muted">（出廠測試位置）</span>'
        rows.append((
            f'<th scope="row" class="mono"><a href="#fossa/{escape(unit["serial"])}">{escape(unit["serial"])}</a></th>',
            f'<td>{"<span class=good>上線</span>" if link.get("active") else "<span class=muted>未上線</span>"}</td>',
            f'<td class="mono">{escape(_hub_time(link.get("last_cell_connection_ms"), millis=True))}</td>',
            f"<td>{where}</td>",
            f'<td>{_num(link.get("temperature_c"), 1, " °C")}／{_num(link.get("humidity_pct"), 0, " %")}</td>',
            f'<td class="number">{_plain(link.get("link_resets"))}／{_plain(link.get("nexus_resets"))}</td>',
            f'<td class="mono">{escape(_hub_version(link.get("link_fw")))}／{escape(_hub_version(link.get("nexus_fw")))}</td>',
            f'<td class="mono">{escape(_hub_next_pass(link.get("next_pass_s")))}</td>',
            f'<td class="mono">{escape(_hub_time(link.get("last_sat_packet_s")))}</td>',
            f'<td class="number">{_plain(link.get("total_packets"))}</td>',
            f'<td class="number">{len(unit["records"]):,}</td>',
        ))
    return render_table(
        ("S/N", "狀態", "最後行動網路連線", "位置（最後回報）", "溫度／濕度", "重置 Link／Nexus", "韌體 Link／Nexus", "下次過境估計", "最後衛星封包", "IoT 封包累計", "本站訊息"),
        rows,
        "FOSSA IoT Hub 裝置狀態",
    )


def render_fossa_views(units: Sequence[dict], records: Sequence[FossaRecord], comparison: Sequence[DailyComparison], predicted: Dict[str, object], now: datetime) -> str:
    links = [u["link"] for u in units if u["link"]]
    online = [l for l in links if l.get("active")]
    hub_packets = sum(int(l.get("total_packets") or 0) for l in links)
    hub_last_packet = max((float(l.get("last_packet_ms") or 0) for l in links), default=0)
    last_cell = max((float(l.get("last_cell_connection_ms") or 0) for l in links), default=0)
    cards = render_cards([
        (f"{hub_packets:,}", "IoT packages received（IoT Hub 累計）", ""),
        (escape(_hub_time(hub_last_packet, millis=True)), "最後 IoT package（IoT Hub）", ""),
        (f'{len(online)}<small> / {len(units)}</small>', "上線裝置（IoT Hub）", "signal" if online else "warn"),
        (escape(_hub_time(last_cell, millis=True)), "最後行動網路連線", ""),
    ])
    points = [
        map_point(u["pos"][0], u["pos"][1], u["serial"], FOSSA_COLOR,
                  f"<b>{escape(u['serial'])}</b><br>{escape(_hub_time(u['link'].get('last_cell_connection_ms'), millis=True))}")
        for u in units if u["pos"] and in_taiwan(*u["pos"])
    ]
    devlinks = "".join(
        devlink(f"#fossa/{u['serial']}", FOSSA_COLOR, escape(u["serial"]) + " " + pill("上線" if u["link"].get("active") else "未上線", bool(u["link"].get("active"))),
                str(u["link"].get("link_mac") or (u["dev"].mac if u["dev"] else "")),
                f'<span class="mono">{escape(_hub_time(u["link"].get("last_cell_connection_ms"), millis=True))}</span><small>最後行動網路連線 · IoT 封包 {_plain(u["link"].get("total_packets"))}</small>')
        for u in units
    ) or '<p class="muted">尚未登錄裝置。</p>'
    passlist = "".join(
        f'<li><span class="when">{escape(_hub_next_pass(u["link"].get("next_pass_s")))}</span><span class="mono">{escape(u["serial"])}</span>'
        f'<span class="number">{"最後衛星封包 " + _hub_time(u["link"].get("last_sat_packet_s")) if u["link"].get("last_sat_packet_s") else "尚無衛星封包"}</span></li>'
        for u in units if u["link"]
    ) or '<li><span class="muted">尚未拉取 IoT Hub 狀態；設定 FOSSA_HUB_EMAIL／FOSSA_HUB_PASSWORD 後由排程自動更新。</span></li>'
    dash = (
        cards
        + grid2(
            hubcard("位置", "IoT Hub 最後回報位置（行動網路狀態）；出廠測試座標不顯示", map_div(points, 15)),
            hubcard("裝置", "點選進入各裝置儀表板", f'<div class="devlist">{devlinks}</div>'),
        )
        + grid2(
            hubcard("下一次過境估計（IoT Hub）", f"Hub 依裝置最後位置計算，只對上線裝置更新；快照 {hub_snapshot_time(FOSSA_SNAPSHOT)}", f'<ul class="passlist">{passlist}</ul>'),
            hubcard("下次過境預測（TLE）", "FOSSASAT-2E 星系，CelesTrak TLE＋SGP4，未來 48 小時；Hub 估計與其中最早一顆吻合", predicted_pass_list(predicted.get("fossa") or {}, now, str(predicted.get("generated_at", "—")))),
        )
        + hubcard("每日訊息", "全部訊息與暫定有效座標", f'<div class="chart chart--compact">{fossa_daily_chart(comparison)}</div>', wide=True)
        + hubcard("最近 IoT 訊息", "欄位與 FOSSA Hub 一致：Queue（裝置排入）、衛星 RX、雲端 RX（Hub／Worker 取得）；座標為暫定解碼",
                  fossa_messages_table(sorted(records, key=fossa_record_order, reverse=True), "FOSSA 最近 10 筆訊息", 10), wide=True)
    )
    out = [
        view_open("fossa", "FOSSA NETWORK · NEXUS", "FOSSA Nexus 網路",
                  [("dash", "儀表板"), ("status", "裝置狀態表"), ("msgs", "IoT 訊息")],
                  f"裝置狀態由 IoT Hub 以帳號權杖拉取（行動網路回報，每 300 s），快照時間 {hub_snapshot_time(FOSSA_SNAPSHOT)}；衛星 IoT 訊息經 Hub API 或 Integration webhook 取得。"),
        tabpanel("dash", dash),
        tabpanel("status", panel("裝置狀態（IoT Hub）", "由 <code>scripts/fetch_fossa_hub.py</code> 拉取；本站訊息為經本儀表板收到的衛星 IoT 訊息筆數。", fossa_hub_table(units))),
        tabpanel("msgs", panel("IoT 訊息", f"{len(records):,} 筆，顯示最近 200 筆。", fossa_messages_table(sorted(records, key=fossa_record_order, reverse=True), "FOSSA IoT 訊息", 200))),
        "</section>",
    ]
    for u in units:
        link, dev, serial = u["link"], u["dev"], u["serial"]
        title = f'{escape(serial)} {pill("上線" if link.get("active") else "未上線", bool(link.get("active")))} <span class="mono">{escape(str(link.get("link_mac") or (dev.mac if dev else "")))}</span>'
        cards = render_cards([
            (_plain(link.get("total_packets")), "IoT packages received（IoT Hub）", ""),
            (escape(_hub_time(link.get("last_packet_ms"), millis=True)), "最後 IoT package（IoT Hub）", ""),
            (escape(_hub_next_pass(link.get("next_pass_s"))), "下次過境估計", ""),
            (escape(_hub_time(link.get("last_cell_connection_ms"), millis=True)), "最後行動網路連線", "signal" if link.get("active") else ""),
        ])
        pts = []
        map_note = "IoT Hub 最後回報位置（Esri 衛星影像）"
        if u["pos"] and in_taiwan(*u["pos"]):
            pts.append(map_point(u["pos"][0], u["pos"][1], serial, FOSSA_COLOR, f"<b>{escape(serial)}</b><br>{u['pos'][0]:.6f}, {u['pos'][1]:.6f}", radius=9))
        elif u["pos"]:
            map_note = f"Hub 僅有出廠測試座標 {u['pos'][0]:.5f}, {u['pos'][1]:.5f}，裝置上線後更新"
        else:
            map_note = "Hub 尚無位置回報"
        dash = (
            cards
            + grid2(hubcard("位置", map_note, map_div(pts, 17)), hubcard("主機狀態", "IoT Hub 最後狀態回報", fossa_host_tiles(link) if link else '<p class="muted">尚未拉取 IoT Hub 狀態。</p>'))
            + hubcard("最近 IoT 訊息", "最近 10 筆", fossa_messages_table(u["records"], f"{serial} 最近訊息", 10), wide=True)
        )
        integrations = link.get("integrations")
        config = render_tiles([
            ("裝置", "FOSSA Nexus（Link + Nexus modem）"),
            ("S/N", f'<span class="mono">{escape(serial)}</span>'),
            ("Link MAC", f'<span class="mono">{escape(str(link.get("link_mac") or (dev.mac if dev else "—")))}</span>'),
            ("Nexus MAC", f'<span class="mono">{escape(str(link.get("nexus_mac") or (dev.nexus_mac if dev else "—")))}</span>'),
            ("Hub link id", f'<span class="mono">{escape(str(link.get("link_id") or (dev.node_id if dev else "—")))}</span>'),
            ("名稱（Hub）", _plain(link.get("name"))),
            ("行動網路回報週期", _num(link.get("cell_period_s"), 0, " s")),
            ("僅衛星模式", "是" if link.get("force_only_sat") else ("否" if link else "—")),
            ("Integrations（webhook）", f"{len(integrations)} 組" if isinstance(integrations, list) and integrations else ("無" if link else "—")),
            ("IoT 命令累計", _plain(link.get("total_commands"))),
        ])
        out += [
            view_open(f"fossa/{serial}", "FOSSA NEXUS · 裝置", title, [("dash", "儀表板"), ("msgs", "IoT 訊息"), ("config", "設定")]),
            tabpanel("dash", dash),
            tabpanel("msgs", panel("IoT 訊息", f"{len(u['records']):,} 筆，顯示最近 200 筆。", fossa_messages_table(u["records"], f"{serial} IoT 訊息", 200))),
            tabpanel("config", hubcard("設定", "對應 IoT Hub Configuration 分頁；資料來自 Hub 狀態快照與 devices.json", config, wide=True)),
            "</section>",
        ]
    return "".join(out)


# ---------------------------------------------------------------- overview / compare / downloads / nav
def render_overview(passes, packets, devices, units, records, predicted: Dict[str, object], now: datetime) -> str:
    decodable = sorted((r for r in packets if r.crc_ok), key=lambda r: (r.unix, r.pass_local), reverse=True)
    known = [d for d in devices if d.key != "other"]
    links = [u["link"] for u in units if u["link"]]
    online = [l for l in links if l.get("active")]
    hub_packets = sum(int(l.get("total_packets") or 0) for l in links)
    cards = render_cards([
        (f"{len(decodable):,}", "鐳洋可解碼 IoT 封包", ""),
        (escape(decodable[0].local) if decodable else "—", "鐳洋最後可解碼封包", ""),
        (f"{hub_packets:,}", "FOSSA IoT packages received", ""),
        (f'{len(online)}<small> / {len(units)}</small>', "FOSSA 上線裝置", "signal" if online else "warn"),
    ])
    points = [
        map_point(d.last_lat, d.last_lng, d.label or d.key, RIOT_COLOR, f"<b>{escape(d.label or d.key)}</b><br><span class=mono>{escape(d.key)}</span><br>鐳洋 Node")
        for d in known if plausible(d.last_lat, d.last_lng)
    ] + [
        map_point(u["pos"][0], u["pos"][1], u["serial"], FOSSA_COLOR, f"<b>{escape(u['serial'])}</b><br>FOSSA Nexus")
        for u in units if u["pos"] and in_taiwan(*u["pos"])
    ]
    devlinks = "".join(
        devlink(f"#riot/{d.key}", RIOT_COLOR, escape(d.label or d.key), d.key, f'<span class="mono">{escape(d.last_correct_local or "—")}</span><small>鐳洋 Node · 最後可解碼</small>')
        for d in known
    ) + "".join(
        devlink(f"#fossa/{u['serial']}", FOSSA_COLOR, escape(u["serial"]) + " " + pill("上線" if u["link"].get("active") else "未上線", bool(u["link"].get("active"))),
                str(u["link"].get("link_mac") or (u["dev"].mac if u["dev"] else "")),
                f'<span class="mono">{escape(_hub_time(u["link"].get("last_cell_connection_ms"), millis=True))}</span><small>FOSSA Nexus · 最後行動網路連線</small>')
        for u in units
    )
    latest = passes[0] if passes else None
    cutoff = now - timedelta(hours=24)
    recent = [row for row in passes if datetime.strptime(row.pass_local, "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI) >= cutoff]
    recent_files = {row.file for row in recent}
    recent_ok = sum(1 for r in decodable if r.file in recent_files)
    riot_status = render_tiles([
        ("最新一場過境", escape(latest.pass_local) if latest else "—"),
        ("TCA 仰角／方位", f"{fmt_number(latest.tca_ele)}°／{fmt_number(latest.tca_azi)}°" if latest else "—"),
        ("最新一場封包／可解碼", f"{latest.n_packets}／{latest.n_crc_ok}" if latest else "—"),
        ("最近 24 小時", f"{len(recent)} 場 · {recent_ok} 包可解碼"),
        ("下次過境預測（TLE）", next_pass_tile(predicted.get("riot") or {}, now)),
    ])
    last_cell = max((float(l.get("last_cell_connection_ms") or 0) for l in links), default=0)
    next_pass = min((float(l.get("next_pass_s") or 0) for l in online), default=0)
    fossa_status = render_tiles([
        ("最後行動網路連線", escape(_hub_time(last_cell, millis=True))),
        ("上線裝置下次過境估計", escape(_hub_next_pass(next_pass))),
        ("Hub IoT packages", f"{hub_packets:,}"),
        ("下次過境預測（TLE）", next_pass_tile(predicted.get("fossa") or {}, now)),
        ("Hub 狀態擷取", escape(hub_snapshot_time(FOSSA_SNAPSHOT))),
    ])
    body = (
        cards
        + grid2(
            hubcard("位置", "兩個網路所有裝置的最後位置（綠：鐳洋 Node，橘：FOSSA Nexus）", map_div(points, 15)),
            hubcard("裝置", "點選進入各裝置儀表板", f'<div class="devlist">{devlinks}</div>'),
        )
        + grid2(hubcard("鐳洋 Node 網路", "最近狀態", riot_status), hubcard("FOSSA Nexus 網路", "IoT Hub 狀態", fossa_status))
    )
    return view_open("overview", "TASA · LEO IOT", "總覽", [], "兩條衛星物聯網鏈路的最新數字；左側選單進入各網路與各裝置。") + body + "</section>"


def render_compare(comparison: Sequence[DailyComparison]) -> str:
    rows = [
        (
            f'<th scope="row" class="mono">{escape(item.date)}</th>',
            f'<td class="number">{item.riot_total:,}</td>',
            f'<td class="number good">{item.riot_valid:,}</td>',
            f'<td class="number">{item.fossa_total:,}</td>',
            f'<td class="number good">{item.fossa_valid:,}</td>',
        )
        for item in reversed(comparison)
    ]
    table = render_table(("日期", "鐳洋收到", "鐳洋可解碼", "FOSSA 收到", "FOSSA 暫定有效"), rows, "鐳洋與 FOSSA 每日接收對照")
    chart = (
        '<article class="panel chart"><div class="panel__body"><h3>每日接收量</h3><p class="legend">'
        '<span><i></i>鐳洋收到</span><span><i class="ok"></i>鐳洋可解碼</span>'
        '<span><i class="fossa"></i>FOSSA 收到</span><span><i class="fossa-valid"></i>FOSSA 暫定有效</span>'
        f"</p>{comparison_chart(comparison)}</div></article>"
    )
    return (
        view_open("compare", "CROSS-NETWORK", "鐳洋 vs FOSSA 對照", [],
                  "鐳洋可解碼代表 CRC-8 通過；FOSSA 暫定有效代表暫定格式可解出合理座標，兩者定義不同。")
        + chart + panel("每日對照表", "最新日期在上。", table) + "</section>"
    )


def render_downloads(summary_name: str, packets_name: str) -> str:
    body = (
        '<div class="downloads">'
        f'<div class="download"><strong><a href="{quote(summary_name)}" download>逐場次 CSV</a></strong><code>{escape(summary_name)}</code></div>'
        f'<div class="download"><strong><a href="{quote(packets_name)}" download>逐封包 CSV</a></strong><code>{escape(packets_name)}</code></div>'
        '<div class="download"><strong><a href="files.json" download>平台清單 JSON</a></strong><code>files.json</code></div>'
        '<div class="download"><strong><a href="data.json" download>儀表板 JSON</a></strong><code>data.json</code></div>'
        "</div>"
    )
    return view_open("downloads", "DATA", "下載資料", [], "產生本頁所用的原始結構化資料。") + f'<section class="panel">{body}</section></section>'


def render_nav(devices: Sequence[DeviceStats], units: Sequence[dict]) -> str:
    def item(key: str, label_html: str, sub: str = "") -> str:
        sub_html = f'<small class="mono">{escape(sub)}</small>' if sub else ""
        return f'<a data-link="{escape(key)}" href="#{escape(key)}">{label_html}{sub_html}</a>'
    riot = "".join(item(f"riot/{d.key}", escape(d.label or d.key), d.key) for d in devices if d.key != "other")
    fossa = "".join(
        item(f"fossa/{u['serial']}", f'<i class="navdot{"" if u["link"].get("active") else " navdot--off"}"></i>{escape(u["serial"])}',
             str(u["link"].get("link_mac") or (u["dev"].mac if u["dev"] else "")))
        for u in units
    )
    return (
        f'{item("overview", "總覽")}'
        f'<p class="sidebar__group">鐳洋 Node</p>{item("riot", "網路總覽")}{riot}'
        f'<p class="sidebar__group">FOSSA Nexus</p>{item("fossa", "網路總覽")}{fossa}'
        f'<p class="sidebar__group">其他</p>{item("compare", "雙軌對照")}{item("downloads", "下載")}'
    )


def render_content(
    passes: Sequence[PassRow],
    packets: Sequence[PacketRow],
    devices: Sequence[DeviceStats],
    fossa_records: Sequence[FossaRecord],
    fossa_devices: Sequence[FossaDevice],
    comparison: Sequence[DailyComparison],
    summary_name: str,
    packets_name: str,
    now: datetime,
) -> Tuple[str, str]:
    """Returns (sidebar nav html, main content html)."""
    units = fossa_units(load_hub_snapshot(FOSSA_SNAPSHOT), fossa_devices, fossa_records)
    predicted = load_predicted_passes(TLE_PASSES)
    content = (
        render_overview(passes, packets, devices, units, fossa_records, predicted, now)
        + render_riot_views(passes, packets, devices, predicted, now)
        + render_fossa_views(units, fossa_records, comparison, predicted, now)
        + render_compare(comparison)
        + render_downloads(summary_name, packets_name)
    )
    return render_nav(devices, units), content


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
    registered = load_fossa_devices(FOSSA_DEVICES)
    fossa_devices = fossa_device_statistics(fossa_records, registered)
    comparison = daily_comparison(packets, fossa_records)
    prepare_public(source_files, summary_path, packets_path)

    coverage = "尚無資料"
    if passes:
        coverage = f"{passes[-1].pass_local} – {passes[0].pass_local}"
    nav, content = render_content(
        passes,
        packets,
        devices,
        fossa_records,
        registered,
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
        nav=nav,
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
