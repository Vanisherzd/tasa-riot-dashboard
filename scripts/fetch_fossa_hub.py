#!/usr/bin/env python3
"""Pull FOSSA IoT Hub device status and IoT packets with the account sign-in token.

The Hub web app talks to three hosts; this script uses the same calls:
  POST https://auth.fossasystems.com/sign-in            -> accessToken
  GET  https://api-hub.fossasystems.com/applications/summary
  GET  https://api-hub.fossasystems.com/links/<id>
  GET  https://api-hub.fossasystems.com/links/<id>/packets?page=N&page_size=100
  GET  https://api-cellui.fossasystems.com/nexus/mac/<nexusMac>

Credentials come only from the environment (FOSSA_HUB_EMAIL / FOSSA_HUB_PASSWORD);
nothing secret is written to disk. Outputs:
  data/fossa/hub_snapshot.json   device status (no tokens)
  data/fossa/messages.jsonl      IoT packets appended in the webhook-like schema
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional

AUTH_URL = "https://auth.fossasystems.com/sign-in"
HUB_API = "https://api-hub.fossasystems.com"
CELL_API = "https://api-cellui.fossasystems.com"
DEFAULT_SNAPSHOT = Path("data/fossa/hub_snapshot.json")
DEFAULT_MESSAGES = Path("data/fossa/messages.jsonl")
TIMEOUT = 30


class HubError(RuntimeError):
    pass


def request_json(url: str, token: Optional[str] = None, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "tasa-fetch-fossa-hub/1"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise HubError(f"{url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HubError(f"{url}: {exc.reason}") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubError(f"{url}: not JSON") from exc
    if not isinstance(parsed, dict):
        raise HubError(f"{url}: unexpected body")
    return parsed


def sign_in(email: str, password: str) -> str:
    body = request_json(AUTH_URL, payload={"email": email, "password": password})
    token = (body.get("data") or {}).get("accessToken")
    if not isinstance(token, str) or len(token) < 20:
        raise HubError("sign-in did not return accessToken")
    return token


def list_links(token: str) -> List[dict]:
    body = request_json(f"{HUB_API}/applications/summary?device_limit=50&search_term=", token)
    links: List[dict] = []
    for app in body.get("data") or []:
        for dev in app.get("devices") or []:
            links.append({"application": (app.get("application") or {}).get("name"), **dev})
    return links


def fetch_packets(token: str, link_id: str) -> List[dict]:
    items: List[dict] = []
    page = 1
    while True:
        body = request_json(f"{HUB_API}/links/{link_id}/packets?page={page}&page_size=100", token)
        data = body.get("data") or []
        items.extend(item for item in data if isinstance(item, dict))
        pager = body.get("paginator") or {}
        if not data or page >= int(pager.get("total_pages") or 0):
            return items
        page += 1


def snapshot_link(token: str, dev: dict) -> dict:
    detail = (request_json(f"{HUB_API}/links/{dev['id']}", token).get("data") or {})
    nexus: dict = {}
    if dev.get("nexusMacAddress"):
        try:
            nexus = request_json(f"{CELL_API}/nexus/mac/{dev['nexusMacAddress']}", token).get("data") or {}
        except HubError as exc:
            print(f"warn: {exc}", file=sys.stderr)
    status = detail.get("status") or {}
    stats = detail.get("statistics") or {}
    nstatus = nexus.get("status") or {}
    nconfig = nexus.get("config") or {}
    # ponytail: flat record with just the fields the dashboard shows; raw JSON stays out of git.
    return {
        "link_id": detail.get("id") or dev.get("id"),
        "serial": detail.get("serialNumber"),
        "link_mac": detail.get("mac") or dev.get("mac"),
        "nexus_mac": detail.get("nexusMacAddress") or dev.get("nexusMacAddress"),
        "name": (detail.get("dirtyConfig") or {}).get("linkName") or (detail.get("config") or {}).get("linkName"),
        "active": bool(nexus.get("active")),
        "last_cell_connection_ms": detail.get("lastCellConnection"),
        "status_time_s": status.get("currentTime"),
        "elapsed_s": status.get("elapsedTime"),
        "position": status.get("position"),
        "next_pass_s": status.get("nextPassTime"),
        "last_sat_packet_s": status.get("lastSatPacket"),
        "rx_packet_count": status.get("rxPktCount"),
        "tx_queue_len": status.get("txQueueLen"),
        "link_resets": status.get("resetCounter"),
        "link_fw": status.get("version"),
        "total_packets": stats.get("totalPackets"),
        "last_packet_ms": stats.get("lastPacketTime"),
        "total_commands": stats.get("totalSentCmds"),
        "temperature_c": nstatus.get("temperature"),
        "humidity_pct": nstatus.get("humidity"),
        "nexus_resets": nstatus.get("resetCounter"),
        "nexus_fw": nstatus.get("version"),
        "power": nstatus.get("power"),
        "cell_period_s": nconfig.get("cellPeriod"),
        "force_only_sat": nconfig.get("forceOnlySat"),
        "integrations": (detail.get("application") or {}).get("integrations"),
    }


def packet_to_record(link: dict, packet: dict) -> dict:
    """Webhook-like record so parse_fossa.py can read it; unknown fields are kept raw."""
    body = {
        "mac": packet.get("mac") or link.get("link_mac") or "",
        "queue_timestamp": packet.get("queue_timestamp") or packet.get("queueTime"),
        "sat_timestamp": packet.get("sat_timestamp") or packet.get("satRxTime"),
        "rssi": packet.get("rssi"),
        "message": packet.get("message") or packet.get("payload"),
        "nodeId": packet.get("nodeId") or link.get("link_id"),
        "source": packet.get("source") or "satellite",
        "hub_raw": packet,
    }
    key = f"hub-{link.get('link_id')}-{packet.get('id') or packet.get('_id') or json.dumps(packet, sort_keys=True)[:64]}"
    return {"key": key, "received_at": None, "content_type": "application/json", "body": body}


def append_unique(path: Path, records: Iterable[dict]) -> int:
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line).get("key"))
            except (json.JSONDecodeError, AttributeError):
                continue
    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for rec in records:
            if rec["key"] in seen:
                continue
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            seen.add(rec["key"])
            added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="從 FOSSA IoT Hub 拉取裝置狀態與 IoT 封包")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--messages", type=Path, default=DEFAULT_MESSAGES)
    parser.add_argument("--no-packets", action="store_true", help="只更新狀態快照")
    args = parser.parse_args()
    email = os.environ.get("FOSSA_HUB_EMAIL", "")
    password = os.environ.get("FOSSA_HUB_PASSWORD", "")
    if not email or not password:
        print("FOSSA_HUB_EMAIL / FOSSA_HUB_PASSWORD 未設定，略過", file=sys.stderr)
        return
    token = sign_in(email, password)
    links = list_links(token)
    snapshot: List[dict] = []
    records: List[dict] = []
    for dev in links:
        snap = snapshot_link(token, dev)
        snapshot.append(snap)
        if not args.no_packets:
            records.extend(packet_to_record(snap, pkt) for pkt in fetch_packets(token, dev["id"]))
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.snapshot.write_text(json.dumps({"fetched_at": __import__("time").time(), "links": snapshot}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    added = append_unique(args.messages, records) if records else 0
    print(f"hub snapshot: {len(snapshot)} links -> {args.snapshot}; packets fetched {len(records)}, new {added}")


if __name__ == "__main__":
    try:
        main()
    except HubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
