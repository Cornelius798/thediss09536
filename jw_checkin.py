#!/usr/bin/env python3
"""justwoker.icu 自动签到（New API 系，GET查询 + POST签到兼容）"""
import os
import sys
import json
import urllib.request
import urllib.error

TOKEN = os.environ.get("JW_TOKEN", "").strip()
BASE = "https://api.justwoker.icu"
CHECKIN_PATH = "/api/user/checkin"


def call(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return -1, str(e)


def parse(body):
    try:
        return json.loads(body)
    except Exception:
        return None


def main():
    if not TOKEN:
        print("[-] 未配置 JW_TOKEN", flush=True)
        sys.exit(1)

    # 1) 先 GET 查状态
    status, body = call(CHECKIN_PATH, "GET")
    data = parse(body)
    if data is None:
        print(f"[!] 查询返回非 JSON: HTTP {status} {body[:200]}", flush=True)
        sys.exit(1)

    d = data.get("data", {})
    stats = d.get("stats", {})

    if not d.get("enabled", True):
        print("[!] 该站签到功能未开启", flush=True)
        return

    if stats.get("checked_in_today"):
        print(f"[+] 今日已签到 | 累计 {stats.get('checkin_count','?')} 次", flush=True)
        recs = stats.get("records", [])
        if recs:
            print(f"[+] 最近: {recs[0].get('checkin_date')} +{recs[0].get('quota_awarded')}", flush=True)
        return

    # 2) 未签 → POST 触发签到
    print("[*] 今日未签，尝试 POST 签到...", flush=True)
    p_status, p_body = call(CHECKIN_PATH, "POST")
    p_data = parse(p_body)
    print(f"[*] 签到返回 HTTP {p_status}: {p_body[:200]}", flush=True)

    # 3) 再查一次确认
    status2, body2 = call(CHECKIN_PATH, "GET")
    data2 = parse(body2) or {}
    stats2 = data2.get("data", {}).get("stats", {})
    if stats2.get("checked_in_today"):
        recs = stats2.get("records", [])
        awarded = recs[0].get("quota_awarded") if recs else "?"
        print(f"[+] 签到成功 | 获得额度 {awarded}", flush=True)
    else:
        print("[!] POST 后仍显示未签，签到接口可能不是这个路径，需抓包确认", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
