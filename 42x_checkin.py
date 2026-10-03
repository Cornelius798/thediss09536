#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""42x.shop（New API v1.0.0-rc.15）每日自动签到 —— GitHub Actions 版。

站点事实（2026-10-03 实测）
--------------------------
- `GET /api/status` → `checkin_enabled=true`、`turnstile_check=false`、`system_name="42 API"`、
  `version="v1.0.0-rc.15"`、`quota_per_unit=500000`。
- New API 标准端点：
      GET  /api/user/checkin   查签到状态
      POST /api/user/checkin   执行签到
- 站点没开 Turnstile，纯 HTTP 即可，不需要浏览器自动化。
- ★★ 鉴权失败的两种形态**必须分清**（本地实测对照）：
      · 请求里**完全不带** Authorization 头 → HTTP **401** `"Unauthorized, not logged in and
        no access token provided"`  ← 说明**头在链路上丢了**（被代理/CF 剥掉、或 token 为空）
      · 请求里带了 Authorization 但 token 不对 → HTTP **200** `"Unauthorized, invalid access
        token"`                          ← 说明头到了、**token 值本身不对**
  所以看到 401 不要去查 token 值，要查"头为什么没发出去"；看到 200+invalid 才是 token 错。

token 来源优先级
----------------
环境变量 `X42_TOKEN`（GitHub repo secret）> 同目录 `42x_checkin.json`。

幂等
----
先 GET 状态，今天已签就直接退出（不发 POST）。

结果推送（可选）
----------------
设了 `TG_BOT_TOKEN` + `TG_CHAT_ID` 就把结果推到自己的 tgbot；缺任意一个或推送失败都静默
跳过，不影响签到本身。

诊断
----
每次请求都打一行 `[diag]`（方法 / 状态码 / 最终 URL / 重定向次数 / 是否带上了 Authorization），
失败时**原样打印服务器返回的 body**——出问题看 Actions 日志就能定位，不用猜。
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


def mask(t):
    """只暴露长度与前 4 位，够判断"secret 有没有传进来 / 有没有多余字符"，又不泄露全文。"""
    if not t:
        return "(空)"
    return "len=%d prefix=%s…" % (len(t), t[:4])


def token():
    t = (os.environ.get("X42_TOKEN") or "").strip()
    src = "env:X42_TOKEN"
    if not t:
        try:
            t = (json.load(open(CFG, encoding="utf-8")).get("access_token") or "").strip()
            src = "file:42x_checkin.json"
        except Exception:
            t = ""
            src = "(无)"
    return t, src


def api(path, tok, method="GET", auth="bearer", tag=""):
    """调 New API 端点。返回 (http_status, 解析后的 json 或原文, diag 字符串)。

    auth: "bearer" → Authorization: Bearer <tok>
          "raw"    → Authorization: <tok>            （少数部署不吃 Bearer 前缀）
          "none"   → 完全不带头                        （只为复现 401 做对照）
    """
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    if auth == "bearer":
        req.add_header("Authorization", "Bearer " + tok)
    elif auth == "raw":
        req.add_header("Authorization", tok)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; 42x-checkin/1.0)")

    sent = ("Bearer " + mask(tok)) if auth == "bearer" else (mask(tok) if auth == "raw" else "(未发送)")

    def _diag(code, final_url):
        return "[diag]%s %s %s -> http=%s final_url=%s auth=%s" % (
            tag, method, path, code, final_url, sent)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "ignore")
            d = _diag(r.status, r.url)
            try:
                return r.status, json.loads(raw), d
            except Exception:
                # Cloudflare 拦截/HTML 挑战页会走到这里 —— 原样带出去，日志里能看出是被墙了
                return r.status, raw[:300], d
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        d = _diag(e.code, getattr(e, "url", url))
        try:
            return e.code, json.loads(raw), d
        except Exception:
            return e.code, raw[:300], d
    except Exception as e:
        return 0, "network error: %s" % e, _diag(0, url)


def body_text(data):
    if isinstance(data, str):
        return data[:300]
    try:
        return json.dumps(data, ensure_ascii=False)[:300]
    except Exception:
        return str(data)[:300]


def is_auth_error(code, data):
    if code == 401:
        return True
    if isinstance(data, dict):
        msg = str(data.get("message") or "").lower()
        if data.get("success") is False and (
            "unauthorized" in msg or "invalid access token" in msg or "not logged in" in msg
        ):
            return True
    return False


def header_missing_hint(code, data):
    """401 + 'not logged in and no access token provided' ⇒ 服务器没收到 Authorization 头。"""
    if code != 401:
        return ""
    msg = str(data.get("message") or "") if isinstance(data, dict) else str(data)
    if "not logged in and no access token provided" in msg:
        return ("\n   ↳ 服务器**没收到 Authorization 头**（不是 token 值错）。"
                "\n     常见原因：① secret X42_TOKEN 为空/未生效；② 出口代理或 Cloudflare 把该头剥掉。"
                "\n     下面自动做一次不带头的对照请求，确认站点行为一致。")
    return ""


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
    try:
        q = float(q)
    except Exception:
        return str(q)
    return "%s（约 $%.2f）" % (format(int(q), ","), q / QUOTA_PER_UNIT)


def balance_line(tok):
    """查当前余额，返回一行文本；查不到返回空串（不影响主流程）。"""
    code, data, d = api("/api/user/self", tok, "GET", tag=" [bal]")
    print(d)
    if code != 200 or not isinstance(data, dict):
        return ""
    d2 = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(d2, dict) or "quota" not in d2:
        return ""
    line = "💰 余额: " + fmt_quota(d2.get("quota"))
    if "used_quota" in d2:
        line += "\n📊 已用: " + fmt_quota(d2.get("used_quota"))
    return line


def main():
    tok, src = token()
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] token 来源=%s %s" % (ts, src, mask(tok)))
    print("[%s] X42_TOKEN 环境变量存在=%s" % (ts, "X42_TOKEN" in os.environ))
    if not tok:
        print("ERR: 没有 token（请在 repo 里配置 secret X42_TOKEN）")
        notify("❌ 42x.shop 签到失败：没有 token（缺 secret X42_TOKEN）")
        return 2

    # ① 查状态
    code, data, d = api("/api/user/checkin", tok, "GET")
    print(d)
    if is_auth_error(code, data):
        print("[%s] ERR: 鉴权失败 http=%s body=%s" % (ts, code, body_text(data)))
        print("[%s]%s" % (ts, header_missing_hint(code, data)))
        # 自动对照：不带 Authorization 头发一次，看站点是不是回同样的 401
        code0, data0, d0 = api("/api/user/checkin", "", "GET", auth="none", tag=" [对照]")
        print(d0)
        print("[%s]   不带头的对照结果: http=%s body=%s" % (ts, code0, body_text(data0)))
        if code == 401 and code0 == 401:
            print("[%s]   ⇒ 判定：**请求头在链路中丢失**（两者同为 401），不是 token 值的问题。"
                  % ts)
        elif code == 200:
            print("[%s]   ⇒ 判定：**token 值不对**（头到了但站点不认）。" % ts)
        notify("❌ 42x.shop 签到失败：鉴权失败 http=%s\n%s\n[%s]" % (code, body_text(data), ts))
        return 3
    if code != 200:
        print("[%s] ERR: 查状态 http=%s %s" % (ts, code, body_text(data)))
        notify("❌ 42x.shop 签到失败：查状态 http=%s\n%s\n[%s]" % (code, body_text(data), ts))
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
    code, data, d = api("/api/user/checkin", tok, "POST")
    print(d)
    detail = body_text(data)
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
