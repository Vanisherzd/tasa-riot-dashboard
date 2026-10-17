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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from string import Template
from typing import Final, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

ROOT: Final = Path(__file__).resolve().parent.parent
WEB_DIR: Final = ROOT / "web"
PUBLIC_DIR: Final = WEB_DIR / "public"
DATA_DIR: Final = ROOT / "data" / "rx_logs"
PASS_DIR: Final = DATA_DIR / "m9_passes"
FILES_JSON: Final = PASS_DIR / "files.json"
TAIPEI: Final = timezone(timedelta(hours=8))
KNOWN_DEVICES: Final = (
    ("cc000001", "蚵棚"),
    ("cc000003", "登山改裝"),
    ("cc000005", "蚵棚"),
    ("cc000006", "本組第一塊 UT"),
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
    groups.append(("other", "其他（疑似位元錯誤）", [row for row in packets if row.mac not in known_macs]))
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


def render_table(headers: Sequence[str], rows: Iterable[Sequence[str]], caption: str) -> str:
    body = "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="empty">目前沒有資料；請執行 <code>sh web/update.sh</code>。</td></tr>'
    head = "".join(f'<th scope="col">{escape(header)}</th>' for header in headers)
    return (
        f'<div class="table-wrap" tabindex="0" role="region" aria-label="{escape(caption)}">'
        f'<table><caption>{escape(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def grouped_bar_svg(
    chart_id: str,
    title: str,
    description: str,
    labels: Sequence[str],
    totals: Sequence[int],
    correct: Sequence[int],
    annotations: Optional[Sequence[str]] = None,
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
    for step in range(5):
        value = peak * step / 4
        y = top + plot_height - plot_height * step / 4
        parts.append(f'<line class="chart-grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="chart-axis" x="{left-8}" y="{y+4:.1f}" text-anchor="end">{value:.0f}</text>')
    label_every = max(1, math.ceil(len(labels) / 10))
    for index, (label, total, ok) in enumerate(zip(labels, totals, correct)):
        center = left + slot * (index + 0.5)
        total_height = plot_height * total / peak
        ok_height = plot_height * ok / peak
        base_y = top + plot_height
        parts.append(
            f'<rect class="bar-total" x="{center-bar_width-1:.1f}" y="{base_y-total_height:.1f}" '
            f'width="{bar_width:.1f}" height="{total_height:.1f}"><title>{escape(label)}：封包 {total}</title></rect>'
        )
        parts.append(
            f'<rect class="bar-ok" x="{center+1:.1f}" y="{base_y-ok_height:.1f}" '
            f'width="{bar_width:.1f}" height="{ok_height:.1f}"><title>{escape(label)}：CRC 正確 {ok}</title></rect>'
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


def fmt_number(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_content(
    passes: Sequence[PassRow],
    packets: Sequence[PacketRow],
    devices: Sequence[DeviceStats],
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
        '<section class="panel"><div class="panel__head"><h2>下載資料</h2><p>提供產生本頁所用的原始結構化資料。</p></div>'
        '<div class="downloads">'
        f'<div class="download"><strong><a href="{quote(summary_name)}" download>逐場次 CSV</a></strong><code>{escape(summary_name)}</code></div>'
        f'<div class="download"><strong><a href="{quote(packets_name)}" download>逐封包 CSV</a></strong><code>{escape(packets_name)}</code></div>'
        '<div class="download"><strong><a href="files.json" download>平台清單 JSON</a></strong><code>files.json</code></div>'
        '<div class="download"><strong><a href="data.json" download>儀表板 JSON</a></strong><code>data.json</code></div>'
        "</div></section>"
    )
    return metrics + latest_panel + passes_panel + devices_panel + charts + packets_panel + downloads


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
    prepare_public(source_files, summary_path, packets_path)

    coverage = "尚無資料"
    if passes:
        coverage = f"{passes[-1].pass_local} – {passes[0].pass_local}"
    content = render_content(passes, packets, devices, summary_path.name, packets_path.name, now)
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
    }
    write_text_atomic(PUBLIC_DIR / "data.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    write_text_atomic(PUBLIC_DIR / "index.html", html)
    print(
        f"dashboard -> {PUBLIC_DIR / 'index.html'} "
        f"({len(passes)} passes, {len(packets)} packets, {sum(row.crc_ok for row in packets)} CRC ok)"
    )


if __name__ == "__main__":
    main()
