#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TabiToken 每日签到

依赖:
    pip install playwright==1.47.0
    playwright install --with-deps chromium

环境变量:
    TABI_ACCOUNTS  必填, JSON 数组:
        [{"name":"acc1","token":"<系统访问令牌>","user_id":1234}, ...]
        token 取自 个人设置 → 系统访问令牌(不是 sk- 开头的 API 密钥)
        user_id 必填: /api/user/self 需要 New-Api-User 头才能通过认证
    SITE_URL       可选, 默认 https://tabitoken.com
    BACKUP_URL     可选, 主站打不开时的备用域名

退出码: 全部成功 0, 任一账号失败 1
"""

import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# ---------------------------------------------------------------- 配置

SITE_URL = os.environ.get("SITE_URL", "https://tabitoken.com").rstrip("/")
BACKUP_URL = os.environ.get("BACKUP_URL", "").rstrip("/")

NAV_TIMEOUT = 60_000        # 页面导航超时 (ms)
SETTLE_MS = 3_000           # 导航后静置, 等前端 JS 与 Turnstile 初始化
MAX_ROUNDS = 20             # 点击后确认签到结果的最大轮询次数
ROUND_INTERVAL = 3_000      # 每轮间隔 (ms), 20*3s = 最长等 60s
MIN_GAP, MAX_GAP = 30, 90   # 账号之间的随机间隔 (s), 降低同 IP 频率特征

BJ = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("checkin")

# 供 route 拦截器使用的当前账号上下文, 每个账号处理前改写
state = {"token": None, "user_id": None}


# ---------------------------------------------------------------- 小工具

def now_bj() -> datetime:
    return datetime.now(BJ)


def current_month() -> str:
    """签到接口按月查询, 用北京时间的月份"""
    return now_bj().strftime("%Y-%m")


def today_bj() -> str:
    return now_bj().strftime("%Y-%m-%d")


def dig(obj, *keys):
    """安全取多层 key, 任一层缺失或类型不符返回 None"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def load_accounts():
    raw = os.environ.get("TABI_ACCOUNTS", "").strip()
    if not raw:
        log.error("TABI_ACCOUNTS 未设置")
        return []
    try:
        accounts = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"TABI_ACCOUNTS 不是合法 JSON: {e}")
        return []
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not isinstance(accounts, list):
        log.error("TABI_ACCOUNTS 应为 JSON 数组")
        return []

    valid = []
    for i, acc in enumerate(accounts, 1):
        if not isinstance(acc, dict) or not acc.get("token"):
            log.error(f"第 {i} 个账号缺少 token, 跳过")
            continue
        acc.setdefault("name", f"acc{i}")
        if not acc.get("user_id"):
            log.warning(
                f"[{acc['name']}] 未配置 user_id, /api/user/self 大概率 401"
            )
        valid.append(acc)
    return valid


# ---------------------------------------------------------------- 网络层

async def install_route(context):
    """
    给所有 /api/ 请求注入当前账号的认证头。
    前端自身的 cookie 会话可能残留, 显式覆盖 Authorization 与 New-Api-User
    才能保证请求落在预期账号上。
    """

    async def handler(route):
        try:
            headers = dict(route.request.headers)
            if state["token"]:
                headers["authorization"] = f"Bearer {state['token']}"
            if state["user_id"] is not None:
                headers["new-api-user"] = str(state["user_id"])
            headers["cache-control"] = "no-store"
            await route.continue_(headers=headers)
        except Exception:
            # 页面关闭或请求已被取消时 continue_ 会抛错, 忽略即可
            try:
                await route.continue_()
            except Exception:
                pass

    await context.route("**/api/**", handler)


async def api_get(page, token: str, path: str, user_id=None):
    """在页面上下文里发 GET, 返回 {http, json, raw}"""
    return await page.evaluate(
        """async ([token, path, userId]) => {
            const headers = { 'Authorization': 'Bearer ' + token };
            if (userId !== null && userId !== undefined) {
                headers['New-Api-User'] = String(userId);
            }
            try {
                const resp = await fetch(path, { headers, cache: 'no-store' });
                const body = await resp.text();
                let json = null;
                try { json = JSON.parse(body); } catch (e) {}
                return { http: resp.status, json: json, raw: body.slice(0, 200) };
            } catch (e) {
                return { http: 0, json: null, raw: String(e) };
            }
        }""",
        [token, path, user_id],
    )


