# bot_min.py — Читалкин&Циферкин (Шаг C: прод с Webhook)
# Локально: polling. В проде: если задан PUBLIC_URL — webhook.
# Функции: умный /story (возраст/герой/мораль/длина), PDF-обложка, лимит 3/день (Мск), JSON-память.

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import random, json, os
from pathlib import Path
from io import BytesIO
from typing import Dict, Any, List

from PIL import Image, ImageDraw
from fpdf import FPDF
from telegram import Update, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # напр.: https://имя.onrender.com (без завершающего /)
PORT = int(os.getenv("PORT", "8080"))  # Render/Railway задают этот порт автоматически
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")  # опционально: свой секретный путь

MAX_STORIES_PER_DAY = 3
TZ_MSK = ZoneInfo("Europe/Moscow")
BOT_USERNAME: str | None = None

DATA_DIR = Path(".")
STATS_PATH = DATA_DIR / "stats.json"
STORIES_PATH = DATA_DIR / "stories.json"

last_story_by_user: Dict[int, Dict[str, Any]] = {}

# ==== Время/JSON ====
def msk_now() -> datetime: return datetime.now(TZ_MSK)
def msk_today_str() -> str: return msk_now().strftime("%Y-%m-%d")
def seconds_to_midnight_msk() -> int:
    now = msk_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())

def load_json(p: Path) -> Dict[str, Any]:
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def save_json(p: Path, data: Dict[str, Any]):
    try: p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass

stats_all: Dict[str, Dict[str, Any]] = load_json(STATS_PATH)
stories_all: Dict[str, Dict[str, Any]] = load_json(STORIES_PATH)

def default_stats() -> Dict[str, Any]:
    return {
        "stories_total": 0, "math_total": 0,
        "today_date": msk_today_str(), "today_stories": 0,
        "last_story_ts": None, "last_story_title": None,
    }

def default_user_stories() -> Dict[str, Any]: 
    return {"last": None, "history": []}

def get_user_stats(uid: int) -> Dict[str, Any]:
    u = stats_all.get(str(uid))
    if not u:
        u = default_stats(); stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)
    if u.get("today_date") != msk_today_str():
        u["today_date"] = msk_today_str(); u["today_stories"] = 0; save_json(STATS_PATH, stats_all)
    return u

def inc_story_counters(uid: int, title: str):
    u = get_user_stats(uid)
    u["stories_total"] = int(u.get("stories_total", 0)) + 1
    u["today_stories"] = int(u.get("today_stories", 0)) + 1
    u["last_story_ts"] = msk_now().isoformat()
    u["last_story_title"] = title
    stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)
    print(f"[LIMIT] uid={uid} today_stories={u['today_stories']}/{MAX_STORIES_PER_DAY}")

def inc_math_counter(uid: int):
    u = get_user_stats(uid)
    u["math_total"] = int(u.get("math_total", 0)) + 1
    stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)

