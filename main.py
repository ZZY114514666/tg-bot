#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Final stable Telegram two-way relay bot with media support.
- Supports text / photo / video / document / voice / audio / animation / sticker etc via Message.copy()
- Admins identified by username(s) (e.g. "ap114514666"); can /register_admin as fallback to save numeric id
- Persistent sqlite (state.db) for banned/pending/active
- Uses asyncio.to_thread for DB ops to avoid blocking
- Mapping admin-side (chat_id, message_id) -> user_id so admin reply routing is unambiguous
- Safe copy with RetryAfter handling and a simple token-bucket rate limiter
"""

import os
import sys
import time
import logging
import sqlite3
import asyncio
from typing import Dict, Set, Optional, Tuple, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "PUT_YOUR_BOT_TOKEN_HERE"
# Admin usernames (no @). Add multiples if needed.
ADMIN_USERNAMES: List[str] = ["ap114514666"]

# SQLite DB file (persistent)
DB_FILE = "state.db"

# Simple rate limiting params for copy operations
MAX_COPY_PER_SECOND = 5  # conservative
COPY_BUCKET_FILL_RATE = MAX_COPY_PER_SECOND
COPY_BUCKET_CAPACITY = MAX_COPY_PER_SECOND

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------- RUNTIME STATE ----------------
# admin-side message mapping: (chat_id, message_id) -> user_id
admin_msgid_to_user: Dict[Tuple[int, int], int] = {}
# user -> last admin-side message id (for convenience)
user_last_admin_msgid: Dict[int, int] = {}

# numeric admin ids resolved at runtime
numeric_admin_ids: Set[int] = set()

# in-memory mirrors of DB tables (kept consistent with DB, protected by state_lock)
pending_requests: Set[int] = set()
active_sessions: Set[int] = set()

# asyncio locks
state_lock = asyncio.Lock()
copy_lock = asyncio.Lock()

# token bucket state for copy rate limiting
_bucket_tokens = COPY_BUCKET_CAPACITY
_bucket_last = time.time()

# ---------------- SYNCHRONOUS DB HELPERS (run in thread) ----------------
def _init_db_sync():
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY, ts INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS pending (user_id INTEGER PRIMARY KEY, username TEXT, ts INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS active (user_id INTEGER PRIMARY KEY, username TEXT, ts INTEGER)")
        conn.commit()
    finally:
        conn.close()

def _db_add_pending_sync(user_id: int, username: Optional[str]):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO pending(user_id, username, ts) VALUES (?,?,?)", (user_id, username or "", int(time.time())))
        conn.commit()
    finally:
        conn.close()

def _db_remove_pending_sync(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM pending WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def _db_get_all_pending_sync() -> List[Tuple[int, str]]:
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM pending")
        rows = c.fetchall()
        return rows
    finally:
        conn.close()

def _db_add_active_sync(user_id: int, username: Optional[str]):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO active(user_id, username, ts) VALUES (?,?,?)", (user_id, username or "", int(time.time())))
        conn.commit()
    finally:
        conn.close()

def _db_remove_active_sync(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM active WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def _db_get_all_active_sync() -> List[Tuple[int, str]]:
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM active")
        rows = c.fetchall()
        return rows
    finally:
        conn.close()

def _db_ban_sync(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO banned(user_id, ts) VALUES (?,?)", (user_id, int(time.time())))
        conn.commit()
    finally:
        conn.close()

def _db_unban_sync(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def _db_is_banned_sync(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM banned WHERE user_id=? LIMIT 1", (user_id,))
        r = c.fetchone()
        return r is not None
    finally:
        conn.close()

# async wrappers using thread pool
async def init_db():
    await asyncio.to_thread(_init_db_sync)

async def db_add_pending(user_id: int, username: Optional[str]):
    await asyncio.to_thread(_db_add_pending_sync, user_id, username)

async def db_remove_pending(user_id: int):
    await asyncio.to_thread(_db_remove_pending_sync, user_id)

async def db_get_all_pending():
    return await asyncio.to_thread(_db_get_all_pending_sync)

async def db_add_active(user_id: int, username: Optional[str]):
    await asyncio.to_thread(_db_add_active_sync, user_id, username)

async def db_remove_active(user_id: int):
    await asyncio.to_thread(_db_remove_active_sync, user_id)

async def db_get_all_active():
    return await asyncio.to_thread(_db_get_all_active_sync)

async def db_ban(user_id: int):
    await asyncio.to_thread(_db_ban_sync, user_id)

async def db_unban(user_id: int):
    await asyncio.to_thread(_db_unban_sync, user_id)

async def db_is_banned(user_id: int) -> bool:
    return await asyncio.to_thread(_db_is_banned_sync, user_id)

# ---------------- KEYBOARDS ----------------
def user_main_keyboard(is_pending: bool, is_active: bool) -> InlineKeyboardMarkup:
    if is_active:
        kb = [[InlineKeyboardButton("🔚 结束聊天", callback_data="user_end")]]
    elif is_pending:
        kb = [[InlineKeyboardButton("⏳ 取消申请", callback_data="user_cancel")]]
    else:
        kb = [[InlineKeyboardButton("📨 申请与管理员连接", callback_data="user_apply")]]
    return InlineKeyboardMarkup(kb)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("📥 查看申请", callback_data="admin_view_pending"),
            InlineKeyboardButton("📋 活动会话", callback_data="admin_view_active"),
        ],
        [
            InlineKeyboardButton("📤 主动连接（/connect）", callback_data="admin_hint_connect"),
            InlineKeyboardButton("🔧 管理帮助", callback_data="admin_help"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

def pending_item_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ 同意", callback_data=f"admin_accept:{user_id}"),
                                 InlineKeyboardButton("❌ 拒绝", callback_data=f"admin_reject:{user_id}")]])

def active_item_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔚 结束该会话", callback_data=f"admin_end:{user_id}"),
                                 InlineKeyboardButton("🚫 封禁该用户", callback_data=f"admin_ban:{user_id}")]])

# ---------------- HELPERS ----------------
def username_is_admin(username: Optional[str]) -> bool:
    if not username:
        return False
    return username.lower() in {u.lower() for u in ADMIN_USERNAMES}

def is_admin_update(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if u.id in numeric_admin_ids:
        return True
    return username_is_admin(u.username)

# token bucket helpers
async def _refill_bucket():
    global _bucket_tokens, _bucket_last
    now = time.time()
    elapsed = now - _bucket_last
    if elapsed <= 0:
        return
    add = elapsed * COPY_BUCKET_FILL_RATE
    if add >= 1:
        _bucket_tokens = min(COPY_BUCKET_CAPACITY, _bucket_tokens + int(add))
        _bucket_last = now

async def acquire_copy_token(timeout=5.0) -> bool:
    # wait until token available or timeout
    start = time.time()
    while time.time() - start < timeout:
        await _refill_bucket()
        if _bucket_tokens > 0:
            _bucket_tokens -= 1
            return True
        await asyncio.sleep(0.05)
    return False

# safe copy with RetryAfter handling and token acquisition
async def safe_copy(msg: Message, chat_id: int, retries=4):
    for attempt in range(retries):
        got = await acquire_copy_token(timeout=3.0)
        if not got:
            # no token, backoff
            await asyncio.sleep(0.2 + attempt*0.1)
        try:
            copied = await msg.copy(chat_id=chat_id)
            return copied
        except RetryAfter as e:
            wait = e.retry_after + 0.5
            logger.warning(f"RetryAfter when copying to {chat_id}, sleep {wait}s")
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.exception(f"TelegramError copying message to {chat_id}: {e}")
            if attempt == retries - 1:
                raise
            await asyncio.sleep(0.2 + attempt*0.1)
    raise RuntimeError("safe_copy failed after retries")

async def notify_admins_new_request(user_id: int, username: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    text = f"📌 新请求：用户 {'@'+username if username else user_id}\nID: `{user_id}`\n是否同意？"
    # try numeric admin ids first
    for aid in list(numeric_admin_ids):
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=pending_item_kb(user_id), parse_mode="Markdown")
        except Exception:
            logger.exception(f"通知 numeric admin {aid} 失败")
    # then try usernames
    for name in ADMIN_USERNAMES:
        try:
            await context.bot.send_message(chat_id=f"@{name}", text=text, reply_markup=pending_item_kb(user_id), parse_mode="Markdown")
        except Exception:
            logger.exception(f"通知 @{name} 失败 (管理员可能未私聊 bot)")

# ---------------- COMMAND HANDLERS ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin_update(update):
        await update.message.reply_text("欢迎，管理员。管理面板：", reply_markup=admin_panel_keyboard())
        return
    is_pending = uid in pending_requests
    is_active = uid in active_sessions
    await update.message.reply_text("欢迎。点击下方按钮申请与管理员连接。", reply_markup=user_main_keyboard(is_pending, is_active))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin_update(update):
        txt = (
            "/start - 管理面板\n"
            "/connect <user_id> - 主动连接用户\n"
            "/end <user_id> - 结束某用户会话\n"
            "/ban <user_id> - 封禁用户\n"
            "/unban <user_id> - 解封用户\n"
            "/list - 列出活动/待处理\n"
            "/send <user_id> <消息> - 给某用户发消息\n"
            "/broadcast <消息> - 向所有活动用户广播\n"
            "/register_admin - 管理员私聊注册（备用）\n"
        )
        await update.message.reply_text(txt)
    else:
        await update.message.reply_text("使用 /start 并点击按钮申请与管理员连接。")

async def register_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Allow admin (by username) to register numeric id in case get_chat('@username') failed
    u = update.effective_user
    if not username_is_admin(u.username):
        await update.message.reply_text("仅允许预设用户名的管理员使用此命令。")
        return
    numeric_admin_ids.add(u.id)
    await update.message.reply_text(f"已注册管理员 id: {u.id}")
    logger.info(f"Admin {u.username} registered numeric id {u.id}")

async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/connect <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    if await db_is_banned(uid):
        await update.message.reply_text("该用户已被封禁，无法连接。")
        return
    async with state_lock:
        pending_requests.discard(uid)
        active_sessions.add(uid)
    await db_remove_pending(uid)
    await db_add_active(uid, None)
    await update.message.reply_text(f"✅ 已主动与用户 {uid} 建立会话。")
    try:
        await context.bot.send_message(chat_id=uid, text="✅ 管理员已主动与你建立专属聊天通道。")
    except Exception:
        await update.message.reply_text("警告：向用户发送消息失败（可能用户未与 bot 私聊过）。")

async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/end <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    async with state_lock:
        if uid in active_sessions:
            active_sessions.discard(uid)
            await db_remove_active(uid)
            try:
                await context.bot.send_message(chat_id=uid, text="⚠️ 管理员已结束本次会话。")
            except:
                pass
            await update.message.reply_text(f"已结束与用户 {uid} 的会话。")
        else:
            await update.message.reply_text("该用户当前没有活动会话。")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    await db_ban(uid)
    async with state_lock:
        pending_requests.discard(uid)
        active_sessions.discard(uid)
    await db_remove_pending(uid)
    await db_remove_active(uid)
    try:
        await context.bot.send_message(chat_id=uid, text="🚫 你已被管理员封禁，无法与管理员聊天。")
    except:
        pass
    await update.message.reply_text(f"已封禁用户 {uid} 并断开任何会话。")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    await db_unban(uid)
    await update.message.reply_text(f"已解封用户 {uid}。")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    async with state_lock:
        act = list(active_sessions)
        pend = list(pending_requests)
    txt = f"🟢 活动会话（{len(act)}）：\n" + ("\n".join(map(str, act)) if act else "无")
    txt += f"\n\n⏳ 待处理申请（{len(pend)}）：\n" + ("\n".join(map(str, pend)) if pend else "无")
    await update.message.reply_text(txt)

async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/send <user_id> <消息>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        await update.message.reply_text("已发送。")
    except Exception as e:
        await update.message.reply_text(f"发送失败：{e}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/broadcast <消息>")
        return
    text = " ".join(context.args)
    count = 0
    async with state_lock:
        targets = list(active_sessions)
    for uid in targets:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            count += 1
        except:
            pass
    await update.message.reply_text(f"已向 {count} 个活动用户广播。")

# ---------------- CALLBACK HANDLER ----------------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    caller = query.from_user
    caller_uid = caller.id
    caller_username = caller.username

    # USER actions
    if data == "user_apply":
        if await db_is_banned(caller_uid):
            await query.edit_message_text("你已被封禁，无法申请。")
            return
        async with state_lock:
            if caller_uid in active_sessions:
                await query.edit_message_text("你已处于会话中；如需结束请点结束按钮。", reply_markup=user_main_keyboard(False, True))
                return
            if caller_uid in pending_requests:
                await query.edit_message_text("你已申请，请耐心等待管理员处理。", reply_markup=user_main_keyboard(True, False))
                return
            pending_requests.add(caller_uid)
        await db_add_pending(caller_uid, caller_username)
        await query.edit_message_text("✅ 已发送申请，请耐心等待管理员确认。", reply_markup=user_main_keyboard(True, False))
        await notify_admins_new_request(caller_uid, caller_username, context)
        return

    if data == "user_cancel":
        async with state_lock:
            if caller_uid in pending_requests:
                pending_requests.discard(caller_uid)
                await db_remove_pending(caller_uid)
                await query.edit_message_text("已取消申请。", reply_markup=user_main_keyboard(False, False))
                # optional: notify admins
                for name in ADMIN_USERNAMES:
                    try:
                        await context.bot.send_message(chat_id=f"@{name}", text=f"ℹ️ 用户 `{caller_uid}` 取消了申请。", parse_mode="Markdown")
                    except:
                        pass
                return
        await query.edit_message_text("你当前没有申请。", reply_markup=user_main_keyboard(False, False))
        return

    if data == "user_end":
        async with state_lock:
            if caller_uid in active_sessions:
                active_sessions.discard(caller_uid)
                await db_remove_active(caller_uid)
                await query.edit_message_text("你已结束与管理员的会话。", reply_markup=user_main_keyboard(False, False))
                # notify admins
                for name in ADMIN_USERNAMES:
                    try:
                        await context.bot.send_message(chat_id=f"@{name}", text=f"⚠️ 用户 `{caller_uid}` 已结束会话。", parse_mode="Markdown")
                    except:
                        pass
                return
        await query.edit_message_text("你当前没有会话。", reply_markup=user_main_keyboard(False, False))
        return

    # ADMIN actions
    if data == "admin_view_pending":
        if not is_admin_update(update):
            await query.edit_message_text("仅管理员可查看。")
            return
        pend = await db_get_all_pending()
        if not pend:
            await query.edit_message_text("当前没有待处理申请。", reply_markup=admin_panel_keyboard())
            return
        await query.edit_message_text("以下为待处理申请：", reply_markup=admin_panel_keyboard())
        for uid, uname in pend:
            txt = f"📌 申请用户 ID: `{uid}`"
            try:
                # deliver to this admin (prefer numeric id)
                if update.effective_user.id in numeric_admin_ids:
                    target = update.effective_user.id
                else:
                    target = f"@{update.effective_user.username}"
                await context.bot.send_message(chat_id=target, text=txt, reply_markup=pending_item_kb(uid), parse_mode="Markdown")
            except Exception:
                logger.exception("向管理员发送 pending item 失败")
        return

    if data == "admin_view_active":
        if not is_admin_update(update):
            await query.edit_message_text("仅管理员可查看。")
            return
        act = await db_get_all_active()
        if not act:
            await query.edit_message_text("当前没有活动会话。", reply_markup=admin_panel_keyboard())
            return
        await query.edit_message_text("活动会话列表：", reply_markup=admin_panel_keyboard())
        for uid, uname in act:
            txt = f"🟢 活动用户 ID: `{uid}`"
            try:
                if update.effective_user.id in numeric_admin_ids:
                    target = update.effective_user.id
                else:
                    target = f"@{update.effective_user.username}"
                await context.bot.send_message(chat_id=target, text=txt, reply_markup=active_item_kb(uid), parse_mode="Markdown")
            except Exception:
                logger.exception("向管理员发送 active item 失败")
        return

    if data.startswith("admin_accept:"):
        try:
            uid = int(data.split(":", 1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            async with state_lock:
                pending_requests.discard(uid)
                active_sessions.add(uid)
            await db_remove_pending(uid)
            await db_add_active(uid, None)
            await query.edit_message_text(f"✅ 已同意用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="✅ 管理员已同意你的申请，你现在已连接到管理员。")
            except:
                pass
            try:
                await context.bot.send_message(chat_id=update.effective_user.id, text=f"🟢 已与用户 `{uid}` 建立连接。", parse_mode="Markdown")
            except:
                pass
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_reject:"):
        try:
            uid = int(data.split(":", 1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            async with state_lock:
                pending_requests.discard(uid)
            await db_remove_pending(uid)
            await query.edit_message_text(f"❌ 已拒绝用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="很抱歉，管理员拒绝了你的聊天申请。")
            except:
                pass
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_end:"):
        try:
            uid = int(data.split(":", 1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        async with state_lock:
            if uid in active_sessions:
                active_sessions.discard(uid)
                await db_remove_active(uid)
                await query.edit_message_text(f"🔚 已结束用户 `{uid}` 的会话。", parse_mode="Markdown")
                try:
                    await context.bot.send_message(chat_id=uid, text="⚠️ 管理员已结束本次会话。")
                except:
                    pass
            else:
                await query.edit_message_text("该用户当前没有活动会话。")
        return

    if data.startswith("admin_ban:"):
        try:
            uid = int(data.split(":", 1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        await db_ban(uid)
        async with state_lock:
            pending_requests.discard(uid)
            active_sessions.discard(uid)
        await db_remove_pending(uid)
        await db_remove_active(uid)
        await query.edit_message_text(f"🚫 已封禁用户 `{uid}`。", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=uid, text="你已被管理员封禁，无法再申请或接收管理员消息。")
        except:
            pass
        return

    if data == "admin_hint_connect":
        await query.edit_message_text("提示：使用 /connect <user_id> 来主动连接用户（管理员无需用户申请）。", reply_markup=admin_panel_keyboard())
        return

    if data == "admin_help":
        await query.edit_message_text("管理员帮助：使用 /help 查看完整命令。", reply_markup=admin_panel_keyboard())
        return

    await query.answer(text="未识别的操作。")

# ---------------- MESSAGE RELAY ----------------
async def message_relay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    sender_id = update.effective_user.id

    # ADMIN path: admin replies to an admin-side message (we mapped (chat_id, message_id) -> user_id)
    if is_admin_update(update):
        reply = msg.reply_to_message
        if reply:
            key = (reply.chat.id, reply.message_id)
            if key in admin_msgid_to_user:
                target_user = admin_msgid_to_user[key]
                try:
                    copied = await safe_copy(msg, chat_id=target_user)
                    user_last_admin_msgid[target_user] = copied.message_id
                    await msg.reply_text(f"已发送给用户 {target_user}")
                except Exception as e:
                    logger.exception("admin -> user copy failed")
                    await msg.reply_text(f"发送失败：{e}")
                return
        await msg.reply_text("要回复某个用户，请在管理面板查看活动会话并回复对应消息，或使用 /connect <user_id>。")
        return

    # USER path
    if await db_is_banned(sender_id):
        await msg.reply_text("你已被封禁，无法使用该服务。")
        return

    # if user in active sessions: copy to admin(s)
    if sender_id in active_sessions:
        # try numeric admins first, then username admins
        sent = False
        # try numeric admins
        for aid in list(numeric_admin_ids):
            try:
                copied = await safe_copy(msg, chat_id=aid)
                admin_msgid_to_user[(copied.chat.id, copied.message_id)] = sender_id
                user_last_admin_msgid[sender_id] = copied.message_id
                sent = True
                break
            except Exception:
                logger.exception(f"copy to numeric admin {aid} failed")
        if not sent:
            for name in ADMIN_USERNAMES:
                try:
                    copied = await safe_copy(msg, chat_id=f"@{name}")
                    admin_msgid_to_user[(copied.chat.id, copied.message_id)] = sender_id
                    user_last_admin_msgid[sender_id] = copied.message_id
                    sent = True
                    break
                except Exception:
                    logger.exception(f"copy to @{name} failed")
        if not sent:
            await msg.reply_text("发送失败：管理员当前不可达（请确认管理员已与机器人私聊或使用 /register_admin 注册）。")
        return

    # If user in pending -> remind waiting
    if sender_id in pending_requests:
        await msg.reply_text("⏳ 你的申请正在等待管理员处理，请耐心等待或点击取消。", reply_markup=user_main_keyboard(is_pending=True, is_active=False))
        return

    # otherwise prompt to apply
    await msg.reply_text("你当前尚未申请与管理员聊天。点击下面按钮申请：", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
    return

# ---------------- STARTUP / MAIN ----------------
async def resolve_admin_usernames_to_ids(app) -> Set[int]:
    resolved = set()
    for name in ADMIN_USERNAMES:
        try:
            chat = await app.bot.get_chat(f"@{name}")
            resolved.add(chat.id)
            logger.info(f"Resolved @{name} -> {chat.id}")
        except Exception:
            logger.warning(f"无法解析 @{name}（管理员可能尚未与 bot 私聊）")
    return resolved

async def load_state_from_db():
    # load pending & active from DB into memory
    pend = await db_get_all_pending()
    act = await db_get_all_active()
    async with state_lock:
        for uid, uname in pend:
            pending_requests.add(uid)
        for uid, uname in act:
            active_sessions.add(uid)
    logger.info(f"Loaded {len(pend)} pending and {len(act)} active from DB")

def main():
    # must be run in sync context to build app
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # schedule startup tasks
    async def startup_tasks(context: ContextTypes.DEFAULT_TYPE):
        # init db
        await init_db()
        # load state
        await load_state_from_db()
        # resolve admin usernames
        res = await resolve_admin_usernames_to_ids(context.application)
        if res:
            numeric_admin_ids.update(res)
            logger.info(f"numeric_admin_ids resolved on startup: {res}")
        else:
            logger.info("no admin username resolved on startup; admins should /register_admin in private chat")

    # run startup once soon after start
    app.job_queue.run_once(lambda ctx: asyncio.create_task(startup_tasks(ctx)), when=0)

    # periodic admin resolver (every 5 minutes)
    async def admin_resolver_job(context: ContextTypes.DEFAULT_TYPE):
        try:
            res = await resolve_admin_usernames_to_ids(context.application)
            if res:
                numeric_admin_ids.update(res)
                logger.info(f"admin resolver updated numeric ids: {res}")
        except Exception:
            logger.exception("admin resolver job failed")
    app.job_queue.run_repeating(admin_resolver_job, interval=300, first=60)

    # command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("register_admin", register_admin_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # callbacks & messages
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_relay_handler))

    logger.info("Bot starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
