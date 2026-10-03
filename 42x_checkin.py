#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""42x.shop（New API v1.0.0-rc.15）每日自动签到 —— GitHub Actions 版。

★ 认证：需要**两个头**（New API 的防 token 泄露机制，缺一不可）
--------------------------------------------------------------
    1. Authorization: Bearer <access token>
       ← 站点「个人设置 → 系统访问令牌」生成，环境变量 X42_TOKEN
    2. New-Api-User: <数字用户 ID>
       ← **必须是该 token 主人的数字 ID，严格相等**，环境变量 X42_USER_ID

    权威依据：QuantumNous/new-api tag v1.0.0-rc.15，middleware/auth.go
        apiUserIdStr := c.Request.Header.Get("New-Api-User")
        if apiUserIdStr == ""            → 401 "New-Api-User header not provided"
        apiUserId, err := strconv.Atoi(apiUserIdStr)
        if err != nil                     → 401 "user id format error"（必须是纯数字）
        if id != apiUserId                → 401 "user id mismatch"（必须严格等于 token 主人）
    路由依据：router/api-router.go → selfRoute.Use(middleware.UserAuth())，
        GET/POST /api/user/checkin 都在其下（POST 另挂 TurnstileCheck，但站点
        turnstile_check=false，等于空操作）。

★ 鉴权失败的四种形态（本地实测 + 源码对照，别搞混）
---------------------------------------------------
    · HTTP 401 "not logged in and no access token provided"  → **Authorization 头没送达**
    · HTTP 401 "New-Api-User header not provided"             → 头到了、token 有效，**缺用户 ID**
    · HTTP 200 "invalid access token"                         → Authorization 头到了，**token 值不对**
    · HTTP 401 "user id mismatch"                             → 两个头都在，**用户 ID 填错了**

token 来源优先级
----------------
环境变量 `X42_TOKEN`（GitHub secret）> 同目录 `42x_checkin.json`。
用户 ID 同理：`X42_USER_ID` > json 的 `user_id`。

幂等
----
先 GET 状态，今天已签就直接退出（不发 POST）。

结果推送（可选）
----------------
设了 `TG_BOT_TOKEN` + `TG_CHAT_ID` 就把结果推到自己的 tgbot；缺任意一个或推送失败都静默
跳过，不影响签到本身。

诊断
----
每次请求都打一行 `[diag]`（方法 / 状态码 / 最终 URL / 实际发出的 auth，只显示长度与前 4 位），
失败时**原样打印服务器返回的 body** 并给出针对性结论。
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
    """只暴露长度与前 4 位，够判断"值有没有传进来 / 有没有多余字符"，又不泄露全文。"""
    if not t:
        return "(空)"
    return "len=%d prefix=%s…" % (len(t), t[:4])


def load_creds():
    """返回 (token, user_id, 来源说明)。env 优先于本地 json。"""
    tok = (os.environ.get("X42_TOKEN") or "").strip()
    uid = (os.environ.get("X42_USER_ID") or "").strip()
    src = "env"
    if not tok or not uid:
        try:
            cfg = json.load(open(CFG, encoding="utf-8"))
        except Exception:
            cfg = {}
        if not tok:
            tok = str(cfg.get("access_token") or "").strip()
            src = "file"
        if not uid:
            uid = str(cfg.get("user_id") or "").strip()
            src = "file" if src == "file" else "env+file"
    if not tok and not uid:
        src = "(无)"
    return tok, uid, src


def api(path, tok, uid, method="GET", auth="bearer", tag=""):
    """调 New API 端点。返回 (http_status, 解析后的 json 或原文, diag 字符串)。

    auth: "bearer" → Authorization: Bearer <tok> + New-Api-User: <uid>
          "raw"    → Authorization: <tok>      + New-Api-User: <uid>（少数部署不吃 Bearer 前缀）
          "none"   → 不带头                      （只为复现 401 做对照）
    """
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    if auth == "bearer":
        req.add_header("Authorization", "Bearer " + tok)
    elif auth == "raw":
        req.add_header("Authorization", tok)
    if auth in ("bearer", "raw") and uid:
        req.add_header("New-Api-User", uid)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; 42x-checkin/1.0)")

    if auth == "none":
        sent = "(未发送)"
    else:
        sent = "%s%s | New-Api-User:%s" % (
            "Bearer " if auth == "bearer" else "", mask(tok), uid or "(空)")

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


