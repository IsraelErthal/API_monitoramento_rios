#!/usr/bin/env python3
"""Simula o ESP32 receptor enviando leituras ao app (sem precisar das placas).

  python simulator/send.py --url http://127.0.0.1:8000/api/v1/readings --token SEU_INGEST_TOKEN --event --errors
"""
import argparse
import json
import math
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def post(url, token, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def level(t_min, event):
    n = 1.5 + 0.08 * math.sin(t_min / 180) + random.gauss(0, 0.01)
    if event:  # cheia suave: sobe e desce (taxa máxima ~0,5 m/h)
        n += 1.2 * math.exp(-(((t_min - 400) / 120) ** 2))
    return round(n, 3)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--station", default="rio-piloto-01")
    p.add_argument("--n", type=int, default=60, help="quantidade de leituras")
    p.add_argument("--step-min", type=int, default=10, help="minutos simulados entre leituras")
    p.add_argument("--delay", type=float, default=0.1, help="segundos reais entre envios")
    p.add_argument("--event", action="store_true", help="inclui uma cheia suave")
    p.add_argument("--errors", action="store_true", help="injeta níveis impossíveis e reenvios duplicados")
    a = p.parse_args()

    start = datetime.now(timezone.utc) - timedelta(minutes=a.step_min * a.n)
    seq = random.randint(0, 1000)
    for i in range(a.n):
        value = level(i * a.step_min, a.event)
        if a.errors and random.random() < 0.05:
            value = 99.0                                  # fora da faixa -> deve dar 422
        payload = {
            "station_id": a.station,
            "timestamp": (start + timedelta(minutes=i * a.step_min)).isoformat().replace("+00:00", "Z"),
            "sequence": seq % 65536,
            "water_level_m": value,
        }
        seq += 1
        status, resp = post(a.url, a.token, payload)
        print(f"[{i + 1:03d}] {payload['timestamp']} {value:6.3f} m -> {status} {resp[:80]}")
        if a.errors and random.random() < 0.05:
            status, resp = post(a.url, a.token, payload)
            print(f"      reenvio duplicado -> {status} {resp[:80]}")
        time.sleep(a.delay)


if __name__ == "__main__":
    main()
