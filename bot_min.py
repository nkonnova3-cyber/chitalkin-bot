# -*- coding: utf-8 -*-
# Читалкин&Циферкин — ТЕКСТ-ТОЛЬКО: качественные сказки с жёстким контролем смысла и длины
# • Картинок нет. Только текст + PDF.
# • Генерация: outline → draft → critique&revise → проверка объёма.
# • Длину можно задавать: короткая (250–400), средняя (450–700), длинная (800–1100).
# • Настройки: возраст, герой, длина по умолчанию, стиль, «избегать».

import os, json, random, re, traceback
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fpdf import FPDF
from telegram import Update, InputFile, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_BOT_TOKEN")
PUBLIC_URL   = os.getenv("PUBLIC_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
PORT         = int(os.getenv("PORT", "8080"))

# лимит отключён для тестов
DISABLE_LIMIT = os.getenv("DISABLE_LIMIT", "1") == "1"
MAX_STORIES_PER_DAY = 10**9 if DISABLE_LIMIT else int(os.getenv("MAX_STORIES_PER_DAY", "3"))

ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")

# OpenAI (только для текста; опционально)
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_TEXT = os.getenv("OPENAI_MODEL_TEXT", "gpt-4.1-mini")
try:
    from openai import OpenAI
    oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    oa_client = None

# ──────────────────────────────────────────────────────────────────────────────
# STORAGE
# ──────────────────────────────────────────────────────────────────────────────
TZ_MSK = ZoneInfo("Europe/Moscow")
DATA_DIR     = Path(".")
STATS_PATH   = DATA_DIR / "stats.json"
STORIES_PATH = DATA_DIR / "stories.json"

FONT_DIR  = Path("fonts")
FONT_REG  = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
PDF_FONT   = "DejaVu"
PDF_FONT_B = "DejaVuB"

def msk_now() -> datetime: return datetime.now(TZ_MSK)
def msk_today_str() -> str: return msk_now().strftime("%Y-%m-%d")

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
    }

def default_user_stories() -> Dict[str, Any]:
    return {
        "last": None, "history": [],
        "profile": {
            "age": 6,
            "hero": "котёнок",
            "length": "средняя",            # короткая/средняя/длинная
            "style": "классика",            # классика/приключение/детектив/фантазия/научпоп
            "avoid": []
        },
    }

def get_user_stats(uid: int) -> Dict[str, Any]:
    u = stats_all.get(str(uid))
    if not u:
        u = default_stats(); stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)
    if u.get("today_date") != msk_today_str():
        u["today_date"] = msk_today_str(); u["today_stories"] = 0; save_json(STATS_PATH, stats_all)
    return u

def inc_story_counters(uid: int, title: str):
    u = get_user_stats(uid)
    u["stories_total"] += 1
    u["today_stories"] += 1
    u["last_story_ts"] = msk_now().isoformat()
    u["last_story_title"] = title
    stats_all[str(uid)] = u
    save_json(STATS_PATH, stats_all)

def inc_math_counter(uid: int):
    u = get_user_stats(uid); u["math_total"] += 1
    stats_all[str(uid)] = u; save_json(STATS_PATH, stats_all)

def get_profile(uid: int) -> Dict[str, Any]:
    rec = stories_all.get(str(uid))
    if not rec:
        rec = default_user_stories(); stories_all[str(uid)] = rec; save_json(STORIES_PATH, stories_all)
    return rec["profile"]