def store_user_story(uid: int, story: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    stamped = dict(story); stamped["ts"] = msk_now().isoformat()
    rec["last"] = stamped
    history: List[Dict[str, Any]] = rec.get("history", [])
    history.append(stamped); rec["history"] = history[-20:]
    stories_all[str(uid)] = rec; save_json(STORIES_PATH, stories_all)
    last_story_by_user[uid] = stamped

# ==== Обложка/синтез текста ====
def make_cover_png_bytes(title: str) -> BytesIO:
    img = Image.new("RGB", (2480, 3508), (246, 247, 251))
    d = ImageDraw.Draw(img)
    d.rectangle((100, 100, 2380, 3408), outline=(59, 92, 204), width=16)
    d.text((180, 300), (title or "Сказка").strip()[:40], fill=(34, 38, 49))
    bio = BytesIO(); img.save(bio, format="PNG"); bio.seek(0); return bio

def synthesize_story(age: int, hero: str, moral: str, length: str) -> Dict[str, Any]:
    title = f"{hero.capitalize()} и урок про «{moral}»"
    length_map = {"короткая": 2, "средняя": 4, "длинная": 6}
    paras = length_map.get(length.lower(), 3)
    tone_key = "young" if age <= 6 else ("mid" if age <= 10 else "teen")
    tone_intro = {
        "young": f"{hero.capitalize()} проснулся(ась) и потянулся(ась). Сегодня будет особенный день!",
        "mid": f"{hero.capitalize()} давно мечтал(а) узнать, что такое {moral}.",
        "teen": f"{hero.capitalize()} думал(а), что всё знает про {moral}, но жизнь приготовила сюрприз."
    }
    tone_body = [
        f"По дороге {hero} встретил(а) друга и вместе они решили помочь тем, кому это нужно.",
        f"Не всё сразу получалось, но {hero} не сдавался(ась) и пробовал(а) снова.",
        f"Оказалось, что маленький шаг может изменить целый день."
    ]
    tone_end = {
        "young": "Вечером все радовались и пили какао. Тёплый день удался!",
        "mid": "К вечеру стало ясно, что главное — быть честным с собой и добрым к другим.",
        "teen": "Домой {hero} возвращался(ась) молча, но с новым пониманием и уверенностью."
    }
    parts = [tone_intro[tone_key]]
    for _ in range(paras - 2): parts.append(random.choice(tone_body))
    parts.append(tone_end[tone_key].replace("{hero}", hero))
    text = "\n\n".join(parts)
    moral_text = f"Важно помнить: {moral}. Даже маленький поступок делает мир теплее."
    questions = [
        f"Что {hero} понял(а) про {moral}?",
        f"Какие трудности встретились {hero}?",
        "Как маленькие шаги помогают менять день?",
        "Как бы ты поступил(а) на месте героя?"
    ]
    return {"title": title, "text": text, "moral": moral_text, "questions": questions}

# ==== PDF ====
class StoryPDF(FPDF):
    def header(self): pass

def render_story_pdf(path: Path, data: Dict[str, Any], cover_png: bytes | None):
    pdf = StoryPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    if cover_png:
        bio = BytesIO(cover_png); bio.seek(0)
        pdf.image(bio, x=0, y=0, w=210, h=297, type="PNG")
    else:
        pdf.set_y(40); pdf.set_font("Helvetica", style="B", size=28)
        pdf.multi_cell(0, 12, data["title"], align="C")
    pdf.add_page()
    pdf.set_font("Helvetica", style="B", size=16)
    pdf.multi_cell(0, 8, data["title"]); pdf.ln(1)
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, f"Создано ИИ • Возраст 6+ • {msk_now().strftime('%d.%m.%Y')}")
    pdf.ln(4)
    pdf.set_font("Helvetica", size=12)
    for para in data["text"].split("\n\n"):
        pdf.multi_cell(0, 7, para); pdf.ln(2)
    pdf.ln(2); pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 7, "Мораль", ln=1)
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 7, data["moral"])
    path = Path(path); pdf.output(str(path))
    try: print(f"[PDF] Готово: {path.name} ({os.path.getsize(str(path))} байт)")
    except Exception: pass

# ==== UI ====
def build_start_html() -> str:
    u = BOT_USERNAME or "your_bot"
    return (
        "<b>Привет!</b>\n"
        "<b>Я — Читалкин&Циферкин 🦉➕🧮</b>\n\n"
        "• 🧚‍♀️ <b>Сказка</b>\n"
        f"  <a href=\"https://t.me/{u}?start=story\">Запустить</a> — подберу по возрасту и теме\n\n"
        "• 🧮 <b>Математика</b>\n"
        f"  <a href=\"https://t.me/{u}?start=math\">Запустить</a> — 10 минут примеров\n\n"
        "• 👪 <b>Отчёт родителю</b>\n"
        f"  <a href=\"https://t.me/{u}?start=parent\">Показать</a>\n\n"
        "• 🗑 <b>Удалить данные</b>\n"
        f"  <a href=\"https://t.me/{u}?start=delete\">Очистить</a>\n\n"
        "<i>Дневной лимит: 3 сказки. Сброс — в 00:00 по Мск.</i>"
    )

