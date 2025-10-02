# -*- coding: utf-8 -*-
# Читалкин&Циферкин — улучшенная генерация:
#   • Story: outline -> draft -> polish, возрастная лексика, стили
#   • Cover: художественные стили + палитры, негатив-промпт
#   • /settings: добавлены стиль_сказки, стиль_иллюстрации, палитра
#   • Остальной функционал как раньше (Pro, алёрты, PDF Unicode, вебхук/поллинг)

import os, json, random, base64, tempfile, math, traceback
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

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_BOT_TOKEN")
PUBLIC_URL   = os.getenv("PUBLIC_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
PORT         = int(os.getenv("PORT", "8080"))

DISABLE_LIMIT = os.getenv("DISABLE_LIMIT", "1") == "1"
MAX_STORIES_PER_DAY = 10**9 if DISABLE_LIMIT else int(os.getenv("MAX_STORIES_PER_DAY", "3"))

ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
PRO_IDS = set([int(x) for x in os.getenv("PRO_IDS", "").split(",") if x.strip().isdigit()])

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_TEXT = os.getenv("OPENAI_MODEL_TEXT", "gpt-4.1-mini")
OPENAI_MODEL_IMG  = os.getenv("OPENAI_MODEL_IMAGE", "gpt-image-1")

# ──────────────────────────────────────────────────────────────────────────────
# OpenAI client
# ──────────────────────────────────────────────────────────────────────────────
try:
    from openai import OpenAI
    oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:
    print(f"[AI] OpenAI client not available: {e}")
    oa_client = None

# ──────────────────────────────────────────────────────────────────────────────
# CONST / STORAGE
# ──────────────────────────────────────────────────────────────────────────────
TZ_MSK = ZoneInfo("Europe/Moscow")
DATA_DIR     = Path(".")
STATS_PATH   = DATA_DIR / "stats.json"
STORIES_PATH = DATA_DIR / "stories.json"

# ──────────────────────────────────────────────────────────────────────────────
# FONTS (для PDF и локальных обложек)
# ──────────────────────────────────────────────────────────────────────────────
FONT_DIR  = Path("fonts")
FONT_REG  = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
PDF_FONT   = "DejaVu"
PDF_FONT_B = "DejaVuB"

# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────
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
    except Exception as e: print(f"[FS] save_json error: {e}")

stats_all: Dict[str, Dict[str, Any]]   = load_json(STATS_PATH)
stories_all: Dict[str, Dict[str, Any]] = load_json(STORIES_PATH)

def default_stats() -> Dict[str, Any]:
    return {
        "stories_total": 0, "math_total": 0,
        "today_date": msk_today_str(), "today_stories": 0,
        "last_story_ts": None, "last_story_title": None,
        "pro": False,
    }

def default_user_stories() -> Dict[str, Any]:
    return {
        "last": None, "history": [],
        "profile": {
            "age": 6, "hero": "котёнок", "length": "средняя", "avoid": [],
            "style": "классика",              # новый параметр
            "art_style": "акварель",          # новый параметр
            "palette": "тёплая пастель",      # новый параметр
        },
    }

def get_user_stats(uid: int) -> Dict[str, Any]:
    u = stats_all.get(str(uid))
    if not u:
        u = default_stats()
        if uid in PRO_IDS: u["pro"] = True
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

def get_profile(uid: int) -> Dict[str, Any]:
    rec = stories_all.get(str(uid))
    if not rec:
        rec = default_user_stories()
        stories_all[str(uid)] = rec
        save_json(STORIES_PATH, stories_all)
    prof = rec.get("profile") or default_user_stories()["profile"]
    rec["profile"] = prof
    return prof

def save_profile(uid: int, prof: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    rec["profile"] = prof
    stories_all[str(uid)] = rec
    save_json(STORIES_PATH, stories_all)

def store_user_story(uid: int, story: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    stamped = dict(story); stamped["ts"] = msk_now().isoformat()
    rec["last"] = stamped
    hist: List[Dict[str, Any]] = rec.get("history", [])
    hist.append(stamped); rec["history"] = hist[-25:]
    stories_all[str(uid)] = rec
    save_json(STORIES_PATH, stories_all)

# ──────────────────────────────────────────────────────────────────────────────
# Цветовые палитры и стили иллюстраций
# ──────────────────────────────────────────────────────────────────────────────
PALETTES = {
    "тёплая пастель": ["peach", "apricot", "cream", "warm pink", "soft gold"],
    "северное сияние": ["teal", "azure", "violet", "lime", "ice blue"],
    "лес и мёд": ["moss green", "pine", "honey", "amber", "mushroom beige"],
    "закат у моря": ["coral", "sunset orange", "lavender", "deep blue", "sand"],
    "ледяная сказка": ["snow white", "silver", "icy blue", "frost teal", "moonlight"],
}

ART_STYLES = {
    "акварель": "watercolor, soft edges, paper texture, vibrant yet gentle pigments",
    "гуашь": "gouache, rich opaque paint, bold brushstrokes, matte finish",
    "пастель": "soft pastel, chalky texture, velvety gradients",
    "вырезки из бумаги": "paper cut-out, layered shapes, subtle drop shadows",
    "пластилин": "claymation look, tactile clay textures, handcrafted",
    "кинетический": "dynamic composition, motion blur accents, cinematic lighting",
}

STORY_STYLES = {
    "классика":  "добрая классическая сказка с плавным ритмом и ясной моралью",
    "приключение": "динамичное приключение с поиском, мини-препятствиями и взаимопомощью",
    "детектив":  "лёгкий детский «детектив»: загадка → подсказки → добрый развяз",
    "фантазия":  "волшебная история с мягким чудом и необычными существами",
    "научпоп":   "познавательная история: герой открывает простое правило/эффект",
}

NEGATIVE_IMG = (
    "blurry, noisy, low contrast, photorealistic, text, watermark, frame, logo, "
    "monochrome, dull colors, deformed, scary, horror"
)

# ──────────────────────────────────────────────────────────────────────────────
# Cover (AI first; fallback local)
# ──────────────────────────────────────────────────────────────────────────────
def gen_cover_ai(title: str, hero: str, art_style: str, palette: str) -> Optional[bytes]:
    if not oa_client:
        return None
    palette_words = ", ".join(PALETTES.get(palette, PALETTES["тёплая пастель"]))
    style_desc = ART_STYLES.get(art_style, ART_STYLES["акварель"])
    prompt = (
        f"Children's storybook cover in Russian for the tale «{title}». "
        f"Hero concept: {hero}. Whimsical illustration, {style_desc}. "
        f"Color palette: {palette_words}. High detail, vibrant, cozy, depth, soft global illumination. "
        "No text on image, no watermark."
    )
    try:
        img = oa_client.images.generate(
            model=OPENAI_MODEL_IMG,
            prompt=prompt + f"  Negative prompt: {NEGATIVE_IMG}",
            size="1024x1440",
        )
        return base64.b64decode(img.data[0].b64_json)
    except Exception as e:
        print(f"[AI] image error: {type(e).__name__}: {e}")
        return None

def _draw_gradient(draw: ImageDraw.ImageDraw, w: int, h: int, top=(246,242,255), bottom=(220,230,255)):
    for y in range(h):
        t = y / max(1, h-1)
        r = int(top[0]*(1-t) + bottom[0]*t)
        g = int(top[1]*(1-t) + bottom[1]*t)
        b = int(top[2]*(1-t) + bottom[2]*t)
        draw.line([(0, y), (w, y)], fill=(r,g,b))

def gen_cover_local(title: str, palette: str) -> bytes:
    W, H = 1024, 1440
    img = Image.new("RGB", (W, H), (255,255,255))
    d = ImageDraw.Draw(img)
    _draw_gradient(d, W, H)

    # рамка
    pad = 26
    d.rounded_rectangle(((pad, pad), (W-pad, H-pad)), radius=28, outline=(60,85,190), width=6)

    # «бумажные» слои
    d.pieslice(((-80, H-520), (W+80, H+200)), 0, 180, fill=(214, 228, 255))
    d.ellipse(((W-240, 70), (W-120, 190)), fill=(255,238,210))
    for sx in range(110, W-220, 130):
        d.ellipse(((sx-6, 130), (sx+6, 142)), fill=(255,255,230))

    # заголовок
    try:
        font_title = ImageFont.truetype(str(FONT_BOLD if FONT_BOLD.exists() else FONT_REG), size=50)
    except Exception:
        font_title = ImageFont.load_default()

    title = (title or "Сказка").strip()
    max_w = W - 160
    words = title.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font_title) <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    if not lines: lines = [title]

    total_h = 0
    for ln in lines:
        bb = d.textbbox((0,0), ln, font=font_title)
        total_h += (bb[3]-bb[1]) + 10
    y = H//2 - total_h//2 - 70
    for ln in lines:
        bb = d.textbbox((0,0), ln, font=font_title)
        x = (W - (bb[2]-bb[0])) // 2
        d.text((x, y), ln, font=font_title, fill=(35, 42, 72))
        y += (bb[3]-bb[1]) + 10

    bio = BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
    return bio.getvalue()

def make_cover_png_bytes(title: str, hero: str, art_style: str, palette: str) -> bytes:
    raw = gen_cover_ai(title, hero, art_style, palette)
    return raw if raw is not None else gen_cover_local(title, palette)

# ──────────────────────────────────────────────────────────────────────────────
# Story synthesis (outline -> draft -> polish) + возрастной словарь
# ──────────────────────────────────────────────────────────────────────────────
AGE_LEVEL = [
    (4,  {"vocab": "очень простые слова, короткие предложения", "sent": "8–12 слов"}),
    (6,  {"vocab": "простые слова, без сложных оборотов",          "sent": "10–14 слов"}),
    (8,  {"vocab": "доступные слова, немного образов",              "sent": "12–16 слов"}),
    (10, {"vocab": "богаче словарь, яркие образы",                  "sent": "14–18 слов"}),
    (14, {"vocab": "сильные образы, но без взрослой сложности",     "sent": "16–20 слов"}),
]
def age_profile(age: int) -> Dict[str,str]:
    age = max(3, min(14, int(age)))
    for lim, prof in AGE_LEVEL:
        if age <= lim: return prof
    return AGE_LEVEL[-1][1]

def _avoid_filter(text: str, avoid: List[str]) -> str:
    if not avoid: return text
    bad = [w.strip().lower() for w in avoid if w.strip()]
    if not bad: return text
    for w in bad:
        text = text.replace(w, "🌟")
    return text

def _target_len(length: str) -> str:
    return {"короткая":"250–400 слов","средняя":"450–700 слов","длинная":"800–1100 слов"}.get(length.lower(),"450–700 слов")

def _json_from_response(resp) -> Dict[str, Any]:
    try:
        return json.loads(resp.output_text or "{}")
    except Exception:
        return {}

def synthesize_story(age: int, hero: str, moral: str, length: str, avoid: List[str], style: str) -> Dict[str, Any]:
    hero  = hero or "герой"
    moral = moral or "доброта"
    tone  = STORY_STYLES.get(style, STORY_STYLES["классика"])
    prof  = age_profile(age)
    target_len = _target_len(length)

    if oa_client:
        try:
            # 1) Outline
            prompt1 = f"""
Ты — талантливый детский автор. Создай план сказки (outline) для ребёнка {age} лет.
Стиль: {tone}. Герой: {hero}. Идея/мораль: {moral}. Тем избегать: {", ".join(avoid) or "нет"}.
Возрастная подача: {prof["vocab"]}, длина предложения {prof["sent"]}.
Структура: завязка → 3–4 сцены препятствий и открытий → светлая развязка → чёткая мораль.
Ответ строго JSON:
{{"title":"...","scenes":[{{"name":"...","beats":["...","..."]}}, ...]}}
"""
            r1 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt1)
            outline = _json_from_response(r1)
            title = outline.get("title") or f"{hero.capitalize()} и урок про «{moral}»"

            # 2) Draft by outline
            prompt2 = f"""
Используя план ниже, напиши сказку на русском для ребёнка {age} лет.
План: {json.dumps(outline, ensure_ascii=False)}
Стиль: {tone}. Объём: {target_len}. Возрастная подача: {prof["vocab"]}, длина предложения {prof["sent"]}.
Избегай тем: {", ".join(avoid) or "нет"}.
Формат: 3–6 абзацев. В конце добавь блок "Мораль" одной-двумя фразами и 4 вопроса ребёнку.
Ответ строго JSON:
{{"text":"...","moral":"...","questions":["...","...","...","..."]}}
"""
            r2 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt2)
            draft = _json_from_response(r2)

            # 3) Polish (яркость/образность, но без «взрослости»)
            prompt3 = f"""
Улучшить текст для возраста {age}: добавить образности, мягких сенсорных деталей,
сохранить простоту языка ({prof["vocab"]}, {prof["sent"]}), не использовать взрослую лексику.
Вернуть тот же JSON {{text, moral, questions}}. Текст ниже:
{json.dumps(draft, ensure_ascii=False)}
"""
            r3 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt3)
            data = _json_from_response(r3)

            text = _avoid_filter(data.get("text",""), avoid)
            return {
                "title": title,
                "text":  text,
                "moral": data.get("moral") or f"Важно помнить: {moral}. Доброта согревает.",
                "questions": (data.get("questions") or [
                    f"Что {hero} понял про {moral}?",
                    f"Какие трудности встретились {hero}?",
                    "Что помогло героям справиться?",
                    "Как бы ты поступил на месте героя?",
                ])[:4],
            }
        except Exception as e:
            print(f"[AI] text error: {type(e).__name__}: {e} — local fallback")

    # Локальный fallback (улучшенный)
    title = f"{hero.capitalize()} и урок про «{moral}»"
    intro = f"{hero.capitalize()} проснулся в тёплом настроении и мечтал понять, что такое {moral}."
    middle = [
        f"По дороге {hero} встретил новых друзей, и вместе они решали маленькие задачи.",
        f"Иногда было непросто, но каждый шаг становился светлее благодаря поддержке.",
        f"Ветер шуршал в листве, пахло мёдом и травами, и {hero} чувствовал смелость в груди.",
        f"Добрые дела отражались, как солнечные зайчики в окнах домов.",
    ]
    ending = f"К вечеру {hero} понял: {moral} — это то, что делают, а не просто произносят. От этого в мире становится теплее."
    paras = {
        "короткая": [intro, random.choice(middle), ending],
        "средняя": [intro, random.choice(middle), random.choice(middle), ending],
        "длинная": [intro, random.choice(middle), random.choice(middle), random.choice(middle), ending],
    }.get(length.lower(), [intro, random.choice(middle), random.choice(middle), ending])

    questions = [
        f"Что {hero} узнал про {moral}?",
        "Какие шаги помогли героям двигаться дальше?",
        "Где в истории чувствовалась дружба?",
        "Что бы ты сделал(а) на месте героя?",
    ]
    return {"title": title, "text": _avoid_filter("\n\n".join(paras), avoid),
            "moral": f"Важно помнить: {moral}. Даже маленькое добро меняет мир.",
            "questions": questions}

