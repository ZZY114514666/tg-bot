#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 精简双向中转 bot（约100 行）
import os, sys, time, logging, asyncio
from typing import Dict, Tuple
from telegram import Update, Message
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    MessageHandler, CommandHandler, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN") or "PUT_YOUR_TOKEN"
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
except:
    ADMIN_ID = 0

if BOT_TOKEN.startswith("PUT_YOUR") or ADMIN_ID == 0:
    logger.error("请在环境变量中设置 BOT_TOKEN 和 ADMIN_ID（数字）后再运行。")
    sys.exit(1)

# (admin_chat_id, admin_msg_id) -> user_id
admin_msg_to_user: Dict[Tuple[int,int], int] = {}
# user -> last admin-side message id (可选)
user_last_admin_msg = {}

# 简单 safe copy with RetryAfter
async def safe_copy(msg: Message, chat_id: int, retries=4):
    for i in range(retries):
        try:
            copied = await msg.copy(chat_id=chat_id)
            return copied
        except RetryAfter as e:
            wait = e.retry_after + 0.5
            logger.warning(f"RetryAfter to {chat_id}, wait {wait}s")
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.exception("TelegramError copy")
            if i == retries-1:
                raise
            await asyncio.sleep(0.2*(i+1))
    raise RuntimeError("safe_copy failed")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id = update.effective_user.id
    # auto forward to admin
    try:
        copied = await safe_copy(msg, chat_id=ADMIN_ID)
        admin_msg_to_user[(copied.chat.id, copied.message_id)] = user_id
        user_last_admin_msg[user_id] = copied.message_id
        logger.info(f"Forwarded user {user_id} -> admin as msg {copied.message_id}")
    except Exception as e:
        logger.exception("forward to admin failed")
        await msg.reply_text("发送失败，稍后重试。")

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    admin = update.effective_user.id
    if admin != ADMIN_ID:
        return
    # If admin replies to a forwarded message -> route to original user
    reply = msg.reply_to_message
    if reply:
        key = (reply.chat.id, reply.message_id)
        if key in admin_msg_to_user:
            target = admin_msg_to_user[key]
            try:
                copied = await safe_copy(msg, chat_id=target)
                user_last_admin_msg[target] = copied.message_id
                await msg.reply_text(f"已发送给用户 {target}")
            except Exception as e:
                logger.exception("admin->user copy failed")
                await msg.reply_text("发送失败")
            return
    # else, not a reply -> hint admin how to reply
    await msg.reply_text("请回复某条用户消息来发送，或用 /reply <user_id> <消息> 指定回复。")

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
    except Exception as e:
        logger.exception("reply_cmd failed")
        await update.message.reply_text("发送失败。")

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # 用户消息（非管理员） -> forward
    app.add_handler(MessageHandler(filters.ALL & (~filters.User(ADMIN_ID)), handle_user_message))
    # 管理员消息 -> route (reply handling)
    app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_ID), handle_admin_message))
    app.add_handler(CommandHandler("reply", reply_cmd))
    return app

def main():
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
