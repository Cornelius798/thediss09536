#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASCS Cloud 免费 VPS 自动续期脚本（Playwright / Python）

目标站点: https://cloud.ascsonoway.com （账号 zhou***@live.com，日志中邮箱打码）
目标服务器: "Micro Instances - i5 3470 Micro Free" —— 30 天续期制，
            到期前在 My Servers / Manage 页点击 Renew 续期。

凭据只从环境变量读取: ASCS_EMAIL / ASCS_PASSWORD（GitHub Secrets 注入），
不硬编码、不打印。DRY_RUN=1 时只登录读状态并截图，不点击续期。

退出码:
  0  成功（已续期并验证日期顺延 / 本来就不需要续 / DRY_RUN 正常读到状态）
  2  登录失败
  3  没找到目标服务器
  4  没找到续期入口（且日期解析后认为尚不需要续期时不会到这里）
  5  续期没生效（点了但日期未顺延 / 确认流走不完）
  6  页面/网络未知错误（站点故障、超时）
  7  凭据环境变量缺失

每一步都会在 screenshots/ 目录留截图，本地运行可看；GitHub Actions 侧
默认不上传 artifact（公开仓人人可下载），如确需排障可在手动触发时
把 upload_screenshots 设为 true 再上传。

运行结束时在当前目录写 result.json（status / server / expiry_before /
expiry_after / failed_step / note / dry_run），供工作流末尾的 Telegram
通知步骤读取；路径可用环境变量 RESULT_FILE 覆盖。
"""
import os
import re
import sys
import json
import datetime as dt
from pathlib import Path

BASE = os.environ.get("ASCS_BASE_URL", "https://cloud.ascsonoway.com").rstrip("/")
LOGIN_URL = BASE + "/login"
# 已知的仪表盘/服务器页路径（按优先顺序试）
SERVERS_URLS = [BASE + "/my-servers", BASE + "/my-servers/", BASE + "/servers", BASE + "/dashboard"]
SERVER_MATCH = os.environ.get("ASCS_SERVER_MATCH", "3470")  # 服务器名包含此串
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "TRUE", "yes")
# 距到期 <= 该天数时才执行续期；面板若随时可续，可在环境变量里放宽。默认 10 天。
RENEW_WINDOW_DAYS = int(os.environ.get("RENEW_WINDOW_DAYS", "10"))
# 即使日期解析不出来也尝试点击续期（面板可能不给明确日期）。默认开启：点不到才判失败。
TRY_RENEW_WITHOUT_DATE = os.environ.get("TRY_RENEW_WITHOUT_DATE", "1") == "1"

SHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "screenshots"))
SHOT_DIR.mkdir(parents=True, exist_ok=True)

# 结构化结果摘要：运行结束时写 result.json，供工作流的 Telegram 通知步骤读取。
# status: renewed（已续期）/ not_due（未到续期窗口或干跑）/ failed（失败，看 failed_step）
RESULT_FILE = Path(os.environ.get("RESULT_FILE", "result.json"))
RESULT = {
    "status": "",
    "server": "",
    "expiry_before": "",
    "expiry_after": "",
    "failed_step": "",
    "note": "",
    "dry_run": DRY_RUN,
}
EXIT_STEP = {
    2: "登录失败",
    3: "没找到服务器",
    4: "没找到续期入口",
    5: "续期没生效",
    6: "页面/网络错误",
    7: "凭据环境变量缺失",
}


def write_result() -> None:
    try:
        RESULT_FILE.write_text(
            json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"结果摘要已写入 {RESULT_FILE}")
    except Exception as e:
        log(f"写结果摘要失败: {type(e).__name__}")

DATE_KEYS = re.compile(
    r"(renew|renewal|expir\w*|valid until|due|next billing|term ends|end date|到期|续期|有效期)",
    re.I,
)
DATE_VALUE = re.compile(
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})|(\d{1,2}[-/]\d{1,2}[-/]\d{4})|"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})|"
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
    re.I,
)
RENEW_TEXT = re.compile(r"renew", re.I)


def mask_email(email: str) -> str:
    """z***@live.com 形式打码。"""
    if not email or "@" not in email:
        return "(未设置)"
    name, _, domain = email.partition("@")
    return (name[:1] or "?") + "***@" + domain


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


def parse_date(text: str):
    """把各种日期字符串解析成 date，失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    fmts = [
        "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y",
        "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
        "%d %B %Y", "%d %b %Y",
    ]
    for fmt in fmts:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def extract_expiry(page_text: str):
    """
    在页面全文里找与 renew/expire 等关键词相邻的日期。
    返回 (date_or_None, 原始命中片段)。
    """
    if not page_text:
        return None, ""
    # 先在含关键词的行/片段里找
    chunks = re.split(r"[\n\r]+", page_text)
    for chunk in chunks:
        if DATE_KEYS.search(chunk):
            m = DATE_VALUE.search(chunk)
            if m:
                d = parse_date(m.group(0))
                if d:
                    return d, chunk.strip()[:160]
    # 兜底：全文找第一个像日期的值（可能误判，仅用于日志参考）
    m = DATE_VALUE.search(page_text)
    if m:
        d = parse_date(m.group(0))
        if d:
            return d, "(全文兜底命中) " + m.group(0)
    return None, ""


