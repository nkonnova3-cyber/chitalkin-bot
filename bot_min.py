# -*- coding: utf-8 -*-
# Читалкин&Циферкин — ИИ-сказки + PDF (Unicode) + обложка (ИИ или локальная)
# Запуск локально (polling) — если переменная PUBLIC_URL НЕ задана.
# Запуск на Render (webhook) — если PUBLIC_URL задана.

import os, json, random, base64, tempfile
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from fpdf import FPDF

from telegram import (
    Update, InputFile, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- Переменные окружения ----------
BOT_TOKEN    = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_BOT_TOKEN")
PUBLIC_URL   = os.getenv("PUBLIC_URL")               # пример: https://chitalkin-bot.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")             # пример: hook  (итоговый URL будет PUBLIC_URL/hook)
PORT         = int(os.getenv("PORT", "8080"))

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")      # для ИИ-обложек и ИИ-текста
OPENAI_MODEL_TEXT = os.getenv("OPENAI_MODEL_TEXT", "gpt-4.1-mini")
OPENAI_MODEL_IMG  = os.getenv("OPENAI_MODEL_IMAGE", "gpt-image-1")

# ---------- OpenAI (не обязателен) ----------
try:
    from openai import OpenAI
    oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[AI] Клиент OpenAI недоступен: {e}")
    oa_client = None

# ---------- Постоянные ----------
MAX_STORIES_PER_DAY = 3
TZ_MSK = ZoneInfo("Europe/Moscow")
DATA_DIR     = Path(".")
STATS_PATH   = DATA_DIR / "stats.json"
STORIES_PATH = DATA_DIR / "stories.json"

# ---------- Шрифты (для PDF и локальной обложки) ----------
FONT_DIR  = Path("fonts")
FONT_REG  = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
PDF_FONT   = "DejaVu"   # regular
PDF_FONT_B = "DejaVuB"  # bold

# ---------- Вспомогательные ----------
def msk_now() -> datetime: return datetime.now(TZ_MSK)
def msk_today_str() -> str: return msk_now().strftime("%Y-%m-%d")
def seconds_to_midnight_msk() -> int:
    now = msk_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())

def load_json(p: Path) -> Dict[str, Any]:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_json(p: Path, data: Dict[str, Any]):
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[FS] save_json error: {e}")

stats_all: Dict[str, Dict[str, Any]]   = load_json(STATS_PATH)
stories_all: Dict[str, Dict[str, Any]] = load_json(STORIES_PATH)

def default_stats() -> Dict[str, Any]:
    return {"stories_total": 0, "math_total": 0,
            "today_date": msk_today_str(), "today_stories": 0,
            "last_story_ts": None, "last_story_title": None}

def default_user_stories() -> Dict[str, Any]:
    return {"last": None, "history": []}

def get_user_stats(uid: int) -> Dict[str, Any]:
    u = stats_all.get(str(uid))
    if not u:
        u = default_stats()
        stats_all[str(uid)] = u
        save_json(STATS_PATH, stats_all)
    if u.get("today_date") != msk_today_str():
        u["today_date"] = msk_today_str()
        u["today_stories"] = 0
        save_json(STATS_PATH, stats_all)
    return u

def inc_story_counters(uid: int, title: str):
    u = get_user_stats(uid)
    u["stories_total"]   = int(u.get("stories_total", 0)) + 1
    u["today_stories"]   = int(u.get("today_stories", 0)) + 1
    u["last_story_ts"]   = msk_now().isoformat()
    u["last_story_title"]= title
    stats_all[str(uid)]  = u
    save_json(STATS_PATH, stats_all)

def inc_math_counter(uid: int):
    u = get_user_stats(uid)
    u["math_total"] = int(u.get("math_total", 0)) + 1
    stats_all[str(uid)] = u
    save_json(STATS_PATH, stats_all)

