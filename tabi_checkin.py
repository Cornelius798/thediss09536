#!/usr/bin/env python3
"""
tabitoken.com 自动签到（Turnstile 硬校验站，GitHub Actions 实验版）
思路：UC Mode 真实浏览器在正确域名下渲染并通过 Turnstile，
拿到 token 后用访问令牌提交签到。
多账号：TABI_TOKEN_1 / TABI_TOKEN_2 ...
"""
import os
import sys
import time
import json

from seleniumbase import SB

BASE = "https://tabitoken.com"
CHECKIN_PATH = "/api/user/checkin"
STATUS_PATH = "/api/status"

# 每个账号解验证码的最大尝试时间（秒）
SOLVE_TIMEOUT = 120


def collect_tokens():
    tokens = []
    # 兼容单号
    if os.environ.get("TABI_TOKEN", "").strip():
        tokens.append(("主号", os.environ["TABI_TOKEN"].strip()))
    for i in range(1, 11):
        v = os.environ.get(f"TABI_TOKEN_{i}", "").strip()
        if v:
            tokens.append((f"号{i}", v))
    return tokens


def get_sitekey(sb):
    """从 /api/status 读取 turnstile_site_key"""
    js = f"""
    const cb = arguments[arguments.length - 1];
    fetch("{BASE}{STATUS_PATH}")
      .then(r => r.json())
      .then(d => cb((d.data && d.data.turnstile_site_key) || ""))
      .catch(() => cb(""));
    """
    try:
        return sb.driver.execute_async_script(js) or ""
    except Exception as e:
        print(f"[!] 读取 sitekey 失败: {e}", flush=True)
        return ""


def inject_turnstile(sb, sitekey):
    """在当前页面注入 turnstile 组件并显式渲染"""
    js = f"""
    window.__tsToken = null;
    window.__tsErr = null;
    let old = document.getElementById('__ts_box');
    if (old) old.remove();
    let box = document.createElement('div');
    box.id = '__ts_box';
    box.style.position = 'fixed';
    box.style.top = '20px';
    box.style.left = '20px';
    box.style.zIndex = '999999';
    box.style.background = '#fff';
    box.style.padding = '10px';
    document.body.appendChild(box);

    function render() {{
      try {{
        turnstile.render('#__ts_box', {{
          sitekey: '{sitekey}',
          callback: function(t) {{ window.__tsToken = t; }},
          'error-callback': function(e) {{ window.__tsErr = String(e); }}
        }});
      }} catch(e) {{ window.__tsErr = 'render:' + e; }}
    }}

    if (window.turnstile) {{
      render();
    }} else {{
      let s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__tsOnload';
      window.__tsOnload = render;
      document.head.appendChild(s);
    }}
    """
    sb.execute_script(js)


def wait_token(sb, timeout=SOLVE_TIMEOUT):
    """轮询等待 turnstile token；期间反复尝试点击，并诊断渲染状态"""
    start = time.time()
    click_count = 0
    diagnosed = False
    while time.time() - start < timeout:
        tok = sb.execute_script("return window.__tsToken;")
        if tok:
            return tok

        err = sb.execute_script("return window.__tsErr;")
        if err:
            print(f"[!] turnstile error-callback: {err}", flush=True)

        # 一次性诊断：组件渲染了吗？iframe 出来了吗？
        if not diagnosed and time.time() - start > 6:
            diag = sb.execute_script("""
              const box = document.getElementById('__ts_box');
              const ifr = box ? box.querySelectorAll('iframe').length : -1;
              const hasTS = !!window.turnstile;
              return JSON.stringify({box: !!box, iframes: ifr, turnstileLoaded: hasTS});
            """)
            print(f"[诊断] {diag}", flush=True)
            diagnosed = True

        # 反复尝试点击（每 ~10 秒一次，最多 5 次）
        if click_count < 5 and int(time.time() - start) % 10 < 2:
            try:
                sb.uc_gui_click_captcha()
                click_count += 1
                print(f"[*] 点击验证框 第{click_count}次", flush=True)
            except Exception as e:
                print(f"[!] 点击失败(可忽略): {e}", flush=True)
        time.sleep(2)
    return None


