import asyncio
import base64
import logging
import os
import random
import sys

from telethon import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

API_ID = int(os.getenv('TG_API_ID', '0'))
API_HASH = os.getenv('TG_API_HASH', '')
BOT_USERNAME = 'lfreeai_bot'
SESSION_B64 = os.getenv('TG_SESSION_B64', '')

# ============ 随机延迟 ============
# 0~360 分钟（0~6小时），签到时间落在 12:00~18:00 之间
DELAY_MINUTES = random.uniform(0, 360)
logging.info(f'⏳ 随机延迟 {DELAY_MINUTES:.1f} 分钟...')
# =================================


async def checkin():
    if not API_ID or not API_HASH or not SESSION_B64:
        logging.error('❌ 缺少环境变量')
        return False

    with open('tg_session.session', 'wb') as f:
        f.write(base64.b64decode(SESSION_B64))

    client = TelegramClient('tg_session', API_ID, API_HASH)
    await client.start()
    logging.info('✅ TG 登录成功')

    bot = await client.get_entity(BOT_USERNAME)

    await client.send_message(bot, '/start')
    logging.info('📤 已发送 /start')

    await asyncio.sleep(random.uniform(3, 6))

    msgs = await client.get_messages(bot, limit=5)
    for msg in msgs:
        if msg.reply_markup:
            if hasattr(msg.reply_markup, 'rows'):
                for row in msg.reply_markup.rows:
                    for button in row.buttons:
                        if '签到' in button.text:
                            logging.info(f'🖱️ 点击: {button.text}')
                            await msg.click(data=button.data)

                            await asyncio.sleep(random.uniform(3, 6))

                            result_msgs = await client.get_messages(bot, limit=3)
                            for rm in result_msgs:
                                if rm.message and ('签到' in rm.message or '余额' in rm.message):
                                    if '成功' in rm.message:
                                        logging.info('✅ 签到成功')
                                        return True
                                    elif '已经' in rm.message:
                                        logging.info('✅ 今日已签到')
                                        return True
                                    else:
                                        logging.info('📩 收到回复')
                            return True

    logging.warning('⚠️ 未找到签到按钮')
    return False


async def main():
    await asyncio.sleep(DELAY_MINUTES * 60)
    success = await checkin()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    asyncio.run(main())
