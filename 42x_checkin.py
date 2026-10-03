#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""42x.shop（New API v1.0.0-rc.15）每日自动签到 —— GitHub Actions 版。

站点事实（2026-10-03 实测）
--------------------------
- `GET /api/status` → `checkin_enabled=true`、`turnstile_check=false`、`system_name="42 API"`、
  `version="v1.0.0-rc.15"`（响应头 `x-new-api-version` 一致）。
- New API 标准端点：
      GET  /api/user/checkin   查签到状态
      POST /api/user/checkin   执行签到
- 站点**没开 Turnstile 人机验证**，纯 HTTP 即可，不需要浏览器自动化。
- ★ 无效 token 的行为是 **HTTP 200 + `{"success":false,"message":"Unauthorized, invalid access
  token"}`**，**不是 401** —— 必须按 body 判，否则会把"token 失效"误报成"签到失败"。

token 来源优先级
----------------
环境变量 `X42_TOKEN`（GitHub repo secret）> 同目录 `42x_checkin.json`。
secret 在 repo → Settings → Secrets and variables → Actions → New repository secret 里建。

幂等
----
先 GET 状态，今天已签就直接退出（不发 POST），避免重复签到被站点判异常。

结果推送（可选）
----------------
设了 `TG_BOT_TOKEN` + `TG_CHAT_ID` 就把结果推到自己的 tgbot；缺任意一个或推送失败都静默
跳过，不影响签到本身。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.42x.shop"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "42x_checkin.json")
CST = timezone(timedelta(hours=8))
TIMEOUT = 20
# New API 默认 1 美元 = 500000 quota（本站 /api/status 实测 quota_per_unit=500000）
QUOTA_PER_UNIT = 500000

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
    t = (os.environ.get("X42_TOKEN") or "").strip()
    if t:
        return t
    try:
        return (json.load(open(CFG, encoding="utf-8")).get("access_token") or "").strip()
    except Exception:
        return ""


def api(path, tok, method="GET"):
    """调 New API 端点。返回 (http_status, 解析后的 json 或原文)。http=0 表示网络层失败。"""
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; 42x-checkin/1.0)")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(raw)
            except Exception:
                # Cloudflare 拦截/HTML 挑战页会走到这里 —— 原样带出去，日志里能看出是被墙了
                return r.status, raw[:300]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:300]
    except Exception as e:
        return 0, "network error: %s" % e


def is_auth_error(code, data):
    """42x.shop 实测：无效 token **不是 401**，而是 200 + success=false +
    message='Unauthorized, invalid access token'（rc.15 行为）。这里统一识别。"""
    if code == 401:
        return True
    if isinstance(data, dict):
        msg = str(data.get("message") or "").lower()
        if data.get("success") is False and (
            "unauthorized" in msg or "invalid access token" in msg or "not logged in" in msg
        ):
            return True
    return False


def extract_checked_in_today(data):
    """宽容解析 GET 返回：不同 rc 版本字段位置略有差异。找不到返回 None（未知）。"""
    if not isinstance(data, dict):
        return None
    d = data.get("data")
    if isinstance(d, dict):
        for k in ("checked_in_today", "checkedInToday"):
            if k in d:
                return bool(d[k])
        stats = d.get("stats")
        if isinstance(stats, dict) and "checked_in_today" in stats:
            return bool(stats["checked_in_today"])
        for k in ("checkin_date", "last_checkin_date", "last_checkin_at"):
            v = d.get(k)
            if isinstance(v, str) and v[:10] == datetime.now(CST).strftime("%Y-%m-%d"):
                return True
    return None


def fmt_quota(q):
    """把 quota 数值格式化成「原始数（约 $x.xx）」。"""
    try:
        q = float(q)
    except Exception:
        return str(q)
    return "%s（约 $%.2f）" % (format(int(q), ","), q / QUOTA_PER_UNIT)


def balance_line(tok):
    """查当前余额，返回一行文本；查不到返回空串（不影响主流程）。"""
    code, data = api("/api/user/self", tok, "GET")
    if code != 200 or not isinstance(data, dict):
        return ""
    d = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(d, dict) or "quota" not in d:
        return ""
    line = "💰 余额: " + fmt_quota(d.get("quota"))
    if "used_quota" in d:
        line += "\n📊 已用: " + fmt_quota(d.get("used_quota"))
    return line


def main():
    tok = token()
    if not tok:
        print("ERR: 没有 token（请在 repo 里配置 secret X42_TOKEN）")
        notify("❌ 42x.shop 签到失败：没有 token（缺 secret X42_TOKEN）")
        return 2

    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    # ① 查状态
    code, data = api("/api/user/checkin", tok, "GET")
    if is_auth_error(code, data):
        print("[%s] ERR: access token 无效/过期（http=%s）" % (ts, code))
        notify("❌ 42x.shop 签到失败：access token 无效/过期\n[%s]" % ts)
        return 3
    if code != 200:
        print("[%s] ERR: 查状态 http=%s %s" % (ts, code, str(data)[:200]))
        notify("❌ 42x.shop 签到失败：查状态 http=%s\n%s\n[%s]" % (code, str(data)[:200], ts))
        return 4

    already = extract_checked_in_today(data)
    detail = json.dumps(data.get("data"), ensure_ascii=False)[:200] if isinstance(data, dict) else str(data)[:200]

    # GET 明确报错（非"已签/未签"语义）→ 不盲发 POST
    if isinstance(data, dict) and data.get("success") is False and already is None:
        msg0 = str(data.get("message") or "")[:200]
        print("[%s] ERR: 查状态返回失败 http=%s %s" % (ts, code, detail))
        notify("❌ 42x.shop 查询签到状态失败：%s\n[%s]" % (msg0, ts))
        return 4

    print("[%s] 状态: checked_in_today=%s %s" % (ts, already, detail))
    if already:
        print("[%s] 今天已签到，跳过" % ts)
        bal = balance_line(tok)
        if bal:
            print(bal)          # 也打到 Actions 日志里，不只在 TG 推送里
        msg = "✅ 42x.shop 今日已签到（跳过）\n[%s]" % ts
        if bal:
            msg += "\n" + bal
        notify(msg)
        return 0

    # ② 执行签到
    code, data = api("/api/user/checkin", tok, "POST")
    detail = json.dumps(data, ensure_ascii=False)[:300] if not isinstance(data, str) else data[:300]
    ok = isinstance(data, dict) and (data.get("success") is True or data.get("code") in (0, "0"))

    # 竞态/重复调用时接口回 success:false + "今日已签到"，视为已签成功
    msg = data.get("message", "") if isinstance(data, dict) else str(data)
    already_msg = ("已签到" in msg) or ("已经签到" in msg) or ("already" in msg.lower())

    if code == 200 and (ok or already_msg):
        head = "✅ 42x.shop 签到成功" if ok else "✅ 42x.shop 今日已签到"
        print("[%s] %s http=200 %s" % (ts, head, detail))
        bal = balance_line(tok)
        if bal:
            print(bal)          # 也打到 Actions 日志里，不只在 TG 推送里
        m = "%s\n%s\n[%s]" % (head, detail, ts)
        if bal:
            m += "\n" + bal
        notify(m)
        return 0

    print("[%s] ❌ 签到失败 http=%s %s" % (ts, code, detail))
    notify("❌ 42x.shop 签到失败 http=%s\n%s\n[%s]" % (code, detail, ts))
    return 5


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 兜底：任何异常都要有明确输出与退出码
        print("fatal: %s" % e)
        notify("❌ 42x.shop 签到脚本异常：%s" % e)
        sys.exit(1)