def auth_hint(code, data):
    """把鉴权失败翻译成"下一步该改什么"，四种形态一一对应。"""
    msg = str(data.get("message") or "") if isinstance(data, dict) else str(data)
    low = msg.lower()
    if "not logged in and no access token provided" in msg:
        return ("   ↳ 判定：**Authorization 头没送达**（不是 token 值错）。"
                "\n     查：secret X42_TOKEN 是否为空/未生效；出口代理或 Cloudflare 是否剥掉了该头。")
    if "new-api-user header not provided" in low:
        return ("   ↳ 判定：token 有效，但**缺 New-Api-User 头**。"
                "\n     修：加 repo secret `X42_USER_ID` = 你的**数字用户 ID**（见 README 的取法）。")
    if "user id format error" in low:
        return ("   ↳ 判定：`X42_USER_ID` 不是纯数字。修：填数字 ID，别带引号/空格/用户名。")
    if "user id mismatch" in low:
        return ("   ↳ 判定：`X42_USER_ID` 填错了（必须严格等于 token 主人的 ID）。"
                "\n     修：核对 repo secret `X42_USER_ID`。")
    if "invalid access token" in low:
        return ("   ↳ 判定：**token 值不对**（头到了但站点不认）。"
                "\n     修：核对 secret `X42_TOKEN` 取自「个人设置 → 系统访问令牌」，不是 `sk-` 开头的 API 密钥。")
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


def balance_line(tok, uid):
    """查当前余额，返回一行文本；查不到返回空串（不影响主流程）。"""
    code, data, d = api("/api/user/self", tok, uid, "GET", tag=" [bal]")
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
    tok, uid, src = load_creds()
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] 凭证来源=%s token=%s user_id=%s" % (ts, src, mask(tok), uid or "(空)"))
    print("[%s] 环境变量 X42_TOKEN=%s X42_USER_ID=%s" % (
        ts, "X42_TOKEN" in os.environ, "X42_USER_ID" in os.environ))

    if not tok:
        print("ERR: 没有 token（请在 repo 里配置 secret X42_TOKEN）")
        notify("❌ 42x.shop 签到失败：没有 token（缺 secret X42_TOKEN）")
        return 2
    if not uid:
        print("ERR: 没有 user_id（New API 要求 New-Api-User 头，请在 repo 里配置 secret X42_USER_ID）")
        notify("❌ 42x.shop 签到失败：缺 secret X42_USER_ID（数字用户 ID）\n[%s]" % ts)
        return 2

    # ① 查状态
    code, data, d = api("/api/user/checkin", tok, uid, "GET")
    print(d)
    if is_auth_error(code, data):
        print("[%s] ERR: 鉴权失败 http=%s body=%s" % (ts, code, body_text(data)))
        hint = auth_hint(code, data)
        if hint:
            print("[%s]%s" % (ts, hint))
        # 自动对照：不带 Authorization 头发一次，看站点是不是回"没带头"的 401
        code0, data0, d0 = api("/api/user/checkin", "", "", "GET", auth="none", tag=" [对照]")
        print(d0)
        print("[%s]   不带头的对照结果: http=%s body=%s" % (ts, code0, body_text(data0)))
        if code == 401 and code0 == 401 and "not logged in" in body_text(data0):
            print("[%s]   （对照也是 401「not logged in」⇒ 若本条与之一致，才是头没送达）" % ts)
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
        bal = balance_line(tok, uid)
        if bal:
            print(bal)          # 也打到 Actions 日志里，不只在 TG 推送里
        msg = "✅ 42x.shop 今日已签到（跳过）\n[%s]" % ts
        if bal:
            msg += "\n" + bal
        notify(msg)
        return 0

    # ② 执行签到
    code, data, d = api("/api/user/checkin", tok, uid, "POST")
    print(d)
    detail = body_text(data)
    ok = isinstance(data, dict) and (data.get("success") is True or data.get("code") in (0, "0"))

    # 竞态/重复调用时接口回 success:false + "今日已签到"，视为已签成功
    msg = data.get("message", "") if isinstance(data, dict) else str(data)
    already_msg = ("已签到" in msg) or ("已经签到" in msg) or ("already" in msg.lower())

    if code == 200 and (ok or already_msg):
        head = "✅ 42x.shop 签到成功" if ok else "✅ 42x.shop 今日已签到"
        print("[%s] %s http=200 %s" % (ts, head, detail))
        bal = balance_line(tok, uid)
        if bal:
            print(bal)          # 也打到 Actions 日志里，不只在 TG 推送里
        m = "%s\n%s\n[%s]" % (head, detail, ts)
        if bal:
            m += "\n" + bal
        notify(m)
        return 0

    print("[%s] ❌ 签到失败 http=%s %s" % (ts, code, detail))
    hint = auth_hint(code, data)
    if hint:
        print("[%s]%s" % (ts, hint))
    notify("❌ 42x.shop 签到失败 http=%s\n%s\n[%s]" % (code, detail, ts))
    return 5


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 兜底：任何异常都要有明确输出与退出码
        print("fatal: %s" % e)
        notify("❌ 42x.shop 签到脚本异常：%s" % e)
        sys.exit(1)
