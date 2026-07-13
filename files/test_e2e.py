# -*- coding: utf-8 -*-
"""🧪 شبیه‌سازی کامل: هر دکمه، هر endpoint، با ری‌استارت وسط بازی"""
import asyncio, os, sys, types
sys.path.insert(0, '.')
os.environ['BOT_TOKEN'] = '1:x'
if os.path.exists('dooz.sqlite3'):
    os.remove('dooz.sqlite3')

import bot, db
from game import Game, BOT_ID

FAILURES = []


class FakeBot:
    def __init__(self):
        self.messages = {}          # imid -> (text, markup)
    async def edit_message_text(self, inline_message_id=None, text=None, reply_markup=None, **kw):
        self.messages[inline_message_id] = (text, reply_markup)
    async def send_dice(self, chat_id=None, emoji=None):
        pass


class FakeJob:
    def schedule_removal(self): pass


class FakeJQ:
    def run_once(self, cb, when, data=None, name=None): return FakeJob()
    def run_repeating(self, *a, **k): return FakeJob()


class Ctx:
    def __init__(self, fb):
        self.bot = fb
        self.job_queue = FakeJQ()
        self.application = types.SimpleNamespace(bot=fb)


class User:
    def __init__(self, uid, name): self.id, self.first_name = uid, name


class Q:
    def __init__(self, data, user, imid):
        self.data, self.from_user, self.inline_message_id = data, user, imid
        self.chat_instance = "chat_test"
        self.answers = []
    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def click(ctx, data, user, imid):
    q = Q(data, user, imid)
    upd = types.SimpleNamespace(callback_query=q)
    await bot.on_callback(upd, ctx)
    return q


def buttons(fb, imid):
    _, markup = fb.messages.get(imid, ("", None))
    out = {}
    if markup:
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data:
                    out[b.callback_data] = b.text
    return out


def check(name, cond, detail=""):
    print(("✅" if cond else "❌"), name, detail if not cond else "")
    if not cond:
        FAILURES.append(name)