def store_user_story(uid: int, story: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    stamped = dict(story); stamped["ts"] = msk_now().isoformat()
    rec["last"] = stamped
    hist: List[Dict[str, Any]] = rec.get("history", [])
    hist.append(stamped); rec["history"] = hist[-20:]
    stories_all[str(uid)] = rec
    save_json(STORIES_PATH, stories_all)

# ---------- ИИ-обложка ----------
def gen_cover_ai(title: str) -> Optional[bytes]:
    """Возвращает PNG байты или None, с подробным логом причин."""
    if not oa_client:
        print("[AI] Обложка: клиент OpenAI отсутствует (нет OPENAI_API_KEY?) — фолбэк.")
        return None
    try:
        prompt = (
            f"A warm, cozy children's book cover for the Russian tale titled “{title}”. "
            "Soft colors, fairy-tale vibe, no text on image."
        )
        img = oa_client.images.generate(model=OPENAI_MODEL_IMG, prompt=prompt, size="1024x1440")
        b64 = img.data[0].b64_json
        raw = base64.b64decode(b64)
        return raw
    except Exception as e:
        print(f"[AI] Обложка: ошибка генерации: {type(e).__name__}: {e} — используем локальную.")
        return None

# ---------- Локальная обложка с кириллицей ----------
def gen_cover_local(title: str) -> bytes:
    W, H = 2480, 3508  # A4 @300dpi
    img = Image.new("RGB", (W, H), (246, 247, 251))
    d = ImageDraw.Draw(img)

    # рамка
    d.rectangle((100, 100, W-100, H-100), outline=(59, 92, 204), width=16)

    # текст по центру
    title = (title or "Сказка").strip()
    try:
        # берём наш DejaVuSans
        font_path = FONT_BOLD if FONT_BOLD.exists() else FONT_REG
        font = ImageFont.truetype(str(font_path), size=120)
    except Exception:
        font = ImageFont.load_default()

    # перенос строк по ширине
    max_w = W - 2*200
    words = title.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    if not lines: lines = [title]

    total_h = sum(d.textbbox((0,0), ln, font=font)[3] - d.textbbox((0,0), ln, font=font)[1] + 20 for ln in lines)
    y = (H - total_h) // 2
    for ln in lines:
        bb = d.textbbox((0,0), ln, font=font)
        w = bb[2] - bb[0]
        x = (W - w) // 2
        d.text((x, y), ln, font=font, fill=(34, 38, 49))
        y += (bb[3] - bb[1]) + 20

    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio.getvalue()

def make_cover_png_bytes(title: str) -> bytes:
    raw = gen_cover_ai(title)
    if raw is not None:
        return raw
    return gen_cover_local(title)

# ---------- ИИ-сказка (с фолбэком) ----------
def synthesize_story(age: int, hero: str, moral: str, length: str) -> Dict[str, Any]:
    if oa_client:
        try:
            target_len = {
                "короткая": "250–400 слов",
                "средняя":  "450–700 слов",
                "длинная":  "800–1100 слов",
            }.get(length.lower(), "450–700 слов")
            prompt = f"""
Ты — добрый детский автор. Напиши сказку для ребёнка {age} лет.
Герой: {hero}. Центральная идея/мораль: {moral}.
Требования:
- Объём: {target_len}
- Язык: русский
- 3–5 абзацев + финальный блок «Мораль»
- Потом 4 вопроса для обсуждения
Ответ строго в JSON с полями: title, text, moral, questions (4 строки).
"""
            resp = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt)
            raw = resp.output_text or ""
            data = json.loads(raw)
            return {
                "title": data.get("title") or f"{hero.capitalize()} и урок про «{moral}»",
                "text":  data.get("text") or "",
                "moral": data.get("moral") or f"Важно помнить: {moral}. Даже маленький поступок делает мир теплее.",
                "questions": (data.get("questions") or [
                    f"Что {hero} понял(а) про {moral}?",
                    f"Какие трудности встретились {hero}?",
                    "Как маленькие шаги помогают менять день?",
                    "Как бы ты поступил(а) на месте героя?",
                ])[:4],
            }
        except Exception as e:
            print(f"[AI] Текст: ошибка генерации: {type(e).__name__}: {e} — локальный фолбэк.")

    # Локальный генератор (если ИИ недоступен)
    title = f"{hero.capitalize()} и урок про «{moral}»"
    length_map = {"короткая": 2, "средняя": 4, "длинная": 6}
    paras = length_map.get(length.lower(), 3)
    tone_key = "young" if age <= 6 else ("mid" if age <= 10 else "teen")
    tone_intro = {
        "young": f"{hero.capitalize()} проснулся(ась) и улыбнулся(ась) новому дню.",
        "mid":   f"{hero.capitalize()} давно хотел(а) узнать, что такое {moral}.",
        "teen":  f"{hero.capitalize()} думал(а), что знает всё про {moral}, но оказалось — нет.",
    }
    tone_body = [
        f"По дороге {hero} встретил(а) друга и вместе они помогли тем, кому это было нужно.",
        f"Иногда было трудно, но {hero} не сдавался(ась) и пробовал(а) снова.",
        f"Порой маленький шаг меняет целый день — и это главное открытие.",
    ]
    tone_end = {
        "young": "Вечером все радовались, пили какао и благодарили друг друга.",
        "mid":   "К вечеру стало ясно: важно быть честным с собой и добрым к другим.",
        "teen":  "Возвращаясь домой, {hero} понял(а), что рост — это маленькие шаги каждый день.",
    }
    parts = [tone_intro[tone_key]]
    for _ in range(paras - 2):
        parts.append(random.choice(tone_body))
    parts.append(tone_end[tone_key].replace("{hero}", hero))
    text = "\n\n".join(parts)
    moral_txt = f"Важно помнить: {moral}. Даже маленький поступок делает мир теплее."
    questions = [
        f"Что {hero} понял(а) про {moral}?",
        f"Какие трудности встретились {hero}?",
        "Как маленькие шаги помогают менять день?",
        "Как бы ты поступил(а) на месте героя?",
    ]
    return {"title": title, "text": text, "moral": moral_txt, "questions": questions}