# ──────────────────────────────────────────────────────────────────────────────
# PDF
# ──────────────────────────────────────────────────────────────────────────────
class StoryPDF(FPDF):
    def header(self): pass

def _ensure_unicode_fonts(pdf: FPDF) -> bool:
    try:
        if not (FONT_REG.exists() and FONT_BOLD.exists()):
            print("[PDF] DejaVu TTF files NOT found (need fonts/DejaVuSans*.ttf)")
            return False
        pdf.add_font(PDF_FONT,   "", str(FONT_REG),  uni=True)
        pdf.add_font(PDF_FONT_B, "", str(FONT_BOLD), uni=True)
        return True
    except Exception as e:
        print(f"[PDF] TTF load error: {e}")
        return False

def render_story_pdf(path: Path, data: Dict[str, Any], cover_png: Optional[bytes]):
    pdf = StoryPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    use_uni = _ensure_unicode_fonts(pdf)

    pdf.add_page()
    if cover_png:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(cover_png); tmp.flush(); tmp_name = tmp.name
        try:
            pdf.image(tmp_name, x=0, y=0, w=210, h=297)
        finally:
            try: os.remove(tmp_name)
            except Exception: pass
    else:
        if use_uni: pdf.set_font(PDF_FONT_B, size=28)
        else:       pdf.set_font("Helvetica", style="B", size=28)
        pdf.set_y(40); pdf.multi_cell(0, 12, data["title"], align="C")

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

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
BOT_USERNAME: Optional[str] = None

