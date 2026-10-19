#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ─── How to run ───
# python3 scripts/parse_fossa.py
# python3 scripts/parse_fossa.py selftest
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Final, List, Optional, Tuple

# 硬解碼規則（廠商正式 frame 格式未到前的暫定做法）：
#  1. Base64 還原 bytes；去掉尾端補齊（0x00、0xA5 為 Hub Kaitai 範例 pad、\r\n 為 UART 結尾）。
#  2. 可解成 UTF-8 可印字串 → 文字（UART／Wi-Fi 餵入的 16／32 bytes ASCII 就是這種）。
#  3. 否則前 12 bytes 試 float32 lat/lng/alt（Hub Kaitai 範例：endian le，先 LE 再 BE）。
#  4. 都不是 → 二進位，只顯示 hex。
# ponytail: 廠商 frame 格式到了，只改 GPS_LAYOUTS 與 GPS_RANGE。
GPS_LAYOUTS: Final = (("<fff", "LE"), (">fff", "BE"))
GPS_RANGE: Final = ((-90.0, 90.0), (-180.0, 180.0), (-500.0, 20000.0))
PAD_BYTES: Final = b"\x00\xa5\r\n"
TAIPEI: Final = timezone(timedelta(hours=8))
DEFAULT_INPUT: Final = Path("data/fossa/messages.jsonl")


@dataclass(frozen=True)
class FossaParseError(Exception):
    path: Path
    line_number: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: {self.message}"


@dataclass(frozen=True)
class FossaRecord:
    key: str
    received_unix: Optional[int]
    received_at_local: Optional[str]
    mac: str
    node_id: str
    source: str
    queue_unix: Optional[int]
    sat_unix: Optional[int]
    queue_local: Optional[str]
    sat_local: Optional[str]
    latency_s: Optional[int]
    rssi: Optional[float]
    payload_hex: str
    payload_len: int
    decode_status: str
    lat: Optional[float]
    lng: Optional[float]
    alt: Optional[float] = None
    message_text: str = ""
    payload_text: str = ""
    payload_encoding: str = ""
    metadata_json: str = ""


def optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def optional_float(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def local_time(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def received_time(value: object) -> Tuple[Optional[int], Optional[str]]:
    timestamp = optional_int(value)
    if timestamp is not None:
        return timestamp, local_time(timestamp)
    if not isinstance(value, str):
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    unix = int(parsed.timestamp())
    return unix, parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def decode_gps(payload: bytes) -> Optional[Tuple[float, float, float, str]]:
    """前 12 bytes 試 float32 lat/lng/alt；值在合理範圍且非全零才算 GPS。"""
    if len(payload) < 12:
        return None
    for value_format, endian in GPS_LAYOUTS:
        values = struct.unpack_from(value_format, payload, 0)
        ok = all(math.isfinite(v) and lo <= v <= hi for v, (lo, hi) in zip(values, GPS_RANGE))
        if ok and not (values[0] == 0.0 and values[1] == 0.0):
            return float(values[0]), float(values[1]), float(values[2]), endian
    return None


def decode_payload(payload: bytes) -> Tuple[str, str, str, Optional[float], Optional[float], Optional[float]]:
    """回傳 (decode_status, 解碼方式標籤, 解碼文字, lat, lng, alt)。"""
    if not payload:
        return "none", "無 payload", "", None, None, None
    body = payload.rstrip(PAD_BYTES)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if text and all(ch.isprintable() or ch in "\t\r\n" for ch in text):
        return "text", f"Base64→文字（{len(payload)} bytes，UTF-8／ASCII）", text, None, None, None
    gps = decode_gps(payload)
    if gps:
        lat, lng, alt, endian = gps
        tail = payload[12:]
        extra = f"；其餘 {len(tail)} bytes {tail.hex()}" if tail else ""
        return ("gps", f"Base64→GPS float32×3 {endian}（lat/lng/alt，暫定欄位）",
                f"lat {lat:.6f}, lng {lng:.6f}, alt {alt:.1f} m{extra}", lat, lng, alt)
    return "binary", f"Base64→二進位（{len(payload)} bytes，無法辨識，見 hex）", "", None, None, None


def decode_message(value: object) -> bytes:
    if not isinstance(value, str):
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return b""


def parse_item(item: object) -> Optional[FossaRecord]:
    if not isinstance(item, dict):
        return None
    nested = item.get("body")
    body = nested if isinstance(nested, dict) else item
    message = body.get("message")
    if message is None:
        message = body.get("payload")
    message_text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False) if message is not None else ""
    payload = decode_message(message)
    raw_bytes = payload or message_text.encode("utf-8")
    status, encoding, decoded_text, latitude, longitude, altitude = decode_payload(payload)
    if not payload and message_text:
        status, encoding, decoded_text = "text", "原文 UTF-8（非 Base64，傳輸編碼未確認）", message_text
    metadata = {key: value for key, value in item.items() if key != "body"}
    metadata_body = {key: value for key, value in body.items() if key not in {"message", "payload", "hub_raw"}}
    hub_raw = body.get("hub_raw")
    if isinstance(hub_raw, dict):
        metadata_body["hub_raw"] = {key: value for key, value in hub_raw.items() if key not in {"message", "payload"}}
    if isinstance(nested, dict):
        metadata["body"] = metadata_body
    else:
        metadata = metadata_body
    queue_unix = optional_int(body.get("queue_timestamp"))
    sat_unix = optional_int(body.get("sat_timestamp"))
    received_unix, received_local = received_time(item.get("received_at"))
    latency = sat_unix - queue_unix if sat_unix is not None and queue_unix is not None else None
    return FossaRecord(
        key=str(item.get("key", "")),
        received_unix=received_unix,
        received_at_local=received_local,
        mac=str(body.get("mac", "")),
        node_id=str(body.get("nodeId", "")),
        source=str(body.get("source", "raw" if body.get("raw") is True else "")),
        queue_unix=queue_unix,
        sat_unix=sat_unix,
        queue_local=local_time(queue_unix),
        sat_local=local_time(sat_unix),
        latency_s=latency,
        rssi=optional_float(body.get("rssi")),
        payload_hex=raw_bytes.hex(),
        payload_len=len(raw_bytes),
        decode_status=status,
        lat=latitude,
        lng=longitude,
        alt=altitude,
        message_text=message_text,
        payload_text=decoded_text,
        payload_encoding=encoding,
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
    )


def load_records(path: Path = DEFAULT_INPUT) -> List[FossaRecord]:
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise FossaParseError(path, line_number, f"無效 JSON: {error}") from error
        record = parse_item(item)
        if record is None:
            raise FossaParseError(path, line_number, "每行必須是 JSON object")
        records.append(record)
    return records


def record_as_dict(record: FossaRecord) -> Dict[str, object]:
    return {
        "mac": record.mac,
        "nodeId": record.node_id,
        "source": record.source,
        "queue_local": record.queue_local,
        "sat_local": record.sat_local,
        "latency_s": record.latency_s,
        "rssi": record.rssi,
        "payload_hex": record.payload_hex,
        "payload_len": record.payload_len,
        "decode_status": record.decode_status,
        "lat": record.lat,
        "lng": record.lng,
        "alt": record.alt,
        "received_at_local": record.received_at_local,
        "key": record.key,
        "message_text": record.message_text,
        "payload_text": record.payload_text,
        "payload_encoding": record.payload_encoding,
        "metadata_json": record.metadata_json,
    }


def selftest() -> None:
    item = {
        "mac": "01504B004800",
        "queue_timestamp": 1760091985,
        "sat_timestamp": 1760092185,
        "rssi": -87,
        "message": "aGVsbG8gd29ybGQ=",
        "nodeId": "67dd4ee23f647d4b31e0f8c8",
        "source": "satellite",
    }
    record = parse_item(item)
    assert record is not None
    assert bytes.fromhex(record.payload_hex) == b"hello world"
    assert record.payload_len == 11
    assert record.latency_s == 200
    assert record.decode_status == "text" and record.payload_text == "hello world"
    assert record.lat is None and record.lng is None
    # 10/19 Hub 實測封包：UART／Wi-Fi 餵入的 16 bytes ASCII
    test = parse_item({**item, "message": "Rk9TU0ExMDE5VEVTVDAwMQ=="})
    assert test.decode_status == "text" and test.payload_text == "FOSSA1019TEST001" and test.payload_len == 16
    # Hub Kaitai 範例格式：float32 LE lat/lng/alt + 其餘欄位
    frame = struct.pack("<fff", 24.79385, 120.99454, 60.0) + b"\x19\x32\x00\x00"
    gps = parse_item({**item, "message": base64.b64encode(frame).decode()})
    assert gps.decode_status == "gps" and abs(gps.lat - 24.79385) < 1e-4 and abs(gps.lng - 120.99454) < 1e-4 and gps.alt == 60.0
    assert "其餘 4 bytes 19320000" in gps.payload_text
    # 全零／亂數 → 二進位，只給 hex
    zero = parse_item({**item, "message": base64.b64encode(b"\x00" * 16).decode()})
    assert zero.decode_status == "binary" and zero.payload_text == "" and zero.lat is None
    noise = parse_item({**item, "message": base64.b64encode(bytes(range(200, 216))).decode()})
    assert noise.decode_status == "binary" and noise.lat is None
    assert parse_item({**item, "message": None}).decode_status == "none"
    print("parse_fossa selftest: OK (text / gps float32 / binary / none)")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析 FOSSA webhook JSONL")
    parser.add_argument("command", nargs="?", choices=("selftest",))
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "selftest":
        selftest()
        return
    rendered = json.dumps(
        [record_as_dict(record) for record in load_records(arguments.input)], ensure_ascii=False, indent=2
    ) + "\n"
    if arguments.output:
        arguments.output.write_text(rendered, encoding="utf-8")
        print(f"FOSSA parsed JSON -> {arguments.output}")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
