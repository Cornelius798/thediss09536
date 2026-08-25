#!/usr/bin/env python3
"""justwoker.icu 自动签到（多账号，New API 系）"""
import os
import sys
import json
import urllib.request
import urllib.error

BASE = "https://api.justwoker.icu"
CHECKIN_PATH = "/api/user/checkin"


def call(token, path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", f"Bearer {token}")
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


def checkin_one(label, token):
    """处理单个账号，返回 True=成功/已签, False=失败"""
    print(f"\n===== 账号 [{label}] =====", flush=True)

    status, body = call(token, CHECKIN_PATH, "GET")
    data = parse(body)
    if data is None:
        print(f"[!] 查询返回非 JSON: HTTP {status} {body[:150]}", flush=True)
        return False

    d = data.get("data", {})
    stats = d.get("stats", {})

    if not d.get("enabled", True):
        print("[!] 该站签到功能未开启", flush=True)
        return False

    if stats.get("checked_in_today"):
        print(f"[+] 今日已签到 | 累计 {stats.get('checkin_count','?')} 次", flush=True)
        recs = stats.get("records", [])
        if recs:
            print(f"[+] 最近: {recs[0].get('checkin_date')} +{recs[0].get('quota_awarded')}", flush=True)
        return True

    print("[*] 今日未签，尝试 POST 签到...", flush=True)
    p_status, p_body = call(token, CHECKIN_PATH, "POST")
    print(f"[*] 签到返回 HTTP {p_status}: {p_body[:150]}", flush=True)

    status2, body2 = call(token, CHECKIN_PATH, "GET")
    data2 = parse(body2) or {}
    stats2 = data2.get("data", {}).get("stats", {})
    if stats2.get("checked_in_today"):
        recs = stats2.get("records", [])
        awarded = recs[0].get("quota_awarded") if recs else "?"
        print(f"[+] 签到成功 | 获得额度 {awarded}", flush=True)
        return True
    else:
        print("[!] POST 后仍显示未签，签到接口可能不是这个路径，需抓包确认", flush=True)
        return False


def main():
    # 收集所有账号令牌：支持 JW_TOKEN（单号）+ JW_TOKEN_1 / JW_TOKEN_2 ...
    accounts = []
    if os.environ.get("JW_TOKEN", "").strip():
        accounts.append(("主号", os.environ["JW_TOKEN"].strip()))
    for i in range(1, 11):
        v = os.environ.get(f"JW_TOKEN_{i}", "").strip()
        if v:
            accounts.append((f"号{i}", v))

    if not accounts:
        print("[-] 未配置任何令牌（JW_TOKEN 或 JW_TOKEN_1..）", flush=True)
        sys.exit(1)

    print(f"[*] 共 {len(accounts)} 个账号", flush=True)
    fail = 0
    for label, token in accounts:
        try:
            if not checkin_one(label, token):
                fail += 1
        except Exception as e:
            print(f"[!] 账号 [{label}] 异常: {e}", flush=True)
            fail += 1

    print(f"\n===== 完成：{len(accounts)-fail}/{len(accounts)} 成功 =====", flush=True)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
