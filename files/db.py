# -*- coding: utf-8 -*-
"""
🗄️ ذخیره‌سازی با SQLite (ایده ۱۹) + Elo (۹) + آمار (۱۲) + دستاورد (۱۰) + جدول امتیازات (۸)
تمام I/O روی thread جدا اجرا می‌شود تا event loop بلاک نشود.
"""

import asyncio
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dooz.sqlite3")
START_ELO = 1000
K_FACTOR = 24
GAME_TTL = 7 * 24 * 3600

_conn: Optional[sqlite3.Connection] = None
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    gid TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    uid INTEGER PRIMARY KEY,
    name TEXT,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    games INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    best_streak INTEGER DEFAULT 0,
    elo INTEGER DEFAULT 1000,
    moves INTEGER DEFAULT 0,
    lang TEXT DEFAULT 'fa'
);
CREATE TABLE IF NOT EXISTS achievements (
    uid INTEGER, key TEXT, ts REAL,
    PRIMARY KEY (uid, key)
);
CREATE TABLE IF NOT EXISTS chat_stats (
    chat_instance TEXT, uid INTEGER, name TEXT,
    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0,
    elo INTEGER DEFAULT 1000,
    PRIMARY KEY (chat_instance, uid)
);
CREATE TABLE IF NOT EXISTS cell_stats (
    uid INTEGER, size INTEGER, cell INTEGER, n INTEGER DEFAULT 0,
    PRIMARY KEY (uid, size, cell)
);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


async def _run(fn, *a):
    async with _lock:                      # sqlite3 تک‌نویسنده است
        return await asyncio.to_thread(fn, *a)


async def init():
    await _run(_connect)


# ---------------------------------------------------------------- بازی‌ها
def _save_game(gid: str, data: dict):
    c = _connect()
    c.execute(
        "INSERT INTO games(gid,data,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(gid) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (gid, json.dumps(data, ensure_ascii=False), time.time()),
    )
    c.commit()


async def save_game(gid: str, data: dict):
    await _run(_save_game, gid, data)


def _load_games() -> List[dict]:
    c = _connect()
    c.execute("DELETE FROM games WHERE updated_at < ?", (time.time() - GAME_TTL,))
    c.commit()
    return [json.loads(r["data"]) for r in c.execute("SELECT data FROM games")]


async def load_games() -> List[dict]:
    return await _run(_load_games)


def _load_game(gid: str) -> Optional[dict]:
    c = _connect()
    r = c.execute("SELECT data FROM games WHERE gid=?", (gid,)).fetchone()
    return json.loads(r["data"]) if r else None


async def load_game(gid: str) -> Optional[dict]:
    return await _run(_load_game, gid)


def _delete_game(gid: str):
    c = _connect()
    c.execute("DELETE FROM games WHERE gid=?", (gid,))
    c.commit()


async def delete_game(gid: str):
    await _run(_delete_game, gid)


# ---------------------------------------------------------------- کاربران
def _ensure_user(c, uid: int, name: str = ""):
    c.execute("INSERT OR IGNORE INTO users(uid,name,elo) VALUES(?,?,?)", (uid, name, START_ELO))
    if name:
        c.execute("UPDATE users SET name=? WHERE uid=?", (name, uid))


def _get_user(uid: int, name: str = "") -> dict:
    c = _connect()
    _ensure_user(c, uid, name)
    c.commit()
    return dict(c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone())


async def get_user(uid: int, name: str = "") -> dict:
    return await _run(_get_user, uid, name)


def _set_lang(uid: int, lang: str):
    c = _connect()
    _ensure_user(c, uid)
    c.execute("UPDATE users SET lang=? WHERE uid=?", (lang, uid))
    c.commit()


async def set_lang(uid: int, lang: str):
    await _run(_set_lang, uid, lang)


def _get_achievements(uid: int) -> List[str]:
    c = _connect()
    return [r["key"] for r in c.execute("SELECT key FROM achievements WHERE uid=?", (uid,))]


async def get_achievements(uid: int) -> List[str]:
    return await _run(_get_achievements, uid)


def _grant(uid: int, keys: List[str]) -> List[str]:
    """دستاوردهای تازه‌قفل‌گشوده را برمی‌گرداند (ایده ۱۰)."""
    c = _connect()
    have = set(_get_achievements(uid))
    new = [k for k in keys if k not in have]
    for k in new:
        c.execute("INSERT OR IGNORE INTO achievements(uid,key,ts) VALUES(?,?,?)", (uid, k, time.time()))
    c.commit()
    return new


async def grant(uid: int, keys: List[str]) -> List[str]:
    if not keys:
        return []
    return await _run(_grant, uid, keys)


