import asyncio
import csv
import datetime
import html
import io
import json
import logging
import os
import re
import shutil
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN, ADMIN_IDS, FILES_DIR, SHEET_WEBHOOK_URL,
    AUDIT_CHANNEL_ID, ADMIN_MASTER_PIN, GEMINI_API_KEY, DB_PATH
)
from database import (
    init_db, get_department, get_dept_by_user, get_dept_by_code,
    bind_user_to_dept, unbind_user, add_entry, get_dept_summary, get_all_summary,
    get_all_detailed_entries, get_dept_entries, delete_entry, clear_all_test_data,
    get_entries_by_category, get_files_for_zip, get_entry_by_id, update_entry_title,
    update_entry_field, append_entry_file, parse_entry_files, restore_from_csv_bytes,
    get_db_health_cache, update_entry_full, DEPARTMENTS, INDICATORS, INDICATOR_LABELS
)
from report_gen import (
    generate_report_docx, generate_codes_docx, generate_report_excel,
    generate_files_zip, stream_files_zip, normalize_work_date
)
from ai_analysis import generate_dept_analysis, generate_full_analysis
from scopus_verifier import verify_article

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_submission_count = 0
_db_dirty_since_backup = False
_delayed_backup_task = None

# Буфер пакетной загрузки файлов (защита от гонки при отправке альбома из 2-10 фото/файлов сразу)
_user_upload_buffers: dict[int, list[types.Message]] = {}
_user_upload_tasks: dict[int, asyncio.Task] = {}


# ─── FSM STATES ─────────────────────────────────────────────────────────────
class AuthState(StatesGroup):
    waiting_for_code = State()


class AdminPinState(StatesGroup):
    waiting_for_pin = State()


class RestoreState(StatesGroup):
    waiting_for_db_file = State()


class EditFieldState(StatesGroup):
    waiting_for_field_value = State()
    waiting_for_file_replace = State()
    waiting_for_file_append = State()


class PeriodReportState(StatesGroup):
    waiting_for_custom_interval = State()
    waiting_for_zip_interval = State()


class AddEntry(StatesGroup):
    choose_category     = State()
    enter_title         = State()
    enter_authors       = State()
    enter_country       = State()   # Давлат / Патент тури / Анжуман шакли
    enter_journal       = State()   # Журнал номи / Буюртмачи / Анжуман кўлами
    enter_pub_date      = State()   # Нашр санаси / Сон / Бетлар
    enter_url           = State()   # URL / DOI
    enter_authors_count = State()   # Муаллифлар / Иштирокчилар сони
    enter_specialty     = State()   # Ихтисослик шифри ва номи (DSc/PhD)
    enter_reg_number    = State()   # Рег. рақам (Патент / Гувоҳнома)
    enter_publisher     = State()   # Нашриёт номи (Монография)
    enter_venue         = State()   # Анжуман жойи ва санаси (Тезис)
    enter_amount        = State()   # Шартнома / Грант суммаси
    upload_file         = State()