async def main():
    await db.init()
    fb = FakeBot()
    ctx = Ctx(fb)
    ali, sara, eve = User(101, "Ali"), User(102, "Sara"), User(103, "Eve")
    imid = "imid_1"

    # === سناریو ۱: بدترین حالت — بدون chosen، بدون PENDING (شبیه ری‌استارت قبل از اولین کلیک)
    gid = "deadbeef" + "3"          # gid ساخته‌شده قبل از ری‌استارت؛ حافظه خالی است
    bot.GAMES.clear(); bot.PENDING.clear()
    q = await click(ctx, f"set:{gid}:mode", ali, imid)
    check("recreate-after-restart: no 'expired' alert", not any("منقضی" in (a[0] or "") for a in q.answers), q.answers)
    check("lobby rendered", imid in fb.messages and "تنظیمات" in fb.messages[imid][0])
    check("clicker adopted as host", bot.GAMES[gid].host_id == 101)

    # برگرداندن حالت به کلاسیک (کلیک اول آن را چرخانده بود)
    for _ in range(3):
        await click(ctx, f"set:{gid}:mode", ali, imid)
    check("mode back to classic", bot.GAMES[gid].mode == "classic")
    await click(ctx, f"set:{gid}:size", ali, imid)
    check("size cycled to 4", bot.GAMES[gid].size == 4)
    # non-host cannot change settings
    q = await click(ctx, f"set:{gid}:mode", sara, imid)
    check("non-host blocked from settings", any(a[1] for a in q.answers))

    # === شروع بازی دونفره
    q = await click(ctx, f"go:{gid}", ali, imid)
    g = bot.GAMES[gid]
    check("started", g.started and g.players == [101])
    await asyncio.sleep(0.05)   # اجازه بده انیمیشن یک دور بزند
    btns = buttons(fb, imid)
    check("join button visible after start", any(k.startswith("jn:") for k in btns), btns)

    # سازنده نمی‌تواند join کند
    q = await click(ctx, f"jn:{gid}", ali, imid)
    check("host cannot join own game", any(a[1] for a in q.answers))
    # حریف join می‌کند
    q = await click(ctx, f"jn:{gid}", sara, imid)
    g = bot.GAMES[gid]
    check("sara joined", g.ready and g.players == [101, 102])
    btns = buttons(fb, imid)
    check("board rendered (16 cells)", sum(1 for k in btns if k.startswith("mv:")) == 16, len(btns))

    # نفر سوم نمی‌تواند join یا حرکت کند
    q = await click(ctx, f"jn:{gid}", eve, imid)
    check("full game rejects join", any(a[1] for a in q.answers))
    q = await click(ctx, f"mv:{gid}:0", eve, imid)
    check("outsider cannot move", any("نیستی" in (a[0] or "") for a in q.answers))

    # نوبت: سارا (نفر دوم) نمی‌تواند اول حرکت کند
    q = await click(ctx, f"mv:{gid}:0", sara, imid)
    check("out-of-turn blocked", any("نوبت" in (a[0] or "") for a in q.answers))

    # === وسط بازی: ری‌استارت کامل ربات (حافظه پاک، فقط SQLite)
    await click(ctx, f"mv:{gid}:0", ali, imid)     # X: 0
    bot.GAMES.clear(); bot.PENDING.clear()
    for gd in await db.load_games():
        pass  # post_init معمولاً همه را برمی‌گرداند؛ اینجا مسیر lazy را تست می‌کنیم
    q = await click(ctx, f"mv:{gid}:4", sara, imid)   # O: 4 — باید از DB برگردد
    g = bot.GAMES.get(gid)
    check("mid-game restart survived", g is not None and g.board[0] != "⠀" and g.board[4] != "⠀")
    check("history intact after restart", len(g.history) == 2, g.history)

    # === ادامه تا برد X (خط عمودی ستون 1: 1,5... صبر کن 5 پر است؛ ستون 2: 2,6,10,14)
    await click(ctx, f"mv:{gid}:2", ali, imid)
    await click(ctx, f"mv:{gid}:5", sara, imid)
    await click(ctx, f"mv:{gid}:6", ali, imid)
    await click(ctx, f"mv:{gid}:8", sara, imid)
    await click(ctx, f"mv:{gid}:10", ali, imid)
    await click(ctx, f"mv:{gid}:9", sara, imid)
    q = await click(ctx, f"mv:{gid}:14", ali, imid)   # X wins col 2: 2,6,10,14
    g = bot.GAMES[gid]
    check("win detected", g.over and g.winner == 0, (g.over, g.winner))
    check("win cells marked", sorted(g.win_cells) == [2, 6, 10, 14], g.win_cells)
    txt, _ = fb.messages[imid]
    check("congrats + elo in message", "تبریک" in txt and "📈" in txt, txt[-120:])
    btns = buttons(fb, imid)
    check("rematch+replay+lb buttons", any(k.startswith("re:") for k in btns)
          and any(k.startswith("rp:") for k in btns) and any(k.startswith("lb:") for k in btns))

    # حرکت بعد از پایان بازی
    q = await click(ctx, f"mv:{gid}:1", sara, imid)
    check("move after game over blocked", any(a[1] for a in q.answers))

    # === مرور بازی
    q = await click(ctx, f"rp:{gid}", ali, imid)
    check("replay ran and board restored", bot.GAMES[gid].board[14] != "⠀")

    # === جدول امتیازات
    q = await click(ctx, f"lb:{gid}", ali, imid)
    check("leaderboard alert shown", any(a[0] and "🏅" in a[0] for a in q.answers), q.answers)

    # === بازی مجدد: شروع‌کننده باید عوض شود
    await click(ctx, f"re:{gid}", sara, imid)
    g = bot.GAMES[gid]
    check("rematch resets board", all(c == "⠀" for c in g.board) and not g.over)
    check("starter swapped", g.turn_idx == 1)
    q = await click(ctx, f"mv:{gid}:0", ali, imid)
    check("old starter blocked in rematch", any("نوبت" in (a[0] or "") for a in q.answers))
    await click(ctx, f"mv:{gid}:0", sara, imid)

    # === undo با تأیید
    q = await click(ctx, f"ud:{gid}", ali, imid)
    check("undo request pending", bot.GAMES[gid].pending == "ud")
    q = await click(ctx, f"ok:{gid}", ali, imid)
    check("requester cannot self-approve", bot.GAMES[gid].pending == "ud")
    q = await click(ctx, f"ok:{gid}", sara, imid)
    g = bot.GAMES[gid]
    check("undo applied", g.board[0] == "⠀" and g.pending is None)

    # === پیشنهاد مساوی + رد
    await click(ctx, f"mv:{gid}:0", sara, imid)
    await click(ctx, f"dr:{gid}", ali, imid)
    q = await click(ctx, f"no:{gid}", sara, imid)
    check("draw offer rejected, game continues", not bot.GAMES[gid].over)

    # === ترک بازی
    q = await click(ctx, f"lv:{gid}", ali, imid)
    g = bot.GAMES[gid]
    check("leave -> opponent wins", g.over and g.winner == 1)

    # === سناریو ۲: بازی با ربات، end-to-end
    gid2 = "cafebabe" + "3"
    imid2 = "imid_2"
    bot.PENDING[gid2] = (3, "fa", 101, "Ali", 0)
    await click(ctx, f"set:{gid2}:opp", ali, imid2)        # دوست -> ربات
    check("vs_bot toggled", bot.GAMES[gid2].vs_bot)
    await click(ctx, f"set:{gid2}:diff", ali, imid2)       # medium -> hard
    check("difficulty hard", bot.GAMES[gid2].difficulty == "hard")
    await click(ctx, f"go:{gid2}", ali, imid2)
    g2 = bot.GAMES[gid2]
    check("bot game started instantly", g2.started and BOT_ID in g2.players)
    # بازی کامل با ربات سخت — ربات نباید ببازد
    for _ in range(30):
        if g2.over:
            break
        if g2.players[g2.turn_idx] == 101:
            cell = next(i for i, v in enumerate(g2.board) if v == "⠀")
            await click(ctx, f"mv:{gid2}:{cell}", ali, imid2)
        else:
            await asyncio.sleep(0.05)
    check("hard bot never loses", not (g2.over and g2.winner is not None and g2.players[g2.winner] == 101),
          (g2.winner, g2.board))
    check("bot game concluded", g2.over, g2.board)

    # === سناریو ۳: دکمه‌ی خراب/ناشناخته
    q = await click(ctx, "xx:zzz", ali, imid)
    q = await click(ctx, "mv:badgid9zzz:0", ali, imid)
    q = await click(ctx, f"mv:{gid}:999", sara, imid)
    check("garbage callbacks handled without crash", True)

    print()
    if FAILURES:
        print("❌ FAILED:", FAILURES); sys.exit(1)
    print("🎉 ALL ENDPOINT TESTS PASSED")

asyncio.run(main())
