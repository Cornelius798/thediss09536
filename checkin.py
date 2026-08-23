#!/usr/bin/env python3
"""
tabitoken.com 自动签到（GitHub Actions 公开库版）
- Playwright 无头浏览器
- Token 注入认证
- 日志不打印任何敏感信息
"""

import asyncio
import logging
import os
import sys

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

TOKEN = os.getenv('TABI_TOKEN', '')
USER_ID = os.getenv('TABI_USER_ID', '')
SITE_URL = os.getenv('SITE_URL', 'https://tabitoken.com')
BACKUP_URL = os.getenv('BACKUP_URL', 'https://tabitoken.cc')


async def do_checkin(base_url: str) -> bool:
    logging.info(f'🌐 尝试站点: {base_url}')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )

        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
            locale='zh-CN',
        )

        async def add_auth(route, request):
            headers = {**request.headers}
            if '/api/' in request.url:
                headers['authorization'] = f'Bearer {TOKEN}'
                headers['new-api-user'] = USER_ID
            await route.continue_(headers=headers)

        await context.route('**/*', add_auth)
        page = await context.new_page()

        try:
            await page.goto(base_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)
            logging.info(f'✅ 页面加载完成')

            # 查签到状态
            status = await page.evaluate(f'''
                async () => {{
                    const resp = await fetch('/api/user/checkin?month=' + new Date().toISOString().slice(0,7), {{
                        headers: {{
                            'Authorization': 'Bearer {TOKEN}',
                            'New-Api-User': '{USER_ID}'
                        }}
                    }});
                    return await resp.json();
                }}
            ''')

            if status.get('success'):
                stats = status.get('data', {}).get('stats', {})
                if stats.get('checked_in_today'):
                    logging.info('✅ 今日已签到')
                    return True

            logging.info('🔘 未签到，尝试点击按钮...')

            btn = page.locator('button:has-text("每日签到")').first
            if await btn.count() == 0:
                btn = page.locator('button:has-text("签到")').first

            if await btn.count() == 0:
                logging.error('❌ 未找到签到按钮')
                await page.screenshot(path='no_button.png')
                return False

            if not await btn.is_enabled():
                logging.info('⚪ 按钮已禁用')
                return True

            await btn.click()
            logging.info('🖱️ 已点击签到按钮')

            # 等待结果
            for i in range(15):
                await page.wait_for_timeout(2000)
                result = await page.evaluate(f'''
                    async () => {{
                        const resp = await fetch('/api/user/checkin?month=' + new Date().toISOString().slice(0,7), {{
                            headers: {{
                                'Authorization': 'Bearer {TOKEN}',
                                'New-Api-User': '{USER_ID}'
                            }}
                        }});
                        return await resp.json();
                    }}
                ''')
                if result.get('success') and result.get('data', {}).get('stats', {}).get('checked_in_today'):
                    logging.info('✅ 签到成功！')
                    return True

            logging.error('⏰ 超时未确认签到成功')
            await page.screenshot(path='timeout.png')
            return False

        except PWTimeout:
            logging.error(f'⏰ 超时')
            return False
        except Exception as e:
            logging.error(f'💥 异常: {type(e).__name__}')
            return False
        finally:
            await browser.close()


async def main():
    if not TOKEN or not USER_ID:
        logging.error('❌ 缺少环境变量')
        return False

    for url in [SITE_URL, BACKUP_URL]:
        ok = await do_checkin(url)
        if ok:
            return True
        logging.info('❌ 该站失败，尝试下一个...')

    logging.error('❌ 所有站点均失败')
    return False


if __name__ == '__main__':
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
