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

# FOSSA 尚未提供正式 payload schema；收到後只需修改這張表。
# 欄位名稱、byte offset、struct 格式、合理值下限、合理值上限。
PROVISIONAL_LAYOUT: Final = (
    ("lat", 0, "<f", -90.0, 90.0),
    ("lng", 4, "<f", -180.0, 180.0),
)
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


def decode_coordinates(payload: bytes) -> Tuple[Optional[float], Optional[float]]:
    decoded = {}
    for name, offset, value_format, minimum, maximum in PROVISIONAL_LAYOUT:
        if len(payload) < offset + struct.calcsize(value_format):
            return None, None
        value = struct.unpack_from(value_format, payload, offset)[0]
        if not math.isfinite(value) or not minimum <= value <= maximum:
            return None, None
        decoded[name] = float(value)
    return decoded["lat"], decoded["lng"]


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
    payload = decode_message(body.get("message"))
    latitude, longitude = decode_coordinates(payload)
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
        payload_hex=payload.hex(),
        payload_len=len(payload),
        decode_status="provisional",
        lat=latitude,
        lng=longitude,
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
        "received_at_local": record.received_at_local,
        "key": record.key,
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
    assert record.lat is None and record.lng is None
    assert record.decode_status == "provisional"
    print("parse_fossa selftest: OK (hello world is not a valid provisional coordinate payload)")


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
