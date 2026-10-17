#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ─── How to run ───
# FOSSA_EXPORT_URL='https://.../export/<TOKEN>' python3 scripts/fetch_fossa.py
# python3 scripts/fetch_fossa.py --import sample.json
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_OUTPUT = Path("data/fossa/messages.jsonl")


@dataclass(frozen=True)
class FossaFetchError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def read_json_response(url: str, headers: Optional[Dict[str, str]] = None) -> object:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "tasa-dashboard/1", **(headers or {})})
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise FossaFetchError(f"FOSSA HTTP {error.code}: {url}") from error
    except URLError as error:
        raise FossaFetchError(f"無法連線 FOSSA: {error.reason}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FossaFetchError(f"FOSSA 回應不是有效 JSON: {error}") from error


def item_identity(item: Dict[str, object]) -> str:
    key = item.get("key")
    if isinstance(key, str) and key:
        return f"key:{key}"
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def load_jsonl(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    items = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise FossaFetchError(f"{path}:{line_number} 不是有效 JSON: {error}") from error
        if not isinstance(item, dict):
            raise FossaFetchError(f"{path}:{line_number} 必須是 JSON object")
        items.append(item)
    return items


def append_unique(path: Path, candidates: Iterable[Dict[str, object]]) -> int:
    existing = load_jsonl(path)
    seen = {item_identity(item) for item in existing}
    additions = []
    for item in candidates:
        identity = item_identity(item)
        if identity not in seen:
            seen.add(identity)
            additions.append(item)
    if not additions:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in additions:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(additions)


def import_items(path: Path) -> List[Dict[str, object]]:
    if path.suffix.lower() == ".jsonl":
        return load_jsonl(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FossaFetchError(f"無法讀取 {path}: {error}") from error
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, dict) and isinstance(value.get("items"), list):
        candidates = value["items"]
    else:
        candidates = [value]
    if not all(isinstance(item, dict) for item in candidates):
        raise FossaFetchError(f"{path} 必須是 object、object 陣列，或含 items 陣列的 export")
    return candidates


def newest_timestamp(items: Iterable[Dict[str, object]]) -> int:
    newest = 0
    for item in items:
        key = item.get("key")
        if isinstance(key, str):
            prefix = key.split("-", 1)[0]
            if prefix.isdigit():
                newest = max(newest, int(prefix))
    return newest


def export_url(base_url: str, since: int, cursor: str) -> str:
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in {"since", "cursor"}]
    query.extend((("since", str(since)), ("cursor", cursor)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_export(base_url: str, since: int) -> List[Dict[str, object]]:
    items = []
    cursor = ""
    seen_cursors = set()
    while True:
        value = read_json_response(export_url(base_url, since, cursor))
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise FossaFetchError("FOSSA export 回應缺少 items 陣列")
        page_items = value["items"]
        if not all(isinstance(item, dict) for item in page_items):
            raise FossaFetchError("FOSSA export items 必須全部是 object")
        items.extend(page_items)
        next_cursor = value.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            return items
        if next_cursor in seen_cursors:
            raise FossaFetchError("FOSSA export cursor 重複，停止以避免無限迴圈")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def fetch_rest() -> List[Dict[str, object]]:
    base = os.environ.get("FOSSA_HUB_API_BASE", "").strip()
    api_key = os.environ.get("FOSSA_HUB_API_KEY", "").strip()
    endpoint = os.environ.get("FOSSA_HUB_ENDPOINT", "").strip()
    if not endpoint:
        print("待 FOSSA 提供 REST 端點")
        return []
    if not base or not api_key:
        raise FossaFetchError("--rest 需要 FOSSA_HUB_API_BASE 與 FOSSA_HUB_API_KEY")
    url = base.rstrip("/") + "/" + endpoint.lstrip("/")
    value = read_json_response(url, {"X-API-Key": api_key})
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        candidates = value["items"]
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]
    if not all(isinstance(item, dict) for item in candidates):
        raise FossaFetchError("FOSSA Hub REST 回應不是可匯入的 object/list")
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="匯出或匯入 FOSSA Nexus 訊息，去重後附加至 JSONL")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--import", dest="import_path", type=Path, help="手動匯入 .json 或 .jsonl")
    modes.add_argument("--rest", action="store_true", help="使用尚待確認的 FOSSA Hub REST API")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    if arguments.import_path:
        candidates = import_items(arguments.import_path)
    elif arguments.rest:
        candidates = fetch_rest()
    else:
        url = os.environ.get("FOSSA_EXPORT_URL", "").strip()
        if not url:
            print("FOSSA_EXPORT_URL 未設定；略過 webhook export")
            return
        candidates = fetch_export(url, newest_timestamp(load_jsonl(arguments.out)))
    added = append_unique(arguments.out, candidates)
    print(f"FOSSA: received {len(candidates)}, appended {added} -> {arguments.out}")


if __name__ == "__main__":
    main()