# ==== Команды ====
def _safe_int(text: str, default: int) -> int:
    try: return max(3, min(14, int(text)))
    except Exception: return default

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args:
        p = args[0].strip().lower()
        if p == "story":  await story_cmd(update, context);  return
        if p == "math":   await math_cmd(update, context);   return
        if p == "parent": await parent_cmd(update, context); return
        if p == "delete": await delete_cmd(update, context); return
    await update.effective_message.reply_html(build_start_html(), disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(build_start_html(), disable_web_page_preview=True)

# --- story state machine (user_data) ---
async def story_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ustat = get_user_stats(uid)
    if ustat["today_stories"] >= MAX_STORIES_PER_DAY:
        secs = seconds_to_midnight_msk(); h = secs // 3600; m = (secs % 3600) // 60
        await update.effective_message.reply_text(
            "На сегодня лимит сказок исчерпан 🌙 (3/день).\n"
            f"Новый день через {h} ч {m} мин по Мск.\n"
            "Пока можно заняться 🧮 математикой: введите /math."
        )
        return
    ud = context.user_data; ud["flow"] = "story"; ud["step"] = "age"; ud["params"] = {}
    await update.effective_message.reply_text("Давай подберём сказку.\nСколько лет ребёнку? (введи число, например 6)")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if ud.get("flow") != "story": return
    step = ud.get("step"); text = (update.effective_message.text or "").strip()

    if step == "age":
        ud["params"]["age"] = _safe_int(text, 6)
        ud["step"] = "hero"
        await update.effective_message.reply_text("Кто будет героем? (например: ёжик, Маша, котёнок)")
        return

    if step == "hero":
        ud["params"]["hero"] = text or "герой"
        ud["step"] = "moral"
        await update.effective_message.reply_text("Какую идею/мораль подчеркнуть? (дружба, щедрость, смелость и т.п.)")
        return

    if step == "moral":
        ud["params"]["moral"] = text or "доброта"
        ud["step"] = "length"
        await update.effective_message.reply_text("Какая длина? (короткая / средняя / длинная)")
        return

    if step == "length":
        length = text.lower()
        if length not in {"короткая", "средняя", "длинная"}: length = "средняя"
        ud["params"]["length"] = length

        uid = update.effective_user.id
        ustat = get_user_stats(uid)
        if ustat["today_stories"] >= MAX_STORIES_PER_DAY:
            secs = seconds_to_midnight_msk(); h = secs // 3600; m = (secs % 3600) // 60
            await update.effective_message.reply_text(
                "На сегодня лимит сказок исчерпан 🌙 (3/день).\n"
                f"Новый день через {h} ч {m} мин по Мск."
            )
            ud.clear(); return

        p = ud["params"]; data = synthesize_story(p["age"], p["hero"], p["moral"], p["length"])
        inc_story_counters(uid, data["title"])  # фикс лимита — до отправки

        cover_bio = make_cover_png_bytes(data["title"])
        data["cover_png_bytes"] = cover_bio.getvalue()
        store_user_story(uid, {k: v for k, v in data.items() if k != "cover_png_bytes"})

        msg = (
            f"🧾 {data['title']}\n\n{data['text']}\n\n"
            f"Мораль: {data['moral']}\n\n"
            "Вопросы:\n"
            f"1) {data['questions'][0]}\n"
            f"2) {data['questions'][1]}\n"
            f"3) {data['questions'][2]}\n"
            f"4) {data['questions'][3]}"
        )
        await update.effective_message.reply_text(msg)
        await update.effective_message.reply_photo(InputFile(BytesIO(data["cover_png_bytes"]), filename="cover.png"))
        pdf_path = Path(f"skazka_{uid}.pdf").resolve()
        render_story_pdf(pdf_path, data, cover_png=data["cover_png_bytes"])
        await update.effective_message.reply_document(InputFile(str(pdf_path), filename=pdf_path.name))
        ud.clear(); return

# --- прочие команды ---
def make_math_sheet():
    problems, answers = [], []
    for _ in range(10):
        a, b = random.randint(4, 15), random.randint(1, 9)
        if random.random() < 0.5:
            problems.append(f"{a} + {b} = "); answers.append(str(a + b))
        else:
            if b > a: a, b = b, a
            problems.append(f"{a} − {b} = "); answers.append(str(a - b))
    return problems, answers

async def math_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    problems, answers = make_math_sheet()
    await update.effective_message.reply_text("🧮 10 минут математики:\n" + "\n".join([f"{i+1}) {p}" for i,p in enumerate(problems)]))
    await update.effective_message.reply_text("Ответы:\n" + "\n".join([f"{i+1}) {a}" for i,a in enumerate(answers)]))
    inc_math_counter(uid)

async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    u = get_user_stats(int(uid))
    last_title = u.get("last_story_title") or "—"
    last_when = u.get("last_story_ts")
    if last_when:
        try: last_when = datetime.fromisoformat(last_when).astimezone(TZ_MSK).strftime("%d.%m.%Y %H:%M")
        except Exception: last_when = "—"
    else: last_when = "—"
    txt = (
        "👪 Отчёт родителю\n\n"
        f"Сегодня (по Мск):\n"
        f"• Сказок: {u.get('today_stories', 0)} / {MAX_STORIES_PER_DAY}\n\n"
        "За всё время:\n"
        f"• Сказок: {u.get('stories_total', 0)}\n"
        f"• Листов математики: {u.get('math_total', 0)}\n\n"
        "Последняя сказка:\n"
        f"• Название: {last_title}\n"
        f"• Время: {last_when}\n\n"
        "Совет: читайте абзацами и обсуждайте «почему?» — так лучше формируется понимание."
    )
    await update.effective_message.reply_text(txt)

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats_all.pop(str(uid), None); save_json(STATS_PATH, stats_all)
    stories_all.pop(str(uid), None); save_json(STORIES_PATH, stories_all)
    last_story_by_user.pop(uid, None); context.user_data.clear()
    await update.effective_message.reply_text("Ваши данные удалены. Можно начать заново 🙂")

# DEV (оставим для теста — потом уберём)
async def dev_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid); u["today_date"] = msk_today_str(); u["today_stories"] = 0
    stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)
    await update.effective_message.reply_text("✅ Дневной лимит сброшен для вашего аккаунта.")

