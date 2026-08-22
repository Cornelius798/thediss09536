#!/usr/bin/env python3
"""
tabitoken.com 多账号自动签到（GitHub Actions 版）
- Playwright 真浏览器，规避 Cloudflare 挑战
- 每账号独立 BrowserContext，Cookie / localStorage 完全隔离
- Token 经路由拦截注入，不写入 JS 源码、不进日志
"""

import asyncio
import json
import logging
import os
import random
import sys

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

SITE_URL = os.getenv("SITE_URL", "https://tabitoken.com")
BACKUP_URL = os.getenv("BACKUP_URL", "https://tabitoken.cc")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 用 evaluate 的参数传递 token，避免 f-string 拼进 JS 源码
STATUS_JS = """
async ({uid, token}) => {
  const month = new Date().toISOString().slice(0, 7);
  const r = await fetch('/api/user/checkin?month=' + month, {
    headers: {'Authorization': 'Bearer ' + token, 'New-Api-User': uid},
    credentials: 'include',
  });
  let body = null;
  try { body = await r.json(); } catch (e) { body = {parse_error: true}; }
  return {status: r.status, body: body};
}
"""

CHECKIN_JS = """
async ({uid, token}) => {
  const r = await fetch('/api/user/checkin', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + token,
      'New-Api-User': uid,
      'Content-Type': 'application/json',
    },
    body: '{}',
    credentials: 'include',
  });
  let body = null;
  try { body = await r.json(); } catch (e) { body = {parse_error: true}; }
  return {status: r.status, body: body};
}
"""


def load_accounts():
    """解析 TABI_ACCOUNTS，兼容 [..] 与 {"accounts": [..]} 两种形态"""
    raw = (os.getenv("TABI_ACCOUNTS") or "").strip()
    if not raw:
        logging.error("TABI_ACCOUNTS 未设置")
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logging.error(f"TABI_ACCOUNTS 不是合法 JSON: {e}")
        sys.exit(1)

    if isinstance(data, dict):
        data = data.get("accounts", [])
    if not isinstance(data, list):
        logging.error("TABI_ACCOUNTS 顶层应为数组或含 accounts 数组的对象")
        sys.exit(1)

    out = []
    for i, a in enumerate(data, 1):
        name = str(a.get("name") or f"acc{i}")
        token = a.get("token") or a.get("access_token") or ""
        uid = str(a.get("user_id") or "")
        if not token or not uid:
            logging.error(f"[{name}] 缺少 token 或 user_id，跳过")
            continue
        out.append({"name": name, "token": token, "user_id": uid})
    return out


def checked_in_today(res) -> bool:
    body = (res or {}).get("body") or {}
    if not body.get("success"):
        return False
    stats = ((body.get("data") or {}).get("stats") or {})
    return bool(stats.get("checked_in_today"))


async def pass_cloudflare(page, name, tries=8) -> bool:
    """CF 挑战页标题为 'Just a moment...'，等它自己过"""
    for _ in range(tries):
        title = (await page.title()) or ""
        if "just a moment" not in title.lower():
            return True
        logging.info(f"[{name}] Cloudflare 挑战中，等待 5s")
        await page.wait_for_timeout(5000)
    logging.error(f"[{name}] Cloudflare 挑战未通过")
    return False


async def checkin_one(browser, acc, base_url) -> bool:
    name, token, uid = acc["name"], acc["token"], acc["user_id"]
    arg = {"uid": uid, "token": token}
    logging.info(f"[{name}] 站点 {base_url}")

    context = await browser.new_context(
        user_agent=UA,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )

    async def add_auth(route, request):
        headers = {**request.headers}
        if "/api/" in request.url:
            headers["authorization"] = f"Bearer {token}"
            headers["new-api-user"] = uid
        await route.continue_(headers=headers)

    await context.route("**/*", add_auth)
    page = await context.new_page()

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
        if not await pass_cloudflare(page, name):
            return False
        await page.wait_for_timeout(3000)

        # 1. 先查状态，已签到直接结束，不做多余请求
        res = await page.evaluate(STATUS_JS, arg)
        if res["status"] != 200:
            logging.warning(f"[{name}] 状态查询 http={res['status']}")
        elif checked_in_today(res):
            logging.info(f"[{name}] 今日已签到，跳过")
            return True

        # 2. 优先点真实按钮，行为最接近正常用户
        clicked = False
        for sel in ('button:has-text("每日签到")', 'button:has-text("签到")'):
            btn = page.locator(sel).first
            if await btn.count() > 0:
                if not await btn.is_enabled():
                    logging.info(f"[{name}] 按钮已禁用，视为已签到")
                    return True
                await btn.click()
                clicked = True
                logging.info(f"[{name}] 已点击签到按钮")
                break

        # 3. 按钮没渲染出来则直接打接口兜底
        if not clicked:
            logging.info(f"[{name}] 未找到按钮，改用 API 直接签到")
            res = await page.evaluate(CHECKIN_JS, arg)
            body = res.get("body") or {}
            msg = str(body.get("message", ""))
            if res["status"] == 200 and body.get("success"):
                logging.info(f"[{name}] API 签到返回成功")
            elif "已签到" in msg:
                logging.info(f"[{name}] 今日已签到，跳过")
                return True
            else:
                logging.error(f"[{name}] API 签到失败 http={res['status']} msg={msg}")

        # 4. 统一回查状态确认，不信任响应体
        for _ in range(10):
            await page.wait_for_timeout(2000)
            res = await page.evaluate(STATUS_JS, arg)
            if checked_in_today(res):
                stats = ((res["body"].get("data") or {}).get("stats") or {})
                logging.info(f"[{name}] 签到成功 累计={stats.get('total_check_ins', '?')}")
                return True

        logging.error(f"[{name}] 超时未确认签到成功")
        await page.screenshot(path=f"fail_{name}.png")
        return False

    except PWTimeout:
        logging.error(f"[{name}] 页面超时")
        return False
    except Exception as e:
        logging.error(f"[{name}] 异常 {type(e).__name__}: {e}")
        return False
    finally:
        await context.close()


async def main() -> int:
    accounts = load_accounts()
    if not accounts:
        logging.error("无可用账号")
        return 1
    logging.info(f"待处理账号数: {len(accounts)}")

    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            for idx, acc in enumerate(accounts, 1):
                logging.info(f"===== [{acc['name']}] ({idx}/{len(accounts)}) =====")
                ok = False
                for url in (SITE_URL, BACKUP_URL):
                    ok = await checkin_one(browser, acc, url)
                    if ok:
                        break
                    logging.info(f"[{acc['name']}] 该站失败，尝试备用站点")
                results[acc["name"]] = ok

                if idx < len(accounts):
                    delay = random.randint(30, 60)
                    logging.info(f"等待 {delay}s 后处理下一个账号")
                    await asyncio.sleep(delay)
        finally:
            await browser.close()

    logging.info("===== 汇总 =====")
    for k, v in results.items():
        logging.info(f"  {k}: {'成功' if v else '失败'}")
    ok_n = sum(results.values())
    logging.info(f"结果: {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