def submit_checkin(sb, token, ts_token):
    """在页面上下文用 fetch 提交签到（带访问令牌 + turnstile token）"""
    js = f"""
    const cb = arguments[arguments.length - 1];
    fetch("{BASE}{CHECKIN_PATH}?turnstile=" + encodeURIComponent("{ts_token}"), {{
      method: "POST",
      headers: {{
        "Authorization": "Bearer {token}",
        "Accept": "application/json"
      }}
    }})
    .then(async r => cb(JSON.stringify({{status: r.status, body: await r.text()}})))
    .catch(e => cb(JSON.stringify({{status: -1, body: String(e)}})));
    """
    try:
        raw = sb.driver.execute_async_script(js)
        return json.loads(raw)
    except Exception as e:
        return {"status": -1, "body": str(e)}


def check_status(sb, token):
    """查询是否已签到"""
    js = f"""
    const cb = arguments[arguments.length - 1];
    fetch("{BASE}{CHECKIN_PATH}", {{
      method: "GET",
      headers: {{ "Authorization": "Bearer {token}", "Accept": "application/json" }}
    }})
    .then(async r => cb(await r.text()))
    .catch(e => cb(""));
    """
    try:
        raw = sb.driver.execute_async_script(js)
        return json.loads(raw)
    except Exception:
        return {}


def process_account(sb, label, token, sitekey):
    print(f"\n===== 账号 [{label}] =====", flush=True)

    # 先查状态
    st = check_status(sb, token)
    stats = (st.get("data") or {}).get("stats") or {}
    if stats.get("checked_in_today"):
        print(f"[+] 今日已签到 | 累计 {stats.get('checkin_count','?')} 次", flush=True)
        return True

    # 注入 turnstile 并等待 token
    inject_turnstile(sb, sitekey)
    ts_token = wait_token(sb)
    if not ts_token:
        print("[!] 未能获取 turnstile token（验证未通过）", flush=True)
        try:
            sb.save_screenshot(f"fail_{label}.png")
            print(f"[*] 已保存截图 fail_{label}.png", flush=True)
        except Exception:
            pass
        return False

    print(f"[*] 拿到 turnstile token（前12位 {ts_token[:12]}...），提交签到", flush=True)
    res = submit_checkin(sb, token, ts_token)
    print(f"[*] 签到返回 HTTP {res.get('status')}: {res.get('body','')[:200]}", flush=True)

    # 复查
    st2 = check_status(sb, token)
    stats2 = (st2.get("data") or {}).get("stats") or {}
    if stats2.get("checked_in_today"):
        recs = stats2.get("records", [])
        awarded = recs[0].get("quota_awarded") if recs else "?"
        print(f"[+] 签到成功 | 获得额度 {awarded}", flush=True)
        return True
    print("[!] 签到后仍显示未签，可能验证被拒或接口参数不同", flush=True)
    return False


def notify_tg(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        return
    import urllib.request
    import urllib.parse
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15
        )
    except Exception as e:
        print(f"[!] TG 通知失败: {e}", flush=True)


def main():
    accounts = collect_tokens()
    if not accounts:
        print("[-] 未配置任何 TABI_TOKEN_x", flush=True)
        sys.exit(1)

    print(f"[*] 共 {len(accounts)} 个账号", flush=True)
    results = []

    with SB(uc=True, xvfb=True, headless=False) as sb:
        # 打开真实域名，过 CF 基础盾
        sb.uc_open_with_reconnect(BASE, reconnect_time=6)
        try:
            sb.uc_gui_click_captcha()
        except Exception:
            pass
        sb.sleep(3)

        sitekey = get_sitekey(sb)
        if not sitekey:
            print("[-] 无法获取 turnstile sitekey，终止", flush=True)
            notify_tg("❌ tabitoken 签到失败：无法获取 sitekey")
            sys.exit(1)
        print(f"[*] sitekey: {sitekey}", flush=True)

        for label, token in accounts:
            try:
                ok = process_account(sb, label, token, sitekey)
            except Exception as e:
                print(f"[!] 账号 [{label}] 异常: {e}", flush=True)
                ok = False
            results.append((label, ok))

    succ = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n===== 完成：{succ}/{total} 成功 =====", flush=True)

    detail = "\n".join(f"{l}: {'✅' if ok else '❌'}" for l, ok in results)
    notify_tg(f"tabitoken 签到 {succ}/{total}\n{detail}")

    if succ < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
