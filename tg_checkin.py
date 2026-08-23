import asyncio
import base64
import logging
import os
import random
import re
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

# 测试模式：本地手动测试时设 TEST_MODE=1，跳过概率和延迟
TEST_MODE = os.getenv('TEST_MODE', '') == '1'

if not TEST_MODE:
    # ============ 概率签到 ============
    CHECKIN_PROBABILITY = 3 / 7
    if random.random() > CHECKIN_PROBABILITY:
        logging.info('🎲 今天抽到休息日，跳过')
        sys.exit(0)

    # ============ 随机延迟 ============
    DELAY_MINUTES = random.uniform(0, 300)
    logging.info(f'⏳ 随机延迟 {DELAY_MINUTES:.1f} 分钟...')
else:
    logging.info('🧪 测试模式：跳过概率与延迟')


def solve_math(text: str):
    """从消息里提取算术题并计算，支持 + - × ÷"""
    m = re.search(r'(-?\d+)\s*([+\-×xX*÷/])\s*(-?\d+)\s*=', text)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    try:
        if op == '+':
            return str(a + b)
        if op == '-':
            return str(a - b)
        if op in ('×', 'x', 'X', '*'):
            return str(a * b)
        if op in ('÷', '/'):
            if b == 0:
                return None
            r = a / b
            return str(int(r)) if r == int(r) else str(round(r, 2))
    except Exception:
        pass
    return None


async def main():
    if not API_ID or not API_HASH or not SESSION_B64:
        logging.error('❌ 缺少环境变量')
        sys.exit(1)

    if not TEST_MODE:
        await asyncio.sleep(DELAY_MINUTES * 60)

    with open('tg_session.session', 'wb') as f:
        f.write(base64.b64decode(SESSION_B64))

    client = TelegramClient('tg_session', API_ID, API_HASH)
    await client.start()
    logging.info('✅ TG 登录成功')

    bot = await client.get_entity(BOT_USERNAME)

    # ===== 第一步：发送 /start，点签到按钮 =====
    await client.send_message(bot, '/start')
    logging.info('📤 已发送 /start')
    await asyncio.sleep(random.uniform(3, 6))

    msgs = await client.get_messages(bot, limit=5)
    clicked = False
    for msg in msgs:
        if msg.reply_markup and hasattr(msg.reply_markup, 'rows'):
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    if '签到' in button.text:
                        logging.info(f'🖱️ 点击: {button.text}')
                        await msg.click(data=button.data)
                        clicked = True
                        break
                if clicked:
                    break
        if clicked:
            break

    if not clicked:
        logging.warning('⚠️ 未找到签到按钮')
        sys.exit(1)

    # ===== 第二步：等题目出现，解析并点答案 =====
    await asyncio.sleep(random.uniform(3, 6))

    quiz_msgs = await client.get_messages(bot, limit=5)
    answer = None
    quiz_msg = None
    for qm in quiz_msgs:
        if qm.message and ('?' in qm.message or '？' in qm.message):
            answer = solve_math(qm.message)
            if answer is not None:
                quiz_msg = qm
                break

    if answer is None:
        # 没有题目 —— 可能直接签到成功了（无答题模式）
        for rm in quiz_msgs:
            if rm.message and ('成功' in rm.message or '已经' in rm.message):
                logging.info('✅ 无需答题，直接完成')
                sys.exit(0)
        logging.warning('⚠️ 未识别到算术题')
        sys.exit(1)

    logging.info(f'🧮 计算答案: {answer}')

    # 点正确答案按钮
    answered = False
    if quiz_msg.reply_markup and hasattr(quiz_msg.reply_markup, 'rows'):
        for row in quiz_msg.reply_markup.rows:
            for button in row.buttons:
                btn_text = button.text.strip()
                # 提取按钮里的数字（可能带 emoji 或其他符号）
                nums = re.findall(r'-?\d+(?:\.\d+)?', btn_text)
                if nums and nums[0] == answer:
                    logging.info(f'🖱️ 点击答案: {btn_text}')
                    await quiz_msg.click(data=button.data)
                    answered = True
                    break
            if answered:
                break

    if not answered:
        logging.error('❌ 未找到正确答案按钮')
        sys.exit(1)

    # ===== 第三步：确认结果 =====
    await asyncio.sleep(random.uniform(3, 6))
    result_msgs = await client.get_messages(bot, limit=3)
    for rm in result_msgs:
        if rm.message:
            if '成功' in rm.message:
                logging.info('✅ 签到成功')
                sys.exit(0)
            elif '已经' in rm.message:
                logging.info('✅ 今日已签到')
                sys.exit(0)

    logging.warning('⚠️ 未确认结果，但流程已走完')
    sys.exit(0)


if __name__ == '__main__':
    asyncio.run(main())
