#!/usr/bin/env python3
"""
tabitoken 多账号自动签到（GitHub Actions / 本地通用）

思路:
  签到 POST 带 Turnstile 校验，纯 requests 无法伪造 token，
  所以用 Playwright 起无头 Chromium，让页面前端自己完成验证。
  身份完全由脚本控制: 用 token 调 /api/user/self 解析出 user_id，
  再把 Authorization + New-Api-User 两个头注入到所有 /api/ 请求上，
  不依赖前端 localStorage 里残留的登录态（多账号复用页面的关键）。

账号来源: 环境变量 TABI_ACCOUNTS，JSON 数组
  [{"name":"acc1","token":"xxx"},{"name":"acc2","token":"yyy"}]
  user_id 可选，不填则自动解析。

退出码: 全部成功 0，任一失败 1
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tabi")

SITE_URL = os.getenv("SITE_URL", "https://tabitoken.com")
BACKUP_URL = os.getenv("BACKUP_URL", "https://tabitoken.cc")

NAV_TIMEOUT = 45_000       # 页面导航超时 ms
POLL_ROUNDS = 20           # 点击后复查轮数
POLL_INTERVAL = 2_000      # 每轮间隔 ms
MIN_GAP, MAX_GAP = 30, 90  # 账号间随机间隔 秒

CST = timezone(timedelta(hours=8))


def load_accounts():
    raw = os.getenv("TABI_ACCOUNTS", "").strip()
    if not raw:
        log.error("未配置 TABI_ACCOUNTS")
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"TABI_ACCOUNTS 不是合法 JSON: {e}")
        return []
    if not isinstance(data, list):
        log.error("TABI_ACCOUNTS 必须是 JSON 数组")
        return []

    out = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            log.error(f"第 {i} 项不是对象，跳过")
            continue
        token = str(item.get("token", "")).strip()
        name = str(item.get("name") or f"account{i}").strip()
        if not token:
            log.error(f"[{name}] 缺少 token，跳过")
            continue
        out.append({"name": name, "token": token, "user_id": item.get("user_id")})
    return out


def safe_name(name: str) -> str:
    """截图文件名去掉特殊字符"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "acc"


def current_month() -> str:
    return datetime.now(CST).strftime("%Y-%m")


async def api_get(page, token: str, path: str):
    """在页面上下文里发 GET，返回 {http, json, raw}"""
    return await page.evaluate(
        """async ([token, path]) => {
            try {
                const resp = await fetch(path, {
                    headers: { 'Authorization': 'Bearer ' + token },
                    cache: 'no-store'
                });
                const body = await resp.text();
                let json = null;
                try { json = JSON.parse(body); } catch (e) {}
                return { http: resp.status, json: json, raw: body.slice(0, 200) };
            } catch (e) {
                return { http: 0, json: null, raw: String(e) };
            }
        }""",
        [token, path],
    )