def save_profile(uid: int, prof: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    rec["profile"] = prof; stories_all[str(uid)] = rec; save_json(STORIES_PATH, stories_all)

def store_user_story(uid: int, story: Dict[str, Any]):
    rec = stories_all.get(str(uid), default_user_stories())
    stamped = dict(story); stamped["ts"] = msk_now().isoformat()
    rec["last"] = stamped
    hist = rec.get("history", []); hist.append(stamped); rec["history"] = hist[-25:]
    stories_all[str(uid)] = rec; save_json(STORIES_PATH, stories_all)

# ──────────────────────────────────────────────────────────────────────────────
# ДЛИНА/ВОЗРАСТ/СТИЛЬ
# ──────────────────────────────────────────────────────────────────────────────
STORY_STYLES = {
    "классика":   "добрая классическая сказка с ясной моралью",
    "приключение":"динамичное детское приключение с мини-препятствиями и взаимопомощью",
    "детектив":   "лёгкий детский детектив: загадка → подсказки → добрый финал",
    "фантазия":   "волшебная история с мягким чудом и необычными существами",
    "научпоп":    "познавательная история: герой открывает простое правило/эффект",
}

LEN_BANDS = {
    "короткая": (250, 400),
    "средняя":  (450, 700),
    "длинная":  (800, 1100),
}

def word_count_ru(text: str) -> int:
    # грубо, но стабильно для контроля диапазона
    return len(re.findall(r"[А-Яа-яЁёA-Za-z0-9-]+", text))

def within_band(text: str, band: Tuple[int,int]) -> bool:
    wc = word_count_ru(text)
    return band[0] <= wc <= band[1]

def clamp_to_band_locally(text: str, band: Tuple[int,int]) -> str:
    # Если длиннее — мягко урезаем последние предложения; если короче — слегка расширяем связками.
    wc = word_count_ru(text)
    if wc > band[1]:
        parts = re.split(r"(\n\n+)", text)  # абзацы
        while word_count_ru("".join(parts)) > band[1] and len(parts) > 1:
            parts = parts[:-1]  # убираем финальную реплику/абзац
        text = "".join(parts)
        # если всё ещё длинно — отрежем последние предложения
        sents = re.split(r"(?<=[\.\!\?])\s+", text)
        while word_count_ru(" ".join(sents)) > band[1] and len(sents) > 3:
            sents = sents[:-1]
        return " ".join(sents)
    if wc < band[0]:
        gap = band[0] - wc
        filler = (
            " Малые шаги приносят большие перемены. "
            "Когда рядом добрые люди, любое дело становится понятнее и светлее. "
        )
        # добавим 1–3 фразы на конце
        need = 1 if gap < 40 else (2 if gap < 120 else 3)
        return text.rstrip() + "\n\n" + (filler * need).strip()
    return text

# ──────────────────────────────────────────────────────────────────────────────
# ГЕНЕРАЦИЯ СКАЗКИ
# ──────────────────────────────────────────────────────────────────────────────
def _avoid_filter(text: str, avoid: List[str]) -> str:
    if not avoid: return text
    for w in [a.strip() for a in avoid if a.strip()]:
        text = re.sub(re.escape(w), "🌟", text, flags=re.IGNORECASE)
    return text

def _local_story(age: int, hero: str, moral: str, target_band: Tuple[int,int], style: str, avoid: List[str]) -> Dict[str, Any]:
    # Локальный разумный генератор: простая дуга «цель → попытки → развязка», 3–5 абзацев, целевая длина.
    hero  = hero or "герой"
    moral = moral or "доброта"
    style_note = STORY_STYLES.get(style, STORY_STYLES["классика"])

    def sent_pool():
        starts = [
            f"Жил-был {hero}, который решил понять, что такое {moral}.",
            f"Однажды {hero} проснулся и загадал день, в котором {moral} станет видна.",
            f"С раннего утра {hero} чувствовал, что сегодня научится замечать {moral}.",
        ]
        tries = [
            f"{hero.capitalize()} помогал тем, кто рядом, и замечал, как меняется настроение.",
            f"Иногда было трудно, но маленькие шаги давали смелость продолжать.",
            f"Друзья поддерживали, и вместе они находили простые ответы.",
            f"Каждая добрая мысль превращалась в тихое действие — и становилось светлее.",
        ]
        reveals = [
            f"К вечеру {hero} понял: {moral} — это не слово, а поступок, который согревает.",
            f"Возвращаясь домой, {hero} улыбался: {moral} живёт в заботе и внимании.",
            f"Так {hero} запомнил: если делишься теплом, {moral} становится заметной для всех.",
        ]
        return starts, tries, reveals

    start_pool, try_pool, reveal_pool = sent_pool()
    paras = [random.choice(start_pool)]
    paras += random.sample(try_pool, k=2)
    paras.append(random.choice(try_pool))
    paras.append(random.choice(reveal_pool))

    # Сборка, затем расширение/сжатие под диапазон
    text = "\n\n".join(paras)
    text = clamp_to_band_locally(text, target_band)
    text = _avoid_filter(text, avoid)

    title = f"{hero.capitalize()} и урок про «{moral}»"
    questions = [
        f"Что {hero} понял про {moral}?",
        "Какие шаги помогли героям продвинуться?",
        "Где в истории чувствовалась дружба?",
        "Как бы ты поступил(а) на месте героя?",
    ]
    moral_txt = f"Важно помнить: {moral}. Даже маленькое добро меняет день."
    return {"title": title, "text": text, "moral": moral_txt, "questions": questions, "style_note": style_note}

def _json_from_response(resp) -> Dict[str, Any]:
    try:
        return json.loads(resp.output_text or "{}")
    except Exception:
        return {}

def synthesize_story(age: int, hero: str, moral: str, length: str, avoid: List[str], style: str) -> Dict[str, Any]:
    band = LEN_BANDS.get((length or "").lower(), LEN_BANDS["средняя"])
    hero  = hero or "герой"
    moral = moral or "доброта"
    style_note = STORY_STYLES.get(style, STORY_STYLES["классика"])

    # Если нет OpenAI — локальный генератор с контролем длины:
    if not oa_client:
        return _local_story(age, hero, moral, band, style, avoid)

    # 1) План
    try:
        prompt1 = f"""
Ты — детский редактор и писатель. Сделай план сказки (outline) для ребёнка {age} лет.
Стиль: {style_note}. Герой: {hero}. Главная идея/мораль: {moral}. Тем избегать: {", ".join(avoid) or "нет"}.
Структура: завязка → 3–4 сцены (цель, препятствие, решение) → светлая развязка → чёткая мораль.
Ответ строго JSON: {{"title":"...","scenes":[{{"name":"...","beats":["...","..."]}}]}}
"""
        r1 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt1)
        outline = _json_from_response(r1)
        title = outline.get("title") or f"{hero.capitalize()} и урок про «{moral}»"
    except Exception as e:
        print("[AI outline]", e)
        return _local_story(age, hero, moral, band, style, avoid)

    # 2) Черновик по плану
    try:
        prompt2 = f"""
Напиши связную сказку на русском для ребёнка {age} лет по плану ниже.
План: {json.dumps(outline, ensure_ascii=False)}
Требования:
- Объём: {band[0]}–{band[1]} слов, соблюдай диапазон.
- Язык: простой и тёплый, без взрослой лексики, без форм "(ась)/(ёл)".
- Структура: 3–6 абзацев, каждый логически ведёт к следующему.
- В конце блок "Мораль" (1–2 фразы) и 4 вопроса ребёнку.
Ответ строго JSON: {{"text":"...","moral":"...","questions":["...","...","...","..."]}}
"""
        r2 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt2)
        draft = _json_from_response(r2)
        text = draft.get("text","")
        moral_txt = draft.get("moral") or f"Важно помнить: {moral}."
        questions = draft.get("questions") or [
            f"Что {hero} понял про {moral}?", "Какие шаги помогли героям?",
            "Где в истории дружба?", "Как бы ты поступил(а)?"
        ]
    except Exception as e:
        print("[AI draft]", e)
        return _local_story(age, hero, moral, band, style, avoid)

    # 3) Критика и правка (если вышли за диапазон или нарушены требования)
    try:
        needs_fix = False
        if not within_band(text, band): needs_fix = True
        # Мини-чеклист: должна быть цель героя, препятствия, развязка, явная мораль.
        checklist = [
            ("цель героя", re.search(r"хочет|решил|мечтал|цель", text, re.IGNORECASE)),
            ("препятствие/трудности", re.search(r"трудн|препятств|не просто|мешал", text, re.IGNORECASE)),
            ("развязка/вывод", re.search(r"к вечеру|в конце|понял|итог|вывод", text, re.IGNORECASE)),
        ]
        if any(v is None for _, v in checklist): needs_fix = True

        if needs_fix:
            prompt3 = f"""
Отредактируй сказку для ребёнка {age} лет так, чтобы она была связной и в диапазоне {band[0]}–{band[1]} слов.
Соблюдай: цель героя → препятствия → решения → светлая развязка + явная мораль.
Сделай язык тёплым и простым. Не используй взрослые темы.
Верни строго JSON {{"text":"...","moral":"...","questions":[...]}}, 4 вопроса обязательно.
Исходный JSON: {json.dumps({"text": text, "moral": moral_txt, "questions": questions}, ensure_ascii=False)}
"""
            r3 = oa_client.responses.create(model=OPENAI_MODEL_TEXT, input=prompt3)
            data = _json_from_response(r3)
            text = data.get("text", text)
            moral_txt = data.get("moral", moral_txt)
            questions = (data.get("questions") or questions)[:4]
    except Exception as e:
        print("[AI revise]", e)

    # Страховка по длине (локально)
    text = clamp_to_band_locally(text, band)
    text = _avoid_filter(text, avoid)

    return {"title": title, "text": text, "moral": moral_txt, "questions": questions}

