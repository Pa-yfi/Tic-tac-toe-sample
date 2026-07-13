#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎲 ربات بازی دوز — نسخه‌ی کامل (۲۰ قابلیت)
اجرا: python bot.py     |     توکن در فایل .env
"""

import asyncio
import atexit
import logging
import os
from pathlib import Path
import random
import subprocess
import time
import uuid
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.error import BadRequest, Conflict, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

import db
from game import (
    BOT_ID, DIFFICULTIES, EMOJI_ORDER, EMPTY, LINE, MODES, SERIES_OPTIONS,
    SIZES, TIMER_OPTIONS, WIN_LEN, Game, apply_move, bot_move, drop_target,
    evaluate_end, is_diagonal_win, legal_moves, next_turn,
)
from i18n import ACHIEVEMENTS, ach_name, t

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()      # ایده ۲۰
PORT = int(os.getenv("PORT", "8443"))
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN در فایل .env پیدا نشد!")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("dooz")
PLAYER_LOG_FILE = Path(__file__).resolve().parent / "player_usernames.log"
PID_FILE = Path(__file__).resolve().parent / "bot.pid"

GAMES: Dict[str, Game] = {}
# gid -> (size, lang, host_id, host_name, ts) — نتایج اینلاینِ هنوز فرستاده‌نشده
PENDING: Dict[str, tuple] = {}
DOTS = [".", "..", "..."]
ANIM_INTERVAL = 3.0
JOIN_TIMEOUT = 10 * 60


# ================================================================ helpers
async def persist(g: Game):
    g.updated_at = time.time()
    await db.save_game(g.gid, g.to_dict())


def username_text(user) -> str:
    return f"@{user.username}" if getattr(user, "username", None) else "-"


def record_player_username(gid: str, user, role: str):
    line = "\t".join([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        gid,
        role,
        str(user.id),
        username_text(user),
        user.first_name.replace("\t", " ").replace("\n", " "),
    ])
    try:
        with PLAYER_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("player log failed: %s", e)


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_command(pid: int) -> str:
    try:
        cp = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return cp.stdout.strip()


def cleanup_pid_file():
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def acquire_single_instance():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = None
        if old_pid and old_pid != os.getpid() and pid_is_alive(old_pid):
            cmd = pid_command(old_pid)
            if "bot.py" in cmd:
                raise SystemExit(
                    f"❌ یک نمونه‌ی دیگر از ربات روی همین سیستم در حال اجراست (PID={old_pid})."
                )
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(cleanup_pid_file)


async def answer(q, text: str = None, alert: bool = False):
    try:
        await q.answer(text=text, show_alert=alert)
    except TelegramError as e:
        log.debug("answer failed: %s", e)


async def edit(ctx, g: Game, text: str, markup: Optional[InlineKeyboardMarkup]):
    try:
        await ctx.bot.edit_message_text(
            inline_message_id=g.inline_message_id, text=text, reply_markup=markup
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("edit: %s", e)
    except TelegramError as e:
        log.debug("edit: %s", e)


# ================================================================ 🎛️ لابی (تنظیمات)
def lobby_text(g: Game) -> str:
    L = g.lang
    opp = t(L, "bot") if g.vs_bot else t(L, "friend")
    lines = [
        f"{t(L, 'lobby_title')}\n",
        f"{t(L, 'host')}: {g.names.get(str(g.host_id), '—')}",
        f"{t(L, 'size')}: {g.size}×{g.size}",
        f"{t(L, 'win_rule', n=g.win_len)}",
        f"{t(L, 'mode')}: {t(L, 'mode_' + g.mode)}",
        f"{t(L, 'emoji')}: {' '.join(g.symbols)}",
        f"{t(L, 'opponent')}: {opp}" + (f" ({t(L, 'diff_' + g.difficulty)})" if g.vs_bot else ""),
        f"{t(L, 'players')}: {g.n_players}",
        f"{t(L, 'timer')}: " + (f"{g.timer}s ⏱️" if g.timer else t(L, "off")),
        f"{t(L, 'series')}: Bo{g.series_len}",
    ]
    return "\n".join(lines)


def lobby_markup(g: Game) -> InlineKeyboardMarkup:
    L, gid = g.lang, g.gid
    rows = [
        [InlineKeyboardButton(f"📐 {g.size}×{g.size}", callback_data=f"set:{gid}:size"),
         InlineKeyboardButton(f"🎯 {t(L, 'mode_' + g.mode)}", callback_data=f"set:{gid}:mode")],
        [InlineKeyboardButton(f"🎨 {''.join(g.symbols[:2])}", callback_data=f"set:{gid}:emoji"),
         InlineKeyboardButton(f"👥 {t(L, 'bot') if g.vs_bot else t(L, 'friend')}",
                              callback_data=f"set:{gid}:opp")],
    ]
    if g.vs_bot:
        rows.append([InlineKeyboardButton(f"🧠 {t(L, 'diff_' + g.difficulty)}",
                                          callback_data=f"set:{gid}:diff")])
    else:
        rows.append([InlineKeyboardButton(f"🧑‍🤝‍🧑 {g.n_players}", callback_data=f"set:{gid}:np"),
                     InlineKeyboardButton(f"🏆 Bo{g.series_len}", callback_data=f"set:{gid}:series")])
    rows.append([
        InlineKeyboardButton(f"⏱️ {g.timer or '—'}", callback_data=f"set:{gid}:timer"),
        InlineKeyboardButton(t(L, "lang_btn"), callback_data=f"set:{gid}:lang"),
    ])
    rows.append([InlineKeyboardButton(t(L, "start"), callback_data=f"go:{gid}")])
    rows.append([InlineKeyboardButton(t(L, "share"), switch_inline_query="")])
    return InlineKeyboardMarkup(rows)


def cycle(lst, cur):
    return lst[(lst.index(cur) + 1) % len(lst)]


# ================================================================ 🧩 صفحه‌ی بازی
def board_markup(g: Game, reveal: Optional[List[int]] = None) -> InlineKeyboardMarkup:
    L = g.lang
    shown = set(reveal if reveal is not None else g.win_cells)
    rows = []
    for r in range(g.size):
        row = []
        for c in range(g.size):
            i = r * g.size + c
            cell = LINE if i in shown else g.board[i]
            row.append(InlineKeyboardButton(cell, callback_data=f"mv:{g.gid}:{i}"))
        rows.append(row)

    if g.over:
        rows.append([
            InlineKeyboardButton(t(L, "rematch"), callback_data=f"re:{g.gid}"),
            InlineKeyboardButton(t(L, "replay"), callback_data=f"rp:{g.gid}"),
        ])
        rows.append([
            InlineKeyboardButton("🏅", callback_data=f"lb:{g.gid}"),
            InlineKeyboardButton(t(L, "share"), switch_inline_query=""),
        ])
    else:
        extra = [InlineKeyboardButton(t(L, "undo"), callback_data=f"ud:{g.gid}")]
        if g.n_players == 2:
            extra.append(InlineKeyboardButton(t(L, "draw_offer"), callback_data=f"dr:{g.gid}"))
        rows.append(extra)
        rows.append([InlineKeyboardButton(t(L, "leave"), callback_data=f"lv:{g.gid}")])
    return InlineKeyboardMarkup(rows)


def board_text(g: Game, extra: str = "") -> str:
    L = g.lang
    head = [f"🎲 {g.size}×{g.size} • {t(L, 'mode_' + g.mode)} • {t(L, 'win_rule', n=g.win_len)}", ""]
    for i in range(len(g.players)):
        head.append(f"{'👤' if i == 0 else '👥'} {g.name(i)} ({g.sym(i)})")
    head.append("")

    if g.series_len > 1:
        head.append(t(L, "series_score", a=g.series_score[0], b=g.series_score[1], n=g.series_len))

    if g.is_draw:
        head.append(t(L, "draw"))
    elif g.over and g.winner is not None:
        key = "congrats_misere" if g.mode == "misere" else "congrats"
        head.append(t(L, key, name=g.name(g.winner), sym=g.sym(g.winner)))
        if g.series_done:
            head.append(t(L, "series_win", name=g.name(g.winner)))
    elif g.timer:
        left = max(0, int(g.deadline - time.time()))
        head.append(t(L, "turn_timer", name=g.name(g.turn_idx), sym=g.sym(g.turn_idx), sec=left))
    else:
        head.append(t(L, "turn", name=g.name(g.turn_idx), sym=g.sym(g.turn_idx)))

    if extra:
        head += ["", extra]
    return "\n".join(head)


async def render(ctx, g: Game, extra: str = "", reveal: Optional[List[int]] = None):
    await edit(ctx, g, board_text(g, extra), board_markup(g, reveal))


# ================================================================ ⏳ انیمیشن انتظار
def waiting_markup(g: Game) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(g.lang, "join"), callback_data=f"jn:{g.gid}")],
        [InlineKeyboardButton(t(g.lang, "share"), switch_inline_query="")],
    ])


async def wait_animation(app: Application, g: Game):
    i, start = 0, time.time()
    try:
        while g.started and not g.ready and not g.over:
            if time.time() - start > JOIN_TIMEOUT:
                g.over = True
                await persist(g)
                await edit(
                    ctx_like(app), g, t(g.lang, "timeout_join"),
                    InlineKeyboardMarkup([[InlineKeyboardButton(
                        t(g.lang, "new_game"), switch_inline_query="")]]),
                )
                return
            txt = (
                f"🎲 {g.size}×{g.size} • {t(g.lang, 'mode_' + g.mode)}\n"
                f"{t(g.lang, 'win_rule', n=g.win_len)}\n\n"
                f"👤 {g.names.get(str(g.host_id), '—')} ({g.sym(0)})\n\n"
                + t(g.lang, "waiting", dots=DOTS[i % 3])
            )
            i += 1
            try:
                await app.bot.edit_message_text(
                    inline_message_id=g.inline_message_id,
                    text=txt, reply_markup=waiting_markup(g),
                )
            except TelegramError as e:
                log.debug("anim: %s", e)
            await asyncio.sleep(ANIM_INTERVAL)
    except asyncio.CancelledError:
        pass
    finally:
        g.anim_task = None


class ctx_like:
    """آداپتور کوچک تا edit() هم با Application و هم با context کار کند."""
    def __init__(self, app):
        self.bot = app.bot


def stop_anim(g: Game):
    task, g.anim_task = g.anim_task, None
    if task and not task.done():
        task.cancel()


# ================================================================ ⏱️ تایمر نوبت (ایده ۲)
def cancel_timer(g: Game):
    if g.timer_job:
        try:
            g.timer_job.schedule_removal()
        except Exception:
            pass
        g.timer_job = None


def arm_timer(ctx, g: Game):
    cancel_timer(g)
    if not g.timer or g.over or not g.started:
        g.deadline = 0
        return
    g.deadline = time.time() + g.timer
    if ctx.job_queue:
        g.timer_job = ctx.job_queue.run_once(on_timeout, g.timer, data=g.gid, name=f"t{g.gid}")


async def on_timeout(ctx: ContextTypes.DEFAULT_TYPE):
    g = GAMES.get(ctx.job.data)
    if not g or g.over or not g.started:
        return
    async with g.lock:
        loser = g.turn_idx
        if g.n_players == 2:
            g.over = True
            g.winner = (loser + 1) % 2
            msg = t(g.lang, "timeout_lose", name=g.name(loser))
            await persist(g)
            await finish(ctx, g, extra=msg)
        else:
            msg = t(g.lang, "timeout_move", name=g.name(loser))
            next_turn(g)
            arm_timer(ctx, g)
            await persist(g)
            await render(ctx, g, extra=msg)


# ================================================================ 🏁 پایان بازی
def collect_achievements(g: Game, uid: int, user: dict, winner_idx: int) -> List[str]:
    keys = []
    if user["wins"] + 1 == 1:
        keys.append("first_win")
    if user["streak"] + 1 >= 5:
        keys.append("streak_5")
    if is_diagonal_win(g):
        keys.append("diagonal")
    my_moves = [c for p, c in g.history if p == winner_idx]
    if len(my_moves) <= 5:
        keys.append("fast_win")
    if g.vs_bot and g.difficulty == "hard":
        keys.append("bot_slayer")
    if g.size == 6:
        keys.append("big_board")
    center = (g.size * g.size) // 2
    if g.size % 2 == 1 and center not in my_moves:
        keys.append("comeback")
    if user["games"] + 1 >= 25:
        keys.append("veteran")
    return keys


async def finish(ctx, g: Game, extra: str = ""):
    """ثبت نتیجه، Elo، دستاورد، سری + انیمیشن برد (ایده ۹/۱۰/۱۱/۱۴/۱۵)"""
    cancel_timer(g)
    g.over = True

    # 🏆 سری Best-of (ایده ۱۱)
    if g.winner is not None and g.series_len > 1:
        g.series_score[g.winner] += 1
        if g.series_score[g.winner] > g.series_len // 2:
            g.series_done = True

    # 🎉 ایده ۱۴: خانه‌های برنده یکی‌یکی قرمز می‌شوند
    if g.win_cells:
        for k in range(1, len(g.win_cells) + 1):
            await render(ctx, g, extra=extra, reveal=g.win_cells[:k])
            await asyncio.sleep(0.35)
    else:
        await render(ctx, g, extra=extra)

    # 📈 Elo + آمار + دستاورد
    human = [(uid, g.names.get(str(uid), "?")) for uid in g.players if uid != BOT_ID]
    cells = [(g.players[p], g.size, c) for p, c in g.history if g.players[p] != BOT_ID]
    lines = []
    if g.winner is not None:
        w_uid = g.players[g.winner]
        winner = (w_uid, g.names.get(str(w_uid), "?")) if w_uid != BOT_ID else None
        losers = [x for x in human if x[0] != w_uid]
        before = {uid: (await db.get_user(uid))["elo"] for uid, _ in human}
        deltas = await db.record_result(g.chat_instance, winner, losers, [], cells)
        if winner:
            user = await db.get_user(w_uid)
            new = await db.grant(w_uid, collect_achievements(g, w_uid, user, g.winner))
            if new:
                lines.append(t(g.lang, "ach_unlocked",
                               names="، ".join(ach_name(g.lang, k) for k in new)))
        if len(human) == 2 and deltas:
            (a, an), (b, bn) = human
            lines.append(t(g.lang, "elo",
                           a=an, da=f"{deltas.get(a, 0):+d}",
                           b=bn, db=f"{deltas.get(b, 0):+d}"))
        # 🎊 ایده ۱۵: افکت تبریک
        if winner:
            try:
                await ctx.bot.send_dice(chat_id=w_uid, emoji="🎯")
            except TelegramError:
                pass
    elif g.is_draw:
        await db.record_result(g.chat_instance, None, [], human, cells)

    if lines:
        extra = (extra + "\n" if extra else "") + "\n".join(lines)
    await persist(g)
    await render(ctx, g, extra=extra)


# ================================================================ 🤖 نوبت ربات
async def maybe_bot(ctx, g: Game):
    while g.bot_turn:
        await render(ctx, g, extra=t(g.lang, "bot_thinking"))
        await asyncio.sleep(0.6)
        cell = await asyncio.to_thread(bot_move, g)
        real = drop_target(g, cell) or cell
        apply_move(g, g.turn_idx, real)
        mover = g.turn_idx
        evaluate_end(g, real, mover)
        if g.over:
            await persist(g)
            return await finish(ctx, g)
        next_turn(g)
        arm_timer(ctx, g)
        await persist(g)
        await render(ctx, g)


# ================================================================ 📜 مرور بازی (ایده ۱۶)
async def replay(ctx, g: Game):
    saved_board = list(g.board)
    tmp = [EMPTY] * (g.size * g.size)
    hist = list(g.history)
    counts = {}
    for p, c in hist:
        g.board = list(tmp)
        markup = board_markup(g, reveal=[])
        await edit(ctx, g, t(g.lang, "replaying"), markup)
        tmp[c] = g.sym(p)
        if g.mode == "three":
            counts.setdefault(p, []).append(c)
            if len(counts[p]) > 3:
                tmp[counts[p].pop(0)] = EMPTY
        await asyncio.sleep(0.5)
    g.board = saved_board
    await render(ctx, g)


# ================================================================ هندلرها
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_user(u.id, u.first_name)
    L = user["lang"]
    await update.message.reply_text(
        t(L, "welcome"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(L, "new_game"), switch_inline_query="")],
            [InlineKeyboardButton("📊 /stats", callback_data="noop")],
        ]),
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """📊 ایده ۱۲"""
    u = update.effective_user
    user = await db.get_user(u.id, u.first_name)
    L = user["lang"]
    ach = await db.get_achievements(u.id)
    total = user["games"] or 1
    rate = round(user["wins"] * 100 / total)
    fav = await db.fav_cell(u.id)
    body = t(L, "stats_body", w=user["wins"], l=user["losses"], d=user["draws"],
             r=rate, elo=user["elo"],
             ach=" ".join(ach_name(L, k) for k in ach) or "—")
    if fav:
        r, c = divmod(fav["cell"], fav["size"])
        body += f"\n🎯 خانه‌ی محبوب: ({r + 1},{c + 1}) روی {fav['size']}×{fav['size']}"
    body += f"\n🔥 بهترین رکورد برد پشت‌سرهم: {user['best_streak']}"
    await update.message.reply_text(f"{t(L, 'stats_title')}\n\n{body}")


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await db.get_user(u.id, u.first_name)
    new = "en" if user["lang"] == "fa" else "fa"
    await db.set_lang(u.id, new)
    await update.message.reply_text(t(new, "welcome"))


async def inline_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.inline_query.from_user
    user = await db.get_user(u.id, u.first_name)
    lang = user["lang"]
    results = []
    for s in SIZES:
        gid = f"{uuid.uuid4().hex[:8]}{s}"   # 🔑 رقم آخر = اندازه؛ دکمه‌ها خودکفا می‌شوند
        PENDING[gid] = (s, lang, u.id, u.first_name, time.time())
        draft = Game(gid=gid, size=s, lang=lang, host_id=u.id)
        draft.names[str(u.id)] = u.first_name
        results.append(
            InlineQueryResultArticle(
                id=f"{gid}|{s}",
                title=f"🎲 {s}×{s}",
                description=t(lang, "win_rule", n=WIN_LEN[s]),
                input_message_content=InputTextMessageContent(lobby_text(draft)),
                # ⚠️ حیاتی: بدون reply_markup تلگرام پیام اینلاین را قابل ویرایش نمی‌کند
                # و اصلاً inline_message_id نمی‌دهد → دکمه‌ها نمی‌آیند.
                reply_markup=lobby_markup(draft),
            )
        )
    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def ensure_game(gid: str, q) -> Optional[Game]:
    """
    🛡️ بازیابی بازی از هر مسیری که ممکن باشد:
    1) حافظه  2) دیتابیس (بعد از ری‌استارت)  3) PENDING  4) بازسازی از خودِ gid
    چون رقم آخر gid اندازه‌ی صفحه است، هیچ کلیکی هرگز «منقضی» نمی‌شود.
    """
    g = GAMES.get(gid)
    if g is None:
        d = await db.load_game(gid)
        if d:
            try:
                g = Game.from_dict(d)
                GAMES[gid] = g
            except Exception as e:
                log.warning("restore %s failed: %s", gid, e)
    if g is None:
        p = PENDING.pop(gid, None)
        if p:
            size, lang, host_id, host_name, _ = p
            g = Game(gid=gid, size=size, lang=lang, host_id=host_id)
            g.names[str(host_id)] = host_name
        else:
            try:
                size = int(gid[-1])
                assert size in SIZES
            except (ValueError, AssertionError):
                return None
            g = Game(gid=gid, size=size)   # سازنده = اولین کلیک‌کننده
        GAMES[gid] = g
    if q is not None and q.inline_message_id and not g.inline_message_id:
        g.inline_message_id = q.inline_message_id      # 🔗 اتصال پیام به بازی
    return g


async def chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    res = update.chosen_inline_result
    if not res.inline_message_id:
        return
    try:
        gid, size = res.result_id.split("|")
        size = int(size)
    except ValueError:
        return
    u = res.from_user
    user = await db.get_user(u.id, u.first_name)
    g = GAMES.get(gid) or Game(gid=gid, size=size, lang=user["lang"], host_id=u.id)
    g.inline_message_id = res.inline_message_id
    g.names[str(u.id)] = u.first_name
    GAMES[gid] = g
    PENDING.pop(gid, None)
    await persist(g)
    await edit(ctx, g, lobby_text(g), lobby_markup(g))   # 🎛️ لابی تنظیمات


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    if parts[0] == "noop":
        return await answer(q, "/stats")
    if len(parts) < 2:
        return await answer(q, "⚠️", alert=True)

    action, gid = parts[0], parts[1]
    g = await ensure_game(gid, q)
    if g is None:
        return await answer(q, t("fa", "expired"), alert=True)

    tg_user = q.from_user
    uid, uname = tg_user.id, tg_user.first_name
    L = g.lang

    async with g.lock:
        g.updated_at = time.time()
        if q.chat_instance and not g.chat_instance:
            g.chat_instance = q.chat_instance

        # ---------------- ⚙️ تنظیمات لابی
        if action == "set":
            if g.host_id is None:
                g.host_id = uid
                g.names[str(uid)] = uname
            if g.started:
                return await answer(q, t(L, "game_over") if g.over else t(L, "not_turn"), alert=True)
            if uid != g.host_id:
                return await answer(q, t(L, "host_only"), alert=True)
            f = parts[2]
            if f == "size":
                g.size = cycle(SIZES, g.size)
                if g.n_players > 2 and g.size < 5:
                    g.n_players = 2
                g.reset_board()
            elif f == "mode":
                g.mode = cycle(MODES, g.mode)
            elif f == "emoji":
                g.emoji_set = cycle(EMOJI_ORDER, g.emoji_set)
            elif f == "opp":
                g.vs_bot = not g.vs_bot
                if g.vs_bot:
                    g.n_players, g.series_len = 2, 1
            elif f == "diff":
                g.difficulty = cycle(DIFFICULTIES, g.difficulty)
            elif f == "np":
                nxt = g.n_players + 1
                if nxt > 4 or (nxt > 2 and g.size < 5):
                    nxt = 2
                g.n_players = nxt
                g.series_score = [0] * g.n_players
            elif f == "series":
                g.series_len = cycle(SERIES_OPTIONS, g.series_len)
            elif f == "timer":
                g.timer = cycle(TIMER_OPTIONS, g.timer)
            elif f == "lang":
                g.lang = "en" if g.lang == "fa" else "fa"
                await db.set_lang(uid, g.lang)
            await persist(g)
            await answer(q)
            return await edit(ctx, g, lobby_text(g), lobby_markup(g))

        # ---------------- ▶️ شروع
        if action == "go":
            if g.host_id is None:
                g.host_id = uid
                g.names[str(uid)] = uname
            if g.started:
                return await answer(q)
            if uid != g.host_id:
                return await answer(q, t(L, "host_only"), alert=True)
            g.started = True
            g.players = [g.host_id]
            record_player_username(g.gid, tg_user, "host")
            g.reset_board()
            if g.vs_bot:
                g.players.append(BOT_ID)
                await answer(q, "🤖")
                arm_timer(ctx, g)
                await persist(g)
                await render(ctx, g)
                return await maybe_bot(ctx, g)
            await answer(q)
            await persist(g)
            stop_anim(g)
            g.anim_task = asyncio.create_task(wait_animation(ctx.application, g))
            return

        # ---------------- 🎮 پیوستن
        if action == "jn":
            if not g.started:
                return await answer(q, "⏳", alert=True)
            if uid in g.players:
                return await answer(q, t(L, "you_are_host"), alert=True)
            if g.ready:
                return await answer(q, t(L, "full"), alert=True)
            g.players.append(uid)
            g.names[str(uid)] = uname
            record_player_username(g.gid, tg_user, "join")
            await db.get_user(uid, uname)
            if g.ready:
                stop_anim(g)
                arm_timer(ctx, g)
                await persist(g)
                await answer(q, t(L, "joined"))
                return await render(ctx, g)
            await persist(g)
            await answer(q, t(L, "joined"))
            joined = len(g.players)
            txt = (
                f"🎲 {g.size}×{g.size} • {t(L, 'mode_' + g.mode)}\n"
                f"{t(L, 'win_rule', n=g.win_len)}\n\n"
                + "\n".join(f"{'👤' if i == 0 else '👥'} {g.name(i)} ({g.sym(i)})"
                             for i in range(joined))
                + f"\n\n⏳ {joined}/{g.n_players}"
            )
            return await edit(ctx, g, txt, waiting_markup(g))

        # ---------------- 🎯 حرکت
        if action == "mv":
            if not g.started or not g.ready:
                return await answer(q, t(L, "waiting", dots="…"), alert=True)
            if g.over:
                return await answer(q, t(L, "game_over"), alert=True)
            p = g.idx_of(uid)
            if p is None:
                return await answer(q, t(L, "not_player"), alert=True)
            if p != g.turn_idx:
                return await answer(q, t(L, "not_turn"), alert=True)
            try:
                cell = int(parts[2])
            except (IndexError, ValueError):
                return await answer(q, "⚠️", alert=True)
            real = drop_target(g, cell)
            if real is None or real not in legal_moves(g):
                return await answer(q, t(L, "cell_taken"), alert=True)

            apply_move(g, p, real)
            evaluate_end(g, real, p)
            await answer(q)
            if g.over:
                await persist(g)
                return await finish(ctx, g)
            next_turn(g)
            arm_timer(ctx, g)
            await persist(g)
            await render(ctx, g)
            return await maybe_bot(ctx, g)

        # ---------------- ↩️ Undo / 🤝 مساوی (ایده ۶ و ۱۷)
        if action in ("ud", "dr"):
            if g.idx_of(uid) is None:
                return await answer(q, t(L, "not_player"), alert=True)
            if action == "ud" and not g.history:
                return await answer(q, t(L, "no_moves"), alert=True)
            if g.vs_bot:  # با ربات نیازی به تأیید نیست
                if action == "ud":
                    for _ in range(2):
                        if g.history:
                            p, c = g.history.pop()
                            g.board[c] = EMPTY
                            g.turn_idx = p
                    cancel_timer(g)
                    arm_timer(ctx, g)
                    await persist(g)
                    await answer(q, t(L, "undo_done"))
                    return await render(ctx, g)
                g.over, g.is_draw = True, True
                await persist(g)
                return await finish(ctx, g)
            g.pending, g.pending_by = action, uid
            await persist(g)
            await answer(q, t(L, "waiting_confirm"))
            key = "undo_req" if action == "ud" else "draw_req"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(L, "accept"), callback_data=f"ok:{gid}"),
                InlineKeyboardButton(t(L, "reject"), callback_data=f"no:{gid}"),
            ]])
            return await edit(ctx, g, board_text(g, extra=t(L, key, name=uname)), kb)

        if action in ("ok", "no"):
            if g.idx_of(uid) is None or uid == g.pending_by or not g.pending:
                return await answer(q, t(L, "not_player"), alert=True)
            req, g.pending, g.pending_by = g.pending, None, None
            if action == "no":
                await persist(g)
                await answer(q, t(L, "rejected"))
                return await render(ctx, g)
            if req == "ud":
                if g.history:
                    p, c = g.history.pop()
                    g.board[c] = EMPTY
                    g.turn_idx = p
                cancel_timer(g)
                arm_timer(ctx, g)
                await persist(g)
                await answer(q, t(L, "undo_done"))
                return await render(ctx, g)
            g.over, g.is_draw = True, True
            await persist(g)
            await answer(q, "🤝")
            return await finish(ctx, g)

        # ---------------- 🔄 بازی مجدد
        if action == "re":
            if g.idx_of(uid) is None:
                return await answer(q, t(L, "not_player"), alert=True)
            if g.series_done:
                g.series_score = [0] * g.n_players
                g.series_done = False
            g.reset_board()
            g.start_idx = (g.start_idx + 1) % len(g.players)   # 🔁 شروع‌کننده عوض می‌شود
            g.turn_idx = g.start_idx
            arm_timer(ctx, g)
            await persist(g)
            await answer(q, t(L, "rematch"))
            await render(ctx, g)
            return await maybe_bot(ctx, g)

        # ---------------- 🚪 ترک بازی
        if action == "lv":
            p = g.idx_of(uid)
            if p is None:
                return await answer(q, t(L, "not_player"), alert=True)
            g.over = True
            g.winner = (p + 1) % len(g.players) if g.ready else None
            stop_anim(g)
            cancel_timer(g)
            await answer(q, "👋")
            if g.winner is not None:
                return await finish(ctx, g, extra=t(L, "left", name=uname,
                                                    winner=g.name(g.winner)))
            await persist(g)
            return await edit(ctx, g, t(L, "cancelled"), InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(L, "new_game"), switch_inline_query="")]]))

        # ---------------- 📜 مرور بازی
        if action == "rp":
            await answer(q, t(L, "replaying"))
            return await replay(ctx, g)

        # ---------------- 🏅 جدول امتیازات این چت
        if action == "lb":
            rows = await db.leaderboard(g.chat_instance or "")
            if not rows:
                return await answer(q, t(L, "lb_empty"), alert=True)
            medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 10
            body = "\n".join(
                t(L, "lb_row", medal=medals[i], name=r["name"], elo=r["elo"],
                  w=r["wins"], l=r["losses"], d=r["draws"])
                for i, r in enumerate(rows)
            )
            return await answer(q, f"{t(L, 'lb_title')}\n\n{body}", alert=True)

    return await answer(q, "❓", alert=True)


# ================================================================ 🧹 نگهداری + 📈 متریک (ایده ۲۰)
async def housekeeping(ctx: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    stale = [gid for gid, g in GAMES.items() if now - g.updated_at > 24 * 3600]
    for gid in stale:
        stop_anim(GAMES[gid])
        cancel_timer(GAMES[gid])
        GAMES.pop(gid, None)
        await db.delete_game(gid)
    for k in [k for k, v in PENDING.items() if now - v[4] > 3600]:
        PENDING.pop(k, None)
    active = sum(1 for g in GAMES.values() if g.started and not g.over)
    log.info("📈 metrics | games=%d active=%d cleaned=%d", len(GAMES), active, len(stale))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    if isinstance(ctx.error, Conflict):
        log.error("❌ Telegram 409 Conflict: this token is already polling somewhere else")
        await ctx.application.stop()
        return
    log.exception("handler error", exc_info=ctx.error)


async def post_init(app: Application):
    await db.init()
    for d in await db.load_games():
        try:
            g = Game.from_dict(d)
            GAMES[g.gid] = g
        except Exception as e:
            log.warning("skip game: %s", e)
    log.info("🗄️ %d games restored", len(GAMES))


def main():
    acquire_single_instance()
    app = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(AIORateLimiter())   # 🛡️ جلوگیری از flood limit
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(housekeeping, interval=1800, first=600)

    log.info("🤖 ربات دوز راه افتاد!")
    if WEBHOOK_URL:                      # ایده ۲۰: webhook به‌جای polling
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