def dig(obj, *keys):
    """逐层安全取值，任一层为 None 或非 dict 就返回 None"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def already_checked(status) -> bool:
    return bool(dig(status.get("json"), "data", "stats", "checked_in_today"))


async def resolve_identity(page, token: str):
    """
    用 token 换取真实 user_id。
    调用时 state['user_id'] 必须为空，否则会带上一个账号的 new-api-user 头。
    """
    res = await api_get(page, token, "/api/user/self")
    data = dig(res.get("json"), "data")
    if not isinstance(data, dict):
        return None, None
    return data.get("id"), data.get("username")


async def find_button(page):
    """签到按钮文案带 emoji 前缀，用包含匹配"""
    for sel in (
        'button:has-text("每日签到")',
        'button:has-text("签到")',
        '[role="button"]:has-text("签到")',
    ):
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc
    return None


async def checkin_one(page, state: dict, acc: dict) -> bool:
    name = acc["name"]
    token = acc["token"]
    tag = safe_name(name)

    # 解析身份前先清掉上个账号的 user_id，避免 /api/user/self 带错头
    state["token"] = token
    state["user_id"] = None

    try:
        await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    except PWTimeout:
        log.error(f"[{name}] 刷新页面超时")
        return False
    await page.wait_for_timeout(3_000)

    uid, uname = await resolve_identity(page, token)
    if uid is None:
        uid = acc.get("user_id")
        if uid is None:
            log.error(f"[{name}] 无法解析 user_id（token 可能失效），跳过")
            return False
        log.warning(f"[{name}] /api/user/self 解析失败，使用配置的 user_id={uid}")
    state["user_id"] = uid
    log.info(f"[{name}] 身份确认 user_id={uid} username={uname}")

    status = await api_get(page, token, f"/api/user/checkin?month={current_month()}")
    if not dig(status.get("json"), "success"):
        log.error(f"[{name}] 状态查询失败 http={status.get('http')} body={status.get('raw')}")
        return False

    if already_checked(status):
        log.info(f"[{name}] 今日已签到")
        return True

    btn = await find_button(page)
    if btn is None:
        log.error(f"[{name}] 未找到签到按钮")
        await page.screenshot(path=f"fail_nobutton_{tag}.png", full_page=True)
        return False

    # 页面 UI 可能还残留上个账号的状态（按钮置灰），
    # 但 API 已确认本账号未签到，所以置灰时强制点击，成功与否只看后面的轮询
    force = not await btn.is_enabled()
    if force:
        log.warning(f"[{name}] 按钮为禁用态（前端残留状态），尝试强制点击")
    try:
        await btn.click(timeout=10_000, force=force)
    except Exception as e:
        log.error(f"[{name}] 点击失败: {type(e).__name__}")
        await page.screenshot(path=f"fail_click_{tag}.png", full_page=True)
        return False

    log.info(f"[{name}] 已点击，等待 Turnstile 校验与服务端确认…")

    for r in range(1, POLL_ROUNDS + 1):
        await page.wait_for_timeout(POLL_INTERVAL)
        st = await api_get(page, token, f"/api/user/checkin?month={current_month()}")
        if already_checked(st):
            log.info(f"[{name}] 签到成功（第 {r} 轮确认）")
            return True

    log.error(f"[{name}] 超时未确认签到结果")
    await page.screenshot(path=f"fail_timeout_{tag}.png", full_page=True)
    return False


async def open_site(page) -> str:
    """主站失败自动切备站，返回实际使用的 base url"""
    for url in (SITE_URL, BACKUP_URL):
        if not url:
            continue
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            log.info(f"站点已打开: {url}")
            return url
        except PWTimeout:
            log.warning(f"打开超时: {url}")
        except Exception as e:
            log.warning(f"打开失败 {url}: {type(e).__name__}")
    raise RuntimeError("主站与备用站均无法访问")


async def main() -> bool:
    accounts = load_accounts()
    if not accounts:
        return False

    random.shuffle(accounts)  # 打散顺序，降低行为特征
    log.info(f"待处理账号数: {len(accounts)}")

    state = {"token": None, "user_id": None}
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.set_default_timeout(20_000)

        async def inject_auth(route, request):
            """只拦 /api/，避免干扰 Turnstile 与静态资源"""
            try:
                if state["token"]:
                    headers = dict(request.headers)
                    headers["authorization"] = f"Bearer {state['token']}"
                    if state["user_id"] is not None:
                        headers["new-api-user"] = str(state["user_id"])
                    await route.continue_(headers=headers)
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await context.route("**/api/**", inject_auth)
        page = await context.new_page()

        try:
            await open_site(page)
        except Exception as e:
            log.error(str(e))
            await browser.close()
            return False

        for i, acc in enumerate(accounts):
            log.info(f"===== [{acc['name']}] ({i + 1}/{len(accounts)}) =====")
            ok = False
            try:
                ok = await checkin_one(page, state, acc)
            except PWTimeout:
                log.error(f"[{acc['name']}] 操作超时")
            except Exception as e:
                log.error(f"[{acc['name']}] 异常: {type(e).__name__}: {e}")
            results.append((acc["name"], ok))

            if i < len(accounts) - 1:
                gap = random.uniform(MIN_GAP, MAX_GAP)
                log.info(f"等待 {gap:.0f}s 后处理下一个账号")
                await asyncio.sleep(gap)

        await browser.close()

    ok_count = sum(1 for _, ok in results if ok)
    log.info("===== 汇总 =====")
    for name, ok in results:
        log.info(f"  {name}: {'成功' if ok else '失败'}")
    log.info(f"结果: {ok_count}/{len(results)}")
    return ok_count == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
