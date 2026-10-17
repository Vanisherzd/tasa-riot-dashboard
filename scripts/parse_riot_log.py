#!/usr/bin/env python3
"""解析鐳洋接收端（RFEB_SYS / RIoT-2 M9）UART log 中的 19-byte 蚵棚節點封包。

格式（big-endian）：MAC 4 | LAT 4（×1e-7 度）| LNG 4（×1e-7 度）| UnixTime 4 | 電池電壓 2（0.1 V）| CRC8 1
CRC-8：多項式 0x31、初始值 0x00、不反射、無 xorout，範圍前 18 bytes（以 2026-10-04 log 的 5 筆乾淨封包驗證）。

用法：python3 scripts/parse_riot_log.py data/rx_logs/2026-10-04_RIoT-2-M9_uart.txt [out.csv]
"""
import csv
import datetime
import re
import struct
import sys

TZ = datetime.timedelta(hours=8)


def crc8_31(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x31) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def records(text: str):
    lines = text.replace("\r", "").split("\n")
    for i, line in enumerate(lines):
        m = re.search(r"RSSI:\s*(-?[\d.]+)\s*dBm,\s*SNR:\s*(-?[\d.]+)\s*dB,\s*Size:\s*(\d+)", line)
        if not m:
            continue
        j = i + 1
        while j < len(lines) and "Payload" not in lines[j]:
            j += 1
        hexs = re.findall(r"[0-9A-Fa-f]{2}", lines[j].split(":", 1)[1]) if j < len(lines) else []
        yield {"line": i + 1, "rssi": float(m.group(1)), "snr": float(m.group(2)),
               "size": int(m.group(3)), "bytes": bytes(int(h, 16) for h in hexs)}


def decode(b: bytes) -> dict:
    lat, lng, ts = struct.unpack(">iii", b[4:16])
    bat = struct.unpack(">H", b[16:18])[0]
    ok = crc8_31(b[:18]) == b[18]
    plausible = 1_600_000_000 < ts < 1_900_000_000
    local = (datetime.datetime.utcfromtimestamp(ts) + TZ).strftime("%Y-%m-%d %H:%M:%S") if plausible else ""
    return {"mac": b[:4].hex(), "lat": lat / 1e7, "lng": lng / 1e7, "unix": ts, "local": local,
            "bat_v": bat / 10, "crc_ok": ok}


def main(path: str, out: str | None = None) -> None:
    text = open(path, "rb").read().decode("latin-1")
    rows = []
    for r in records(text):
        b = r["bytes"]
        if r["size"] < 19 or len(b) < 19 or b[0] != 0xCC:
            continue
        d = decode(b[:19])  # 19-byte prefix; longer packets (20/45 B) keep their tail in hex
        d.update(size=r["size"], line=r["line"], rssi=r["rssi"], snr=r["snr"], hex=b.hex(" "))
        rows.append(d)
    ok = sum(d["crc_ok"] for d in rows)
    print(f"CC packets (>=19 B): {len(rows)}, CRC ok: {ok}, bit errors: {len(rows) - ok}")
    for d in rows:
        print(f"L{d['line']:4d} {d['size']:2d}B {d['mac']} {d['local'] or 'time?':19s} lat={d['lat']:.6f} lng={d['lng']:.6f} "
              f"bat={d['bat_v']:6.1f}V rssi={d['rssi']:6.1f} snr={d['snr']:6.1f} {'OK' if d['crc_ok'] else 'ERR'}")
    if out:
        cols = ["mac", "lat", "lng", "unix", "local", "bat_v", "crc_ok", "size", "line", "rssi", "snr", "hex"]
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print("csv ->", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