async def resolve_identity(page, token: str, hint=None):
    """
    用 token 换取真实 user_id 与 username。
    /api/user/self 需要 New-Api-User 头, 所以 hint 不能为空;
    hint 正确时顺便验证 token 与 id 是否匹配。
    """
    res = await api_get(page, token, "/api/user/self", user_id=hint)
    data = dig(res.get("json"), "data")
    if not isinstance(data, dict):
        log.error(
            f"/api/user/self 失败 http={res.get('http')} body={res.get('raw')}"
        )
        return None, None
    return data.get("id"), data.get("username")


def checked_today(payload) -> bool:
    """
    容忍多种返回形态判断今天是否已签到:
        data = [1, 5, 22]                      -> 当月日号
        data = ["2026-08-22", ...]             -> 日期字符串
        data = [{"date":"2026-08-22", ...}]    -> 对象数组
        data = {"days":[...]} / {"checked_in_today": true}
    """
    data = dig(payload, "data")
    if data is None:
        data = payload

    if isinstance(data, dict):
        for k in ("checked_in_today", "today", "is_checked_in", "checked"):
            if isinstance(data.get(k), bool):
                return data[k]
        for k in ("days", "dates", "records", "list", "check_in_days"):
            if isinstance(data.get(k), list):
                data = data[k]
                break

    if not isinstance(data, list):
        return False

    today_str = today_bj()
    today_day = now_bj().day
    for item in data:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item == today_day:
            return True
        if isinstance(item, str) and today_str in item:
            return True
        if isinstance(item, dict):
            for v in item.values():
                if isinstance(v, str) and today_str in v:
                    return True
                if isinstance(v, int) and v == today_day:
                    return True
    return False


# ---------------------------------------------------------------- 页面操作

async def clear_session(context, page):
    """清掉上一个账号的会话痕迹, 避免前端登录态串号"""
    await context.clear_cookies()
    try:
        await page.evaluate(
            "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
        )
    except Exception:
        pass