# ---------- PDF (Unicode) ----------
class StoryPDF(FPDF):
    def header(self): pass

def _ensure_unicode_fonts(pdf: FPDF) -> bool:
    """Подключаем TTF, если в репозитории есть fonts/DejaVuSans*. Возвращаем True/False."""
    have = FONT_REG.exists() and FONT_BOLD.exists()
    if have:
        try:
            pdf.add_font(PDF_FONT,   "", str(FONT_REG),  uni=True)
            pdf.add_font(PDF_FONT_B, "", str(FONT_BOLD), uni=True)
            return True
        except Exception as e:
            print(f"[PDF] Не удалось подключить TTF: {e} — фолбэк на Helvetica.")
    else:
        print("[PDF] Внимание: нет файлов шрифтов fonts/DejaVuSans.ttf и/или DejaVuSans-Bold.ttf")
    return False

def render_story_pdf(path: Path, data: Dict[str, Any], cover_png: Optional[bytes]):
    pdf = StoryPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    use_uni = _ensure_unicode_fonts(pdf)

    # Обложка
    pdf.add_page()
    if cover_png:
        # Сохраняем во временный файл — так надёжнее для fpdf2
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(cover_png); tmp.flush()
            tmp_name = tmp.name
        try:
            pdf.image(tmp_name, x=0, y=0, w=210, h=297)
        finally:
            try: os.remove(tmp_name)
            except Exception: pass
    else:
        if use_uni: pdf.set_font(PDF_FONT_B, size=26)
        else:       pdf.set_font("Helvetica", style="B", size=26)
        pdf.set_y(40); pdf.multi_cell(0, 12, data["title"], align="C")

    # Текст
    pdf.add_page()
    if use_uni: pdf.set_font(PDF_FONT_B, size=16)
    else:       pdf.set_font("Helvetica", style="B", size=16)
    pdf.multi_cell(0, 8, data["title"]); pdf.ln(1)

    meta = f"Создано ИИ • {msk_now().strftime('%d.%m.%Y')}"
    if use_uni: pdf.set_font(PDF_FONT, size=11)
    else:       pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, meta); pdf.ln(4)

    if use_uni: pdf.set_font(PDF_FONT, size=12)
    else:       pdf.set_font("Helvetica", size=12)
    for para in data["text"].split("\n\n"):
        pdf.multi_cell(0, 7, para); pdf.ln(2)

    pdf.ln(2)
    if use_uni: pdf.set_font(PDF_FONT_B, size=13)
    else:       pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 7, "Мораль", ln=1)

    if use_uni: pdf.set_font(PDF_FONT, size=12)
    else:       pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 7, data["moral"])

    pdf.output(str(Path(path)))

# ---------- UI ----------
BOT_USERNAME: Optional[str] = None

def menu_keyboard() -> InlineKeyboardMarkup:
    u = BOT_USERNAME or "your_bot"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧚‍♀️ Сказка", url=f"https://t.me/{u}?start=story"),
         InlineKeyboardButton("🧮 Математика", url=f"https://t.me/{u}?start=math")],
        [InlineKeyboardButton("👪 Отчёт", url=f"https://t.me/{u}?start=parent"),
         InlineKeyboardButton("🗑 Удалить данные", url=f"https://t.me/{u}?start=delete")],
    ])

def menu_text() -> str:
    return (
        "<b>Привет!</b>\n<b>Я — Читалкин&Циферкин 🦉➕🧮</b>\n\n"
        "• <b>Сказка</b> — подберу по возрасту и теме\n"
        "• <b>Математика</b> — 10 минут примеров\n"
        "• <b>Отчёт</b> — прогресс ребёнка\n"
        "• <b>Удалить данные</b> — очистка\n\n"
        "<i>Дневной лимит: 3 сказки. Сброс — в 00:00 (Мск).</i>"
    )

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.effective_message or update.message).reply_html(
        menu_text(), reply_markup=menu_keyboard(), disable_web_page_preview=True
    )

# ---------- Команды ----------
def _safe_int(text: str, default: int) -> int:
    try:
        return max(3, min(14, int(text)))
    except Exception:
        return default

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args:
        p = args[0].strip().lower()
        if p == "story":  await story_cmd(update, context);  return
        if p == "math":   await math_cmd(update, context);   return
        if p == "parent": await parent_cmd(update, context); return
        if p == "delete": await delete_cmd(update, context); return
    await show_menu(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):  # alias
    await show_menu(update, context)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):  # alias
    await show_menu(update, context)