# ---------------------------------------------------------------- Elo (ایده ۹)
def expected(ra: int, rb: int) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def _record_result(
    chat_instance: Optional[str],
    winner: Optional[Tuple[int, str]],
    losers: List[Tuple[int, str]],
    draw_players: List[Tuple[int, str]],
    cells: List[Tuple[int, int, int]],
) -> Dict[int, int]:
    """
    نتیجه را ثبت و تغییرات Elo را برمی‌گرداند: {uid: delta}
    - winner: (uid, name) یا None برای مساوی
    - cells: (uid, size, cell) برای آمار خانه‌ی محبوب
    """
    c = _connect()
    deltas: Dict[int, int] = {}

    everyone = ([winner] if winner else []) + losers + draw_players
    for uid, name in everyone:
        _ensure_user(c, uid, name)

    def elo_of(uid):
        return c.execute("SELECT elo FROM users WHERE uid=?", (uid,)).fetchone()["elo"]

    if winner:
        w_uid, w_name = winner
        w_elo = elo_of(w_uid)
        for l_uid, _ in losers:
            l_elo = elo_of(l_uid)
            e = expected(w_elo, l_elo)
            d = round(K_FACTOR * (1 - e))
            deltas[w_uid] = deltas.get(w_uid, 0) + d
            deltas[l_uid] = deltas.get(l_uid, 0) - d
        c.execute(
            "UPDATE users SET wins=wins+1, games=games+1, streak=streak+1, "
            "best_streak=MAX(best_streak, streak+1) WHERE uid=?", (w_uid,)
        )
        for l_uid, _ in losers:
            c.execute(
                "UPDATE users SET losses=losses+1, games=games+1, streak=0 WHERE uid=?",
                (l_uid,),
            )
    else:
        ids = [u for u, _ in draw_players]
        if len(ids) == 2:
            a, b = ids
            ea = expected(elo_of(a), elo_of(b))
            d = round(K_FACTOR * (0.5 - ea))
            deltas[a], deltas[b] = d, -d
        for uid in ids:
            c.execute("UPDATE users SET draws=draws+1, games=games+1 WHERE uid=?", (uid,))

    for uid, d in deltas.items():
        c.execute("UPDATE users SET elo=elo+? WHERE uid=?", (d, uid))

    # جدول امتیازات این چت (ایده ۸)
    if chat_instance:
        for uid, name in everyone:
            elo = elo_of(uid)
            c.execute(
                "INSERT OR IGNORE INTO chat_stats(chat_instance,uid,name,elo) VALUES(?,?,?,?)",
                (chat_instance, uid, name, elo),
            )
            c.execute("UPDATE chat_stats SET name=?, elo=? WHERE chat_instance=? AND uid=?",
                      (name, elo, chat_instance, uid))
        if winner:
            c.execute("UPDATE chat_stats SET wins=wins+1 WHERE chat_instance=? AND uid=?",
                      (chat_instance, winner[0]))
            for l_uid, _ in losers:
                c.execute("UPDATE chat_stats SET losses=losses+1 WHERE chat_instance=? AND uid=?",
                          (chat_instance, l_uid))
        else:
            for uid, _ in draw_players:
                c.execute("UPDATE chat_stats SET draws=draws+1 WHERE chat_instance=? AND uid=?",
                          (chat_instance, uid))

    # آمار خانه‌ها (ایده ۱۲)
    for uid, size, cell in cells:
        c.execute(
            "INSERT INTO cell_stats(uid,size,cell,n) VALUES(?,?,?,1) "
            "ON CONFLICT(uid,size,cell) DO UPDATE SET n=n+1", (uid, size, cell)
        )
        c.execute("UPDATE users SET moves=moves+1 WHERE uid=?", (uid,))

    c.commit()
    return deltas


async def record_result(chat_instance, winner, losers, draw_players, cells) -> Dict[int, int]:
    return await _run(_record_result, chat_instance, winner, losers, draw_players, cells)


def _leaderboard(chat_instance: str, limit: int = 10) -> List[dict]:
    c = _connect()
    rows = c.execute(
        "SELECT * FROM chat_stats WHERE chat_instance=? AND uid>0 "
        "ORDER BY elo DESC, wins DESC LIMIT ?",
        (chat_instance, limit),
    )
    return [dict(r) for r in rows]


async def leaderboard(chat_instance: str, limit: int = 10) -> List[dict]:
    return await _run(_leaderboard, chat_instance, limit)


def _fav_cell(uid: int) -> Optional[dict]:
    c = _connect()
    r = c.execute(
        "SELECT size, cell, n FROM cell_stats WHERE uid=? ORDER BY n DESC LIMIT 1", (uid,)
    ).fetchone()
    return dict(r) if r else None


async def fav_cell(uid: int) -> Optional[dict]:
    return await _run(_fav_cell, uid)