# ──────────────────────────────────────────────────────────────────────────────
# PDF (без картинок)
# ──────────────────────────────────────────────────────────────────────────────
class StoryPDF(FPDF):
    def header(self): pass

def _ensure_unicode_fonts(pdf: FPDF) -> bool:
    try:
        if not (FONT_REG.exists() and FONT_BOLD.exists()):
            print("[PDF] TTF не найдены (fonts/DejaVuSans*.ttf)")
            return False
        pdf.add_font(PDF_FONT,   "", str(FONT_REG),  uni=True)
        pdf.add_font(PDF_FONT_B, "", str(FONT_BOLD), uni=True)
        return True
    except Exception as e:
        print(f"[PDF] font error: {e}")
        return False

def render_story_pdf(path: Path, data: Dict[str, Any]):
    pdf = StoryPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    uni = _ensure_unicode_fonts(pdf)

    # титул
    pdf.add_page()
    pdf.set_fill_color(235, 240, 255)
    pdf.rect(0, 0, 210, 297, style="F")
    pdf.set_draw_color(60, 80, 180); pdf.set_line_width(1.2)
    pdf.rect(8, 8, 210-16, 297-16)

    if uni: pdf.set_font(PDF_FONT_B, size=26)
    else:   pdf.set_font("Helvetica", style="B", size=26)
    pdf.set_xy(15, 60)
    pdf.multi_cell(0, 12, data["title"], align="C")

    if uni: pdf.set_font(PDF_FONT, size=12)
    else:   pdf.set_font("Helvetica", size=12)
    meta = f"Создано: {msk_now().strftime('%d.%m.%Y')}"
    pdf.ln(4); pdf.multi_cell(0, 8, meta, align="C")

    # текст
    pdf.add_page()
    if uni: pdf.set_font(PDF_FONT_B, size=16)
    else:   pdf.set_font("Helvetica", style="B", size=16)
    pdf.multi_cell(0, 8, data["title"]); pdf.ln(2)

    if uni: pdf.set_font(PDF_FONT, size=12)
    else:   pdf.set_font("Helvetica", size=12)
    for p in data["text"].split("\n\n"):
        pdf.multi_cell(0, 7, p); pdf.ln(1)

    pdf.ln(2)
    if uni: pdf.set_font(PDF_FONT_B, size=13)
    else:   pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 7, "Мораль", ln=1)

    if uni: pdf.set_font(PDF_FONT, size=12)
    else:   pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 7, data["moral"]); pdf.ln(2)

    if uni: pdf.set_font(PDF_FONT_B, size=13)
    else:   pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 7, "Вопросы", ln=1)

    if uni: pdf.set_font(PDF_FONT, size=12)
    else:   pdf.set_font("Helvetica", size=12)
    for i, q in enumerate(data["questions"][:4], 1):
        pdf.multi_cell(0, 7, f"{i}) {q}")

    pdf.output(str(Path(path)))