# ─── КОНФИГУРАЦИЯ ДОПОЛНИТЕЛЬНЫХ ШАГОВ ПО КАТЕГОРИЯМ ────────────────────────
CATEGORY_EXTRA_STEPS = {
    "scopus_wos": [
        ("enter_country",       "🌍 <b>Журнал нашр этилган давлат:</b>\n<i>(Масалан: Germany, USA, UK, Switzerland, Netherlands)</i>"),
        ("enter_journal",       "📰 <b>Илмий журнал номи:</b>\n<i>(Масалан: Journal of Nanostructures)</i>"),
        ("enter_pub_date",      "📅 <b>Нашр санаси, жилди (Vol), сони (Issue) ва бетлари:</b>\n<i>(Масалан: 16.03.2026, Vol. 5, No. 2, 431-437)</i>"),
        ("enter_url",           "🔗 <b>Мақола ҳаволаси (DOI ёки URL):</b>\n<i>(Масалан: https://doi.org/10.1016/... ёки пастдаги тугмани босинг)</i>"),
        ("enter_authors_count", "👥 <b>Муаллифлар умумий сони (рақам билан):</b>\n<i>(Масалан: 4)</i>"),
    ],
    "oak_ru_if": [
        ("enter_country",       "🌍 <b>Журнал нашр этилган давлат:</b>\n<i>(Масалан: Россия, Германия, Польша, США)</i>"),
        ("enter_journal",       "📰 <b>Илмий журнал номи:</b>\n<i>(Масалан: Клиническая медицина)</i>"),
        ("enter_pub_date",      "📅 <b>Нашр санаси, йили, сони ва бетлари:</b>\n<i>(Масалан: 12.04.2026 ёки Vol. 6 No. 04 (2026), 15-20)</i>"),
        ("enter_url",           "🔗 <b>Мақола интернет ҳаволаси (URL ёки DOI):</b>\n<i>(Масалан: https://doi.org/... ёки пастдаги тугмани босинг)</i>"),
        ("enter_authors_count", "👥 <b>Муаллифлар умумий сони (рақам билан):</b>\n<i>(Масалан: 3)</i>"),
    ],
    "oak_uz": [
        ("enter_journal",       "📰 <b>Илмий журнал номи:</b>\n<i>(Масалан: Профилактик тиббиёт ва саломатлик)</i>"),
        ("enter_pub_date",      "📅 <b>Нашр санаси, йили ва сони:</b>\n<i>(Масалан: 05.02.2026 ёки 2026 йил, №2, 45-49 бетлар)</i>"),
        ("enter_url",           "🔗 <b>Мақоланинг интернет ҳаволаси (URL):</b>\n<i>(Агар ҳавола бўлмаса, «⏭ Ўтказиб юбориш» тугмасини босинг)</i>"),
    ],
    "dsc": [
        ("enter_specialty",     "🎓 <b>Ихтисослик шифри ва номи:</b>\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",      "📅 <b>Ҳимоя ёки ОАК тасдиқлаган сана:</b>\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "phd": [
        ("enter_specialty",     "🎓 <b>Ихтисослик шифри ва номи:</b>\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",      "📅 <b>Ҳимоя ёки ОАК тасдиқлаган сана:</b>\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "monography": [
        ("enter_publisher",     "🏢 <b>Нашриёт номи:</b>\n<i>(Масалан: «Fan va texnologiya» нашриёти)</i>"),
        ("enter_pub_date",      "📅 <b>Нашр этилган санаси (ёки йили ва ойи):</b>\n<i>(Масалан: 14.04.2026 ёки Апрел 2026)</i>"),
    ],
    "patent": [
        ("enter_country",       "📋 <b>Патент ёки гувоҳнома турини танланг (ёки ёзинг):</b>\n<i>(IAP — Ихтиро, FAP — Фойдали модель, DGU — ЭҲМ дастури, BGU — Маълумотлар базаси)</i>"),
        ("enter_reg_number",    "🔢 <b>Патент / Гувоҳнома рўйхат рақами:</b>\n<i>(Масалан: № DGU 34512 ёки № IAP 07542)</i>"),
        ("enter_pub_date",      "📅 <b>Берилган санаси:</b>\n<i>(Масалан: 23.02.2026)</i>"),
    ],
    "thesis_foreign": [
        ("enter_venue",         "📍 <b>Анжуман ўтказилган давлат/шаҳар ва санаси:</b>\n<i>(Масалан: Москва, 15.04.2026)</i>"),
    ],
    "thesis_uz": [
        ("enter_venue",         "📍 <b>Анжуман ўтказилган шаҳар ва санаси:</b>\n<i>(Масалан: Тошкент, 10.05.2026)</i>"),
    ],
    "rationalizer": [
        ("enter_reg_number",    "🔢 <b>Гувоҳнома рақами (агар бўлса):</b>\n<i>(Масалан: № 45/2026 ёки «⏭ Ўтказиб юбориш» тугмасини босинг)</i>"),
        ("enter_pub_date",      "📅 <b>Гувоҳнома берилган сана:</b>\n<i>(Масалан: 18.03.2026)</i>"),
    ],
    "implementation": [
        ("enter_journal",       "🏥 <b>Жорий этилган муассаса номи:</b>\n<i>(Масалан: АДТИ клиникаси ёки Вилоят кўп тармоқли тиббиёт маркази)</i>"),
        ("enter_pub_date",      "📅 <b>Далолатнома тасдиқланган сана:</b>\n<i>(Масалан: 20.03.2026)</i>"),
    ],
    "conferences": [
        ("enter_country",       "📋 <b>Анжуман ўтказилиш шаклини танланг:</b>\n<i>(Офлайн / Онлайн / Гибрид)</i>"),
        ("enter_journal",       "🌐 <b>Анжуман миқёсини (кўламини) танланг:</b>\n<i>(Маҳаллий / Республика / Халқаро)</i>"),
        ("enter_pub_date",      "📅 <b>Ўтказилган санаси:</b>\n<i>(Масалан: 15.03.2026)</i>"),
        ("enter_authors_count", "👥 <b>Қатнашувчилар умумий сони (рақам билан):</b>\n<i>(Масалан: 85)</i>"),
    ],
    "contracts": [
        ("enter_amount",        "💰 <b>Шартнома суммаси (млн сўмда ёки сўмда):</b>\n<i>(Масалан: 25 млн сўм ёки 25 000 000 сўм)</i>"),
        ("enter_journal",       "🏢 <b>Буюртмачи корхона / ташкилот номи:</b>\n<i>(Масалан: «Андижон биофарм» МЧЖ)</i>"),
        ("enter_pub_date",      "📅 <b>Шартнома рақами ва санаси:</b>\n<i>(Масалан: №14/2026, 10.03.2026)</i>"),
    ],
    "grants": [
        ("enter_amount",        "💰 <b>Грант суммаси (млн сўм ёки валютада):</b>\n<i>(Масалан: 180 млн сўм ёки 25,000 USD)</i>"),
        ("enter_journal",       "🏢 <b>Молиялаштирувчи фонд / ташкилот:</b>\n<i>(Масалан: Инновацион ривожланиш агентлиги)</i>"),
        ("enter_pub_date",      "📅 <b>Грант бошланган сана ва муддати:</b>\n<i>(Масалан: 01.03.2026, 2026-2027 йй.)</i>"),
    ],
}

STATE_FIELD_MAP = {
    "enter_country":       "country",
    "enter_journal":       "journal_name",
    "enter_pub_date":      "pub_date",
    "enter_url":           "url",
    "enter_authors_count": "authors_count",
    "enter_specialty":     "specialty",
    "enter_reg_number":    "reg_number",
    "enter_publisher":     "publisher",
    "enter_venue":         "pub_date",
    "enter_amount":        "amount",
}


# ─── SMART INPUT VALIDATORS & NORMALIZERS ───────────────────────────────────
def clean_single_line(text: str) -> str:
    """Убирает лишние переносы строк и двойные пробелы"""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


def validate_and_clean_title(text: str, category: str = "") -> tuple[bool, str, str]:
    """
    Проверяет корректность названия работы.
    Возвращает: (is_valid, cleaned_title, error_message)
    """
    cleaned = clean_single_line(text)
    if not cleaned or cleaned in ("-", "—", ".", "..", "...", "йўқ", "yo'q", "нет"):
        return False, "", "⚠️ <b>Иш номи бўш бўлиши мумкин эмас!</b>\nИлтимос, илмий ишнинг тўлиқ номини (мавзусини) ёзиб юборинг."

    # Защита от ввода количества ("1", "2", "1 та", "3ta") вместо названия
    if re.match(r'^\d+\s*(та|ta|дона|шт\.?)?$', cleaned, flags=re.IGNORECASE):
        return False, "", (
            "⚠️ <b>Сиз ишлар сонини киритдингиз!</b>\n"
            "Бу қадамда ишлар сонини эмас, балки илмий ишнинг <b>ТЎЛИҚ НОМИНИ (мавзусини)</b> ёзинг.\n"
            "<i>(Масалан: «Юрак ишемик касаллигини эрта ташхислашнинг замонавий усуллари»)</i>"
        )

    # Защита от вставки голой ссылки вместо названия
    if re.match(r'^https?://\S+$', cleaned, flags=re.IGNORECASE):
        return False, "", (
            "⚠️ <b>Сиз интернет ҳавола (URL) юбордингиз!</b>\n"
            "Илтимос, аввал мақола ёки ишнинг <b>НОМИНИ (сарлавҳасини)</b> матн кўринишида ёзинг.\n"
            "<i>(Интернет ҳаволани кейинги қадамларда сўраймиз)</i>"
        )

    min_len = 2 if category in ("contracts", "grants") else 4
    if len(cleaned) < min_len:
        return False, "", "⚠️ <b>Иш номи жуда қисқа!</b>\nИлтимос, мавзуни тўлиқроқ ёзинг (камида 4 та ҳарф)."

    return True, cleaned, ""


def validate_and_clean_authors(text: str) -> tuple[bool, str, str]:
    """
    Проверяет и нормализует список авторов (Ф.И.Ш.).
    Возвращает: (is_valid, cleaned_authors, error_message)
    """
    if not text:
        return False, "", "⚠️ Илтимос, муаллиф(лар)нинг Ф.И.Ш.ни киритинг."

    # Заменяем точки с запятой и переносы строк на запятые
    normalized = re.sub(r'[\r\n;]+', ', ', text)
    normalized = re.sub(r'\s+', ' ', normalized).strip(' ,;')

    if not normalized or normalized in ("-", "—", ".", "..", "...", "йўқ", "yo'q"):
        return False, "", "⚠️ <b>Муаллиф Ф.И.Ш. киритилмади!</b>\nИлтимос, муаллиф(лар) исм-фамилиясини ёзинг."

    # Защита от ввода числа авторов ("5", "3 та") вместо Ф.И.Ш.
    if re.match(r'^\d+\s*(та|ta|киши|нафар)?$', normalized, flags=re.IGNORECASE):
        return False, "", (
            "⚠️ <b>Сиз рақам киритдингиз!</b>\n"
            "Илтимос, бу қадамда муаллифлар сонини эмас, уларнинг <b>Ф.И.Ш. (Исм ва фамилияси)ни</b> ёзинг.\n"
            "<i>(Масалан: Каримов А.А., Тошматов Б.Б.)</i>"
        )

    if re.match(r'^https?://\S+$', normalized, flags=re.IGNORECASE):
        return False, "", "⚠️ Сиз ҳавола (URL) юбордингиз. Илтимос, муаллиф(лар)нинг <b>Ф.И.Ш.ни</b> ёзинг!"

    if len(normalized) < 3 or not re.search(r'[A-Za-zА-Яа-яЎўҚқҒғҲҳЁё]', normalized):
        return False, "", "⚠️ Илтимос, муаллифнинг исм-фамилиясини ҳарфлар билан тўғри ёзинг."

    parts = [p.strip() for p in normalized.split(",") if p.strip()]
    return True, ", ".join(parts), ""


def validate_and_clean_count(text: str, default_from_authors: str = "") -> tuple[bool, str, str]:
    """Проверяет количество авторов/участников (извлекает число из '4 та', '45 киши' и т.д.)"""
    cleaned = clean_single_line(text)
    if cleaned.isdigit() and 1 <= int(cleaned) <= 5000:
        return True, cleaned, ""

    # Если вставили URL
    if "http://" in cleaned.lower() or "https://" in cleaned.lower():
        return False, "", "⚠️ Сиз интернет ҳавола юбордингиз. Илтимос, сонини <b>рақам билан</b> киритинг (Масалан: <code>4</code>)."

    m = re.search(r'\b(\d{1,4})\b', cleaned)
    if m and len(cleaned) <= 45:
        val = int(m.group(1))
        if 1 <= val <= 5000:
            return True, str(val), ""

    return False, "", "⚠️ Илтимос, сонини фақат <b>рақам билан</b> киритинг!\n<i>(Масалан: <code>1</code>, <code>3</code> ёки <code>5</code>)</i>"


def validate_and_clean_pub_date(text: str) -> tuple[bool, str, str]:
    """Проверяет дату/выпуск публикации"""
    cleaned = clean_single_line(text)
    if not cleaned or len(cleaned) < 2 or cleaned in ("-", "—", ".", "йўқ", "yo'q"):
        return False, "", (
            "⚠️ <b>Сана ёки нашр маълумоти киритилмади!</b>\n"
            "Илтимос, санани ёзинг (Масалан: <code>15.03.2026</code> ёки <code>2026 йил, №3</code>)."
        )
    if re.match(r'^https?://\S+$', cleaned, flags=re.IGNORECASE):
        return False, "", "⚠️ Сиз ҳавола (URL) юбордингиз. Илтимос, нашр санасини ёзинг (Масалан: <code>15.03.2026</code>)."
    return True, cleaned, ""


def validate_and_clean_amount(text: str) -> tuple[bool, str, str]:
    """Нормализует сумму договора или гранта"""
    cleaned = clean_single_line(text)
    if not cleaned or not re.search(r'\d', cleaned):
        return False, "", (
            "⚠️ <b>Суммада рақам бўлиши шарт!</b>\n"
            "Масалан: <code>25 млн сўм</code> ёки <code>15 000 000 сўм</code> ёки <code>10,000 USD</code>."
        )
    # Если ввели просто число >= 100000 без слова млн/usd, форматируем понятно
    raw_digits = cleaned.replace(" ", "").replace(",", ".")
    if re.match(r'^\d+(\.\d+)?$', raw_digits):
        num = float(raw_digits)
        if num >= 100000:
            mln = num / 1_000_000
            return True, f"{mln:g} млн сўм ({int(num):,} сўм)".replace(",", " "), ""
        else:
            return True, f"{num:g} млн сўм", ""
    return True, cleaned, ""


def parse_user_interval(text: str):
    """Парсит пользовательский ввод: '2025', '2026-04', '2025-09 - 2026-05', '2024 - 2025'"""
    text = text.strip()
    matches_ym = re.findall(r'\b(201[0-9]|202[0-9])[\.\/\-](\d{1,2})\b', text)
    if len(matches_ym) >= 2:
        s_ym = f"{int(matches_ym[0][0]):04d}-{int(matches_ym[0][1]):02d}"
        e_ym = f"{int(matches_ym[1][0]):04d}-{int(matches_ym[1][1]):02d}"
        if s_ym > e_ym:
            s_ym, e_ym = e_ym, s_ym
        return s_ym, e_ym
    elif len(matches_ym) == 1:
        s_ym = f"{int(matches_ym[0][0]):04d}-{int(matches_ym[0][1]):02d}"
        return s_ym, s_ym

    matches_y = re.findall(r'\b(201[0-9]|202[0-9])\b', text)
    if len(matches_y) >= 2:
        y1, y2 = sorted([int(matches_y[0]), int(matches_y[1])])
        return f"{y1:04d}-01", f"{y2:04d}-12"
    elif len(matches_y) == 1:
        y = int(matches_y[0])
        return f"{y:04d}-01", f"{y:04d}-12"

    return None, None


# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb(user_id: int):
    """Эргономичная клавиатура с понятными иконками"""
    if user_id in ADMIN_IDS:
        buttons = [
            [KeyboardButton(text="➕ Ҳисобот қўшиш")],
            [KeyboardButton(text="🏛 Сводный ҳисобот"), KeyboardButton(text="📊 Кафедра статистикаси")],
            [KeyboardButton(text="🤖 AI Сводный таҳлил"), KeyboardButton(text="🤖 AI Кафедра таҳлили")],
            [KeyboardButton(text="📊 Excel ҳисобот (.xlsx)"), KeyboardButton(text="📥 Word ҳисобот (.docx)")],
            [KeyboardButton(text="📅 Давр бўйича ҳисобот (Йил-Ой)")],
            [KeyboardButton(text="📦 Файллар ZIP архиви"), KeyboardButton(text="🗂 Файллар базаси")],
            [KeyboardButton(text="🔑 Кафедралар пароллари"), KeyboardButton(text="🚪 Кафедрадан чиқиш")],
        ]
    else:
        buttons = [
            [KeyboardButton(text="➕ Ҳисобот қўшиш")],
            [KeyboardButton(text="🤖 AI Илмий таҳлил"), KeyboardButton(text="📊 Кафедра статистикаси")],
            [KeyboardButton(text="📋 Юборилган ишлар"), KeyboardButton(text="🗂 Файллар базаси")],
            [KeyboardButton(text="🚪 Кафедрадан чиқиш")],
        ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def categories_kb():
    kb = InlineKeyboardBuilder()
    for key, label in INDICATORS:
        kb.button(text=label, callback_data=f"cat_{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Бекор қилиш", callback_data="cancel"))
    return kb.as_markup()


def step_nav_kb(can_go_back: bool = True, can_skip: bool = False, quick_options: list[tuple[str, str]] = None):
    """Клавиатура навигации по шагам: быстрые кнопки + Назад + Пропустить + Отмена"""
    kb = InlineKeyboardBuilder()
    if quick_options:
        for btn_text, btn_val in quick_options:
            kb.button(text=btn_text, callback_data=f"qopt:{btn_val}")
        kb.adjust(2 if len(quick_options) >= 2 else 1)

    nav_row = []
    if can_skip:
        nav_row.append(InlineKeyboardButton(text="⏭ Ўтказиб юбориш", callback_data="skip_extra_step"))
    if can_go_back:
        nav_row.append(InlineKeyboardButton(text="⬅️ Олдинги қадам", callback_data="step_back"))
    if nav_row:
        kb.row(*nav_row)
    kb.row(InlineKeyboardButton(text="❌ Бекор қилиш", callback_data="cancel"))
    return kb.as_markup()


def skip_kb(can_go_back: bool = True):
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Файлсиз сақлаш", callback_data="skip_file")
    if can_go_back:
        kb.button(text="⬅️ Олдинги қадам", callback_data="step_back")
    kb.button(text="❌ Бекор қилиш", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def build_zip_period_cat_kb(start_ym: str, end_ym: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Барча категориялар (Жами)", callback_data=f"ziprun:{start_ym}:{end_ym}:all")
    kb.button(text="🔬 Scopus / WoS", callback_data=f"ziprun:{start_ym}:{end_ym}:scopus_wos")
    kb.button(text="🎓 DSc ва PhD диссертациялар", callback_data=f"ziprun:{start_ym}:{end_ym}:cat_dissertations")
    kb.button(text="💡 Патентлар", callback_data=f"ziprun:{start_ym}:{end_ym}:patent")
    kb.button(text="📚 Монографиялар", callback_data=f"ziprun:{start_ym}:{end_ym}:monography")
    kb.button(text="📄 ЎзОАК мақолалари", callback_data=f"ziprun:{start_ym}:{end_ym}:oak_uz")
    kb.button(text="🌍 Россия ва хорижий ОАК", callback_data=f"ziprun:{start_ym}:{end_ym}:oak_ru_if")
    kb.button(text="📑 Тезислар (Ўз / Хор)", callback_data=f"ziprun:{start_ym}:{end_ym}:theses")
    kb.button(text="⚙️ Рац.таклиф ва амалиёт", callback_data=f"ziprun:{start_ym}:{end_ym}:practical")
    kb.button(text="💼 Шартнома ва грантлар", callback_data=f"ziprun:{start_ym}:{end_ym}:grants_contracts")
    kb.button(text="🏛 Кафедра анжуманлари", callback_data=f"ziprun:{start_ym}:{end_ym}:conferences")
    kb.button(text="🔙 Даврни ўзгартириш", callback_data="zipmenu_period")
    kb.adjust(1, 2, 2, 2, 2, 2, 1)
    return kb.as_markup()


def post_save_kb(entry_id: int):
    """Удобные кнопки сразу после сохранения отчёта"""
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 Қўшимча файл бириктириш", callback_data=f"addfile_{entry_id}")
    kb.button(text="✏️ Таҳрирлаш", callback_data=f"edit_menu_{entry_id}")
    kb.button(text="➕ Яна ҳисобот қўшиш", callback_data="new_report_now")
    kb.adjust(2, 1)
    return kb.as_markup()


def edit_entry_menu_kb(entry_id: int, has_file: bool = False):
    """Меню точечного редактирования любого поля записи без повторного ввода всего отчёта"""
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Иш номини ўзгартириш", callback_data=f"efield:{entry_id}:title")
    kb.button(text="👥 Муаллифларни ўзгартириш", callback_data=f"efield:{entry_id}:authors")
    kb.button(text="📅 Сана / Нашрни ўзгартириш", callback_data=f"efield:{entry_id}:pub_date")
    kb.button(text="📰 Журнал / Буюртмачини ўзгартириш", callback_data=f"efield:{entry_id}:journal_name")
    kb.button(text="🌍 Давлат / Патент турини ўзгартириш", callback_data=f"efield:{entry_id}:country")
    kb.button(text="🔗 URL / DOI ҳаволани ўзгартириш", callback_data=f"efield:{entry_id}:url")
    kb.button(text="📎 Асосий файлни юклаш/алмаштириш", callback_data=f"ereplacefile:{entry_id}")
    kb.button(text="➕ Қўшимча файл қўшиш (2-ҳужжат)", callback_data=f"addfile_{entry_id}")
    kb.button(text="🔄 Барча майдонларни бошидан киритиш", callback_data=f"edit_full_{entry_id}")
    kb.button(text="❌ Ёпиш", callback_data="cancel")
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


# ─── GOOGLE SHEETS SYNC ─────────────────────────────────────────────────────
async def sync_to_sheets(entry: dict):
    if not SHEET_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                SHEET_WEBHOOK_URL,
                json=entry,
                timeout=aiohttp.ClientTimeout(total=10)
            )
        logger.info(f"Synced entry to Google Sheets: dept={entry.get('dept_id')}")
    except Exception as e:
        logger.warning(f"Google Sheets sync failed: {e}")


# ─── AUDIT CHANNEL NOTIFICATIONS ────────────────────────────────────────────
async def send_to_audit_channel(dept_id: int, dept_name: str, head_name: str,
                                category_label: str, title: str, authors: str,
                                file_path: str, file_id: str, entry_id: int,
                                extra_lines: str = ""):
    """Мгновенно публикует новый отчёт и файл в закрытый канал руководства"""
    if not AUDIT_CHANNEL_ID:
        return
    try:
        now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
        caption = (
            f"🔔 <b>ЯНГИ ИЛМИЙ ҲИСОБОТ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра #{dept_id}:</b> {html.escape(dept_name or '')}\n"
            f"👤 <b>Мудир:</b> {html.escape(head_name or '—')}\n"
            f"📌 <b>Категория:</b> {html.escape(category_label or '')}\n"
            f"📝 <b>Иш номи:</b> {html.escape(title or '—')[:400]}\n"
            f"👥 <b>Муаллифлар:</b> {html.escape(authors or '—')[:250]}"
            f"{extra_lines}\n"
            f"📅 <b>Вақт:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        if file_id:
            try:
                await bot.send_document(AUDIT_CHANNEL_ID, file_id, caption=caption, parse_mode="HTML")
            except Exception:
                try:
                    await bot.send_photo(AUDIT_CHANNEL_ID, file_id, caption=caption, parse_mode="HTML")
                except Exception:
                    await bot.send_message(AUDIT_CHANNEL_ID, caption, parse_mode="HTML")
        else:
            await bot.send_message(AUDIT_CHANNEL_ID, caption, parse_mode="HTML")
        logger.info(f"Published audit report #{entry_id} to channel {AUDIT_CHANNEL_ID}")
    except Exception as e:
        logger.warning(f"Failed to post to audit channel: {e}")


async def notify_delete_to_audit(dept_name: str, entry_id: int):
    if not AUDIT_CHANNEL_ID:
        return
    try:
        text = (
            f"🗑 <b>ИЛМИЙ ҲИСОБОТ ЎЧИРИЛДИ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра:</b> {html.escape(dept_name or '')}\n"
            f"⚠️ <i>Ушбу ёзув масъул ходим томонидан базадан ўчирилди.</i>"
        )
        await bot.send_message(AUDIT_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to notify delete to channel: {e}")


async def notify_edit_to_audit(dept_name: str, entry_id: int, info_str: str):
    if not AUDIT_CHANNEL_ID:
        return
    try:
        now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
        text = (
            f"✏️ <b>ИЛМИЙ ҲИСОБОТ ТАҲРИРЛАНДИ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра:</b> {html.escape(dept_name or '')}\n"
            f"📝 <b>Ўзгариш:</b> {html.escape(info_str)[:400]}\n"
            f"📅 <b>Вақт:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(AUDIT_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to notify edit to channel: {e}")


# ─── DATABASE BACKUP & AUTO-RESTORE VIA AUDIT CHANNEL ───────────────────────
async def backup_database_to_channel(reason: str = "автоматик"):
    """Назорат каналига маълумотлар базасининг тўлиқ CSV бэкапини жўнатади"""
    global _db_dirty_since_backup
    if not AUDIT_CHANNEL_ID:
        return
    try:
        now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        now_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        rows = await get_all_detailed_entries()
        buf = io.StringIO()
        writer = csv.writer(buf)
        if rows:
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(r.values())
        csv_bytes = buf.getvalue().encode("utf-8")
        await bot.send_document(
            AUDIT_CHANNEL_ID,
            types.BufferedInputFile(csv_bytes, filename=f"adti_pg_backup_{now_tag}.csv"),
            caption=f"💾 <b>#PG_BACKUP — АДТИ Маълумотлар базаси бэкапи</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📅 <b>Вақт:</b> {now_str}\n"
                    f"📌 <b>Ҳолат:</b> {html.escape(reason)}\n"
                    f"📊 <b>Жами ёзувлар:</b> {len(rows)} та\n"
                    f"🛡 <i>Барча 65 кафедра маълумотлари хавфсиз сақланди.</i>",
            parse_mode="HTML"
        )
        _db_dirty_since_backup = False
        logger.info(f"Database backup sent to channel: reason={reason} ({len(rows)} rows)")
    except Exception as e:
        logger.warning(f"Failed to send database backup to channel: {e}")


def schedule_debounced_backup(reason: str):
    """Планирует облачный бэкап в канал через 60 секунд после изменения базы"""
    global _db_dirty_since_backup, _delayed_backup_task
    _db_dirty_since_backup = True

    async def _run_later():
        await asyncio.sleep(60)
        if _db_dirty_since_backup:
            await backup_database_to_channel(reason=reason)

    if _delayed_backup_task and not _delayed_backup_task.done():
        _delayed_backup_task.cancel()
    _delayed_backup_task = asyncio.create_task(_run_later())


async def periodic_backup_loop():
    """Ҳар 6 соатда (фақат янги ўзгаришлар бўлган бўлса) базанинг бэкапини юборади"""
    while True:
        await asyncio.sleep(21600)
        if _db_dirty_since_backup:
            await backup_database_to_channel(reason="Режали авто-бэкап")


async def auto_sync_latest_backup_from_channel():
    """
    При старте контейнера (например, на Render) проверяет последние сообщения в AUDIT_CHANNEL_ID.
    Если там есть более свежий #PG_BACKUP CSV, автоматически дозагружает новые записи!
    """
    if not AUDIT_CHANNEL_ID:
        return
    try:
        low, high = 4000, 8000
        while True:
            try:
                await bot.edit_message_reply_markup(chat_id=AUDIT_CHANNEL_ID, message_id=high, reply_markup=None)
                low = high
                high *= 2
            except Exception as e:
                s = str(e).lower()
                if "not found" in s or "message_id_invalid" in s:
                    break
                low = high
                high *= 2

        while low + 1 < high:
            mid = (low + high) // 2
            found = False
            for test_id in (mid, mid - 1, mid + 1):
                try:
                    await bot.edit_message_reply_markup(chat_id=AUDIT_CHANNEL_ID, message_id=test_id, reply_markup=None)
                    found = True
                    break
                except Exception as e:
                    s = str(e).lower()
                    if "not found" not in s and "message_id_invalid" not in s:
                        found = True
                        break
            if found:
                low = mid
            else:
                high = mid

        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡", callback_data="noop")]])
        for m_id in range(low + 2, max(1, low - 15), -1):
            try:
                msg = await bot.edit_message_reply_markup(chat_id=AUDIT_CHANNEL_ID, message_id=m_id, reply_markup=kb)
                await bot.edit_message_reply_markup(chat_id=AUDIT_CHANNEL_ID, message_id=m_id, reply_markup=None)
                if msg and msg.document and msg.document.file_name and "backup" in msg.document.file_name and msg.document.file_name.endswith(".csv"):
                    f_info = await bot.get_file(msg.document.file_id)
                    f_stream = await bot.download_file(f_info.file_path)
                    csv_bytes = f_stream.read() if hasattr(f_stream, "read") else f_stream.getvalue()
                    res = await restore_from_csv_bytes(csv_bytes, only_newer=True)
                    logger.info(f"Auto-synced from channel backup {msg.document.file_name}: +{res['imported']} newer entries (total={res['total']})")
                    break
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Auto-sync from channel skipped: {e}")


# ─── ADMIN: СКАЧАТЬ БЭКАП БАЗЫ ДАННЫХ (.CSV + .DB) ──────────────────────────
@dp.message(Command("backup_db"))
async def cmd_backup_db(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ <b>Маълумотлар базаси бэкапи тайёрланмоқда...</b>", parse_mode="HTML")
    try:
        rows = await get_all_detailed_entries()
        buf = io.StringIO()
        writer = csv.writer(buf)
        if rows:
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(r.values())
        csv_bytes = buf.getvalue().encode("utf-8")
        now_str = datetime.datetime.now().strftime("%Y_%m_%d_%H%M")

        await bot.send_document(
            message.chat.id,
            types.BufferedInputFile(csv_bytes, filename=f"adti_database_{now_str}.csv"),
            caption=f"💾 <b>АДТИ: Тўлиқ база бэкапи (.csv)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Жами ёзувлар:</b> {len(rows)} та\n"
                    f"🛡 <i>Барча 65 та кафедра ҳисоботлари, файл дескрипторлари ва маълумотлари.</i>",
            parse_mode="HTML"
        )
        if DB_PATH.exists():
            await bot.send_document(
                message.chat.id,
                types.BufferedInputFile(DB_PATH.read_bytes(), filename=f"adti_database_{now_str}.db"),
                caption="🗄 <b>SQLite (.db) тўлиқ база файли</b> (кафедралар, фойдаланувчилар ва ҳисоботлар билан)",
                parse_mode="HTML"
            )
    except Exception as e:
        await message.answer(f"❌ Бэкап яратишда хатолик: {e}")


# ─── ADMIN: ВОССТАНОВИТЬ БАЗУ ДАННЫХ (.DB ИЛИ .CSV) ─────────────────────────
@dp.message(Command("restore_db"))
async def cmd_restore_db_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚠️ <b>ДИҚҚАТ: Базани қайта тиклаш (Restore)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Илтимос, тикламоқчи бўлган <b>.db</b> ёки <b>.csv</b> бэкап файлингизни юборинг (документ сифатида).\n\n"
        "Ёки бекор қилиш учун /cancel деб ёзинг.",
        parse_mode="HTML"
    )
    await state.set_state(RestoreState.waiting_for_db_file)


@dp.message(RestoreState.waiting_for_db_file, F.document)
async def process_restore_db(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    doc = message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".db") or fname.endswith(".csv")):
        await message.answer("❌ Фақат <b>.db</b> ёки <b>.csv</b> кенгайтмали бэкап файл юборинг!", parse_mode="HTML")
        return

    try:
        tg_file = await bot.get_file(doc.file_id)
        f_stream = await bot.download_file(tg_file.file_path)
        content = f_stream.read() if hasattr(f_stream, 'read') else f_stream.getvalue()

        if fname.endswith(".csv"):
            res = await restore_from_csv_bytes(content, only_newer=False)
            await state.clear()
            await message.answer(
                f"✅ <b>CSV бэкапдан база муваффақиятли тикланди!</b>\n"
                f"📊 Юкланган ёзувлар: <b>{res['total']} та</b>",
                parse_mode="HTML"
            )
            return

        if DB_PATH.exists():
            shutil.copyfile(DB_PATH, str(DB_PATH) + ".bak")

        with open(DB_PATH, "wb") as f:
            f.write(content)

        await init_db()
        await state.clear()
        hc = get_db_health_cache()
        await message.answer(
            f"✅ <b>База (.db) муваффақиятли қайта тикланди!</b>\n"
            f"📊 Жами ёзувлар: <b>{hc['indicators']} та</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Базани тиклашда хатолик: {e}")


# ─── /start ─────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if dept:
        await message.answer(
            f"🏛 <b>АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>Илмий бўлим ҳисобот тизими — 2026</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра:</b> {dept['name']}\n"
            f"👤 <b>Мудир:</b> {dept.get('head_name') or '—'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Керакли бўлимни танланг 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    elif user_id in ADMIN_IDS:
        hc = get_db_health_cache()
        await message.answer(
            f"👑 <b>Ассалому алайкум, Ҳурматли Администратор!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>АДТИ Илмий ҳисоботлар бошқарув панели</b>\n"
            f"📊 <b>Базадаги жами ишлар:</b> {hc['indicators']} та\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Сиз барча 65 та кафедра ҳисоботларини кўришингиз, Excel/Word ва ZIP файлларни юклаб олишингиз ҳамда паролларни бошқаришингиз мумкин.\n\n"
            f"Керакли амални танланг 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🔒 <b>Ассалому алайкум! АДТИ Илмий бўлим тизими</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Кафедрангизга кириш учун берилган <b>МАХСУС ПАРОЛни (кодни)</b> киритинг:\n\n"
            "<i>(Масалан: ADTI-01-XXXX)</i>",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        await state.set_state(AuthState.waiting_for_code)


# ─── CANCEL & MENU INTERCEPTOR (БЕЗОПАСНЫЙ ПЕРЕХВАТ КНОПОК МЕНЮ) ────────────
CANCEL_WORDS = {
    "❌ бекор қилиш", "бекор қилиш", "бекор килиш", "bekor qilish",
    "отмена", "отменить", "назад", "/cancel", "cancel", "стоп", "stop"
}


def is_cancel_text(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower() in CANCEL_WORDS


@dp.message(Command("cancel"))
@dp.message(F.text.lower().in_(CANCEL_WORDS))
async def global_cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ <b>Амал бекор қилинди.</b>\n\nАсосий меню 👇",
        reply_markup=main_kb(message.from_user.id),
        parse_mode="HTML"
    )


async def check_menu_or_cancel(message: types.Message, state: FSMContext) -> bool:
    """
    Перехватывает нажатия любых кнопок главного меню или команд во время ввода данных,
    чтобы названия кнопок НИКОГДА не записывались в базу как название статьи или ФИО автора.
    """
    text = (message.text or "").strip()
    if not text:
        return False

    if is_cancel_text(text):
        await state.clear()
        await message.answer(
            "❌ <b>Амал бекор қилинди.</b>\n\nАсосий меню 👇",
            reply_markup=main_kb(message.from_user.id),
            parse_mode="HTML"
        )
        return True

    if text.startswith("/start"):
        await state.clear()
        await cmd_start(message, state)
        return True

    if text.startswith("/backup_db"):
        await state.clear()
        await cmd_backup_db(message)
        return True

    if text.startswith("/restore_db"):
        await state.clear()
        await cmd_restore_db_start(message, state)
        return True

    if text in ["🚪 Кафедрадан чиқиш", "🚪 Чиқиш"]:
        await logout_dept(message, state)
        return True

    if text in ["➕ Ҳисобот қўшиш", "📁 Ҳисобот қўшиш"]:
        await add_report_start(message, state)
        return True

    if text in ["📊 Кафедра статистикаси", "📊 Кафедрам статистикаси"]:
        await state.clear()
        await my_stats(message, state)
        return True

    if text in ["📋 Юборилган ишлар", "📋 Юборилган ишлар (Ўчириш)"]:
        await state.clear()
        await list_dept_submissions(message)
        return True

    if text in ["🗂 Файллар базаси", "🗂 Файлларни кўриш (категория)"]:
        await state.clear()
        await browse_by_category_start(message)
        return True

    if text in ["🏛 Сводный ҳисобот", "🏛 Сводный ҳисобот (65 кафедра)"]:
        await state.clear()
        await admin_summary(message)
        return True

    if text in ["📊 Excel ҳисобот (.xlsx)", "📊 Excel ҳисобот юклаш (.xlsx)"]:
        await state.clear()
        await download_excel_report(message)
        return True

    if text in ["📥 Word ҳисобот (.docx)", "📥 Word ҳисобот юклаш"]:
        await state.clear()
        await download_report(message)
        return True

    if text == "📅 Давр бўйича ҳисобот (Йил-Ой)":
        await state.clear()
        await prompt_period_report(message)
        return True

    if text in ["📦 Файллар ZIP архиви", "📦 Файллар ZIP архиви (.zip)"]:
        await state.clear()
        await prompt_zip_download(message)
        return True

    if text in ["🔑 Кафедралар пароллари", "🔑 Кафедралар пароллари (Word)"]:
        await state.clear()
        await prompt_admin_pin_for_passwords(message, state)
        return True

    if text in ["🤖 AI Сводный таҳлил", "🤖 AI Институт таҳлили", "/ai_summary"]:
        await state.clear()
        await ai_full_analysis_handler(message)
        return True

    if text in ["🤖 AI Илмий таҳлил", "🤖 AI Кафедра таҳлили", "/ai_dept"]:
        await state.clear()
        await ai_dept_analysis_handler(message, state)
        return True

    if text == "📋 Google Таблица янгилаш":
        await state.clear()
        await force_sync_sheets(message)
        return True

    return False


# ─── АВТОРИЗАЦИЯ ПО КОДУ ────────────────────────────────────────────────────
@dp.message(AuthState.waiting_for_code, F.text)
async def process_dept_code(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    code_input = message.text.strip()
    dept = await get_dept_by_code(code_input)

    if not dept:
        await message.answer(
            "❌ <b>Нотўғри парол!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Илтимос, кодни қайта текшириб тўғри киритинг (Масалан: <code>ADTI-01-XXXX</code>) ёки Илмий бўлимга мурожаат қилинг.",
            parse_mode="HTML"
        )
        return

    await bind_user_to_dept(message.from_user.id, dept['id'])
    await state.clear()

    await message.answer(
        f"✅ <b>Кафедра тасдиқланди!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Кафедра:</b> {dept['name']}\n"
        f"👤 <b>Мудир:</b> {dept.get('head_name') or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Энди ҳисоботларни юборишингиз мумкин 👇",
        reply_markup=main_kb(message.from_user.id),
        parse_mode="HTML"
    )


# ─── ВЫХОД ИЗ КАФЕДРЫ ───────────────────────────────────────────────────────
@dp.message(F.text.in_(["🚪 Кафедрадан чиқиш", "🚪 Чиқиш"]))
async def logout_dept(message: types.Message, state: FSMContext):
    await unbind_user(message.from_user.id)
    await state.clear()
    await message.answer(
        "🔓 <b>Сиз кафедрадан чиқдингиз.</b>\n\n"
        "Бошқа кафедрага кириш учун махсус код-паролни киритинг:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )
    await state.set_state(AuthState.waiting_for_code)


# ─── ПОШАГОВЫЙ МАСТЕР ВВОДА ОТЧЁТА С КНОПКОЙ «НАЗАД» И БЫСТРЫМ ВЫБОРОМ ─────
def get_total_steps_for_cat(cat: str) -> int:
    # 1: title, 2: authors, + extra_steps, + 1: upload_file
    return 3 + len(CATEGORY_EXTRA_STEPS.get(cat, []))


def get_quick_options_for_step(cat: str, state_name: str) -> list[tuple[str, str]] | None:
    if cat == "patent" and state_name == "enter_country":
        return [
            ("💡 IAP (Ихтиро)", "IAP"),
            ("⚙️ FAP (Фойдали модель)", "FAP"),
            ("💻 DGU (ЭҲМ дастури)", "DGU"),
            ("🗄 BGU (Маълумотлар базаси)", "BGU"),
        ]
    if cat == "conferences" and state_name == "enter_country":
        return [
            ("🏛 Офлайн (Анъанавий)", "Офлайн"),
            ("🌐 Онлайн", "Онлайн"),
            ("🔄 Гибрид (Офлайн+Онлайн)", "Гибрид"),
        ]
    if cat == "conferences" and state_name == "enter_journal":
        return [
            ("🇺🇿 Республика", "Республика"),
            ("🌍 Халқаро", "Халқаро"),
            ("🏢 Маҳаллий (Институт)", "Маҳаллий"),
        ]
    return None


async def prompt_current_step(target_msg: types.Message, state: FSMContext, edit_message: bool = False):
    """Отображает текущий шаг мастера добавления/редактирования с точным номером шага и кнопками"""
    data = await state.get_data()
    cat = data.get("category", "")
    label = INDICATOR_LABELS.get(cat, cat)
    total_steps = get_total_steps_for_cat(cat)
    curr_state = await state.get_state()

    text = ""
    kb = None

    if curr_state == AddEntry.enter_title.state:
        step_hdr = f"✅ <b>{label}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        if cat == "conferences":
            text = f"{step_hdr}📝 <b>[1/{total_steps}-қадам] Анжуман мавзусини киритинг:</b>\n<i>(Масалан: «Тиббиётда сунъий интеллект» мавзусидаги илмий-амалий анжуман)</i>"
        elif cat == "patent":
            text = f"{step_hdr}📝 <b>[1/{total_steps}-қадам] Патент / Дастур номини (мавзусини) киритинг:</b>\n<i>(Масалан: Юрак касалликларини эрта аниқлаш дастури)</i>"
        elif cat in ("contracts", "grants"):
            text = f"{step_hdr}📝 <b>[1/{total_steps}-қадам] Лойиҳа ёки шартнома мавзусини киритинг:</b>"
        else:
            text = f"{step_hdr}📝 <b>[1/{total_steps}-қадам] Иш номини (мавзусини) киритинг:</b>\n<i>(Мақола, монография ёки диссертациянинг тўлиқ номи)</i>"
        kb = step_nav_kb(can_go_back=not bool(data.get("editing_entry_id")), can_skip=False)

    elif curr_state == AddEntry.enter_authors.state:
        if cat == "conferences":
            text = (
                f"👤 <b>[2/{total_steps}-қадам] Масъул шахс (Ф.И.Ш. ёки ташкилотчи):</b>\n"
                f"<i>(Масалан: Кафедра мудири Тошматов А.А.)</i>"
            )
        else:
            text = (
                f"👥 <b>[2/{total_steps}-қадам] Муаллиф(лар)нинг Ф.И.Ш.ни киритинг:</b>\n"
                f"<i>(Масалан: Узбекова Нелли Рафиковна, Хужамбердиев М.)</i>"
            )
        kb = step_nav_kb(can_go_back=True, can_skip=False)

    elif curr_state == AddEntry.upload_file.state:
        step_num = total_steps
        if cat == "conferences":
            text = (
                f"📎 <b>[{step_num}/{total_steps}-қадам] Асос (Тасдиқловчи ҳужжат):</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Анжуман дастури, буйруқ, сертификат ёки баённомани юборинг:\n"
                "• <b>PDF</b> ёки <b>Word (.docx)</b> файли (бир ёки бир нечта файл юбориш мумкин)\n"
                "• Ёки <b>Фото скан / расмлар</b> (бир нечта расм юборсангиз, бот уларни автоматик ягона PDF ҳужжатга бирлаштиради!)\n\n"
                "<i>Файл бўлмаса, «⏭ Файлсиз сақлаш» тугмасини босинг:</i>"
            )
        else:
            text = (
                f"📎 <b>[{step_num}/{total_steps}-қадам] Тасдиқловчи ҳужжатни юборинг:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Илтимос, тасдиқловчи ҳужжатни бириктиринг:\n"
                "• <b>PDF</b> ёки <b>Word (.docx)</b> файли (20 МБ гача, бир нечта файл юбориш ҳам мумкин)\n"
                "• Ёки <b>Фото скан / расмлар</b> (бир нечта расмни альбом қилиб юборсангиз, автоматик PDF қилинади!)\n\n"
                "<i>Файл бўлмаса, «⏭ Файлсиз сақлаш» тугмасини босинг:</i>"
            )
        kb = skip_kb(can_go_back=True)

    else:
        # Один из дополнительных шагов CATEGORY_EXTRA_STEPS
        steps = CATEGORY_EXTRA_STEPS.get(cat, [])
        idx = data.get("extra_step_index", 1) - 1
        idx = max(0, min(idx, len(steps) - 1))
        state_name, raw_prompt = steps[idx]
        step_num = 3 + idx
        # Убираем старую нумерацию [X/Y-қадам] из шаблона, если она там была, и ставим точную
        clean_prompt = re.sub(r'\[[^\]]*-қадам\]\s*', '', raw_prompt)
        # Вставляем точный номер шага после первого эмодзи/тега <b>
        if "<b>" in clean_prompt:
            clean_prompt = clean_prompt.replace("<b>", f"<b>[{step_num}/{total_steps}-қадам] ", 1)
        else:
            clean_prompt = f"<b>[{step_num}/{total_steps}-қадам]</b> {clean_prompt}"

        can_skip = state_name in ("enter_url", "enter_reg_number")
        q_opts = get_quick_options_for_step(cat, state_name)
        text = clean_prompt
        kb = step_nav_kb(can_go_back=True, can_skip=can_skip, quick_options=q_opts)

    if edit_message:
        try:
            await target_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await target_msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ─── ДОБАВЛЕНИЕ ЗАПИСИ: СТАРТ ───────────────────────────────────────────────
@dp.message(F.text.in_(["➕ Ҳисобот қўшиш", "📁 Ҳисобот қўшиш"]))
async def add_report_start(message: types.Message, state: FSMContext, user_id_override: int = None):
    uid = user_id_override or message.from_user.id
    dept = await get_dept_by_user(uid)
    if not dept:
        await message.answer(
            "🔒 <b>Ҳисобот киритиш учун аввал кафедра паролини киритинг:</b>\n\n"
            "<i>(Масалан: ADTI-15-9CE2)</i>",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        await state.set_state(AuthState.waiting_for_code)
        return

    dept_id = dept['id']
    dept_name = dept['name']
    head_name = dept.get('head_name') or ''

    await state.clear()
    await state.update_data(dept_id=dept_id, dept_name=dept_name, head_name=head_name)
    await message.answer(
        f"📂 <b>{dept_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Қайси йўналиш бўйича ҳисобот киритмоқчисиз?\n"
        f"Категорияни танланг 👇",
        reply_markup=categories_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.choose_category)


@dp.callback_query(F.data == "new_report_now")
async def cb_new_report_now(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await add_report_start(cb.message, state, user_id_override=cb.from_user.id)


@dp.callback_query(F.data.startswith("cat_"), AddEntry.choose_category)
async def choose_category(cb: types.CallbackQuery, state: FSMContext):
    cat = cb.data[4:]
    await state.update_data(category=cat, extra_step_index=0)
    await state.set_state(AddEntry.enter_title)
    await prompt_current_step(cb.message, state, edit_message=True)
    await cb.answer()


# ─── КНОПКА «⬅️ ОЛДИНГИ ҚАДАМ» (ШАГ НАЗАД) ─────────────────────────────────
@dp.callback_query(F.data == "step_back")
async def handle_step_back(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cat = data.get("category", "")
    steps = CATEGORY_EXTRA_STEPS.get(cat, [])
    curr_state = await state.get_state()

    if curr_state == AddEntry.enter_title.state:
        await state.set_state(AddEntry.choose_category)
        dept_name = data.get("dept_name", "")
        await cb.message.edit_text(
            f"📂 <b>{dept_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Қайси йўналиш бўйича ҳисобот киритмоқчисиз?\n"
            f"Категорияни танланг 👇",
            reply_markup=categories_kb(),
            parse_mode="HTML"
        )
        await cb.answer("Категория танлашга қайтилди")
        return

    if curr_state == AddEntry.enter_authors.state:
        await state.set_state(AddEntry.enter_title)
        await prompt_current_step(cb.message, state, edit_message=True)
        await cb.answer("Олдинги қадам")
        return

    if curr_state == AddEntry.upload_file.state:
        if steps:
            last_idx = len(steps)
            await state.update_data(extra_step_index=last_idx)
            target_state = getattr(AddEntry, steps[last_idx - 1][0])
            await state.set_state(target_state)
        else:
            await state.set_state(AddEntry.enter_authors)
        await prompt_current_step(cb.message, state, edit_message=True)
        await cb.answer("Олдинги қадам")
        return

    # Если мы на одном из доп-шагов
    idx = data.get("extra_step_index", 1)
    if idx <= 1:
        await state.update_data(extra_step_index=0)
        await state.set_state(AddEntry.enter_authors)
    else:
        prev_idx = idx - 1
        await state.update_data(extra_step_index=prev_idx)
        target_state = getattr(AddEntry, steps[prev_idx - 1][0])
        await state.set_state(target_state)

    await prompt_current_step(cb.message, state, edit_message=True)
    await cb.answer("Олдинги қадам")


# ─── БЫСТРЫЕ КНОПКИ ВЫБОРА (qopt:) И ПРОПУСК НЕОБЯЗАТЕЛЬНОГО ПОЛЯ ──────────
@dp.callback_query(F.data.startswith("qopt:"))
async def handle_quick_option_cb(cb: types.CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    curr_state = await state.get_state()
    if not curr_state:
        await cb.answer()
        return
    state_short = curr_state.split(":")[-1]
    field_key = STATE_FIELD_MAP.get(state_short)
    if field_key:
        await state.update_data(**{field_key: val})
    await cb.answer(f"Танланди: {val}")
    await go_to_next_extra_step(cb.message, state, edit_message=True)


@dp.callback_query(F.data == "skip_extra_step")
async def handle_skip_extra_step_cb(cb: types.CallbackQuery, state: FSMContext):
    curr_state = await state.get_state()
    if curr_state:
        state_short = curr_state.split(":")[-1]
        field_key = STATE_FIELD_MAP.get(state_short)
        if field_key:
            await state.update_data(**{field_key: ""})
    await cb.answer("Ўтказиб юборилди")
    await go_to_next_extra_step(cb.message, state, edit_message=True)


# ─── ШАГ 1 — НАЗВАНИЕ РАБОТЫ ─────────────────────────────────────────────────
@dp.message(AddEntry.enter_title, F.text)
async def enter_title(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    data = await state.get_data()
    cat = data.get('category', '')
    ok, cleaned_title, err_msg = validate_and_clean_title(message.text, category=cat)
    if not ok:
        await message.answer(err_msg, parse_mode="HTML")
        return

    await state.update_data(title=cleaned_title)
    await state.set_state(AddEntry.enter_authors)
    await prompt_current_step(message, state)


# ─── ШАГ 2 — АВТОРЫ ──────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_authors, F.text)
async def enter_authors(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    ok, cleaned_authors, err_msg = validate_and_clean_authors(message.text)
    if not ok:
        await message.answer(err_msg, parse_mode="HTML")
        return

    await state.update_data(authors=cleaned_authors, extra_step_index=0)
    await go_to_next_extra_step(message, state)


# ─── HELPER: перейти к следующему доп-шагу для категории ────────────────────
async def go_to_next_extra_step(message: types.Message, state: FSMContext, edit_message: bool = False):
    data = await state.get_data()
    cat = data.get('category', '')
    steps = CATEGORY_EXTRA_STEPS.get(cat, [])
    step_index = data.get('extra_step_index', 0)

    if step_index < len(steps):
        state_name, _ = steps[step_index]
        await state.update_data(extra_step_index=step_index + 1)
        target_state = getattr(AddEntry, state_name)
        await state.set_state(target_state)
        await prompt_current_step(message, state, edit_message=edit_message)
    else:
        await state.set_state(AddEntry.upload_file)
        await prompt_current_step(message, state, edit_message=edit_message)


# ─── УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДОП-ШАГОВ С ВАЛИДАЦИЕЙ ────────────────────────
async def handle_extra_step(message: types.Message, state: FSMContext, field_key: str):
    if await check_menu_or_cancel(message, state):
        return

    raw_val = message.text.strip()
    data = await state.get_data()

    if field_key == "authors_count":
        ok, cleaned_val, err_msg = validate_and_clean_count(raw_val, data.get("authors", ""))
        if not ok:
            await message.answer(err_msg, parse_mode="HTML")
            return
    elif field_key == "pub_date":
        ok, cleaned_val, err_msg = validate_and_clean_pub_date(raw_val)
        if not ok:
            await message.answer(err_msg, parse_mode="HTML")
            return
    elif field_key == "amount":
        ok, cleaned_val, err_msg = validate_and_clean_amount(raw_val)
        if not ok:
            await message.answer(err_msg, parse_mode="HTML")
            return
    elif field_key == "url":
        cleaned_val = clean_single_line(raw_val)
        if cleaned_val.lower() in ("-", "—", ".", "йўқ", "yo'q", "нет", "йук", "yoq"):
            cleaned_val = ""
    else:
        cleaned_val = clean_single_line(raw_val)
        if not cleaned_val:
            await message.answer("⚠️ Илтимос, маълумотни киритинг.", parse_mode="HTML")
            return

    await state.update_data(**{field_key: cleaned_val})
    await go_to_next_extra_step(message, state)


@dp.message(AddEntry.enter_country, F.text)
async def step_country(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'country')


@dp.message(AddEntry.enter_journal, F.text)
async def step_journal(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'journal_name')


@dp.message(AddEntry.enter_pub_date, F.text)
async def step_pub_date(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'pub_date')


@dp.message(AddEntry.enter_url, F.text)
async def step_url(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'url')


@dp.message(AddEntry.enter_authors_count, F.text)
async def step_authors_count(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'authors_count')


@dp.message(AddEntry.enter_specialty, F.text)
async def step_specialty(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'specialty')


@dp.message(AddEntry.enter_reg_number, F.text)
async def step_reg_number(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'reg_number')


@dp.message(AddEntry.enter_publisher, F.text)
async def step_publisher(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'publisher')


@dp.message(AddEntry.enter_venue, F.text)
async def step_venue(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'pub_date')


@dp.message(AddEntry.enter_amount, F.text)
async def step_amount(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'amount')


# ─── FALLBACK: Агар фойдаланувчи файлни эрта юборса ──────────────────────────
@dp.message(StateFilter(
    AddEntry.choose_category,
    AddEntry.enter_title,
    AddEntry.enter_authors,
    AddEntry.enter_country,
    AddEntry.enter_journal,
    AddEntry.enter_pub_date,
    AddEntry.enter_url,
    AddEntry.enter_authors_count,
    AddEntry.enter_specialty,
    AddEntry.enter_reg_number,
    AddEntry.enter_publisher,
    AddEntry.enter_venue,
    AddEntry.enter_amount,
), F.document | F.photo)
async def early_file_warning(message: types.Message):
    await message.answer(
        "⚠️ <b>Сиз файлни эрта юбордингиз!</b>\n\n"
        "Илтимос, аввал юқорида сўралган маълумотни <b>матн кўринишида</b> ёзиб юборинг.\n"
        "Тасдиқловчи ҳужжат файлини (PDF, Word ёки расм) эса охирги қадамда сўраймиз. 👇",
        parse_mode="HTML"
    )


# ─── HELPER: КОНВЕРТАЦИЯ НЕСКОЛЬКИХ ФОТО В ЕДИНЫЙ PDF ────────────────────────
def merge_photos_to_pdf_bytes(photo_paths: list[Path]) -> bytes | None:
    """Объединяет список путей к изображениям в один многостраничный PDF-документ"""
    try:
        from PIL import Image
        pil_images = []
        for p in photo_paths:
            if p.exists() and p.stat().st_size > 0:
                img = Image.open(p)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                pil_images.append(img)
        if not pil_images:
            return None
        pdf_buf = io.BytesIO()
        pil_images[0].save(
            pdf_buf,
            format="PDF",
            save_all=True,
            append_images=pil_images[1:],
            resolution=150.0
        )
        return pdf_buf.getvalue()
    except Exception as e:
        logger.warning(f"Failed to merge photos to PDF: {e}")
        return None


async def download_tg_message_file(msg: types.Message, dept_id: int) -> tuple[str, str]:
    """Скачивает документ или фото из сообщения в папку кафедры. Возвращает (file_id, local_dest_str)"""
    dept_dir = FILES_DIR / str(dept_id)
    dept_dir.mkdir(parents=True, exist_ok=True)

    if msg.document:
        f = msg.document
        file_id = f.file_id
        fname = f.file_name or f"doc_{file_id[:16]}.pdf"
        clean_name = re.sub(r'[^\w\.\-\_ ]', '_', fname)
        dest = dept_dir / clean_name
        dest_str = ""
        try:
            if not f.file_size or f.file_size < 20 * 1024 * 1024:
                file_obj = await bot.get_file(file_id)
                await bot.download_file(file_obj.file_path, destination=str(dest))
                dest_str = str(dest)
        except Exception as e_dl:
            logger.warning(f"Could not download doc locally: {e_dl}")
        return file_id, dest_str

    if msg.photo:
        f = msg.photo[-1]
        file_id = f.file_id
        fname = f"photo_{file_id[:20]}.jpg"
        dest = dept_dir / fname
        dest_str = ""
        try:
            file_obj = await bot.get_file(file_id)
            await bot.download_file(file_obj.file_path, destination=str(dest))
            dest_str = str(dest)
        except Exception as e_dl:
            logger.warning(f"Could not download photo locally: {e_dl}")
        return file_id, dest_str

    return "", ""


# ─── ШАГ ФИНАЛЬНЫЙ — ПРИЁМ ФАЙЛОВ И ФОТО (С ПАКЕТНОЙ БУФЕРИЗАЦИЕЙ БЕЗ ГОНОК) ─
@dp.message(AddEntry.upload_file, F.document | F.photo)
async def receive_file_or_album(message: types.Message, state: FSMContext):
    """
    Принимает как одиночный документ/фото, так и альбом из нескольких фото или пачку PDF.
    Ждёт 1.2 секунды для сбора всех файлов из одной пачки, чтобы НИКОГДА не создавать пустых дублей.
    """
    uid = message.from_user.id
    if uid not in _user_upload_buffers:
        _user_upload_buffers[uid] = []
    _user_upload_buffers[uid].append(message)

    # Если уже запущен таймер сбора пачки для этого пользователя — пусть он обработает все сообщения
    existing_task = _user_upload_tasks.get(uid)
    if existing_task and not existing_task.done():
        return

    async def _process_batch():
        # Если это медиагруппа (альбом) или несколько файлов подряд, ждём 1.2 сек
        await asyncio.sleep(1.2 if message.media_group_id else 0.6)
        batch_msgs = _user_upload_buffers.pop(uid, [])
        _user_upload_tasks.pop(uid, None)
        if not batch_msgs:
            return

        first_msg = batch_msgs[0]
        data = await state.get_data()
        # Проверяем, что состояние ещё не было очищено
        if not data.get("category") or not data.get("title"):
            return

        # Захватываем данные и сразу очищаем FSM-состояние, чтобы исключить любые повторные сохранения
        state_snapshot = dict(data)
        await state.clear()

        dept_id = state_snapshot.get("dept_id")
        if not dept_id:
            dept = await get_dept_by_user(uid)
            dept_id = dept["id"] if dept else 1

        wait_msg = await first_msg.answer(
            f"⏳ <b>Ҳужжат қабул қилинди ({len(batch_msgs)} та файл), текширилмоқда ва сақланмоқда...</b>",
            parse_mode="HTML"
        )

        downloaded_items = []
        photo_paths = []
        all_are_photos = all(bool(m.photo and not m.document) for m in batch_msgs)

        for m in batch_msgs:
            fid, fpath = await download_tg_message_file(m, dept_id)
            if fid:
                downloaded_items.append({"file_id": fid, "file_path": fpath})
                if fpath and m.photo:
                    photo_paths.append(Path(fpath))

        primary_file_id = downloaded_items[0]["file_id"] if downloaded_items else ""
        primary_file_path = downloaded_items[0]["file_path"] if downloaded_items else ""
        extra_files = downloaded_items[1:] if len(downloaded_items) > 1 else []

        # Если прислали 2+ фото (например, скан из нескольких страниц), объединяем их в единый PDF
        if all_are_photos and len(photo_paths) >= 2:
            pdf_bytes = merge_photos_to_pdf_bytes(photo_paths)
            if pdf_bytes:
                dept_dir = FILES_DIR / str(dept_id)
                merged_pdf_path = dept_dir / f"scan_merged_{int(datetime.datetime.now().timestamp())}.pdf"
                try:
                    merged_pdf_path.write_bytes(pdf_bytes)
                    primary_file_path = str(merged_pdf_path)
                except Exception:
                    pass

        try:
            await wait_msg.delete()
        except Exception:
            pass

        notes_str = json.dumps({"extra_files": extra_files}, ensure_ascii=False) if extra_files else ""
        await save_entry(
            first_msg,
            state,
            file_path=primary_file_path,
            file_id=primary_file_id,
            user_id=uid,
            data_override=state_snapshot,
            notes_override=notes_str
        )

    _user_upload_tasks[uid] = asyncio.create_task(_process_batch())


# ─── ПРОПУСТИТЬ ФАЙЛ ─────────────────────────────────────────────────────────
@dp.callback_query(F.data == "skip_file", AddEntry.upload_file)
async def skip_file(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("category") or not data.get("title"):
        await cb.answer("Амал аллақачон якунланган")
        return
    state_snapshot = dict(data)
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await save_entry(cb.message, state, file_path='', file_id='', user_id=cb.from_user.id, data_override=state_snapshot)
    await cb.answer()


# ─── FALLBACK: НОТЎҒРИ ФОРМАТ / МАТН ЮБОРИЛГАНДА ─────────────────────────────
@dp.message(AddEntry.upload_file)
async def fallback_upload_file(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    await message.answer(
        "⚠️ <b>Илтимос, тасдиқловчи ҳужжатни файл ёки фото кўринишида юборинг!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📎 Ҳужжатни Telegram скрепкаси 📎 орқали юборинг:\n"
        "• <b>Файл (Document):</b> PDF ёки Word (.docx, .doc)\n"
        "• <b>Фото (Photo):</b> Расм ёки ҳужжат скани\n\n"
        "<i>Агар ҳужжат ҳозир ёнингизда бўлмаса, «⏭ Файлсиз сақлаш» тугмасини босинг:</i>",
        reply_markup=skip_kb(can_go_back=True),
        parse_mode="HTML"
    )


# ─── ОТМЕНА ──────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "cancel")
async def cancel_action(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ <b>Амал бекор қилинди.</b>", parse_mode="HTML")
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await cb.message.answer("Асосий меню 👇", reply_markup=main_kb(cb.from_user.id), parse_mode="HTML")
    try:
        await cb.answer("Бекор қилинди")
    except Exception:
        pass


# ─── СОХРАНЕНИЕ ЗАПИСИ ──────────────────────────────────────────────────────
async def save_entry(message: types.Message, state: FSMContext,
                     file_path: str, file_id: str, user_id: int = None,
                     data_override: dict = None, notes_override: str = ""):
    if user_id is None:
        user_id = message.from_user.id

    data = data_override if data_override is not None else await state.get_data()
    await state.clear()

    cat = (data.get('category') or '').strip()
    title = (data.get('title') or '').strip()
    authors = (data.get('authors') or '').strip()

    # Жёсткая защита: никогда не сохранять пустую запись без категории или названия
    if not cat or not title:
        logger.warning(f"Prevented saving empty entry for user {user_id}: cat={cat!r}, title={title!r}")
        return

    dept_id = data.get('dept_id')
    dept_name = data.get('dept_name')
    head_name = data.get('head_name', '')

    if not dept_id:
        dept = await get_dept_by_user(user_id)
        if dept:
            dept_id = dept['id']
            dept_name = dept['name']
            head_name = dept.get('head_name') or ''
        else:
            await message.answer("⚠️ Кафедра аниқланмади. Илтимос, /start орқали қайта киринг.")
            return

    country        = data.get('country', '')
    journal_name   = data.get('journal_name', '')
    pub_date       = data.get('pub_date', '')
    url            = data.get('url', '')
    authors_count  = data.get('authors_count', '')
    specialty      = data.get('specialty', '')
    reg_number     = data.get('reg_number', '')
    publisher      = data.get('publisher', '')
    amount         = data.get('amount', '')

    # Автоматический подсчёт количества авторов, если поле не заполнено
    if not authors_count and authors and cat != "conferences":
        auth_parts = [a.strip() for a in authors.replace(";", ",").split(",") if a.strip()]
        authors_count = str(max(1, len(auth_parts)))

    # ── АВТОМАТИК ТЕКШИРУВ (Scopus / Web of Science / Халқаро базалар) ──
    doi_val = ""
    api_verify_line = ""
    if cat in ("scopus_wos", "oak_ru_if"):
        try:
            ver_res = await verify_article(title=title, journal=journal_name, authors=authors, url_or_doi=url)
            doi_val = ver_res.get('badge_excel', '')
            badge_tg = ver_res.get('badge', '')
            if badge_tg:
                api_verify_line = f"\n🔍 <b>Халқаро база (API):</b> {badge_tg}"
        except Exception as e:
            logger.warning(f"Auto-verification error: {e}")

    entry_data = dict(
        category=cat, title=title, authors=authors, year=2026,
        doi=doi_val,
        file_path=file_path, file_id=file_id,
        country=country, journal_name=journal_name, pub_date=pub_date, url=url,
        authors_count=authors_count, specialty=specialty, reg_number=reg_number,
        publisher=publisher, amount=amount,
    )

    editing_id = data.get('editing_entry_id')
    if editing_id:
        await update_entry_full(editing_id, entry_data)
        if notes_override:
            await update_entry_field(editing_id, "notes", notes_override)
        entry_id = editing_id
        is_edit = True
    else:
        entry_id = await add_entry(
            dept_id=dept_id, category=cat, title=title, authors=authors, year=2026,
            doi=doi_val,
            file_path=file_path, file_id=file_id, notes=notes_override,
            country=country, journal_name=journal_name, pub_date=pub_date, url=url,
            authors_count=authors_count, specialty=specialty, reg_number=reg_number,
            publisher=publisher, amount=amount,
        )
        is_edit = False

    asyncio.create_task(sync_to_sheets({
        "dept_id": dept_id,
        "dept_name": dept_name,
        "head_name": head_name,
        "category": cat,
        "category_label": INDICATOR_LABELS.get(cat, cat),
        "title": title,
        "authors": authors,
        "year": 2026,
        "has_file": bool(file_path or file_id),
        "file_name": Path(file_path).name if file_path else ("photo.jpg" if file_id.startswith("AgAC") else "document.pdf" if file_id else ""),
        "entry_id": entry_id,
        "country": country,
        "journal_name": journal_name,
        "pub_date": pub_date,
        "url": url,
        "authors_count": authors_count,
        "specialty": specialty,
        "reg_number": reg_number,
        "publisher": publisher,
        "amount": amount,
    }))

    extra_lines = ""
    if cat == "patent":
        if country:    extra_lines += f"\n📋 <b>Патент тури:</b> {html.escape(country)}"
        if reg_number: extra_lines += f"\n🔢 <b>Рег. рақами:</b> {html.escape(reg_number)}"
        if pub_date:   extra_lines += f"\n📅 <b>Олинган санаси:</b> {html.escape(pub_date)}"
    elif cat == "conferences":
        if country:       extra_lines += f"\n📋 <b>Шакли:</b> {html.escape(country)}"
        if journal_name:  extra_lines += f"\n🌐 <b>Кўлами:</b> {html.escape(journal_name)}"
        if pub_date:      extra_lines += f"\n📅 <b>Санаси:</b> {html.escape(pub_date)}"
        if authors_count: extra_lines += f"\n👥 <b>Қатнашувчилар сони:</b> {html.escape(authors_count)}"
    else:
        if country:       extra_lines += f"\n🌍 <b>Давлат:</b> {html.escape(country)}"
        if journal_name:  extra_lines += f"\n📰 <b>Журнал/Буюртмачи:</b> {html.escape(journal_name)}"
        if pub_date:      extra_lines += f"\n📅 <b>Сана/Нашр:</b> {html.escape(pub_date)}"
        if url:           extra_lines += f"\n🔗 <b>URL:</b> {html.escape(url)}"
        if specialty:     extra_lines += f"\n🎓 <b>Ихтисослик:</b> {html.escape(specialty)}"
        if reg_number:    extra_lines += f"\n🔢 <b>Рег. рақам:</b> {html.escape(reg_number)}"
        if publisher:     extra_lines += f"\n🏢 <b>Нашриёт:</b> {html.escape(publisher)}"
        if amount:        extra_lines += f"\n💰 <b>Суммаси:</b> {html.escape(amount)}"
        if authors_count: extra_lines += f"\n👥 <b>Муаллифлар сони:</b> {html.escape(authors_count)}"

    if api_verify_line:
        extra_lines += f"\n{api_verify_line.strip()}"

    asyncio.create_task(send_to_audit_channel(
        dept_id=dept_id,
        dept_name=dept_name,
        head_name=head_name,
        category_label=INDICATOR_LABELS.get(cat, cat),
        title=title,
        authors=authors,
        file_path=file_path,
        file_id=file_id,
        entry_id=entry_id,
        extra_lines=extra_lines,
    ))
    asyncio.create_task(sync_sheets_background())
    schedule_debounced_backup(reason=f"Янги ёзув #{entry_id} ({dept_name})")

    global _submission_count
    _submission_count += 1

    summary = await get_dept_summary(dept_id)
    label = INDICATOR_LABELS.get(cat, cat)
    total_cat = summary.get(cat, 0)

    extra_f_cnt = 0
    if notes_override:
        try:
            extra_f_cnt = len(json.loads(notes_override).get("extra_files", []))
        except Exception:
            pass
    total_f_cnt = (1 if (file_path or file_id) else 0) + extra_f_cnt
    if total_f_cnt > 1:
        has_file_str = f"📎 {total_f_cnt} та ҳужжат бириктирилди"
    elif total_f_cnt == 1:
        has_file_str = "📎 Ҳужжат бириктирилди"
    else:
        has_file_str = "📝 Файлсиз сақланди (кейинроқ файл қўшиш мумкин)"

    _, _, _, norm_ym, _ = normalize_work_date(pub_date, 2026)
    header = "✏️ <b>ҲИСОБОТ МУВАФФАҚИЯТЛИ ЯНГИЛАНДИ!</b>" if is_edit else "✅ <b>ҲИСОБОТ МУВАФФАҚИЯТЛИ САҚЛАНДИ!</b>"

    await message.answer(
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📑 <b>Ҳисобот ID:</b> #{entry_id}\n"
        f"📂 <b>Кафедра:</b> {html.escape(dept_name or '')}\n"
        f"📌 <b>Категория:</b> {html.escape(label)}\n"
        f"📝 <b>Иш номи:</b> {html.escape(title)}\n"
        f"👥 <b>Муаллиф(лар):</b> {html.escape(authors)}\n"
        f"📅 <b>Ҳисобот даври:</b> {norm_ym}\n"
        f"📄 <b>Ҳолати:</b> {has_file_str}{api_verify_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Ушбу йўналишдаги жами ишлар: <b>{total_cat} та</b>",
        reply_markup=post_save_kb(entry_id),
        parse_mode="HTML"
    )


# ─── СТАТИСТИКА КАФЕДРЫ ─────────────────────────────────────────────────────
@dp.message(F.text.in_(["📊 Кафедра статистикаси", "📊 Кафедрам статистикаси"]))
async def my_stats(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if not dept:
        if user_id in ADMIN_IDS:
            await message.answer(
                "👑 <b>Сиз Администраторсиз.</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Ўз кафедрангиз статистикасини кўриш учун аввал кафедра кодини киритинг:\n"
                "<i>(Масалан: ADTI-02-EE76)</i>\n\n"
                "Ёки институтнинг умумий сводкасини кўриш учун:\n"
                "👉 <b>🏛 Сводный ҳисобот</b> тугмасини босинг.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
            await state.set_state(AuthState.waiting_for_code)
        else:
            await message.answer("⚠️ Аввал кафедра паролини киритинг: /start")
        return

    summary = await get_dept_summary(dept['id'])
    entries = await get_dept_entries(dept['id'], limit=500)

    lines = [
        f"📊 <b>КАФЕДРА СТАТИСТИКАСИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Кафедра:</b> {dept['name']}\n"
        f"👤 <b>Мудир:</b> {dept.get('head_name') or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    total = 0
    for key, label in INDICATORS:
        cnt = summary.get(key, 0)
        if cnt:
            lines.append(f"  • {label}: <b>{cnt} та</b>")
            total += cnt

    if total == 0:
        lines.append("  <i>Ҳозирча юборилган илмий ишлар йўқ</i>")

    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📈 <b>Жами топширилган ишлар: {total} та</b>")

    author_counts = {}
    SKIP_AUTHORS = {"va boshqalar", "et al", "—", "-", "и др", "and others", "бошқалар"}
    for e in entries:
        auth_str = (e.get('authors') or '').strip()
        for a in auth_str.replace(";", ",").split(","):
            ac = a.strip()
            if not ac or len(ac) < 3 or len(ac) > 80 or ac.isdigit():
                continue
            if ac.lower() in SKIP_AUTHORS:
                continue
            if len(ac.split()) > 6:
                continue
            if ac == ac.upper() and len(ac) > 30:
                continue
            author_counts[ac] = author_counts.get(ac, 0) + 1

    if author_counts:
        top_auth = sorted(author_counts.items(), key=lambda x: -x[1])
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n👥 <b>Иш топширган муаллифлар (шаффофлик):</b>")
        for a_name, cnt in top_auth:
            lines.append(f"  • <b>{html.escape(a_name)}</b> — {cnt} та иш")

    full_text = "\n".join(lines)
    if len(full_text) <= 4096:
        await message.answer(full_text, parse_mode="HTML")
    else:
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await message.answer(chunk, parse_mode="HTML")
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk.strip():
            await message.answer(chunk, parse_mode="HTML")


# ─── ПРОСМОТР, ТОЧЕЧНОЕ РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ ЗАПИСЕЙ ───────────────────
def format_entry_card(e: dict, include_dept: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    cat_lbl = INDICATOR_LABELS.get(e.get('category', ''), e.get('category', ''))
    all_files = parse_entry_files(e)
    if len(all_files) > 1:
        has_f = f"📎 {len(all_files)} та ҳужжат бор"
    elif len(all_files) == 1:
        has_f = "📎 Ҳужжат бор"
    else:
        has_f = "📝 Файлсиз"

    raw_title = (e.get('title') or '').strip()
    title_str = html.escape(raw_title) if raw_title and raw_title != '—' else "⚠️ <i>(Мавзу киритилмаган)</i>"
    authors_str = html.escape(e.get('authors') or '—')

    details = []
    if include_dept and e.get('dept_name'):
        details.append(f"├ 📂 <b>Кафедра:</b> {html.escape(e['dept_name'])}")
    else:
        details.append(f"├ 📌 <b>Категория:</b> {html.escape(cat_lbl)}")
    details.append(f"├ 📝 <b>Иш номи:</b> {title_str}")
    details.append(f"├ 👤 <b>Муаллиф:</b> {authors_str}")
    if e.get('country'):
        details.append(f"├ 🌍 <b>Давлат/Тури:</b> {html.escape(str(e['country']))}")
    if e.get('journal_name'):
        details.append(f"├ 📰 <b>Журнал/Ташкилот:</b> {html.escape(str(e['journal_name']))}")
    if e.get('amount'):
        details.append(f"├ 💰 <b>Суммаси:</b> {html.escape(str(e['amount']))}")
    if e.get('pub_date'):
        details.append(f"├ 📅 <b>Сана/Нашр:</b> {html.escape(str(e['pub_date']))}")
    if e.get('reg_number'):
        details.append(f"├ 🔢 <b>Рег. рақам:</b> {html.escape(str(e['reg_number']))}")
    if e.get('specialty'):
        details.append(f"├ 🎓 <b>Ихтисослик:</b> {html.escape(str(e['specialty']))}")
    details.append(f"├ 🕒 <b>Киритилган:</b> {str(e.get('created_at', ''))[:16]}")
    details.append(f"└ 📄 <b>Ҳолати:</b> {has_f}")

    text = f"┌ 📑 <b>Ҳисобот #{e['id']}</b>\n" + "\n".join(details)

    row_kb = InlineKeyboardBuilder()
    row_kb.button(text="✏️ Таҳрирлаш", callback_data=f"edit_menu_{e['id']}")
    if all_files:
        btn_lbl = f"📥 Ҳужжатни кўриш ({len(all_files)} та)" if len(all_files) > 1 else "📥 Ҳужжатни кўриш"
        row_kb.button(text=btn_lbl, callback_data=f"getfile_{e['id']}")
    else:
        row_kb.button(text="📎 Файл бириктириш", callback_data=f"addfile_{e['id']}")
    row_kb.button(text="🗑 Ўчириш", callback_data=f"del_entry_{e['id']}")
    row_kb.adjust(2, 1)
    return text, row_kb.as_markup()


@dp.message(F.text.in_(["📋 Юборилган ишлар", "📋 Юборилган ишлар (Ўчириш)"]))
async def list_dept_submissions(message: types.Message):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    if not dept and not is_admin:
        await message.answer("⚠️ Аввал кафедра кодини киритинг: /start")
        return

    if not dept and is_admin:
        await message.answer(
            "👑 <b>Администратор учун:</b>\n"
            "Кафедра ҳисоботларини кўриш учун аввал кафедра кодини киритинг,\n"
            "ёки <b>🗂 Файллар базаси</b> тугмасини босиб барча кафедралар ишларини кўринг.",
            parse_mode="HTML"
        )
        return

    dept_id = dept['id']
    dept_name = dept['name']

    entries = await get_dept_entries(dept_id, limit=15)
    if not entries:
        await message.answer(
            f"📂 <b>{dept_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Ҳозирча юборилган илмий ишлар йўқ 📭",
            parse_mode="HTML"
        )
        return

    await message.answer(
        f"📋 <b>{dept_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Сўнгги юборилган ишлар рўйхати:\n"
        f"<i>(Исталган майдонни ўзгартириш, файл қўшиш ёки ўчириш учун тугмалардан фойдаланинг)</i>",
        parse_mode="HTML"
    )

    for e in entries:
        text, markup = format_entry_card(e, include_dept=False)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


# ─── ИНТЕРАКТИВНОЕ МЕНЮ РЕДАКТИРОВАНИЯ ЗАПИСИ ───────────────────────────────
@dp.callback_query(F.data.startswith("edit_menu_"))
async def open_edit_entry_menu(cb: types.CallbackQuery, state: FSMContext):
    entry_id = int(cb.data.replace("edit_menu_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    entry = await get_entry_by_id(entry_id)
    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return

    if not is_admin and (not dept or entry['dept_id'] != dept['id']):
        await cb.answer("❌ Бу ҳисоботни таҳрирлаш учун рухсатингиз йўқ", show_alert=True)
        return

    await state.clear()
    cat_lbl = INDICATOR_LABELS.get(entry['category'], entry['category'])
    all_files = parse_entry_files(entry)

    await cb.message.answer(
        f"✏️ <b>#{entry_id}-РАҚАМЛИ ҲИСОБОТНИ ТАҲРИРЛАШ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Кафедра:</b> {html.escape(entry.get('dept_name', ''))}\n"
        f"📌 <b>Бўлим:</b> {html.escape(cat_lbl)}\n"
        f"📝 <b>Иш номи:</b> {html.escape(entry.get('title') or '—')}\n"
        f"👥 <b>Муаллифлар:</b> {html.escape(entry.get('authors') or '—')}\n"
        f"📅 <b>Сана/Нашр:</b> {html.escape(entry.get('pub_date') or '—')}\n"
        f"📎 <b>Бириктирилган файллар:</b> {len(all_files)} та\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Қайси маълумотни ўзгартирмоқчисиз? Танланг 👇",
        reply_markup=edit_entry_menu_kb(entry_id, has_file=bool(all_files)),
        parse_mode="HTML"
    )
    await cb.answer()


FIELD_EDIT_PROMPTS = {
    "title":        ("Иш номи (мавзуси)", "📝 Янги иш номини (мавзусини) ёзиб юборинг:"),
    "authors":      ("Муаллифлар (Ф.И.Ш.)", "👥 Муаллиф(лар)нинг тўғри Ф.И.Ш.ни ёзиб юборинг:"),
    "pub_date":     ("Сана / Нашр маълумоти", "📅 Тўғри сана ёки нашр маълумотини ёзиб юборинг (Масалан: 15.03.2026):"),
    "journal_name": ("Журнал / Буюртмачи номи", "📰 Журнал ёки буюртмачи ташкилот номини ёзиб юборинг:"),
    "country":      ("Давлат / Патент тури", "🌍 Давлат номи ёки патент турини ёзиб юборинг:"),
    "url":          ("URL / DOI ҳавола", "🔗 Мақола интернет ҳаволасини (URL ёки DOI) юборинг:"),
}


@dp.callback_query(F.data.startswith("efield:"))
async def start_single_field_edit(cb: types.CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    entry_id = int(parts[1])
    field_name = parts[2]

    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS
    entry = await get_entry_by_id(entry_id)

    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return
    if not is_admin and (not dept or entry['dept_id'] != dept['id']):
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    lbl, prompt_txt = FIELD_EDIT_PROMPTS.get(field_name, (field_name, "Янги қийматни киритинг:"))
    curr_val = entry.get(field_name) or "—"

    await state.clear()
    await state.update_data(edit_entry_id=entry_id, edit_field_name=field_name, edit_dept_name=entry.get("dept_name", ""))
    await state.set_state(EditFieldState.waiting_for_field_value)

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Бекор қилиш", callback_data="cancel")

    await cb.message.edit_text(
        f"✏️ <b>Ҳисобот #{entry_id} — {lbl}ни ўзгартириш</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Ҳозирги қиймати:</b> <code>{html.escape(str(curr_val))}</code>\n\n"
        f"{prompt_txt}",
        reply_markup=cancel_kb.as_markup(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(EditFieldState.waiting_for_field_value, F.text)
async def process_single_field_edit(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    data = await state.get_data()
    entry_id = data.get("edit_entry_id")
    field_name = data.get("edit_field_name")
    dept_name = data.get("edit_dept_name", "")

    raw_val = message.text.strip()
    if field_name == "title":
        ok, cleaned_val, err = validate_and_clean_title(raw_val)
        if not ok:
            await message.answer(err, parse_mode="HTML")
            return
    elif field_name == "authors":
        ok, cleaned_val, err = validate_and_clean_authors(raw_val)
        if not ok:
            await message.answer(err, parse_mode="HTML")
            return
    elif field_name == "pub_date":
        ok, cleaned_val, err = validate_and_clean_pub_date(raw_val)
        if not ok:
            await message.answer(err, parse_mode="HTML")
            return
    else:
        cleaned_val = clean_single_line(raw_val)

    await update_entry_field(entry_id, field_name, cleaned_val)
    await state.clear()

    asyncio.create_task(notify_edit_to_audit(dept_name, entry_id, f"{field_name} -> {cleaned_val}"))
    schedule_debounced_backup(reason=f"Таҳрирланди #{entry_id} ({field_name})")

    updated_entry = await get_entry_by_id(entry_id)
    if updated_entry:
        card_text, card_kb = format_entry_card(updated_entry)
        await message.answer(
            f"✅ <b>Ҳисобот #{entry_id} маълумоти муваффақиятли янгиланди!</b>\n\n{card_text}",
            reply_markup=card_kb,
            parse_mode="HTML"
        )
    else:
        await message.answer("✅ Маълумот янгиланди!", reply_markup=main_kb(message.from_user.id))


# ─── ПРИКРЕПИТЬ ДОПОЛНИТЕЛЬНЫЙ ИЛИ ЗАМЕНИТЬ ОСНОВНОЙ ФАЙЛ К ОТЧЁТУ ──────────
@dp.callback_query(F.data.startswith("addfile_"))
async def start_append_file_to_entry(cb: types.CallbackQuery, state: FSMContext):
    entry_id = int(cb.data.replace("addfile_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS
    entry = await get_entry_by_id(entry_id)

    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return
    if not is_admin and (not dept or entry['dept_id'] != dept['id']):
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    await state.clear()
    await state.update_data(target_entry_id=entry_id, target_dept_id=entry['dept_id'], target_dept_name=entry.get('dept_name', ''))
    await state.set_state(EditFieldState.waiting_for_file_append)

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Бекор қилиш", callback_data="cancel")

    await cb.message.answer(
        f"📎 <b>Ҳисобот #{entry_id} га ҳужжат бириктириш</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Иш номи:</b> {html.escape(entry.get('title') or '—')}\n\n"
        f"Илтимос, <b>PDF</b>, <b>Word (.docx)</b> ёки <b>Фото/скан</b> файлини юборинг 👇",
        reply_markup=cancel_kb.as_markup(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("ereplacefile:"))
async def start_replace_file_on_entry(cb: types.CallbackQuery, state: FSMContext):
    entry_id = int(cb.data.split(":")[1])
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS
    entry = await get_entry_by_id(entry_id)

    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return
    if not is_admin and (not dept or entry['dept_id'] != dept['id']):
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    await state.clear()
    await state.update_data(target_entry_id=entry_id, target_dept_id=entry['dept_id'], target_dept_name=entry.get('dept_name', ''))
    await state.set_state(EditFieldState.waiting_for_file_replace)

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Бекор қилиш", callback_data="cancel")

    await cb.message.edit_text(
        f"🔄 <b>Ҳисобот #{entry_id} асосий файлини алмаштириш</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Янги <b>PDF</b>, <b>Word (.docx)</b> ёки <b>Фото/скан</b> файлини юборинг 👇",
        reply_markup=cancel_kb.as_markup(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(StateFilter(EditFieldState.waiting_for_file_append, EditFieldState.waiting_for_file_replace), F.document | F.photo)
async def process_file_attach_or_replace(message: types.Message, state: FSMContext):
    curr_state = await state.get_state()
    data = await state.get_data()
    entry_id = data.get("target_entry_id")
    dept_id = data.get("target_dept_id", 1)
    dept_name = data.get("target_dept_name", "")

    fid, fpath = await download_tg_message_file(message, dept_id)
    if not fid:
        await message.answer("❌ Файлни қабул қилишда хатолик. Қайта уриниб кўринг.")
        return

    if curr_state == EditFieldState.waiting_for_file_replace.state:
        await update_entry_field(entry_id, "file_id", fid)
        await update_entry_field(entry_id, "file_path", fpath)
        action_msg = "✅ <b>Асосий ҳужжат файли муваффақиятли янгиланди!</b>"
    else:
        total_f = await append_entry_file(entry_id, fid, fpath)
        action_msg = f"✅ <b>Ҳужжат муваффақиятли бириктирилди! (Жами файллар: {total_f} та)</b>"

    await state.clear()
    asyncio.create_task(notify_edit_to_audit(dept_name, entry_id, f"Файл бириктирилди/янгиланди"))
    schedule_debounced_backup(reason=f"Файл бириктирилди #{entry_id}")

    updated = await get_entry_by_id(entry_id)
    if updated:
        card_text, card_kb = format_entry_card(updated)
        await message.answer(f"{action_msg}\n\n{card_text}", reply_markup=card_kb, parse_mode="HTML")
    else:
        await message.answer(action_msg, reply_markup=main_kb(message.from_user.id), parse_mode="HTML")


# ─── ПОЛНОЕ РЕДАКТИРОВАНИЕ ЗАПИСИ (ВСЕ ПОЛЯ СНАЧАЛА) ────────────────────────
@dp.callback_query(F.data.startswith("edit_entry_") | F.data.startswith("edit_full_"))
async def start_edit_entry_full(cb: types.CallbackQuery, state: FSMContext):
    raw_id = cb.data.replace("edit_entry_", "").replace("edit_full_", "")
    entry_id = int(raw_id)
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    entry = await get_entry_by_id(entry_id)
    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return

    if not is_admin and (not dept or entry['dept_id'] != dept['id']):
        await cb.answer("❌ Бу ҳисоботни таҳрирлаш учун рухсатингиз йўқ", show_alert=True)
        return

    cat = entry['category']
    await state.clear()
    await state.update_data(
        editing_entry_id=entry_id,
        category=cat,
        dept_id=entry['dept_id'],
        dept_name=entry.get('dept_name', ''),
        head_name=entry.get('head_name', ''),
        extra_step_index=0,
    )
    await state.set_state(AddEntry.enter_title)
    await prompt_current_step(cb.message, state, edit_message=False)
    await cb.answer()


@dp.callback_query(F.data.startswith("del_entry_"))
async def process_delete_entry(cb: types.CallbackQuery):
    entry_id = int(cb.data.replace("del_entry_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_adm = user_id in ADMIN_IDS

    dept_id = None if is_adm else (dept['id'] if dept else -1)
    success = await delete_entry(entry_id, dept_id)

    if success:
        dept_name = dept['name'] if dept else f"Кафедра #{dept_id}"
        asyncio.create_task(notify_delete_to_audit(dept_name, entry_id))
        schedule_debounced_backup(reason=f"Ўчирилди: #{entry_id} ({dept_name})")
        asyncio.create_task(sync_sheets_background())
        await cb.message.edit_text(
            f"🗑 <b>#{entry_id} рақамли ёзув ўчирилди.</b>\n"
            f"Ҳисобот статистикаси автомат янгиланди.",
            parse_mode="HTML"
        )
    else:
        await cb.answer("❌ Бу ёзувни ўчириш учун рухсатингиз йўқ ёки у топилмади.", show_alert=True)
    await cb.answer()


# ─── ADMIN: ОЧИСТКА ТЕСТОВЫХ ДАННЫХ ─────────────────────────────────────────
@dp.message(Command("clear_test_data"))
async def cmd_clear_test(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ ҲА, барча тест маълумотларни ўчириш", callback_data="confirm_clear_all")
    kb.button(text="❌ Бекор қилиш", callback_data="cancel")
    kb.adjust(1)
    await message.answer(
        "⚠️ <b>ДИҚҚАТ!</b>\n\n"
        "Сиз барча юборилган тест ҳисоботларни базадан бутунлай тозаламоқчисиз.\n"
        "Ушбу амални фақат тизимни расмий ишга туширишдан олдин бажаринг!",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "confirm_clear_all")
async def confirm_clear_all(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Рухсат йўқ", show_alert=True)
        return
    await clear_all_test_data()
    asyncio.create_task(backup_database_to_channel(reason="Тест маълумотлар тозаланди"))
    asyncio.create_task(sync_sheets_background())
    await cb.message.edit_text("✅ <b>Барча тест маълумотлар муваффақиятли тозаланди!</b>\nБаза бўш ва расмий қабулга тайёр.", parse_mode="HTML")
    await cb.answer()


# ─── ПРОСМОТР ФАЙЛОВ ПО КАТЕГОРИЯМ ─────────────────────────────────────────
def browse_categories_kb():
    kb = InlineKeyboardBuilder()
    for key, label in INDICATORS:
        kb.button(text=label, callback_data=f"browse_{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Ёпиш", callback_data="cancel"))
    return kb.as_markup()


@dp.message(F.text.in_(["🗂 Файллар базаси", "🗂 Файлларни кўриш (категория)"]))
async def browse_by_category_start(message: types.Message):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    if not dept and not is_admin:
        await message.answer("⚠️ Аввал кафедра кодини киритинг: /start")
        return

    scope = "Барча 65 та кафедра бўйича" if is_admin and not dept else f"{dept['name']} кафедраси бўйича"
    await message.answer(
        f"🗂 <b>ФАЙЛЛАР БАЗАСИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <b>Кўриш ҳудуди:</b> {scope}\n\n"
        f"Қайси бўлимдаги файлларни кўрмоқчисиз? 👇",
        reply_markup=browse_categories_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("browse_"))
async def browse_category_entries(cb: types.CallbackQuery):
    cat = cb.data[7:]
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    dept_filter = None if (is_admin and not dept) else (dept['id'] if dept else None)

    label = INDICATOR_LABELS.get(cat, cat)
    entries = await get_entries_by_category(cat, dept_id=dept_filter, limit=30)

    scope_title = " (Барча кафедралар)" if not dept_filter else ""
    await cb.message.edit_text(
        f"📂 <b>{label}{scope_title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Топилган ишлар сони: <b>{len(entries)} та</b>\n\n"
        f"{'Ҳозирча ушбу бўлимда ёзувлар йўқ 📭' if not entries else 'Рўйхат қуйида келтирилган 👇'}",
        parse_mode="HTML",
        reply_markup=None
    )

    if not entries:
        await cb.answer()
        return

    for e in entries:
        card_text, card_kb = format_entry_card(e, include_dept=True)
        await cb.message.answer(card_text, reply_markup=card_kb, parse_mode="HTML")

    await cb.answer()


@dp.callback_query(F.data.startswith("getfile_"))
async def send_entry_file(cb: types.CallbackQuery):
    """Отправляет все прикреплённые файлы записи (включая дополнительные файлы из notes)"""
    entry_id = int(cb.data.replace("getfile_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    row = await get_entry_by_id(entry_id)
    if not row:
        await cb.answer("❌ Ёзув топилмади", show_alert=True)
        return

    if not is_admin and (not dept or row['dept_id'] != dept['id']):
        await cb.answer("❌ Бу файлга кириш рухсати йўқ", show_alert=True)
        return

    all_files = parse_entry_files(row)
    if not all_files:
        await cb.answer("❌ Файл сақланмаган", show_alert=True)
        return

    await cb.answer("⏳ Файл юборилмоқда...")
    raw_title = row.get('title') or 'Ҳужжат'
    safe_title = html.escape(raw_title)[:250]

    for idx, f_item in enumerate(all_files, 1):
        fid = f_item.get("file_id")
        fpath = f_item.get("file_path")
        part_tag = f" ({idx}/{len(all_files)}-файл)" if len(all_files) > 1 else ""
        cap_html = f"📎 <b>{safe_title}{part_tag}</b>\n<i>ID: #{entry_id}</i>"
        cap_plain = f"📎 {raw_title[:250]}{part_tag}\nID: #{entry_id}"

        sent = False
        if fid:
            for send_fn in (bot.send_document, bot.send_photo):
                try:
                    await send_fn(cb.message.chat.id, fid, caption=cap_html, parse_mode="HTML")
                    sent = True
                    break
                except Exception:
                    try:
                        await send_fn(cb.message.chat.id, fid, caption=cap_plain)
                        sent = True
                        break
                    except Exception:
                        continue

        if not sent and fpath and Path(fpath).exists():
            try:
                p = Path(fpath)
                await bot.send_document(
                    cb.message.chat.id,
                    types.BufferedInputFile(p.read_bytes(), filename=p.name),
                    caption=cap_html,
                    parse_mode="HTML"
                )
                sent = True
            except Exception as e:
                await cb.message.answer(f"❌ Файлни юборишда хатолик: {e}")

        if not sent:
            await cb.message.answer(f"❌ #{entry_id} ҳужжатни Telegram серверидан юклаб бўлмади.")


# ─── ADMIN: СВОДНАЯ ТАБЛИЦА ─────────────────────────────────────────────────
@dp.message(F.text.in_(["🏛 Сводный ҳисобот", "🏛 Сводный ҳисобот (65 кафедра)"]))
async def admin_summary(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⚠️ Ушбу функция фақат маъмурият (Admin) учун очиқ.")
        return

    rows = await get_all_summary()
    total_scopus = total_phd = total_dsc = total_pat = total_all = 0
    active_depts = 0

    dept_lines = []
    for r in rows:
        scopus = r.get('scopus_wos') or 0
        phd = r.get('phd') or 0
        dsc = r.get('dsc') or 0
        pat = r.get('patent') or 0
        tot = r.get('total') or 0

        total_scopus += scopus
        total_phd += phd
        total_dsc += dsc
        total_pat += pat
        total_all += tot

        if tot > 0:
            active_depts += 1
            name_short = r['name'][:35] + '…' if len(r['name']) > 35 else r['name']
            dept_lines.append(
                f"<b>{r['id']}. {html.escape(name_short)}</b>\n"
                f"   └ 🌐 Scopus: <b>{scopus}</b> | 💡 Патент: <b>{pat}</b> | 🎓 DSc/PhD: <b>{dsc}/{phd}</b> | Жами: <b>{tot}</b>"
            )

    header = (
        f"🏛 <b>АДТИ — БАРЧА 65 ТА КАФЕДРА СВОДКАСИ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>ИНСТИТУТ БЎЙИЧА УМУМИЙ КЎРСАТКИЧЛАР:</b>\n"
        f"• 🌐 Scopus / WoS мақолалар: <b>{total_scopus} та</b>\n"
        f"• 💡 Ихтиро ва патентлар: <b>{total_pat} та</b>\n"
        f"• 🎓 DSc ҳимоялар: <b>{total_dsc} та</b> | PhD: <b>{total_phd} та</b>\n"
        f"• 🏢 Иш топширган кафедралар: <b>{active_depts} / 65 та</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ЖАМИ БАРЧА ЁЗУВЛАР: {total_all} та</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 <b>Кафедралар кесимида:</b>\n\n"
    )

    if not dept_lines:
        await message.answer(header + "<i>Ҳозирча бирорта ҳам кафедра иш топширмаган.</i>", parse_mode="HTML")
        return

    chunks = []
    curr = header
    for line in dept_lines:
        if len(curr) + len(line) + 2 > 3500:
            chunks.append(curr.strip())
            curr = line + "\n\n"
        else:
            curr += line + "\n\n"
    if curr.strip():
        chunks.append(curr.strip())

    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode="HTML")
        except Exception:
            await message.answer(chunk)
        await asyncio.sleep(0.2)


# ─── ADMIN: СКАЧАТЬ ZIP АРХИВ ВСЕХ ФАЙЛОВ ПО ПАПКАМ С РЕЕСТРОМ ──────────────
@dp.message(F.text.in_(["📦 Файллар ZIP архиви", "📦 Файллар ZIP архиви (.zip)"]))
async def prompt_zip_download(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Давр бўйича ZIP (Йил-Ой)", callback_data="zipmenu_period")
    for key, label in INDICATORS:
        kb.button(text=f"{label} (.zip)", callback_data=f"zip_cat_{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Бекор қилиш", callback_data="cancel"))

    await message.answer(
        "📦 <b>Файллар ZIP архивини юклаб олиш</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <i>Ҳар бир ZIP архив ичига автоматик тарзда <b>00_РЕЕСТР_ФАЙЛЛАР_РЎЙХАТИ.xlsx</b> жадвали жойланади!</i>\n"
        "Керакли бўлим ёки даврни танланг 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "zipmenu_period")
async def show_zip_period_menu(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 1-ярим йиллик (01—06)", callback_data="zip_period:2026-01:2026-06")
    kb.button(text="📅 2-ярим йиллик (07—12)", callback_data="zip_period:2026-07:2026-12")

    months = [
        ("Январ", "2026-01"), ("Феврал", "2026-02"), ("Март", "2026-03"),
        ("Апрел", "2026-04"), ("Май", "2026-05"), ("Июн", "2026-06"),
        ("Июл", "2026-07"), ("Август", "2026-08"), ("Сентябр", "2026-09"),
        ("Октябр", "2026-10"), ("Ноябр", "2026-11"), ("Декабр", "2026-12"),
    ]
    for lbl, ym in months:
        kb.button(text=lbl, callback_data=f"zip_period:{ym}:{ym}")

    kb.button(text="📚 2025 йил тўлиқ", callback_data="zip_period:2025-01:2025-12")
    kb.button(text="📚 2024 йил тўлиқ", callback_data="zip_period:2024-01:2024-12")
    kb.button(text="✍️ Бошқа йил/интервал киритиш", callback_data="zip_custom_interval")
    kb.button(text="🔙 Орқага", callback_data="zip_back_main")
    kb.adjust(2, 3, 3, 3, 3, 2, 1, 1)

    caption_txt = (
        "📦 <b>Қайси даврнинг (Йил ва Ой) файллар архивини юкламоқчисиз?</b>\n\n"
        "<i>Танланган даврга тегишли барча ҳужжатлар категориялар бўйича папкаларга ажратилиб, "
        "Excel-реестр билан бирга ZIP архив сифатида юборилади.</i>"
    )
    await cb.message.edit_text(caption_txt, reply_markup=kb.as_markup(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "zip_back_main")
async def back_to_zip_main(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Давр бўйича ZIP (Йил-Ой)", callback_data="zipmenu_period")
    for key, label in INDICATORS:
        kb.button(text=f"{label} (.zip)", callback_data=f"zip_cat_{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Бекор қилиш", callback_data="cancel"))

    await cb.message.edit_text(
        "📦 <b>Файллар ZIP архивини юклаб олиш</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <i>Ҳар бир ZIP архив ичига автоматик тарзда <b>00_РЕЕСТР_ФАЙЛЛАР_РЎЙХАТИ.xlsx</b> жадвали жойланади!</i>\n\n"
        "Керакли бўлим ёки даврни танланг 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "zip_custom_interval")
async def ask_zip_custom_interval(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    await state.set_state(PeriodReportState.waiting_for_zip_interval)
    msg_prompt = (
        "✍️ <b>ZIP архив учун исталган йил ёки ойни ёзиб юборинг:</b>\n\n"
        "Масалан:\n"
        "• <code>2025</code> (2025 йил тўлиқ)\n"
        "• <code>2024</code> (2024 йил тўлиқ)\n"
        "• <code>2026-04</code> (бир ой учун)\n"
        "• <code>2025-09 - 2026-05</code> (ўқув йили учун)\n\n"
        "<i>Бекор қилиш учун /cancel деб ёзинг.</i>"
    )
    await cb.message.edit_text(msg_prompt, parse_mode="HTML")
    await cb.answer()


@dp.message(PeriodReportState.waiting_for_zip_interval, F.text)
async def handle_zip_custom_interval(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip()
    start_ym, end_ym = parse_user_interval(text)
    if not start_ym:
        await message.answer("⚠️ <b>Нотўғри формат!</b>\nИлтимос, <code>2025</code>, <code>2026-04</code> ёки <code>2025-09 - 2026-05</code> кўринишида ёзинг.", parse_mode="HTML")
        return

    period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"
    await state.clear()

    entries = await get_files_for_zip(start_ym=start_ym, end_ym=end_ym)
    if not entries:
        await message.answer(f"📭 <b>{period_text} даврида бирорта файл топилмади.</b>", parse_mode="HTML")
        return

    kb = build_zip_period_cat_kb(start_ym, end_ym)
    caption_txt = (
        f"📦 <b>{period_text} даври учун қайси категория файлларини юкламоқчисиз?</b>\n\n"
        f"<i>Жами топилган файллар: {len(entries)} та.\n"
        f"Сиз барча файлларни ёки фақат маълум категорияни танлаб олишингиз мумкин.</i>"
    )
    await message.answer(caption_txt, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("zip_period:"))
async def choose_zip_period_category(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    parts = cb.data.split(":")
    start_ym = parts[1]
    end_ym = parts[2]
    period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"

    kb = build_zip_period_cat_kb(start_ym, end_ym)
    caption_txt = (
        f"📦 <b>{period_text} даври учун қайси категория файлларини юкламоқчисиз?</b>\n\n"
        f"<i>Сиз ушбу даврдаги барча файлларни ёки фақат алоҳида йўналиш архивини олишингиз мумкин.</i>"
    )
    await cb.message.edit_text(caption_txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("ziprun:"))
async def process_ziprun_download(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    parts = cb.data.split(":")
    start_ym = parts[1]
    end_ym = parts[2]
    cat_key = parts[3]
    period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"

    await cb.message.edit_text(
        f"⏳ <b>{period_text} даври файллар архиви тайёрланмоқда...</b>\n"
        f"<i>Файллар юкланиб, 00_РЕЕСТР_ФАЙЛЛАР_РЎЙХАТИ.xlsx билан ZIP пакетга жойланмоқда. Илтимос, кутинг.</i>",
        parse_mode="HTML"
    )

    entries = []
    if cat_key == "all":
        entries = await get_files_for_zip(start_ym=start_ym, end_ym=end_ym)
        label_info = f"{period_text} (Барча категориялар)"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Barcha_Fayllar.zip" if start_ym != end_ym else f"ADTI_{start_ym}_Barcha_Fayllar.zip"
    elif cat_key == "cat_dissertations":
        dsc = await get_files_for_zip(category="dsc", start_ym=start_ym, end_ym=end_ym)
        phd = await get_files_for_zip(category="phd", start_ym=start_ym, end_ym=end_ym)
        entries = dsc + phd
        label_info = f"{period_text} (DSc ва PhD диссертациялари)"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Dissertatsiyalar.zip"
    elif cat_key == "theses":
        th_uz = await get_files_for_zip(category="thesis_uz", start_ym=start_ym, end_ym=end_ym)
        th_for = await get_files_for_zip(category="thesis_foreign", start_ym=start_ym, end_ym=end_ym)
        entries = th_uz + th_for
        label_info = f"{period_text} (Республика ва хорижий тезислар)"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Tezislar.zip"
    elif cat_key == "practical":
        r = await get_files_for_zip(category="rationalizer", start_ym=start_ym, end_ym=end_ym)
        i = await get_files_for_zip(category="implementation", start_ym=start_ym, end_ym=end_ym)
        entries = r + i
        label_info = f"{period_text} (Рационализаторлик ва амалиётга тадбиқ)"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Ratsionalizatorlik.zip"
    elif cat_key == "grants_contracts":
        c = await get_files_for_zip(category="contracts", start_ym=start_ym, end_ym=end_ym)
        g = await get_files_for_zip(category="grants", start_ym=start_ym, end_ym=end_ym)
        entries = c + g
        label_info = f"{period_text} (Шартнома ва грантлар)"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Shartnomalar_Grantlar.zip"
    else:
        entries = await get_files_for_zip(category=cat_key, start_ym=start_ym, end_ym=end_ym)
        cat_lbl = INDICATOR_LABELS.get(cat_key, cat_key)
        label_info = f"{period_text} ({cat_lbl})"
        zip_name = f"ADTI_{start_ym}_{end_ym}_{cat_key}.zip"

    if not entries:
        await cb.message.edit_text(
            f"📭 <b>Ушбу давр ва категорияда бирорта файл топилмади.</b>\n<i>({label_info})</i>",
            parse_mode="HTML"
        )
        await cb.answer()
        return

    sent_parts = 0
    total_files_sent = 0
    try:
        async for part_buf, part_count in stream_files_zip(bot, entries, max_zip_bytes=35 * 1024 * 1024):
            sent_parts += 1
            total_files_sent += part_count
            part_size_mb = part_buf.getbuffer().nbytes / (1024 * 1024)

            part_base = zip_name.replace('.zip', '')
            part_filename = f"{part_base}_Part_{sent_parts:02d}.zip"

            await bot.send_document(
                cb.message.chat.id,
                types.BufferedInputFile(part_buf.getvalue(), filename=part_filename),
                caption=f"📦 <b>{label_info}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📑 Қисм: <b>{sent_parts}</b>\n"
                        f"📁 Файллар сони: <b>{part_count} та</b> (+ Excel реестр)\n"
                        f"💾 Ҳажми: <b>{part_size_mb:.1f} МБ</b>",
                parse_mode="HTML"
            )
            part_buf.close()
            await asyncio.sleep(0.3)

        if sent_parts == 1:
            await cb.message.edit_text(
                f"✅ <b>Архив муваффақиятли юборилди!</b>\n"
                f"📌 Бўлим: <b>{label_info}</b>\n"
                f"📁 Жами юборилган файллар: <b>{total_files_sent} та</b>",
                parse_mode="HTML"
            )
        else:
            await cb.message.edit_text(
                f"✅ <b>Барча {sent_parts} та ZIP қисм муваффақиятли юборилди!</b>\n"
                f"📌 Бўлим: <b>{label_info}</b>\n"
                f"📁 Жами юборилган файллар: <b>{total_files_sent} та</b>",
                parse_mode="HTML"
            )
    except Exception as ex:
        await cb.message.edit_text(f"❌ ZIP архив юборишда хатолик: {ex}", parse_mode="HTML")

    await cb.answer()


@dp.callback_query(F.data.startswith("zip_"))
async def process_zip_download(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Рухсат йўқ", show_alert=True)
        return

    zip_type = cb.data.replace("zip_", "")
    await cb.message.edit_text(
        "⏳ <b>Файллар юкланиб, Excel реестр билан ZIP архив яратилмоқда...</b>\n"
        "<i>Файллар сонига қараб 5-20 сония вақт олиши мумкин. Илтимос, кутинг.</i>",
        parse_mode="HTML"
    )

    entries = []
    zip_name = "ADTI_2026_Fayllar_Arxiv.zip"
    label_info = "Барча категориялар"

    if zip_type == "all":
        entries = await get_files_for_zip()
        zip_name = "ADTI_2026_Barcha_Fayllar.zip"
        label_info = "Барча категориялар"
    elif zip_type == "cat_dissertations":
        dsc_entries = await get_files_for_zip(category="dsc")
        phd_entries = await get_files_for_zip(category="phd")
        entries = dsc_entries + phd_entries
        zip_name = "ADTI_2026_Dissertatsiyalar_DSc_PhD.zip"
        label_info = "DSc ва PhD диссертациялари"
    elif zip_type.startswith("cat_"):
        cat_key = zip_type[4:]
        entries = await get_files_for_zip(category=cat_key)
        label_info = INDICATOR_LABELS.get(cat_key, cat_key)
        zip_name = f"ADTI_2026_{cat_key}.zip"
    elif zip_type.startswith("period:"):
        parts = zip_type.split(":")
        start_ym = parts[1]
        end_ym = parts[2]
        period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"
        label_info = f"{period_text} даври файллар архиви"
        zip_name = f"ADTI_{start_ym}_{end_ym}_Fayllar.zip" if start_ym != end_ym else f"ADTI_{start_ym}_Fayllar.zip"
        entries = await get_files_for_zip(start_ym=start_ym, end_ym=end_ym)

    if not entries:
        await cb.message.edit_text(f"📭 <b>Ушбу бўлимда ҳали бирорта файл бириктирилмаган.</b>\n<i>({label_info})</i>", parse_mode="HTML")
        await cb.answer()
        return

    sent_parts = 0
    total_files_sent = 0

    try:
        async for part_buf, part_count in stream_files_zip(bot, entries, max_zip_bytes=35 * 1024 * 1024):
            sent_parts += 1
            total_files_sent += part_count
            part_size_mb = part_buf.getbuffer().nbytes / (1024 * 1024)

            part_base = zip_name.replace('.zip', '')
            part_filename = f"{part_base}_Part_{sent_parts:02d}.zip"

            await bot.send_document(
                cb.message.chat.id,
                types.BufferedInputFile(part_buf.getvalue(), filename=part_filename),
                caption=f"📦 <b>{label_info}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📑 Қисм: <b>{sent_parts}</b>\n"
                        f"📁 Файллар сони: <b>{part_count} та</b> (+ Excel реестр)\n"
                        f"💾 Ҳажми: <b>{part_size_mb:.1f} МБ</b>",
                parse_mode="HTML"
            )
            part_buf.close()
            await asyncio.sleep(0.3)

        if sent_parts == 0:
            await cb.message.edit_text("📭 Файлларни юклаб бўлмади ёки улар топилмади.", parse_mode="HTML")
        elif sent_parts == 1:
            await cb.message.edit_text(
                f"✅ <b>Архив муваффақиятли юборилди!</b>\n"
                f"📌 Бўлим: <b>{label_info}</b>\n"
                f"📁 Жами архивланган файллар: <b>{total_files_sent} та</b>",
                parse_mode="HTML"
            )
        else:
            await cb.message.edit_text(
                f"✅ <b>Барча {sent_parts} та ZIP қисм муваффақиятли юборилди!</b>\n"
                f"📌 Бўлим: <b>{label_info}</b>\n"
                f"📁 Жами архивланган файллар: <b>{total_files_sent} та</b>",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Error in process_zip_download: {e}")
        await cb.message.edit_text(f"❌ ZIP архив яратишда хатолик: {e}", parse_mode="HTML")

    await cb.answer()


# ─── ADMIN: EXCEL ОТЧЁТ ПО ХИСАБОТУ (.xlsx) ──────────────────────────────
@dp.message(F.text.in_(["📊 Excel ҳисобот (.xlsx)", "📊 Excel ҳисобот юклаш (.xlsx)"]))
async def download_excel_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ <b>Excel ҳисобот яратилмоқда (.xlsx)...</b>", parse_mode="HTML")
    summary_rows = await get_all_summary()
    detailed_rows = await get_all_detailed_entries()
    buf = await generate_report_excel(summary_rows, detailed_rows)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_2026_hisobot.xlsx"),
        caption="✅ <b>АДТИ 2026 йил Excel расмий ҳисоботи (.xlsx)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 <b>1-варақ:</b> 65 та кафедра сводкаси (барча индикаторлар)\n"
                "📝 <b>2-варақ:</b> Барча юборилган илмий ишлар базаси (фаол ҳаволалар ва ZIP файл номлари билан)\n"
                "🏆 <b>3-варақ:</b> Кафедралар фаоллик рейтинги (#1..#65) ва ТОП-30 фаол муаллифлар",
        parse_mode="HTML"
    )


# ─── ADMIN: WORD ОТЧЁТ ПО ХИСАБОТУ ──────────────────────────────────────────
@dp.message(F.text.in_(["📥 Word ҳисобот (.docx)", "📥 Word ҳисобот юклаш"]))
async def download_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ <b>Word ҳисобот яратилмоқда (.docx)...</b>", parse_mode="HTML")
    rows = await get_all_summary()
    detailed_rows = await get_all_detailed_entries()
    buf = await generate_report_docx(rows, detailed_rows)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_2026_hisobot.docx"),
        caption="✅ <b>АДТИ 2026 йил Word расмий ҳисоботи</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 <b>1-қисм:</b> 65 та кафедра сводкаси\n"
                "📝 <b>2-қисм (Илова):</b> Илмий ишлар ва уларнинг муаллифлари (шаффофлик рўйхати)",
        parse_mode="HTML"
    )


# ─── ADMIN: ОТЧЁТЫ ПО ИНТЕРВАЛУ (ГОД + МЕСЯЦ) ──────────────────────────────
@dp.message(F.text == "📅 Давр бўйича ҳисобот (Йил-Ой)")
async def prompt_period_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 1-ярим йиллик (01—06)", callback_data="rep_period:2026-01:2026-06")
    kb.button(text="📅 2-ярим йиллик (07—12)", callback_data="rep_period:2026-07:2026-12")

    months = [
        ("Январ", "2026-01"), ("Феврал", "2026-02"), ("Март", "2026-03"),
        ("Апрел", "2026-04"), ("Май", "2026-05"), ("Июн", "2026-06"),
        ("Июл", "2026-07"), ("Август", "2026-08"), ("Сентябр", "2026-09"),
        ("Октябр", "2026-10"), ("Ноябр", "2026-11"), ("Декабр", "2026-12"),
    ]
    for lbl, ym in months:
        kb.button(text=lbl, callback_data=f"rep_period:{ym}:{ym}")

    kb.button(text="📚 2025 йил тўлиқ", callback_data="rep_period:2025-01:2025-12")
    kb.button(text="📚 2024 йил тўлиқ", callback_data="rep_period:2024-01:2024-12")
    kb.button(text="✍️ Бошқа йил/интервал киритиш", callback_data="rep_custom_interval")
    kb.button(text="❌ Бекор қилиш", callback_data="cancel")
    kb.adjust(2, 3, 3, 3, 3, 2, 1, 1)

    caption_txt = (
        "📅 <b>Илмий ишлар бўйича ҳисобот даврини (Йил ва Ой) танланг:</b>\n\n"
        "<i>Танланган давр учун 65 та кафедранинг барча кўрсаткичлари қайта ҳисобланади "
        "ва алоҳида Excel ҳамда Word файллари шакллантирилади.</i>"
    )
    await message.answer(caption_txt, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("rep_period:"))
async def process_period_report(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    parts = cb.data.split(":")
    start_ym = parts[1]
    end_ym = parts[2]

    period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"
    await cb.message.edit_text(f"⏳ <b>{period_text} даври учун Excel ва Word ҳисоботлар тайёрланмоқда...</b>", parse_mode="HTML")

    summary_rows = await get_all_summary()
    detailed_rows = await get_all_detailed_entries()

    buf_xl = await generate_report_excel(summary_rows, detailed_rows, start_ym=start_ym, end_ym=end_ym)
    buf_doc = await generate_report_docx(summary_rows, detailed_rows, start_ym=start_ym, end_ym=end_ym)

    xl_cap = (
        f"✅ <b>АДТИ {period_text} даври Excel ҳисоботи (.xlsx)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 1-варақ: 65 та кафедра қайта ҳисобланган сводкаси\n"
        f"📝 2-варақ: Ушбу даврдаги барча ишлар базаси (автофильтр ва файл номлари билан)\n"
        f"🏆 3-варақ: Кафедраларнинг мазкур даврдаги фаоллик рейтинги"
    )
    await bot.send_document(
        cb.message.chat.id,
        types.BufferedInputFile(buf_xl.read(), filename=f"ADTI_{period_text}_hisobot.xlsx"),
        caption=xl_cap,
        parse_mode="HTML"
    )

    doc_cap = (
        f"✅ <b>АДТИ {period_text} даври Word расмий ҳисоботи (.docx)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 1-қисм: 65 та кафедранинг {period_text} даври сводкаси\n"
        f"📝 2-қисм: Мазкур даврдаги илмий ишларнинг батафсил рўйхати"
    )
    zip_btn_kb = InlineKeyboardBuilder()
    zip_btn_kb.button(text="📦 Ушбу давр ZIP архивини юклаш", callback_data=f"zip_period:{start_ym}:{end_ym}")

    await bot.send_document(
        cb.message.chat.id,
        types.BufferedInputFile(buf_doc.read(), filename=f"ADTI_{period_text}_hisobot.docx"),
        caption=doc_cap,
        reply_markup=zip_btn_kb.as_markup(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "rep_custom_interval")
async def ask_custom_interval(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("❌ Рухсат йўқ", show_alert=True)
        return

    await state.set_state(PeriodReportState.waiting_for_custom_interval)
    msg_prompt = (
        "✍️ <b>Ҳисобот учун исталган йил ёки ойни ёзиб юборинг:</b>\n\n"
        "Масалан:\n"
        "• <code>2025</code> (2025 йил тўлиқ ҳисоботи)\n"
        "• <code>2024</code> (2024 йил тўлиқ ҳисоботи)\n"
        "• <code>2026-04</code> (бир ой учун)\n"
        "• <code>2025-09 - 2026-05</code> (ўқув йили учун)\n\n"
        "<i>Бекор қилиш учун /cancel деб ёзинг.</i>"
    )
    await cb.message.edit_text(msg_prompt, parse_mode="HTML")
    await cb.answer()


@dp.message(PeriodReportState.waiting_for_custom_interval, F.text)
async def handle_custom_interval(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip()
    start_ym, end_ym = parse_user_interval(text)
    if not start_ym:
        await message.answer("⚠️ <b>Нотўғри формат!</b>\nИлтимос, <code>2025</code>, <code>2026-04</code> ёки <code>2025-09 - 2026-05</code> кўринишида ёзинг.", parse_mode="HTML")
        return

    period_text = start_ym if start_ym == end_ym else f"{start_ym} — {end_ym}"
    await state.clear()
    await message.answer(f"⏳ <b>{period_text} даври учун Excel ва Word ҳисоботлар тайёрланмоқда...</b>", parse_mode="HTML")

    summary_rows = await get_all_summary()
    detailed_rows = await get_all_detailed_entries()

    buf_xl = await generate_report_excel(summary_rows, detailed_rows, start_ym=start_ym, end_ym=end_ym)
    buf_doc = await generate_report_docx(summary_rows, detailed_rows, start_ym=start_ym, end_ym=end_ym)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf_xl.read(), filename=f"ADTI_{period_text}_hisobot.xlsx"),
        caption=f"✅ <b>АДТИ {period_text} даври Excel ҳисоботи (.xlsx)</b>",
        parse_mode="HTML"
    )
    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf_doc.read(), filename=f"ADTI_{period_text}_hisobot.docx"),
        caption=f"✅ <b>АДТИ {period_text} даври Word расмий ҳисоботи (.docx)</b>",
        parse_mode="HTML"
    )


# ─── HELPER: УЗУН МАТННИ ЧИРОЙЛИ ВА ХАВФСИЗ ЮБОРИШ (Telegram HTML) ──────────
def format_telegram_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'^(?:#{1,4})\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'^\s*[\*\-•]\s*', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-—_]{3,}\s*$', '━━━━━━━━━━━━━━━━━━━━━━━━━━━━', text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("• •", "• ")
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def send_long_text(chat_id: int, text: str, header: str = ""):
    clean_text = format_telegram_html(text)
    if not clean_text:
        clean_text = "Маълумот олинмади."

    paragraphs = clean_text.split("\n\n")
    chunks = []
    curr = f"{header}\n\n" if header else ""

    for p in paragraphs:
        if len(curr) + len(p) + 2 > 3800:
            if curr.strip():
                chunks.append(curr.strip())
            curr = p + "\n\n"
        else:
            curr += p + "\n\n"
    if curr.strip():
        chunks.append(curr.strip())

    for i, chunk in enumerate(chunks):
        try:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id, chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.3)


# ─── AI ТАҲЛИЛ: БАРЧА КАФЕДРАЛАР БЎЙИЧА (СВОДНЫЙ) ───────────────────────────
@dp.message(F.text.in_(["🤖 AI Сводный таҳлил", "🤖 AI Институт таҳлили", "/ai_summary"]))
async def ai_full_analysis_handler(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⚠️ Ушбу функция фақат маъмурият (Admin) учун очиқ.")
        return

    loading_msg = await message.answer(
        "🧠 <b>Gemini AI барча 65 та кафедра илмий кўрсаткичларини таҳлил қилмоқда...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <i>База маълумотлари ўқилмоқда ва расмий илмий таҳлил матни ёзилмоқда (5–15 сония)...</i>",
        parse_mode="HTML"
    )

    try:
        all_data = await get_all_summary()
        detailed_entries = await get_all_detailed_entries()
        analysis_text = await generate_full_analysis(all_data, detailed_entries)

        try:
            await loading_msg.delete()
        except Exception:
            pass

        header = (
            "🏛 <b>АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ</b>\n"
            "📊 <b>2026 ЙИЛ ИЛМИЙ ФАОЛИЯТ БЎЙИЧА AI ТАҲЛИЛ ХИСОБОТИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await send_long_text(message.chat.id, analysis_text, header=header)

    except Exception as e:
        logger.error(f"AI full analysis failed: {e}")
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await message.answer(f"❌ AI таҳлилини тайёрлашда хатолик: {e}")


# ─── AI ТАҲЛИЛ: БИТТА КАФЕДРА БЎЙИЧА ─────────────────────────────────────────
@dp.message(F.text.in_(["🤖 AI Илмий таҳлил", "🤖 AI Кафедра таҳлили", "/ai_dept"]))
async def ai_dept_analysis_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    if not dept:
        if is_admin:
            await message.answer(
                "👑 <b>Администратор учун:</b>\n"
                "Аниқ бир кафедра таҳлилини кўриш учун аввал кафедра паролини киритинг,\n"
                "ёки <b>🤖 AI Сводный таҳлил</b> тугмасини босинг.",
                parse_mode="HTML"
            )
        else:
            await message.answer("⚠️ Аввал кафедра паролини киритинг: /start")
        return

    loading_msg = await message.answer(
        f"🧠 <b>Gemini AI «{dept['name']}» илмий кўрсаткичларини таҳлил қилмоқда...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <i>Кафедра топширган ишлар ўрганилмоқда (5–15 сония)...</i>",
        parse_mode="HTML"
    )

    try:
        summary = await get_dept_summary(dept['id'])
        entries = await get_dept_entries(dept['id'], limit=50)

        analysis_text = await generate_dept_analysis(
            dept_name=dept['name'],
            head_name=dept.get('head_name', ''),
            summary=summary,
            entries=entries
        )

        try:
            await loading_msg.delete()
        except Exception:
            pass

        header = (
            f"📂 <b>{dept['name']}</b>\n"
            f"👤 Мудир: <b>{dept.get('head_name') or '—'}</b>\n"
            "📊 <b>2026 ЙИЛ ИЛМИЙ ФАОЛИЯТИНИНГ AI ТАҲЛИЛИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await send_long_text(message.chat.id, analysis_text, header=header)

    except Exception as e:
        logger.error(f"AI dept analysis failed: {e}")
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await message.answer(f"❌ Кафедра AI таҳлилини тайёрлашда хатолик: {e}")


# ─── ADMIN: СКАЧАТЬ СПИСОК ПАРОЛЕЙ ВСЕХ 65 КАФЕДР (ЗАЩИЩЕНО MASTER PIN) ─────
@dp.message(F.text.in_(["🔑 Кафедралар пароллари", "🔑 Кафедралар пароллари (Word)"]))
async def prompt_admin_pin_for_passwords(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()
    await state.set_state(AdminPinState.waiting_for_pin)
    await state.update_data(target_action="download_passwords")

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Бекор қилиш", callback_data="cancel")

    await message.answer(
        "🔒 <b>ХАВФСИЗЛИК ТЕКШИРУВИ (2FA Master PIN)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Барча 65 та кафедранинг махсус кириш пароллари ҳужжатини олиш учун <b>Мастер ПИН-кодни</b> киритинг:\n\n"
        "<i>(ПИН-кодни қуйида ёзиб юборинг)</i>",
        reply_markup=cancel_kb.as_markup(),
        parse_mode="HTML"
    )


@dp.message(AdminPinState.waiting_for_pin, F.text)
async def process_admin_pin(message: types.Message, state: FSMContext):
    if await check_menu_or_cancel(message, state):
        return

    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    entered_pin = message.text.strip()
    data = await state.get_data()
    action = data.get("target_action", "download_passwords")
    await state.clear()

    now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
    admin_name = message.from_user.full_name or f"Admin #{message.from_user.id}"

    if entered_pin != ADMIN_MASTER_PIN:
        if AUDIT_CHANNEL_ID:
            try:
                alert_text = (
                    f"🚨 <b>#SECURITY_ALERT — НОТЎҒРИ PIN-КОД ТЕРИЛДИ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Фойдаланувчи:</b> {html.escape(admin_name)} (ID: <code>{message.from_user.id}</code>)\n"
                    f"⚠️ <b>Ҳаракат:</b> Кафедралар паролларини очишга уриниш\n"
                    f"❌ <b>Терилган код:</b> <code>{html.escape(entered_pin)}</code>\n"
                    f"📅 <b>Вақт:</b> {now_str}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡 <i>Тизим маълумотлар берилишини тўхтатди.</i>"
                )
                await bot.send_message(AUDIT_CHANNEL_ID, alert_text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send security alert: {e}")

        await message.answer(
            "❌ <b>ХАТО ПИН-КОД!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Кириш пароллари ҳужжати очилмади.\n"
            "Ушбу хавфсизлик ҳодисаси тўғрисида раҳбариятнинг назорат каналига хабарнома юборилди.",
            reply_markup=main_kb(message.from_user.id),
            parse_mode="HTML"
        )
        return

    if action == "download_passwords":
        await message.answer("⏳ <b>ПИН-код тасдиқланди. Пароллар ҳужжати тайёрланмоқда...</b>", parse_mode="HTML")
        buf = await generate_codes_docx(DEPARTMENTS)

        await bot.send_document(
            message.chat.id,
            types.BufferedInputFile(buf.read(), filename="ADTI_Kafedralar_Parollari.docx"),
            caption="🔑 <b>АДТИ: Барча 65 та кафедра учун кириш пароллари (кодлари)</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Ушбу ҳужжатни чиқариб ёки мудирларга тарқатишингиз мумкин. Ҳар бир кафедра фақат ўз пароли орқали киради!",
            reply_markup=main_kb(message.from_user.id),
            parse_mode="HTML"
        )

        if AUDIT_CHANNEL_ID:
            try:
                log_text = (
                    f"🔐 <b>#MASTER_PIN — ПАРОЛЛАР МУВАФФАҚИЯТЛИ ЮКЛАНДИ</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Администратор:</b> {html.escape(admin_name)} (ID: <code>{message.from_user.id}</code>)\n"
                    f"📅 <b>Вақт:</b> {now_str}\n"
                    f"🛡 <i>Мастер ПИН-код тўғри терилди. Ҳужжат жўнатилди.</i>"
                )
                await bot.send_message(AUDIT_CHANNEL_ID, log_text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to log pin success: {e}")


# ─── GOOGLE SHEETS LIVE SYNC ────────────────────────────────────────────────
async def sync_sheets_background():
    """Фоновая синхронизация сводной таблицы с Google Sheets"""
    if not SHEET_WEBHOOK_URL:
        return
    try:
        rows = await get_all_summary()
        payload = []
        for r in rows:
            payload.append({
                "dept_id": r['id'],
                "dept_name": r['name'],
                "head_name": r.get('head_name') or '',
                "dsc": r.get('dsc') or 0, "phd": r.get('phd') or 0,
                "monography": r.get('monography') or 0, "patent": r.get('patent') or 0,
                "oak_uz": r.get('oak_uz') or 0, "oak_ru_if": r.get('oak_ru_if') or 0,
                "thesis_uz": r.get('thesis_uz') or 0, "thesis_foreign": r.get('thesis_foreign') or 0,
                "scopus_wos": r.get('scopus_wos') or 0,
                "rationalizer": r.get('rationalizer') or 0,
                "implementation": r.get('implementation') or 0,
                "conferences": r.get('conferences') or 0,
                "contracts": r.get('contracts') or 0,
                "grants": r.get('grants') or 0,
                "total": r.get('total') or 0,
                "action": "full_sync"
            })
        async with aiohttp.ClientSession() as session:
            await session.post(
                SHEET_WEBHOOK_URL,
                json={"action": "full_sync", "departments": payload},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True
            )
        logger.info("Google Sheets synchronized successfully in background.")
    except Exception as e:
        logger.warning(f"Failed to sync Google Sheets: {e}")


@dp.message(F.text == "📋 Google Таблица янгилаш")
async def force_sync_sheets(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    if not SHEET_WEBHOOK_URL:
        await message.answer("⚠️ SHEET_WEBHOOK_URL созланмаган.")
        return

    await message.answer("⏳ Барча маълумотлар Google Таблицага юборилмоқда...")
    try:
        await sync_sheets_background()
        await message.answer("✅ Google Таблицага муваффақиятли юборилди!\nБарча 65 та кафедра янгиланди.")
    except Exception as e:
        await message.answer(f"❌ Хатолик: {e}")


# ─── KEEP-ALIVE HTTP СЕРВЕР (БЕЗ ПРОБУЖДЕНИЯ POSTGRESQL!) ───────────────────
async def health_handler(request):
    """
    Возвращает статус бота из кэша в памяти за <1 мс.
    НЕ делает SQL-запросов к Neon PostgreSQL, чтобы не сжигать месячный лимит compute-часов!
    """
    try:
        hc = get_db_health_cache()
        return web.Response(
            text=json.dumps({
                "status": "ok",
                "bot": "ADTI Report Bot",
                "version": "2.4",
                "database": f"connected ({hc['engine']}: {hc['indicators']} indicators)",
                "engine": hc["engine"],
                "indicators": hc["indicators"],
                "ai_configured": bool(GEMINI_API_KEY)
            }, ensure_ascii=False),
            content_type="application/json"
        )
    except Exception as top_err:
        return web.Response(
            text=json.dumps({"status": "error", "error": str(top_err)}),
            content_type="application/json",
            status=500
        )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server started on port {port}")


# ─── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    await auto_sync_latest_backup_from_channel()
    logger.info("ADTI Bot v2.4 started. Polling...")

    asyncio.create_task(periodic_backup_loop())
    asyncio.create_task(sync_sheets_background())

    await start_web_server()
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