class Shot:
    def __init__(self, page):
        self.page = page
        self.n = 0

    def take(self, name: str):
        self.n += 1
        path = SHOT_DIR / f"{self.n:02d}-{name}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            log(f"截图: {path.name}")
        except Exception as e:  # 截图失败不致命
            log(f"截图失败({name}): {type(e).__name__}")
        return path


def first_visible(page, selectors, timeout=2500):
    """按选择器列表顺序找第一个可见元素，返回 locator 或 None。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            continue
    return None


def click_by_text(page, patterns, timeout=4000):
    """
    按文本/正则找可点击元素（button / a / [role=button]）并点击。
    patterns: list[re.Pattern|str]，顺序即优先级。返回命中的文本或 None。
    """
    roles = ["button", "link", "menuitem", "tab"]
    for pat in patterns:
        for role in roles:
            try:
                loc = page.get_by_role(role, name=pat).first
                if loc.is_visible(timeout=timeout):
                    txt = (loc.inner_text(timeout=1000) or str(pat)).strip()
                    loc.click()
                    return txt or str(pat)
            except Exception:
                continue
        # 兜底：任意元素文本匹配
        try:
            loc = page.locator(f"text={pat}").first if isinstance(pat, str) else \
                page.locator("button, a, [role=button]").filter(has_text=pat).first
            if loc.is_visible(timeout=timeout):
                txt = (loc.inner_text(timeout=1000) or "").strip()
                loc.click()
                return txt or str(pat)
        except Exception:
            continue
    return None


def wait_idle(page, timeout=20000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass


def do_login(page, shot, email: str, password: str) -> bool:
    """登录，带 3 次重试（站点历史上偶发无响应）。"""
    email_sels = [
        "input[type='email']",
        "input[name='email']",
        "input[id*='email' i]",
        "input[placeholder*='mail' i]",
        "input[autocomplete='username']",
        "input[name='username']",
    ]
    pass_sels = [
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
    ]
    submit_sels = [
        "button[type='submit']",
        "input[type='submit']",
    ]
    for attempt in range(1, 4):
        try:
            log(f"登录尝试 {attempt}/3 -> {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            wait_idle(page)
            shot.take(f"login-page-{attempt}")
            em = first_visible(page, email_sels)
            pw = first_visible(page, pass_sels)
            if em is None or pw is None:
                log("登录页未找到邮箱/密码输入框（可能已登录或页面改版）")
                # 可能已处于登录态
                if "login" not in page.url:
                    return True
                page.wait_for_timeout(3000)
                continue
            em.fill(email)
            pw.fill(password)
            shot.take(f"login-filled-{attempt}")
            btn = first_visible(page, submit_sels, timeout=1500)
            if btn is not None:
                btn.click()
            else:
                clicked = click_by_text(page, [re.compile(r"sign in|log in|login", re.I)])
                if not clicked:
                    pw.press("Enter")
            wait_idle(page, timeout=30000)
            page.wait_for_timeout(2500)
            shot.take(f"after-login-{attempt}")
            # 成功判据：URL 离开 /login，或页面出现登出/服务器字样
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass
            if "login" not in page.url or re.search(r"log ?out|my servers|dashboard", body, re.I):
                log("登录成功")
                return True
            log("登录未确认成功（仍在登录页或无仪表盘特征）")
        except Exception as e:
            log(f"登录尝试 {attempt} 异常: {type(e).__name__}: {str(e)[:120]}")
        page.wait_for_timeout(3000)
    return False


def goto_servers(page, shot) -> bool:
    """打开服务器列表页。多路径兜底 + 导航链接兜底。"""
    for url in SERVERS_URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_idle(page)
            if "login" in page.url:
                continue
            body = page.locator("body").inner_text(timeout=8000)
            if re.search(r"server|vps|instance", body, re.I):
                log(f"服务器列表页: {url}")
                shot.take("servers-page")
                return True
        except Exception as e:
            log(f"打开 {url} 失败: {type(e).__name__}")
    # 导航链接兜底
    clicked = click_by_text(page, [re.compile(r"my servers|servers|my vps|instances", re.I)])
    if clicked:
        wait_idle(page)
        shot.take("servers-page-via-nav")
        return True
    return False


def find_server_container(page):
    """
    定位目标服务器的卡片/行容器（包含 SERVER_MATCH 的最小可点击祖先）。
    返回 (container_locator, 卡片可见文本) 或 (None, 全文)。
    """
    txt_loc = page.locator(f"text=/{re.escape(SERVER_MATCH)}/").first
    try:
        if not txt_loc.is_visible(timeout=6000):
            return None, ""
    except Exception:
        return None, ""
    # 向上找卡片容器：依次尝试祖先层级，取第一个同时含日期或按钮的
    node = txt_loc
    for _ in range(6):
        try:
            node = node.locator("xpath=..")
            text = node.inner_text(timeout=1000)
            if re.search(r"renew|manage|view|expir|due|\d{4}[-/]\d{1,2}[-/]\d{1,2}", text, re.I):
                return node, text
        except Exception:
            break
    try:
        return txt_loc, txt_loc.inner_text(timeout=1000)
    except Exception:
        return txt_loc, ""


def open_server_detail(page, shot, container) -> bool:
    """点击卡片内的 Manage/View/Details 或卡片本身进入详情页。"""
    clicked = None
    if container is not None:
        for pat in [re.compile(r"manage", re.I), re.compile(r"view details|details", re.I),
                    re.compile(r"view", re.I), re.compile(r"open", re.I)]:
            try:
                loc = container.get_by_role("button", name=pat).first
                if loc.is_visible(timeout=1200):
                    loc.click()
                    clicked = str(pat)
                    break
            except Exception:
                continue
            try:
                loc = container.get_by_role("link", name=pat).first
                if loc.is_visible(timeout=1200):
                    loc.click()
                    clicked = str(pat)
                    break
            except Exception:
                continue
    if not clicked:
        clicked = click_by_text(page, [re.compile(r"manage", re.I),
                                       re.compile(r"view details|details", re.I)])
    if not clicked and container is not None:
        try:
            container.click()
            clicked = "container"
        except Exception:
            pass
    if clicked:
        wait_idle(page)
        page.wait_for_timeout(1500)
        shot.take("server-detail")
        log(f"已进入服务器详情/管理页（via {clicked}）")
        return True
    log("未能打开服务器详情页，停留在列表页继续尝试")
    return False


def find_renew_button(page, container=None):
    """在详情页（或卡片容器）里定位 Renew 按钮。返回 (点击函数, 描述)。"""
    scopes = []
    if container is not None:
        scopes.append(container)
    scopes.append(page)
    for scope in scopes:
        for role in ("button", "link"):
            try:
                loc = scope.get_by_role(role, name=RENEW_TEXT).first
                if loc.is_visible(timeout=1500):
                    return loc, f"{role}:renew"
            except Exception:
                continue
        try:
            loc = scope.locator("button, a, [role=button]").filter(has_text=RENEW_TEXT).first
            if loc.is_visible(timeout=1500):
                return loc, "filter:renew"
        except Exception:
            continue
    return None, ""


def confirm_flow(page, shot) -> bool:
    """
    续期确认流：可能弹 modal / 跳确认页。循环找确认类按钮点掉，
    直到出现成功字样或无可点按钮。返回是否观察到成功信号。
    """
    confirm_pats = [
        re.compile(r"confirm|yes|proceed|continue", re.I),
        re.compile(r"renew", re.I),
        re.compile(r"submit|ok", re.I),
    ]
    for i in range(4):
        page.wait_for_timeout(1200)
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            pass
        if re.search(r"renew(ed)? successfully|renewal (complete|successful)|successfully renewed|已续期|续期成功", body, re.I):
            shot.take("renew-success")
            return True
        clicked = click_by_text(page, confirm_pats, timeout=2500)
        if clicked:
            log(f"确认流点击: {clicked!r}")
            shot.take(f"renew-confirm-{i}")
            wait_idle(page)
            continue
        break
    return False


def read_current_expiry(page, container=None):
    """从详情页全文（优先）/ 卡片文本里提取到期日。"""
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=8000)
    except Exception:
        pass
    d, frag = extract_expiry(text)
    if d:
        return d, frag, text
    if container is not None:
        try:
            ctext = container.inner_text(timeout=1500)
            d, frag = extract_expiry(ctext)
            if d:
                return d, frag, text
        except Exception:
            pass
    return None, frag, text


def run() -> int:
    email = os.environ.get("ASCS_EMAIL", "").strip()
    password = os.environ.get("ASCS_PASSWORD", "")
    if not email or not password:
        RESULT["failed_step"] = "凭据环境变量缺失（ASCS_EMAIL / ASCS_PASSWORD 未配置）"
        log("缺少环境变量 ASCS_EMAIL / ASCS_PASSWORD（请在 GitHub Secrets 里配置）")
        return 7
    log(f"账号: {mask_email(email)} | DRY_RUN={DRY_RUN} | 续期窗口: 到期前 {RENEW_WINDOW_DAYS} 天")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        RESULT["failed_step"] = "playwright 未安装"
        log("playwright 未安装")
        return 6

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
        )
        ctx.set_default_timeout(30000)
        page = ctx.new_page()
        shot = Shot(page)
        try:
            # 1) 登录
            if not do_login(page, shot, email, password):
                RESULT["failed_step"] = "登录失败（3 次重试后仍未进入仪表盘）"
                log("失败阶段: 登录失败（3 次重试后仍未进入仪表盘）")
                shot.take("fail-login")
                return 2

            # 2) 服务器列表
            if not goto_servers(page, shot):
                RESULT["failed_step"] = "打不开服务器列表页"
                log("失败阶段: 打不开服务器列表页")
                shot.take("fail-servers-page")
                return 6

            container, card_text = find_server_container(page)
            if container is None:
                # 列表异步加载时再等一轮
                page.wait_for_timeout(4000)
                container, card_text = find_server_container(page)
            if container is None:
                RESULT["failed_step"] = f"没找到服务器（列表里没有包含 {SERVER_MATCH!r} 的条目）"
                log(f"失败阶段: 没找到服务器（列表里没有包含 {SERVER_MATCH!r} 的条目）")
                shot.take("fail-server-not-found")
                return 3
            RESULT["server"] = (
                card_text.splitlines()[0].strip()[:120]
                if card_text
                else f"Micro Instances - i5 {SERVER_MATCH} Micro Free"
            )
            log(f"找到服务器卡片: {card_text.splitlines()[0][:120] if card_text else '(无文本)'}")

            # 3) 进详情页（失败也不致命，可在卡片上找续期）
            open_server_detail(page, shot, container)

            # 详情页里重新定位容器（Renew 可能在详情页）
            detail_container = None
            try:
                loc = page.locator(f"text=/{re.escape(SERVER_MATCH)}/").first
                if loc.is_visible(timeout=3000):
                    detail_container = loc.locator("xpath=..")
            except Exception:
                pass

            # 4) 读到期日
            expiry, frag, _ = read_current_expiry(page, detail_container or container)
            today = dt.datetime.now(dt.timezone.utc).date()
            if expiry:
                RESULT["expiry_before"] = str(expiry)
                days_left = (expiry - today).days
                log(f"当前到期/续期日: {expiry} （剩余 {days_left} 天；依据: {frag!r}）")
            else:
                log("未能从页面解析出到期日期（将按 TRY_RENEW_WITHOUT_DATE 策略决定是否尝试续期）")

            need_renew = False
            if expiry is None:
                need_renew = TRY_RENEW_WITHOUT_DATE
            elif (expiry - today).days <= RENEW_WINDOW_DAYS:
                need_renew = True
            log(f"需要续期: {need_renew}")

            if not need_renew:
                RESULT["status"] = "not_due"
                if expiry:
                    RESULT["note"] = f"距到期还有 {(expiry - today).days} 天，未进入续期窗口"
                log("结论: 尚未进入续期窗口，本次不操作。")
                shot.take("done-no-renew-needed")
                return 0

            # 5) 找续期入口
            renew_loc, how = find_renew_button(page, detail_container or container)
            if renew_loc is None and detail_container is None:
                renew_loc, how = find_renew_button(page, container)
            if renew_loc is None:
                # 有时续期入口在 Actions / 更多菜单里
                click_by_text(page, [re.compile(r"actions|more|manage", re.I)], timeout=2000)
                page.wait_for_timeout(1000)
                renew_loc, how = find_renew_button(page)
            if renew_loc is None:
                RESULT["failed_step"] = "没找到续期入口（页面无 Renew 按钮/链接）"
                log("失败阶段: 没找到续期入口（页面无 Renew 按钮/链接）")
                shot.take("fail-no-renew-button")
                return 4
            log(f"找到续期入口: {how}")

            if DRY_RUN:
                RESULT["status"] = "not_due"
                RESULT["note"] = "干跑：已定位续期入口，未点击"
                log("DRY_RUN: 已定位续期入口，按要求不点击。")
                shot.take("dry-run-renew-located")
                return 0

            # 6) 点续期 + 确认流
            try:
                renew_loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            renew_loc.click()
            log("已点击 Renew")
            shot.take("after-renew-click")
            wait_idle(page)
            success_signal = confirm_flow(page, shot)

            # 7) 回读验证日期顺延
            page.wait_for_timeout(2000)
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                wait_idle(page)
            except Exception:
                pass
            shot.take("after-renew-recheck")
            new_expiry, nfrag, _ = read_current_expiry(page, detail_container or container)
            if new_expiry:
                RESULT["expiry_after"] = str(new_expiry)
                log(f"回读到期/续期日: {new_expiry} （依据: {nfrag!r}）")
            if expiry and new_expiry and new_expiry > expiry:
                RESULT["status"] = "renewed"
                log(f"续期成功: {expiry} -> {new_expiry}")
                return 0
            if success_signal and (expiry is None or new_expiry is None):
                RESULT["status"] = "renewed"
                RESULT["note"] = "页面出现成功字样，但日期无法回读比对"
                log("续期大概率成功（页面出现成功字样，但日期无法回读比对）")
                return 0
            if expiry is None and new_expiry is not None:
                RESULT["status"] = "renewed"
                log(f"续期完成（续前日期未知，续后到期日 {new_expiry}）")
                return 0
            RESULT["failed_step"] = "续期没生效（点击并走完确认流后，到期日未顺延）"
            log("失败阶段: 续期没生效（点击并走完确认流后，到期日未顺延）")
            return 5
        except Exception as e:
            RESULT["failed_step"] = f"未知错误 {type(e).__name__}: {str(e)[:120]}"
            log(f"失败阶段: 未知错误 {type(e).__name__}: {str(e)[:200]}")
            try:
                shot.take("fail-exception")
            except Exception:
                pass
            return 6
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass


def main() -> int:
    """跑 run() 并保证 result.json 落盘（供 Telegram 通知步骤读取）。"""
    code = 6
    try:
        code = run()
    except Exception as e:  # run() 内部已分阶段兜底，这里只防万一
        RESULT["failed_step"] = f"未捕获异常 {type(e).__name__}: {str(e)[:120]}"
        code = 6
    if not RESULT["status"]:
        RESULT["status"] = "not_due" if code == 0 else "failed"
    if RESULT["status"] == "failed" and not RESULT["failed_step"]:
        RESULT["failed_step"] = EXIT_STEP.get(code, f"退出码 {code}")
    write_result()
    return code


if __name__ == "__main__":
    sys.exit(main())