def menu_keyboard() -> InlineKeyboardMarkup:
    u = BOT_USERNAME or "your_bot"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧚‍♀️ Сказка", url=f"https://t.me/{u}?start=story"),
         InlineKeyboardButton("🧮 Математика", url=f"https://t.me/{u}?start=math")],
        [InlineKeyboardButton("👪 Отчёт", url=f"https://t.me/{u}?start=parent"),
         InlineKeyboardButton("⚙️ Настройки", url=f"https://t.me/{u}?start=settings")],
        [InlineKeyboardButton("🗑 Удалить данные", url=f"https://t.me/{u}?start=delete")],
    ])

def menu_text(u_is_pro: bool) -> str:
    pro = "Pro: включён ✅" if u_is_pro else "Pro: выключен"
    lim = "без лимита (тест)" if DISABLE_LIMIT else f"лимит: {MAX_STORIES_PER_DAY}/день"
    return (
        "<b>Привет!</b>\n<b>Я — Читалкин&Циферкин 🦉➕🧮</b>\n\n"
        "• <b>Сказка</b> — подберу по возрасту и теме\n"
        "• <b>Математика</b> — 10 минут примеров\n"
        "• <b>Отчёт</b> — прогресс ребёнка\n"
        "• <b>Настройки</b> — профиль ребёнка (возраст, герой, длина, стиль сказки и иллюстрации)\n"
        "• <b>Удалить данные</b> — очистка\n\n"
        f"<i>{pro} • {lim}. Сброс в 00:00 (Мск).</i>"
    )

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid)
    await (update.effective_message or update.message).reply_html(
        menu_text(u.get("pro", False)), reply_markup=menu_keyboard(), disable_web_page_preview=True
    )

