# -*- coding: utf-8 -*-
"""
🎮 منطق بازی دوز: حالت‌ها، تشخیص برد، هوش مصنوعی (minimax + alpha-beta)
ایده‌های ۱، ۳، ۴، ۵، ۷، ۱۳
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

EMPTY = "⠀"  # کاراکتر خالی (braille blank) — دکمه خالی به نظر می‌رسد

# 🎨 ایده ۱۳: ست‌های ایموجی
EMOJI_SETS: Dict[str, List[str]] = {
    "classic": ["❌", "⭕️", "🔷", "⭐️"],
    "animals": ["🐱", "🐶", "🦊", "🐼"],
    "elements": ["🔥", "💧", "🌿", "🪨"],
    "space": ["⚡️", "🌙", "☄️", "🪐"],
}
EMOJI_ORDER = list(EMOJI_SETS)

LINE = "🟥"  # خط قرمز برنده

SIZES = [3, 4, 5, 6]
WIN_LEN: Dict[int, int] = {3: 3, 4: 4, 5: 4, 6: 5}

# 🎯 حالت‌های بازی (ایده ۳، ۴، ۵)
MODES = ["classic", "misere", "gravity", "three"]
DIFFICULTIES = ["easy", "medium", "hard"]
SERIES_OPTIONS = [1, 3, 5]          # ایده ۱۱: Best-of
TIMER_OPTIONS = [0, 30, 60]         # ایده ۲: ثانیه (۰ = خاموش)

BOT_ID = -1  # شناسه‌ی مجازی ربات


@dataclass
class Game:
    gid: str
    size: int = 3
    mode: str = "classic"
    emoji_set: str = "classic"
    n_players: int = 2                 # ایده ۷: ۲ تا ۴ بازیکن
    vs_bot: bool = False               # ایده ۱
    difficulty: str = "medium"
    timer: int = 0                     # ایده ۲
    series_len: int = 1                # ایده ۱۱
    lang: str = "fa"                   # ایده ۱۸

    started: bool = False
    board: List[str] = field(default_factory=list)
    players: List[int] = field(default_factory=list)        # user_id ها به ترتیب نوبت
    names: Dict[str, str] = field(default_factory=dict)     # str(user_id) -> name
    turn_idx: int = 0
    start_idx: int = 0                                      # شروع‌کننده (برای بازی مجدد جابه‌جا می‌شود)
    winner: Optional[int] = None                            # index بازیکن یا None
    is_draw: bool = False
    win_cells: List[int] = field(default_factory=list)
    over: bool = False
    history: List[Tuple[int, int]] = field(default_factory=list)  # (player_idx, cell) — ایده ۶ و ۱۶
    series_score: List[int] = field(default_factory=list)
    series_done: bool = False
    pending: Optional[str] = None       # "undo" یا "draw" — ایده ۶ و ۱۷
    pending_by: Optional[int] = None
    host_id: Optional[int] = None
    inline_message_id: Optional[str] = None
    chat_instance: Optional[str] = None  # برای جدول امتیازات هر چت (ایده ۸)
    deadline: float = 0.0
    updated_at: float = field(default_factory=time.time)

    # فیلدهای runtime (ذخیره نمی‌شوند)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    anim_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)
    timer_job: object = field(default=None, repr=False, compare=False)

    # ----------------------------------------------------------- helpers
    def __post_init__(self):
        if not self.board:
            self.reset_board()
        if not self.series_score:
            self.series_score = [0] * max(self.n_players, 2)

    def reset_board(self):
        self.board = [EMPTY] * (self.size * self.size)
        self.win_cells = []
        self.winner = None
        self.is_draw = False
        self.over = False
        self.history = []
        self.pending = None
        self.pending_by = None

    @property
    def symbols(self) -> List[str]:
        return EMOJI_SETS[self.emoji_set][: self.n_players]

    def sym(self, idx: int) -> str:
        return self.symbols[idx]

    def name(self, idx: int) -> str:
        if idx >= len(self.players):
            return "…"
        uid = self.players[idx]
        if uid == BOT_ID:
            return "🤖"
        return self.names.get(str(uid), f"P{idx + 1}")

    def idx_of(self, uid: int) -> Optional[int]:
        try:
            return self.players.index(uid)
        except ValueError:
            return None

    @property
    def ready(self) -> bool:
        return len(self.players) >= self.n_players

    @property
    def win_len(self) -> int:
        return WIN_LEN[self.size]

    @property
    def bot_turn(self) -> bool:
        return (
            self.vs_bot
            and self.started
            and not self.over
            and self.players[self.turn_idx] == BOT_ID
        )

    def to_dict(self) -> dict:
        d = {}
        for k in (
            "gid", "size", "mode", "emoji_set", "n_players", "vs_bot", "difficulty",
            "timer", "series_len", "lang", "started", "board", "players", "names",
            "turn_idx", "start_idx", "winner", "is_draw", "win_cells", "over",
            "history", "series_score", "series_done", "pending", "pending_by",
            "host_id", "inline_message_id", "chat_instance", "deadline", "updated_at",
        ):
            d[k] = getattr(self, k)
        d["history"] = [list(h) for h in self.history]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Game":
        allowed = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        allowed["history"] = [tuple(h) for h in allowed.get("history", [])]
        return cls(**allowed)


# ---------------------------------------------------------------- قوانین
def legal_moves(g: Game) -> List[int]:
    n = g.size
    if g.mode == "gravity":
        # 🪂 ایده ۳: مهره از بالا می‌افتد → فقط پایین‌ترین خانه‌ی خالی هر ستون
        out = []
        for c in range(n):
            for r in range(n - 1, -1, -1):
                if g.board[r * n + c] == EMPTY:
                    out.append(r * n + c)
                    break
        return out
    return [i for i, v in enumerate(g.board) if v == EMPTY]


def drop_target(g: Game, idx: int) -> Optional[int]:
    """در حالت گراویتی هر خانه‌ای از یک ستون به پایین‌ترین خانه‌ی خالی همان ستون تبدیل می‌شود."""
    if g.mode != "gravity":
        return idx if g.board[idx] == EMPTY else None
    n = g.size
    c = idx % n
    for r in range(n - 1, -1, -1):
        if g.board[r * n + c] == EMPTY:
            return r * n + c
    return None


def line_through(board: List[str], n: int, need: int, idx: int) -> Optional[List[int]]:
    sym = board[idx]
    if sym == EMPTY:
        return None
    r0, c0 = divmod(idx, n)
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        cells = [idx]
        for sign in (1, -1):
            r, c = r0 + dr * sign, c0 + dc * sign
            while 0 <= r < n and 0 <= c < n and board[r * n + c] == sym:
                cells.append(r * n + c)
                r, c = r + dr * sign, c + dc * sign
        if len(cells) >= need:
            cells.sort()
            # فقط `need` خانه‌ی شامل idx را برگردان (برای خط قرمز تمیزتر)
            return cells
    return None


def apply_move(g: Game, player_idx: int, cell: int) -> int:
    """مهره را می‌گذارد و در حالت سه‌مهره‌ای قدیمی‌ترین مهره را برمی‌دارد. خانه‌ی نهایی را برمی‌گرداند."""
    g.board[cell] = g.sym(player_idx)
    g.history.append((player_idx, cell))

    if g.mode == "three":
        # ♻️ ایده ۵: هر بازیکن حداکثر ۳ مهره
        mine = [c for p, c in g.history if p == player_idx]
        if len(mine) > 3:
            oldest = mine[0]
            g.board[oldest] = EMPTY
    return cell


def evaluate_end(g: Game, cell: int, player_idx: int) -> None:
    """برد/مساوی را بعد از حرکت تعیین می‌کند."""
    cells = line_through(g.board, g.size, g.win_len, cell)
    if cells:
        g.win_cells = cells
        g.over = True
        if g.mode == "misere":
            # 🙃 ایده ۴: هر کس خط کند، می‌بازد → برنده نفر بعدی
            g.winner = (player_idx + 1) % len(g.players)
        else:
            g.winner = player_idx
        return
    if g.mode == "three":
        return  # این حالت هیچ‌وقت مساوی نمی‌شود
    if not legal_moves(g):
        g.over = True
        g.is_draw = True


def next_turn(g: Game) -> None:
    g.turn_idx = (g.turn_idx + 1) % len(g.players)


def is_diagonal_win(g: Game) -> bool:
    if len(g.win_cells) < 2:
        return False
    n = g.size
    a, b = divmod(g.win_cells[0], n), divmod(g.win_cells[1], n)
    return a[0] != b[0] and a[1] != b[1]


# ---------------------------------------------------------------- 🤖 هوش مصنوعی (ایده ۱)
def _score_window(window: List[str], me: str, need: int) -> float:
    others = [v for v in window if v not in (EMPTY, me)]
    mine = window.count(me)
    if others:
        opp = max(set(others), key=others.count)
        cnt = window.count(opp)
        if mine:
            return 0.0
        return -(10 ** cnt) if cnt < need else -(10 ** 6)
    if mine == 0:
        return 0.0
    return 10 ** mine if mine < need else 10 ** 6


def heuristic(g: Game, me_sym: str) -> float:
    n, need = g.size, g.win_len
    total = 0.0
    b = g.board
    for r in range(n):
        for c in range(n):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                rr, cc = r + dr * (need - 1), c + dc * (need - 1)
                if not (0 <= rr < n and 0 <= cc < n):
                    continue
                win = [b[(r + dr * k) * n + (c + dc * k)] for k in range(need)]
                total += _score_window(win, me_sym, need)
    if g.mode == "misere":
        total = -total  # 🙃 در حالت معکوس، خط زدن بد است
    return total


def _terminal_score(g: Game, cell: int, mover: str, me_sym: str, depth: int) -> Optional[float]:
    cells = line_through(g.board, g.size, g.win_len, cell)
    if not cells:
        return None
    maker_wins = g.mode != "misere"
    good = (mover == me_sym) == maker_wins
    return (10 ** 7 + depth) if good else -(10 ** 7 + depth)


def _minimax(g: Game, depth: int, alpha: float, beta: float,
             turn: int, me: int, deadline: float) -> float:
    if time.time() > deadline:
        raise TimeoutError
    moves = legal_moves(g)
    if depth == 0 or not moves:
        return heuristic(g, g.sym(me))

    maximizing = turn == me
    best = -math.inf if maximizing else math.inf
    # مرتب‌سازی ساده: مرکز اول (بهبود alpha-beta)
    center = (g.size - 1) / 2
    moves.sort(key=lambda i: abs(i // g.size - center) + abs(i % g.size - center))

    for cell in moves[:12]:              # محدود کردن شاخه‌ها روی صفحه‌های بزرگ
        snapshot = list(g.board)
        hist_len = len(g.history)
        real = apply_move(g, turn, cell)
        term = _terminal_score(g, real, g.sym(turn), g.sym(me), depth)
        if term is not None:
            val = term
        else:
            val = _minimax(g, depth - 1, alpha, beta,
                           (turn + 1) % len(g.players), me, deadline)
        g.board = snapshot
        del g.history[hist_len:]

        if maximizing:
            best = max(best, val)
            alpha = max(alpha, val)
        else:
            best = min(best, val)
            beta = min(beta, val)
        if beta <= alpha:
            break
    return best


def bot_move(g: Game) -> int:
    """حرکت ربات را برمی‌گرداند (خانه‌ی نهایی، با در نظر گرفتن گراویتی)."""
    moves = legal_moves(g)
    me = g.turn_idx

    if g.difficulty == "easy":
        return random.choice(moves)

    # متوسط: برد فوری بگیر، باخت فوری را ببند، وگرنه heuristic عمق ۱
    depth = {"medium": 2, "hard": 4 if g.size <= 4 else 3}[g.difficulty]
    if g.size >= 5 and g.difficulty == "hard":
        depth = 2

    deadline = time.time() + (4.0 if g.difficulty == "hard" else 1.5)
    best_val, best_move = -math.inf, random.choice(moves)
    center = (g.size - 1) / 2
    moves.sort(key=lambda i: abs(i // g.size - center) + abs(i % g.size - center))

    for cell in moves:
        snapshot = list(g.board)
        hist_len = len(g.history)
        real = apply_move(g, me, cell)
        term = _terminal_score(g, real, g.sym(me), g.sym(me), depth)
        try:
            if term is not None:
                val = term
            else:
                val = _minimax(g, depth - 1, -math.inf, math.inf,
                               (me + 1) % len(g.players), me, deadline)
        except TimeoutError:
            val = heuristic(g, g.sym(me))
        finally:
            g.board = snapshot
            del g.history[hist_len:]

        if val > best_val:
            best_val, best_move = val, cell
    return best_move