async def dev_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid); secs = seconds_to_midnight_msk(); h, m = secs // 3600, (secs % 3600) // 60
    await update.effective_message.reply_text(
        "🔎 Статус лимита\n"
        f"• Сегодня: {u.get('today_stories',0)} / {MAX_STORIES_PER_DAY}\n"
        f"• Сброс через: {h} ч {m} мин (Мск)\n"
        f"• Всего сказок: {u.get('stories_total',0)}\n"
        f"• Последняя: {u.get('last_story_title','—')}"
    )

# ==== post_init / main ====
async def post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me(); BOT_USERNAME = me.username
    await app.bot.set_my_commands([
        BotCommand("start", "показать меню"),
        BotCommand("story", "умная сказка по параметрам"),
        BotCommand("math", "10 минут математики"),
        BotCommand("parent", "отчёт родителю"),
        BotCommand("delete", "удалить мои данные"),
        BotCommand("help", "помощь"),
    ])

def main():
    if BOT_TOKEN.startswith("ВСТАВЬ_"):
        print("⚠️ Внимание: BOT_TOKEN не задан. В проде задайте переменную окружения BOT_TOKEN!")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("story", story_cmd))
    app.add_handler(CommandHandler("math", math_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("dev_reset", dev_reset))
    app.add_handler(CommandHandler("dev_status", dev_status))
    # обработчик текста (диалог story)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if PUBLIC_URL:
        # Webhook режим
        path = (WEBHOOK_PATH or BOT_TOKEN).lstrip("/")
        webhook_url = f"{PUBLIC_URL.rstrip('/')}/{path}"
        print(f"[WEBHOOK] Starting on 0.0.0.0:{PORT}, path=/{path}")
        print(f"[WEBHOOK] Setting webhook to: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0", port=PORT,
            url_path=path, webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        # Локальный режим — polling
        print("[POLLING] Starting long-polling locally…")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
