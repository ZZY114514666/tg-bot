#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 精简双向中转 bot（Zeabur 适配，指定回复）
import os, sys, time, asyncio, logging
from typing import Dict, Tuple
from telegram import Message, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")
except:
    ADMIN_ID = 0

if not BOT_TOKEN or ADMIN_ID == 0:
    logger.error("请在环境变量中设置 BOT_TOKEN 和 ADMIN_ID（数字），然后重启。")
    sys.exit(1)

# mapping: (admin_chat_id, admin_msg_id) -> user_id
admin_msg_to_user: Dict[Tuple[int,int], int] = {}
# optional: user -> last_admin_msg_id
user_last_admin_msg: Dict[int,int] = {}

# safe copy with simple retry for RetryAfter
async def safe_copy(msg: Message, chat_id: int, retries: int = 4):
    for i in range(retries):
        try:
            return await msg.copy(chat_id=chat_id)
        except RetryAfter as e:
            wait = e.retry_after + 0.5
            logger.warning(f"RetryAfter to {chat_id}, waiting {wait}s")
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.exception("TelegramError on copy")
            if i == retries - 1:
                raise
            await asyncio.sleep(0.2 * (i + 1))
    raise RuntimeError("safe_copy failed")

# when a normal user sends anything -> forward (copy) to admin and remember mapping
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id == ADMIN_ID:
        return
    try:
        copied = await safe_copy(msg, chat_id=ADMIN_ID)
        admin_msg_to_user[(copied.chat.id, copied.message_id)] = user.id
        user_last_admin_msg[user.id] = copied.message_id
        logger.info(f"Forwarded user {user.id} -> admin msg {copied.message_id}")
    except Exception:
        logger.exception("forward to admin failed")
        await msg.reply_text("发送失败，请稍后再试。")

# when admin sends a message: if it's a reply to a forwarded message -> route to original user
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != ADMIN_ID:
        return
    reply = msg.reply_to_message
    if reply:
        key = (reply.chat.id, reply.message_id)
        if key in admin_msg_to_user:
            target = admin_msg_to_user[key]
            try:
                copied = await safe_copy(msg, chat_id=target)
                user_last_admin_msg[target] = copied.message_id
                await msg.reply_text(f"✅ 已发送给用户 {target}")
            except Exception:
                logger.exception("admin->user copy failed")
                await msg.reply_text("发送失败。")
            return
    await msg.reply_text("请回复某条用户消息来单独回复，或用 /reply <user_id> <文本> 指定回复。")

# /reply user_id text
async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("用法：/reply <user_id> <消息>")
        return
    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("user_id 必须为数字")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        await update.message.reply_text("已发送。")
    except Exception:
        logger.exception("reply send failed")
        await update.message.reply_text("发送失败（对方可能未启动 bot）。")

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # user messages (not admin) forwarded
    app.add_handler(MessageHandler(filters.ALL & (~filters.User(ADMIN_ID)), handle_user_message))
    # admin messages handled separately
    app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_ID), handle_admin_message))
    app.add_handler(CommandHandler("reply", reply_cmd))
    return app

def main():
    app = build_app()
    logger.info("Bot started (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