async def find_checkin_button(page):
    """按文案找签到按钮, 逐个候选尝试"""
    candidates = [
        "button:has-text('签到')",
        "button:has-text('每日签到')",
        "[role='button']:has-text('签到')",
        "a:has-text('签到')",
        "div:has-text('立即签到')",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None


async def wait_turnstile(page, timeout_ms=25_000):
    """
    等 Turnstile 出结果。存在 cf-turnstile-response 且非空即视为通过;
    页面没有 Turnstile 时立即返回 True。
    """
    try:
        has_widget = await page.evaluate(
            """() => !!document.querySelector(
                '.cf-turnstile, [name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"]'
            )"""
        )
    except Exception:
        return True
    if not has_widget:
        return True

    waited = 0
    step = 1_000
    while waited < timeout_ms:
        try:
            token = await page.evaluate(
                """() => {
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    return el ? el.value : '';
                }"""
            )
        except Exception:
            token = ""
        if token:
            return True
        await page.wait_for_timeout(step)
        waited += step
    return False


async def open_site(page):
    """打开主站, 失败则回退备用域名, 返回实际使用的 base url"""
    for url in [u for u in (SITE_URL, BACKUP_URL) if u]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            await page.wait_for_timeout(SETTLE_MS)
            log.info(f"站点已打开: {url}")
            return url
        except PWTimeout:
            log.error(f"打开超时: {url}")
        except Exception as e:
            log.error(f"打开失败: {url} ({e})")
    return None


# ---------------------------------------------------------------- 单账号

async def checkin_one(context, page, acc, base_url):
    name = acc["name"]
    token = acc["token"]

    await clear_session(context, page)

    state["token"] = token
    hint = acc.get("user_id")
    state["user_id"] = hint  # 有配置就先注入; 没有则为 None, 等解析结果

    try:
        await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    except PWTimeout:
        log.error(f"[{name}] 刷新页面超时")
        return False
    await page.wait_for_timeout(SETTLE_MS)

    uid, uname = await resolve_identity(page, token, hint)
    if uid is None:
        if hint is None:
            log.error(
                f"[{name}] 无法解析身份: 请在 TABI_ACCOUNTS 中为该账号补上 user_id"
            )
            return False
        uid = hint
        log.warning(f"[{name}] 身份校验接口失败, 按配置的 user_id={uid} 继续")
    elif hint is not None and str(uid) != str(hint):
        log.warning(f"[{name}] 配置 user_id={hint} 与实际 {uid} 不一致, 以实际为准")
    state["user_id"] = uid
    log.info(f"[{name}] 身份确认 user_id={uid} username={uname}")

    # 点击前先看状态, 已签到直接跳过, 省掉一次 Turnstile
    status = await api_get(
        page, token, f"/api/user/checkin?month={current_month()}", user_id=uid
    )
    if status.get("http") == 200 and checked_today(status.get("json")):
        log.info(f"[{name}] 今日已签到")
        return True
    if status.get("http") not in (200, 404):
        log.warning(
            f"[{name}] 状态查询异常 http={status.get('http')} body={status.get('raw')}"
        )

    btn, sel = await find_checkin_button(page)
    if btn is None:
        log.error(f"[{name}] 未找到签到按钮, 站点文案可能已变更")
        await page.screenshot(path=f"fail_nobutton_{name}.png", full_page=True)
        return False

    if not await wait_turnstile(page):
        log.warning(f"[{name}] Turnstile 未在预期时间内完成, 仍尝试点击")

    try:
        await btn.click(timeout=15_000)
        log.info(f"[{name}] 已点击签到按钮 ({sel})")
    except Exception as e:
        log.error(f"[{name}] 点击签到按钮失败: {e}")
        await page.screenshot(path=f"fail_click_{name}.png", full_page=True)
        return False

    # 点击是异步的, 轮询接口确认服务端真的记上了
    for r in range(1, MAX_ROUNDS + 1):
        await page.wait_for_timeout(ROUND_INTERVAL)
        st = await api_get(
            page, token, f"/api/user/checkin?month={current_month()}", user_id=uid
        )
        if st.get("http") == 200 and checked_today(st.get("json")):
            log.info(f"[{name}] 签到成功(第 {r} 轮确认)")
            return True

    log.error(f"[{name}] 超时未确认签到结果")
    await page.screenshot(path=f"fail_timeout_{name}.png", full_page=True)
    return False


# ---------------------------------------------------------------- 主流程

async def main() -> int:
    accounts = load_accounts()
    if not accounts:
        log.error("没有可处理的账号")
        return 1

    random.shuffle(accounts)  # 打乱顺序, 避免每天固定账号第一个签
    total = len(accounts)
    log.info(f"待处理账号数: {total}")

    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(30_000)
        await install_route(context)
        page = await context.new_page()

        base_url = await open_site(page)
        if not base_url:
            log.error("主站与备用域名均无法打开")
            await browser.close()
            return 1

        for idx, acc in enumerate(accounts, 1):
            name = acc["name"]
            log.info(f"===== [{name}] ({idx}/{total}) =====")
            try:
                ok = await checkin_one(context, page, acc, base_url)
            except Exception as e:
                log.error(f"[{name}] 未预期异常: {type(e).__name__}: {e}")
                try:
                    await page.screenshot(path=f"fail_error_{name}.png", full_page=True)
                except Exception:
                    pass
                ok = False
            results[name] = ok

            if idx < total:
                gap = random.randint(MIN_GAP, MAX_GAP)
                log.info(f"等待 {gap}s 后处理下一个账号")
                await asyncio.sleep(gap)

        await context.close()
        await browser.close()

    log.info("===== 汇总 =====")
    for name, ok in results.items():
        log.info(f"  {name}: {'成功' if ok else '失败'}")
    succeeded = sum(1 for v in results.values() if v)
    log.info(f"结果: {succeeded}/{total}")

    return 0 if succeeded == total else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