# ──────────────────────────────────────────────────────────────────────────────
# commands / flow
# ──────────────────────────────────────────────────────────────────────────────
def _safe_int(text: str, default: int) -> int:
    try: return max(3, min(14, int(text)))
    except Exception: return default

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args:
        p = args[0].strip().lower()
        if p == "story":    await story_cmd(update, context);    return
        if p == "math":     await math_cmd(update, context);     return
        if p == "parent":   await parent_cmd(update, context);   return
        if p == "delete":   await delete_cmd(update, context);   return
        if p == "settings": await settings_cmd(update, context); return
    await show_menu(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

# ——— SETTINGS ———
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_profile(uid)
    ud = context.user_data; ud.clear()
    ud["flow"] = "settings"; ud["step"] = "age"; ud["profile"] = prof.copy()
    await update.effective_message.reply_text(
        "⚙️ Настройки профиля.\n"
        f"Сейчас: возраст={prof['age']}, герой=«{prof['hero']}», длина={prof['length']}, стиль=«{prof['style']}», "
        f"иллюстрация=«{prof['art_style']}», палитра=«{prof['palette']}», избегать={', '.join(prof['avoid']) or '—'}.\n\n"
        "Введите возраст (3–14):"
    )

# ——— STORY ———
async def story_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ustat = get_user_stats(uid)
    if not DISABLE_LIMIT and ustat["today_stories"] >= MAX_STORIES_PER_DAY:
        secs = seconds_to_midnight_msk(); h = secs // 3600; m = (secs % 3600) // 60
        await update.effective_message.reply_text(
            "На сегодня лимит сказок исчерпан 🌙.\n"
            f"Новый день через {h} ч {m} мин по Мск."
        ); return

    prof = get_profile(uid)
    ud = context.user_data; ud.clear()
    ud["flow"] = "story"; ud["step"] = "age"; ud["params"] = {
        "age": prof["age"], "hero": prof["hero"], "length": prof["length"],
        "style": prof["style"], "art_style": prof["art_style"], "palette": prof["palette"]
    }
    await update.effective_message.reply_text(
        f"Давай подберём сказку. Сколько лет ребёнку? (по умолчанию {prof['age']})"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    step = ud.get("step")
    flow = ud.get("flow")
    if not flow: return
    text = (update.effective_message.text or "").strip()

    # SETTINGS FLOW
    if flow == "settings":
        prof = ud.get("profile", {})
        if step == "age":
            prof["age"] = _safe_int(text, prof.get("age", 6))
            ud["step"] = "hero"
            await update.effective_message.reply_text("Герой по умолчанию (например: котёнок, ёжик, Маша):")
            return
        if step == "hero":
            prof["hero"] = text or prof.get("hero","герой")
            ud["step"] = "length"
            await update.effective_message.reply_text("Длина сказки? (короткая / средняя / длинная)")
            return
        if step == "length":
            length = text.lower()
            if length not in {"короткая","средняя","длинная"}: length = "средняя"
            prof["length"] = length
            ud["step"] = "style"
            await update.effective_message.reply_text("Стиль сказки? (классика / приключение / детектив / фантазия / научпоп)")
            return
        if step == "style":
            st = text.lower()
            if st not in STORY_STYLES.keys(): st = "классика"
            prof["style"] = st
            ud["step"] = "art"
            await update.effective_message.reply_text("Стиль иллюстрации? (акварель / гуашь / пастель / вырезки из бумаги / пластилин / кинетический)")
            return
        if step == "art":
            a = text.lower()
            if a not in ART_STYLES.keys(): a = "акварель"
            prof["art_style"] = a
            ud["step"] = "palette"
            await update.effective_message.reply_text("Палитра? (тёплая пастель / северное сияние / лес и мёд / закат у моря / ледяная сказка)")
            return
        if step == "palette":
            p = text.lower()
            if p not in PALETTES.keys(): p = "тёплая пастель"
            prof["palette"] = p
            ud["step"] = "avoid"
            await update.effective_message.reply_text("Каких тем избегать? Напишите через запятую (или «нет»).")
            return
        if step == "avoid":
            avoid = [] if text.lower() in {"нет","no","none"} else [w.strip() for w in text.split(",") if w.strip()]
            prof["avoid"] = avoid
            save_profile(update.effective_user.id, prof)
            ud.clear()
            await update.effective_message.reply_text(
                "Готово! Профиль сохранён ✅"
            )
            return

    # STORY FLOW
    if flow == "story":
        p = ud["params"]
        if step == "age":
            p["age"] = _safe_int(text, p.get("age",6))
            ud["step"] = "hero"
            await update.effective_message.reply_text(f"Кто будет героем? (по умолчанию «{p.get('hero','герой')}»)")
            return
        if step == "hero":
            p["hero"] = text or p.get("hero","герой")
            ud["step"] = "moral"
            await update.effective_message.reply_text("Какую идею/мораль подчеркнуть? (дружба, щедрость, смелость...)")
            return
        if step == "moral":
            ud["moral"] = text or "доброта"
            ud["step"] = "length"
            await update.effective_message.reply_text(f"Какая длина? (короткая / средняя / длинная) — по умолчанию {p.get('length','средняя')}")
            return
        if step == "length":
            length = text.lower() if text else p.get("length","средняя")
            if length not in {"короткая","средняя","длинная"}: length = "средняя"
            p["length"] = length

            uid = update.effective_user.id
            prof = get_profile(uid)
            ustat = get_user_stats(uid)
            if not DISABLE_LIMIT and ustat["today_stories"] >= MAX_STORIES_PER_DAY:
                secs = seconds_to_midnight_msk(); h = secs // 3600; m = (secs % 3600) // 60
                await update.effective_message.reply_text(
                    "На сегодня лимит сказок исчерпан 🌙.\n"
                    f"Новый день через {h} ч {m} мин по Мск."
                ); ud.clear(); return

            data = synthesize_story(
                p["age"], p["hero"], ud["moral"], p["length"],
                avoid=prof["avoid"], style=p["style"]
            )
            inc_story_counters(uid, data["title"])

            cover_bytes = make_cover_png_bytes(data["title"], p["hero"], p["art_style"], p["palette"])
            data["cover_png_bytes"] = cover_bytes
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

            await update.effective_message.reply_photo(InputFile(BytesIO(cover_bytes), filename="cover.png"))
            pdf_path = Path(f"skazka_{uid}.pdf").resolve()
            render_story_pdf(pdf_path, data, cover_png=cover_bytes)
            await update.effective_message.reply_document(InputFile(str(pdf_path), filename=pdf_path.name))

            ud.clear(); return

# математика
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

# отчёт
async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid)
    last_title = u.get("last_story_title") or "—"
    last_when = u.get("last_story_ts")
    if last_when:
        try: last_when = datetime.fromisoformat(last_when).astimezone(TZ_MSK).strftime("%d.%m.%Y %H:%M")
        except Exception: last_when = "—"
    else: last_when = "—"
    prof = get_profile(uid)
    txt = (
        "👪 Отчёт родителю\n\n"
        f"Сегодня (Мск):\n• Сказок: {u.get('today_stories',0)} / {('∞' if DISABLE_LIMIT else MAX_STORIES_PER_DAY)}\n\n"
        "За всё время:\n"
        f"• Сказок: {u.get('stories_total',0)}\n"
        f"• Листов математики: {u.get('math_total',0)}\n\n"
        "Последняя сказка:\n"
        f"• {last_title}\n"
        f"• {last_when}\n\n"
        "Профиль ребёнка:\n"
        f"• возраст={prof['age']}, герой=«{prof['hero']}», длина={prof['length']}, стиль=«{prof['style']}», "
        f"иллюстрация=«{prof['art_style']}», палитра=«{prof['palette']}», избегать={', '.join(prof['avoid']) or '—'}"
    )
    await update.effective_message.reply_text(txt)

# удалить данные
async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats_all.pop(str(uid), None); save_json(STATS_PATH, stats_all)
    stories_all.pop(str(uid), None); save_json(STORIES_PATH, stories_all)
    context.user_data.clear()
    await update.effective_message.reply_text("Ваши данные удалены. Можно начать заново 🙂")

# ──────────────────────────────────────────────────────────────────────────────
# error alerts
# ──────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALERT_CHAT_ID:
        print("[ERR] No ALERT_CHAT_ID; error:\n", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
        return
    try:
        tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
        text = "🚨 <b>Ошибка в боте</b>\n\n<pre>" + (tb[-3500:] if len(tb)>3500 else tb) + "</pre>"
        await context.bot.send_message(chat_id=int(ALERT_CHAT_ID), text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print("[ERR] failed to send alert:", e)

# ──────────────────────────────────────────────────────────────────────────────
# init / run
# ──────────────────────────────────────────────────────────────────────────────
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
        BotCommand("settings","настройки профиля"),
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
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("story",  story_cmd))
    app.add_handler(CommandHandler("math",   math_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(error_handler)

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