# ──────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ И ДИАЛОГ
# ──────────────────────────────────────────────────────────────────────────────
def _safe_int(text: str, default: int) -> int:
    try: return max(3, min(14, int(text)))
    except Exception: return default

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if args and args[0].lower() in {"story","math","parent","settings","delete"}:
        return await globals()[args[0].lower()+"_cmd"](update, context)
    await update.effective_message.reply_html(
        "<b>Привет! Я — Читалкин&Циферкин 🦉➕🧮</b>\n\n"
        "• /story — сказка (текст → PDF)\n"
        "• /math — 10 минут примеров\n"
        "• /parent — отчёт родителю\n"
        "• /settings — профиль ребёнка (возраст, герой, длина, стиль, «избегать»)\n"
        "• /delete — удалить мои данные\n\n"
        f"<i>Лимит: {'без ограничений (тест) ' if DISABLE_LIMIT else str(MAX_STORIES_PER_DAY)+'/день'}; сброс в 00:00 Мск.</i>"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    prof = get_profile(uid)
    ud = context.user_data; ud.clear()
    ud["flow"] = "settings"; ud["step"] = "age"; ud["profile"] = prof.copy()
    await update.effective_message.reply_text(
        "⚙️ Настройки.\n"
        f"Сейчас: возраст={prof['age']}, герой=«{prof['hero']}», длина={prof['length']}, стиль=«{prof['style']}», избегать={', '.join(prof['avoid']) or '—'}.\n\n"
        "Введите возраст (3–14):"
    )

async def story_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ustat = get_user_stats(uid)
    if not DISABLE_LIMIT and ustat["today_stories"] >= MAX_STORIES_PER_DAY:
        await update.effective_message.reply_text("На сегодня лимит исчерпан."); return

    prof = get_profile(uid)
    ud = context.user_data; ud.clear()
    ud["flow"] = "story"; ud["step"] = "age"; ud["params"] = {
        "age": prof["age"], "hero": prof["hero"], "length": prof["length"], "style": prof["style"]
    }
    await update.effective_message.reply_text(
        f"Давай подберём сказку. Сколько лет ребёнку? (по умолчанию {prof['age']})"
    )

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
    prof = get_profile(uid)
    await update.effective_message.reply_text(
        "👪 Отчёт родителю\n\n"
        f"Сегодня: сказок {u.get('today_stories',0)} / {('∞' if DISABLE_LIMIT else MAX_STORIES_PER_DAY)}\n"
        f"Итого: сказок {u.get('stories_total',0)}, математика {u.get('math_total',0)}\n\n"
        f"Последняя сказка: {last_title} • {last_when}\n\n"
        "Профиль:\n"
        f"возраст={prof['age']}, герой=«{prof['hero']}», длина={prof['length']}, стиль=«{prof['style']}», избегать={', '.join(prof['avoid']) or '—'}"
    )

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats_all.pop(str(uid), None); save_json(STATS_PATH, stats_all)
    stories_all.pop(str(uid), None); save_json(STORIES_PATH, stories_all)
    context.user_data.clear()
    await update.effective_message.reply_text("Ваши данные удалены. Можно начать заново 🙂")

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
    pr, an = make_math_sheet()
    await update.effective_message.reply_text("🧮 10 минут математики:\n" + "\n".join([f"{i+1}) {p}" for i,p in enumerate(pr)]))
    await update.effective_message.reply_text("Ответы:\n" + "\n".join([f"{i+1}) {a}" for i,a in enumerate(an)]))
    inc_math_counter(uid)

# текстовые шаги (settings/story)
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data; flow = ud.get("flow"); step = ud.get("step")
    if not flow: return
    text = (update.effective_message.text or "").strip()

    if flow == "settings":
        prof = ud.get("profile", {})
        if step == "age":
            prof["age"] = _safe_int(text, prof.get("age",6)); ud["step"] = "hero"
            await update.effective_message.reply_text("Герой по умолчанию (например: котёнок, ёжик, Маша):"); return
        if step == "hero":
            prof["hero"] = text or prof.get("hero","герой"); ud["step"] = "length"
            await update.effective_message.reply_text("Длина сказки? (короткая / средняя / длинная)"); return
        if step == "length":
            l = text.lower(); prof["length"] = l if l in LEN_BANDS else "средняя"
            ud["step"] = "style"
            await update.effective_message.reply_text("Стиль? (классика / приключение / детектив / фантазия / научпоп)"); return
        if step == "style":
            st = text.lower(); prof["style"] = st if st in STORY_STYLES else "классика"
            ud["step"] = "avoid"
            await update.effective_message.reply_text("Каких тем избегать? Через запятую (или «нет»)."); return
        if step == "avoid":
            prof["avoid"] = [] if text.lower() in {"нет","no","none"} else [w.strip() for w in text.split(",") if w.strip()]
            save_profile(update.effective_user.id, prof); ud.clear()
            await update.effective_message.reply_text("Готово! Профиль сохранён ✅"); return

    if flow == "story":
        p = ud["params"]
        if step == "age":
            p["age"] = _safe_int(text, p.get("age",6)); ud["step"] = "hero"
            await update.effective_message.reply_text(f"Кто будет героем? (по умолчанию «{p.get('hero','герой')}»)"); return
        if step == "hero":
            p["hero"] = text or p.get("hero","герой"); ud["step"] = "moral"
            await update.effective_message.reply_text("Какую идею/мораль подчеркнуть? (дружба, щедрость, смелость...)"); return
        if step == "moral":
            ud["moral"] = text or "доброта"; ud["step"] = "length"
            await update.effective_message.reply_text(f"Какая длина? (короткая / средняя / длинная) — по умолчанию {p.get('length','средняя')}"); return
        if step == "length":
            l = (text.lower() if text else p.get("length","средняя"))
            p["length"] = l if l in LEN_BANDS else "средняя"

            uid = update.effective_user.id
            prof = get_profile(uid)
            ustat = get_user_stats(uid)
            if not DISABLE_LIMIT and ustat["today_stories"] >= MAX_STORIES_PER_DAY:
                await update.effective_message.reply_text("На сегодня лимит исчерпан."); ud.clear(); return

            data = synthesize_story(p["age"], p["hero"], ud["moral"], p["length"], avoid=prof["avoid"], style=prof["style"])
            inc_story_counters(uid, data["title"])
            store_user_story(uid, data)

            # текст в чат
            msg = (
                f"📖 <b>{data['title']}</b>\n\n{data['text']}\n\n"
                f"<b>Мораль:</b> {data['moral']}\n\n"
                "Вопросы:\n"
                f"1) {data['questions'][0]}\n"
                f"2) {data['questions'][1]}\n"
                f"3) {data['questions'][2]}\n"
                f"4) {data['questions'][3]}"
            )
            await update.effective_message.reply_html(msg)

            # pdf
            pdf_path = Path(f"skazka_{uid}.pdf").resolve()
            render_story_pdf(pdf_path, data)
            await update.effective_message.reply_document(InputFile(str(pdf_path), filename=pdf_path.name))

            ud.clear(); return

# ошибки → алёрт (если указан чат)
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALERT_CHAT_ID:
        print("[ERR]", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
        return
    try:
        tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
        text = "🚨 <b>Ошибка</b>\n\n<pre>" + (tb[-3500:] if len(tb)>3500 else tb) + "</pre>"
        await context.bot.send_message(chat_id=int(ALERT_CHAT_ID), text=text, parse_mode="HTML")
    except Exception as e:
        print("[ERR alert send]", e)

# ──────────────────────────────────────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start","меню"),
        BotCommand("story","сказка (текст → PDF)"),
        BotCommand("math","10 минут математики"),
        BotCommand("parent","отчёт родителю"),
        BotCommand("settings","настройки профиля"),
        BotCommand("delete","удалить мои данные"),
        BotCommand("help","помощь"),
    ])

def main():
    if BOT_TOKEN.startswith("ВСТАВЬ_"):
        raise SystemExit("Сначала задайте BOT_TOKEN (переменная окружения).")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("story", story_cmd))
    app.add_handler(CommandHandler("math", math_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    if PUBLIC_URL:
        path = (WEBHOOK_PATH or BOT_TOKEN).lstrip("/")
        webhook_url = f"{PUBLIC_URL.rstrip('/')}/{path}"
        print(f"[WEBHOOK] starting on 0.0.0.0:{PORT}, path=/{path}")
        print(f"[WEBHOOK] set webhook → {webhook_url}")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=webhook_url, drop_pending_updates=True)
    else:
        print("[POLLING] starting…")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
