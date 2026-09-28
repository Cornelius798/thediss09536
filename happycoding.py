#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HappyCoding (New API) 每日自动签到 —— GitHub Actions 版（放在 thediss09536 仓库里跑）。

token 来源优先级：环境变量 HAPPYCODING_TOKEN（GitHub secret）> 同目录 happycoding_checkin.json。
端点（New API 标准路由，已实测）：
    GET  /api/user/checkin   查状态
    POST /api/user/checkin   签到
站点未开 Turnstile，纯 HTTP 即可。
幂等：今天已签则跳过（不发 POST）。

结果推送（可选）：设了 TG_BOT_TOKEN + TG_CHAT_ID 就把结果推到自己的 tgbot；
缺任意一个或推送失败都静默跳过，不影响签到本身。
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

BASE = "https://happycoding.xyz"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "happycoding_checkin.json")
CST = timezone(timedelta(hours=8))
TIMEOUT = 20

# ===== 结果推送配置 =====
TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()


def notify(text):
    """把签到结果推给自己的 tgbot。拿不到 token/chat 或失败都静默跳过。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % TG_BOT_TOKEN
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "ignore")
            if not json.loads(body).get("ok", False):
                print("推送返回非 ok: " + body[:200])
    except Exception as e:
        print("推送失败（不影响签到）: %s" % e)


def token():
    t = (os.environ.get("HAPPYCODING_TOKEN") or "").strip()
    if t:
        return t
    try:
        return (json.load(open(CFG, encoding="utf-8")).get("access_token") or "").strip()
    except Exception:
        return ""


def api(path, tok, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; happycoding-checkin/1.0)")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def main():
    tok = token()
    if not tok:
        print("ERR: 没有 token（请在 repo 里配置 secret HAPPYCODING_TOKEN）")
        notify("❌ HappyCoding 签到失败：没有 token（缺 secret HAPPYCODING_TOKEN）")
        return 2
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    code, data = api("/api/user/checkin", tok, "GET")
    if code == 401:
        print(f"[{ts}] ERR: access token 无效/过期")
        notify(f"❌ HappyCoding 签到失败：access token 无效/过期\n[{ts}]")
        return 3
    if code != 200:
        print(f"[{ts}] ERR: 查状态 http={code} {str(data)[:200]}")
        notify(f"❌ HappyCoding 签到失败：查状态 http={code}\n{str(data)[:200]}\n[{ts}]")
        return 4

    d = data.get("data") if isinstance(data, dict) else None
    already = bool(d.get("checked_in_today")) if isinstance(d, dict) else False
    print(f"[{ts}] 状态: checked_in_today={already} {json.dumps(d, ensure_ascii=False)[:200] if d else ''}")
    if already:
        print(f"[{ts}] 今天已签到，跳过")
        notify(f"✅ HappyCoding 今日已签到（跳过）\n[{ts}]")
        return 0

    code, data = api("/api/user/checkin", tok, "POST")
    ok = isinstance(data, dict) and (data.get("success") is True or data.get("code") in (0, "0"))
    detail = json.dumps(data, ensure_ascii=False)[:300] if not isinstance(data, str) else data[:300]
    if code == 200 and ok:
        print(f"[{ts}] ✅ 签到成功 http=200 {detail}")
        notify(f"✅ HappyCoding 签到成功\n{detail}\n[{ts}]")
        return 0
    print(f"[{ts}] ❌ 签到失败 http={code} {detail}")
    notify(f"❌ HappyCoding 签到失败 http={code}\n{detail}\n[{ts}]")
    return 5


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"fatal: {e}")
        notify(f"❌ HappyCoding 签到脚本异常：{e}")
        sys.exit(1)