async def story_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ustat = get_user_stats(uid)
    if ustat["today_stories"] >= MAX_STORIES_PER_DAY:
        secs = seconds_to_midnight_msk(); h = secs // 3600; m = (secs % 3600) // 60
        await update.effective_message.reply_text(
            "На сегодня лимит сказок исчерпан 🌙 (3/день).\n"
            f"Новый день через {h} ч {m} мин по Мск."
        )
        return
    ud = context.user_data; ud.clear()
    ud["flow"] = "story"; ud["step"] = "age"; ud["params"] = {}
    await update.effective_message.reply_text("Давай подберём сказку. Сколько лет ребёнку? (введи число)")

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
        await update.effective_message.reply_text("Какую идею/мораль подчеркнуть? (дружба, щедрость, смелость... )")
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

        p = ud["params"]
        data = synthesize_story(p["age"], p["hero"], p["moral"], p["length"])
        inc_story_counters(uid, data["title"])

        # обложка
        cover_bytes = make_cover_png_bytes(data["title"])
        data["cover_png_bytes"] = cover_bytes
        store_user_story(uid, {k: v for k, v in data.items() if k != "cover_png_bytes"})

        # высылаем текст
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

        # обложка как фото
        await update.effective_message.reply_photo(InputFile(BytesIO(cover_bytes), filename="cover.png"))

        # PDF
        pdf_path = Path(f"skazka_{uid}.pdf").resolve()
        render_story_pdf(pdf_path, data, cover_png=cover_bytes)
        await update.effective_message.reply_document(InputFile(str(pdf_path), filename=pdf_path.name))

        ud.clear(); return

# --- математика
def make_math_sheet():
    problems, answers = [], []
    for _ in range(10):
        a, b = random.randint(4, 15), random.randint(1, 9)
        if random.random() < 0.5:
            problems.append(f"{a} + {b} = ")
            answers.append(str(a + b))
        else:
            if b > a: a, b = b, a
            problems.append(f"{a} − {b} = ")
            answers.append(str(a - b))
    return problems, answers

async def math_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pr, an = make_math_sheet()
    await update.effective_message.reply_text("🧮 10 минут математики:\n" + "\n".join([f"{i+1}) {p}" for i,p in enumerate(pr)]))
    await update.effective_message.reply_text("Ответы:\n" + "\n".join([f"{i+1}) {a}" for i,a in enumerate(an)]))
    inc_math_counter(uid)

# --- отчёт
async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid)
    last_title = u.get("last_story_title") or "—"
    last_when = u.get("last_story_ts")
    if last_when:
        try:
            last_when = datetime.fromisoformat(last_when).astimezone(TZ_MSK).strftime("%d.%m.%Y %H:%M")
        except Exception:
            last_when = "—"
    else:
        last_when = "—"
    txt = (
        "👪 Отчёт родителю\n\n"
        f"Сегодня (Мск):\n• Сказок: {u.get('today_stories',0)} / {MAX_STORIES_PER_DAY}\n\n"
        "За всё время:\n"
        f"• Сказок: {u.get('stories_total',0)}\n"
        f"• Листов математики: {u.get('math_total',0)}\n\n"
        "Последняя сказка:\n"
        f"• {last_title}\n"
        f"• {last_when}"
    )
    await update.effective_message.reply_text(txt)

# --- удалить данные
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats_all.pop(str(uid), None); save_json(STATS_PATH, stats_all)
    stories_all.pop(str(uid), None); save_json(STORIES_PATH, stories_all)
    context.user_data.clear()
    await update.effective_message.reply_text("Ваши данные удалены. Можно начать заново 🙂")

# ---------- Инициализация ----------
async def post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    await app.bot.set_my_commands([
        BotCommand("start", "показать меню"),
        BotCommand("menu",  "показать меню"),
        BotCommand("story", "умная сказка"),
        BotCommand("math",  "10 минут математики"),
        BotCommand("parent","отчёт родителю"),
        BotCommand("delete","удалить мои данные"),
        BotCommand("help",  "помощь"),
    ])

def main():
    if BOT_TOKEN.startswith("ВСТАВЬ_"):
        raise SystemExit("Сначала задайте BOT_TOKEN (переменная окружения).")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("menu",   menu_cmd))
    app.add_handler(CommandHandler("help",   help_cmd))
    app.add_handler(CommandHandler("story",  story_cmd))
    app.add_handler(CommandHandler("math",   math_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if PUBLIC_URL:
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
        print("[POLLING] Starting long-polling…")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
