import logging
import asyncio
import aiohttp
from aiohttp import web
import json
import os
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, ADMIN_IDS, FILES_DIR, SHEET_WEBHOOK_URL, AUDIT_CHANNEL_ID, ADMIN_MASTER_PIN
from database import (
    init_db, get_department, get_dept_by_user, get_dept_by_code,
    bind_user_to_dept, unbind_user, add_entry, get_dept_summary, get_all_summary,
    get_all_detailed_entries, get_dept_entries, delete_entry, clear_all_test_data,
    get_entries_by_category, get_files_for_zip, get_entry_by_id, update_entry_title,
    update_entry_full, DEPARTMENTS, INDICATORS, INDICATOR_LABELS
)
from report_gen import (
    generate_report_docx, generate_codes_docx, generate_report_excel,
    generate_files_zip
)
from ai_analysis import generate_dept_analysis, generate_full_analysis
from scopus_verifier import verify_article

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
_submission_count = 0


# ─── FSM STATES ─────────────────────────────────────────────────────────────
class AuthState(StatesGroup):
    waiting_for_code = State()

class AdminPinState(StatesGroup):
    waiting_for_pin = State()

class RestoreState(StatesGroup):
    waiting_for_db_file = State()

class EditTitleState(StatesGroup):
    waiting_for_new_title = State()

class AddEntry(StatesGroup):
    choose_category = State()
    enter_title     = State()
    enter_authors   = State()
    # Расширенные поля (собираются только для нужных категорий)
    enter_country       = State()   # Страна журнала (Scopus, WoS, хорижий ОАК)
    enter_journal       = State()   # Название журнала / Буюртмачи ташкилот
    enter_pub_date      = State()   # Год, выпуск, страницы / дата выхода / шартнома рақами
    enter_url           = State()   # Ссылка / DOI
    enter_authors_count = State()   # Количество авторов
    enter_specialty     = State()   # Шифр и название специальности (дисс., монография)
    enter_reg_number    = State()   # Рег. номер (патент)
    enter_publisher     = State()   # Издательство (монография)
    enter_venue         = State()   # Место и дата проведения (конференция/тезис)
    enter_amount        = State()   # Шартнома / Грант суммаси (млн сўмда)
    upload_file         = State()

# Маппинг категорий → список доп-шагов после enter_authors
# Каждый элемент = (state, prompt_text, field_name)
CATEGORY_EXTRA_STEPS = {
    "scopus_wos": [
        ("enter_country",       "🌍 <b>[3/6-қадам] Журнал нашр этилган давлат:</b>\n<i>(Масалан: Germany, USA, Switzerland, China, Iran)</i>"),
        ("enter_journal",       "📰 <b>[4/6-қадам] Илмий журнал номи:</b>\n<i>(Масалан: Journal of Nanostructures)</i>"),
        ("enter_pub_date",      "📅 <b>[5/6-қадам] Нашр санаси ва бетлари:</b>\n<i>(Масалан: 16.03.2026, 431-437 ёки Vol. 5, No. 2)</i>"),
        ("enter_url",           "🔗 <b>[6/6-қадам] Мақола ҳаволаси (DOI / URL):</b>\n<i>(Масалан: https://doi.org/10.1016/...)</i>"),
        ("enter_authors_count", "👥 <b>Муаллифлар умумий сони:</b>\n<i>(Масалан: 5)</i>"),
    ],
    "oak_ru_if": [
        ("enter_country",       "🌍 <b>[3/6-қадам] Журнал нашр этилган давлат:</b>\n<i>(Масалан: Россия, Германия)</i>"),
        ("enter_journal",       "📰 <b>[4/6-қадам] Илмий журнал номи:</b>\n<i>(Масалан: Кардиология ва терапия)</i>"),
        ("enter_pub_date",      "📅 <b>[5/6-қадам] Нашр йили, сони ва бетлари:</b>\n<i>(Масалан: Vol. 6 No. 02 (2026))</i>"),
        ("enter_url",           "🔗 <b>[6/6-қадам] Мақола интернет ҳаволаси (URL/DOI):</b>"),
        ("enter_authors_count", "👥 <b>Муаллифлар сони:</b>\n<i>(Масалан: 3)</i>"),
    ],
    "oak_uz": [
        ("enter_journal",   "📰 <b>[3/5-қадам] Илмий журнал номи:</b>\n<i>(Масалан: Профилактик тиббиёт ва саломатлик)</i>"),
        ("enter_pub_date",  "📅 <b>[4/5-қадам] Нашр санаси ёки йили/сони:</b>\n<i>(Масалан: 05.02.2026 ёки 2026, №1)</i>"),
        ("enter_url",       "🔗 <b>[5/5-қадам] Мақоланинг интернет ҳаволаси (URL):</b>"),
    ],
    "dsc": [
        ("enter_specialty", "🎓 <b>[3/4-қадам] Ихтисослик шифри ва номи:</b>\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",  "📅 <b>[4/4-қадам] ОАК тасдиқлаган сана:</b>\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "phd": [
        ("enter_specialty", "🎓 <b>[3/4-қадам] Ихтисослик шифри ва номи:</b>\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",  "📅 <b>[4/4-қадам] ОАК тасдиқлаган сана:</b>\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "monography": [
        ("enter_publisher", "🏢 <b>[3/4-қадам] Нашриёт номи:</b>\n<i>(Масалан: Fan va texnologiya)</i>"),
        ("enter_pub_date",  "📅 <b>[4/4-қадам] Нашр йили ва санаси:</b>\n<i>(Масалан: 2026)</i>"),
    ],
    "patent": [],   # custom flow: тури → санаси → мавзуси → муаллиф → файл
    "thesis_foreign": [
        ("enter_venue",    "📍 <b>[3/3-қадам] Анжуман ўтказилган жой ва сана:</b>\n<i>(Масалан: Москва, 15.04.2026)</i>"),
    ],
    "thesis_uz": [
        ("enter_venue",    "📍 <b>[3/3-қадам] Анжуман ўтказилган жой ва сана:</b>\n<i>(Масалан: Тошкент, 10.05.2026)</i>"),
    ],
    "conferences": [
        ("enter_country",       "📋 <b>[2/6-қадам] Анжуман шакли:</b>\n<i>(Масалан: Онлайн / Офлайн)</i>"),
        ("enter_journal",       "🌐 <b>[3/6-қадам] Анжуман қулами:</b>\n<i>(Масалан: Маҳаллий / Республика / Халқаро)</i>"),
        ("enter_pub_date",      "📅 <b>[4/6-қадам] Санаси:</b>\n<i>(Масалан: 15.03.2026)</i>"),
        ("enter_authors_count", "👥 <b>[5/6-қадам] Қатнашувчилар сони:</b>\n<i>(Масалан: 45)</i>"),
    ],
    "contracts": [
        ("enter_amount",   "💰 <b>[3/5-қадам] Шартнома суммаси (млн сўмда):</b>\n<i>(Масалан: 25 ёки 50.5)</i>"),
        ("enter_journal",  "🏢 <b>[4/5-қадам] Буюртмачи корхона / ташкилот номи:</b>\n<i>(Масалан: «Андижон биофарм» МЧЖ)</i>"),
        ("enter_pub_date", "📅 <b>[5/5-қадам] Шартнома рақами ва санаси:</b>\n<i>(Масалан: №14/2026, 10.03.2026)</i>"),
    ],
    "grants": [
        ("enter_amount",   "💰 <b>[3/5-қадам] Грант суммаси (млн сўм ёки валютада):</b>\n<i>(Масалан: 180 млн сўм ёки 25,000 USD)</i>"),
        ("enter_journal",  "🏢 <b>[4/5-қадам] Молиялаштирувчи фонд / ташкилот:</b>\n<i>(Масалан: Инновацион ривожланиш агентлиги)</i>"),
        ("enter_pub_date", "📅 <b>[5/5-қадам] Лойиҳа муддати ва санаси:</b>\n<i>(Масалан: 2026-2027 йй.)</i>"),
    ],
}

# Маппинг state-имени → ключ в FSM data
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


# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb(user_id: int):
    """Эргономичная клавиатура с понятными иконками"""
    if user_id in ADMIN_IDS:
        buttons = [
            [KeyboardButton(text="➕ Ҳисобот қўшиш")],
            [KeyboardButton(text="🏛 Сводный ҳисобот"), KeyboardButton(text="📊 Кафедра статистикаси")],
            [KeyboardButton(text="🤖 AI Сводный таҳлил"), KeyboardButton(text="🤖 AI Кафедра таҳлили")],
            [KeyboardButton(text="📊 Excel ҳисобот (.xlsx)"), KeyboardButton(text="📥 Word ҳисобот (.docx)")],
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


def skip_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Файлсиз давом этиш", callback_data="skip_file")
    kb.button(text="❌ Бекор қилиш", callback_data="cancel")
    kb.adjust(1)
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
        import datetime
        now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
        caption = (
            f"🔔 <b>ЯНГИ ИЛМИЙ ҲИСОБОТ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра #{dept_id}:</b> {dept_name}\n"
            f"👤 <b>Мудир:</b> {head_name or '—'}\n"
            f"📌 <b>Категория:</b> {category_label}\n"
            f"📝 <b>Иш номи:</b> {title or '—'}\n"
            f"👥 <b>Муаллифлар:</b> {authors or '—'}"
            f"{extra_lines}\n"
            f"📅 <b>Вақт:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        if file_id:
            # Отправляем документ или фото прямо в канал с подписью
            try:
                await bot.send_document(AUDIT_CHANNEL_ID, file_id, caption=caption, parse_mode="HTML")
            except Exception:
                await bot.send_message(AUDIT_CHANNEL_ID, caption, parse_mode="HTML")
        else:
            await bot.send_message(AUDIT_CHANNEL_ID, caption, parse_mode="HTML")
        logger.info(f"Published audit report #{entry_id} to channel {AUDIT_CHANNEL_ID}")
    except Exception as e:
        logger.warning(f"Failed to post to audit channel: {e}")


async def notify_delete_to_audit(dept_name: str, entry_id: int):
    """Уведомляет канал руководства об удалении записи"""
    if not AUDIT_CHANNEL_ID:
        return
    try:
        text = (
            f"🗑 <b>ИЛМИЙ ҲИСОБОТ ЎЧИРИЛДИ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра:</b> {dept_name}\n"
            f"⚠️ <i>Ушбу ёзув масъул ходим томонидан базадан ўчирилди.</i>"
        )
        await bot.send_message(AUDIT_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to notify delete to channel: {e}")


async def notify_edit_to_audit(dept_name: str, entry_id: int, new_title: str):
    """Уведомляет канал руководства о редактировании названия записи"""
    if not AUDIT_CHANNEL_ID:
        return
    try:
        import datetime
        now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
        text = (
            f"✏️ <b>ИЛМИЙ ҲИСОБОТ НОМИ ТАҲРИРЛАНДИ (ID: #{entry_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 <b>Кафедра:</b> {dept_name}\n"
            f"📝 <b>Янги номи:</b> {new_title}\n"
            f"📅 <b>Вақт:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(AUDIT_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to notify edit to channel: {e}")


# ─── DATABASE BACKUP & RESTORE ──────────────────────────────────────────────
async def backup_database_to_channel(reason: str = "автоматик"):
    """Каналга PostgreSQL маълумотларининг бэкапини жўнатади (CSV формат)"""
    if not AUDIT_CHANNEL_ID:
        return
    try:
        import datetime, io, csv
        from database import get_pg_pool, get_all_detailed_entries, DB_PATH

        now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        now_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # PostgreSQL: экспортируем как CSV
        pool = await get_pg_pool()
        if pool:
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
                caption=f"💾 <b>#PG_BACKUP — АДТИ PostgreSQL бэкапи</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📅 <b>Вақт:</b> {now_str}\n"
                        f"📌 <b>Ҳолат:</b> {reason}\n"
                        f"📊 <b>Жами ёзувлар:</b> {len(rows)} та\n"
                        f"🛡 <i>Барча 65 кафедра маълумотлари ҳавфсиз сақланди.</i>",
                parse_mode="HTML"
            )
        else:
            # Fallback: SQLite файл
            if DB_PATH.exists():
                with open(DB_PATH, "rb") as f:
                    content = f.read()
                await bot.send_document(
                    AUDIT_CHANNEL_ID,
                    types.BufferedInputFile(content, filename=f"adti_sqlite_backup_{now_tag}.db"),
                    caption=f"💾 <b>#SQLITE_BACKUP — АДТИ SQLite бэкапи</b>\n"
                            f"📅 <b>Вақт:</b> {now_str}\n📌 <b>Ҳолат:</b> {reason}",
                    parse_mode="HTML"
                )
        logger.info(f"Database backup sent to channel: reason={reason}")
    except Exception as e:
        logger.warning(f"Failed to send database backup to channel: {e}")


async def periodic_backup_loop():
    """Ҳар 3 соатда автоматик тарзда базанинг тўлиқ бэкапини назорат каналига юборади"""
    while True:
        await asyncio.sleep(10800)  # 3 соат
        await backup_database_to_channel(reason="3 соатлик режали бэкап")


# ─── ADMIN: СКАЧАТЬ БЭКАП БАЗЫ ДАННЫХ (POSTGRESQL CSV) ──────────────────────
@dp.message(Command("backup_db"))
async def cmd_backup_db(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    import datetime, io, csv
    from database import get_all_detailed_entries

    await message.answer("⏳ <b>PostgreSQL маълумотлар базаси бэкапи тайёрланмоқда...</b>", parse_mode="HTML")
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
            caption=f"💾 <b>АДТИ: PostgreSQL тўлиқ база бэкапи (.csv)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Жами ёзувлар:</b> {len(rows)} та\n"
                    f"🛡 <i>Барча 65 та кафедра ҳисоботлари, файл дескрипторлари ва маълумотлари.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Бэкап яратишда хатолик: {e}")


# ─── ADMIN: ВОССТАНОВИТЬ БАЗУ ДАННЫХ ────────────────────────────────────────
@dp.message(Command("restore_db"))
async def cmd_restore_db_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "⚠️ <b>ДИҚҚАТ: Базани қайта тиклаш (Restore)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Илтимос, тикламоқчи бўлган <b>.db</b> файлингизни юборинг (документ сифатида).\n\n"
        "Ёки бекор қилиш учун /cancel деб ёзинг.",
        parse_mode="HTML"
    )
    await state.set_state(RestoreState.waiting_for_db_file)


@dp.message(RestoreState.waiting_for_db_file, F.document)
async def process_restore_db(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    doc = message.document
    if not doc.file_name.endswith(".db"):
        await message.answer("❌ Фақат <b>.db</b> кенгайтмали файл юборинг!", parse_mode="HTML")
        return

    try:
        from config import DB_PATH
        tg_file = await bot.get_file(doc.file_id)
        f_stream = await bot.download_file(tg_file.file_path)
        content = f_stream.read() if hasattr(f_stream, 'read') else f_stream.getvalue()

        # Создаем временную копию старой базы
        if DB_PATH.exists():
            import shutil
            shutil.copyfile(DB_PATH, str(DB_PATH) + ".bak")

        # Записываем новую базу
        with open(DB_PATH, "wb") as f:
            f.write(content)

        await init_db()
        await state.clear()
        await message.answer("✅ <b>База муваффақиятли қайта тикланди!</b>\nБарча маълумотлар янгиланди.", parse_mode="HTML")
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
            f"👤 <b>Мудир:</b> {dept['head_name'] or '—'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Керакли бўлимни танланг 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    elif user_id in ADMIN_IDS:
        await message.answer(
            f"👑 <b>Ассалому алайкум, Ҳурматли Администратор!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>АДТИ Илмий ҳисоботлар бошқарув панели</b>\n"
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


# ─── АВТОРИЗАЦИЯ ПО КОДУ ────────────────────────────────────────────────────
@dp.message(AuthState.waiting_for_code, F.text)
async def process_dept_code(message: types.Message, state: FSMContext):
    code_input = message.text.strip()
    dept = await get_dept_by_code(code_input)

    if not dept:
        await message.answer(
            "❌ <b>Нотўғри парол!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Илтимос, кодни қайта текшириб тўғри киритинг ёки Илмий бўлимга мурожаат қилинг.",
            parse_mode="HTML"
        )
        return

    # Привязываем пользователя к кафедре
    await bind_user_to_dept(message.from_user.id, dept['id'])
    await state.clear()

    await message.answer(
        f"✅ <b>Кафедра тасдиқланди!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Кафедра:</b> {dept['name']}\n"
        f"👤 <b>Мудир:</b> {dept['head_name'] or '—'}\n"
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


# ─── ДОБАВЛЕНИЕ ЗАПИСИ: ШАГ 1 — КАТЕГОРИЯ ───────────────────────────────────
@dp.message(F.text.in_(["➕ Ҳисобот қўшиш", "📁 Ҳисобот қўшиш"]))
async def add_report_start(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
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
    head_name = dept['head_name'] or ''

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


@dp.callback_query(F.data.startswith("cat_"), AddEntry.choose_category)
async def choose_category(cb: types.CallbackQuery, state: FSMContext):
    cat = cb.data[4:]
    await state.update_data(category=cat)
    label = INDICATOR_LABELS.get(cat, cat)

    if cat == "patent":
        await cb.message.edit_text(
            f"✅ <b>{label}</b> танланди.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>[1/4-қадам] Патент тури:</b>\n"
            "<i>(FAP / IAP / DGU)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.enter_country)
    elif cat == "conferences":
        await cb.message.edit_text(
            f"✅ <b>{label}</b> танланди.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 <b>[1/6-қадам] Анжуман мавзуси:</b>\n"
            "<i>(Масалан: «Тиббиётда сунъий интеллект» мавзусидаги анжуман)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.enter_title)
    else:
        await cb.message.edit_text(
            f"✅ <b>{label}</b> танланди.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 <b>[1/N-қадам] Иш номини киритинг:</b>\n"
            "<i>(Мақола, монография, патент ёки диссертация номи)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.enter_title)
    await cb.answer()


# ─── ШАГ 2 — НАЗВАНИЕ ────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_title, F.text)
async def enter_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    data = await state.get_data()
    cat = data.get('category', '')
    if cat == "patent":
        authors_prompt = (
            "👤 <b>[4/4-қадам] Муаллиф (муаллифлар):</b>\n"
            "<i>(Масалан: Каримов Б.Т., Ниёзов А.Р.)</i>"
        )
    elif cat == "conferences":
        authors_prompt = (
            "👤 <b>[2/6-қадам] Масъул шахс (ФИО ёки лавозими):</b>\n"
            "<i>(Масалан: Кафедра мудири Тошматов А.А. ёки АДТИ илмий бўлими)</i>"
        )
    else:
        authors_prompt = (
            "👥 <b>[2/N-қадам] Муаллифларнинг Ф.И.Ш.ни киритинг:</b>\n"
            "<i>(Масалан: Узбекова Нелли Рафиковна, Хужамбердиев М.)</i>"
        )
    await message.answer(authors_prompt, parse_mode="HTML")
    await state.set_state(AddEntry.enter_authors)


# ─── HELPER: перейти к следующему доп-шагу для категории ────────────────────
async def go_to_next_extra_step(message: types.Message, state: FSMContext):
    """Определяет следующий шаг по CATEGORY_EXTRA_STEPS и переходит к нему.
    Если доп-шагов не осталось — переходит к загрузке файла."""
    data = await state.get_data()
    cat = data.get('category', '')
    steps = CATEGORY_EXTRA_STEPS.get(cat, [])
    step_index = data.get('extra_step_index', 0)

    if step_index < len(steps):
        state_name, prompt = steps[step_index]
        await state.update_data(extra_step_index=step_index + 1)
        target_state = getattr(AddEntry, state_name)
        await state.set_state(target_state)
        await message.answer(prompt, parse_mode="HTML")
    else:
        # Все доп-шаги пройдены — запрашиваем файл
        if cat == "conferences":
            file_prompt = (
                "📎 <b>[6/6-қадам] Асос:</b>\n"
                "<i>(Расм ёки баённома PDF кўринишида юборинг)</i>\n\n"
                "Ёки 👇 тугмани босинг:"
            )
        else:
            file_prompt = (
                "📎 <b>Тасдиқловчи ҳужжатни юборинг</b>\n"
                "<i>(PDF, Word ёки фото скан)</i>\n\n"
                "Ёки 👇 тугмани босинг:"
            )
        await message.answer(
            file_prompt,
            reply_markup=skip_kb(),
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.upload_file)


# ─── УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДОП-ШАГОВ ────────────────────────────────────
async def handle_extra_step(message: types.Message, state: FSMContext, field_key: str):
    """Сохраняет значение текущего доп-шага и переходит к следующему."""
    await state.update_data(**{field_key: message.text.strip()})
    await go_to_next_extra_step(message, state)


@dp.message(AddEntry.enter_country, F.text)
async def step_country(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('category') == 'patent':
        await state.update_data(country=msg.text.strip())
        await msg.answer(
            "📅 <b>[2/4-қадам] Олинган санаси:</b>\n"
            "<i>(Масалан: 23.02.2026)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.enter_pub_date)
    else:
        await handle_extra_step(msg, state, 'country')

@dp.message(AddEntry.enter_journal, F.text)
async def step_journal(msg: types.Message, state: FSMContext):
    await handle_extra_step(msg, state, 'journal_name')

@dp.message(AddEntry.enter_pub_date, F.text)
async def step_pub_date(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('category') == 'patent':
        await state.update_data(pub_date=msg.text.strip())
        await msg.answer(
            "📝 <b>[3/4-қадам] Мавзуси:</b>\n"
            "<i>(Масалан: Юрак касалликларини эрта аниқлаш усули)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AddEntry.enter_title)
    else:
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


# ─── ШАГ 3 — АВТОРЫ ──────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_authors, F.text)
async def enter_authors(message: types.Message, state: FSMContext):
    await state.update_data(authors=message.text.strip(), extra_step_index=0)
    await go_to_next_extra_step(message, state)


# ─── ШАГ 4 — ФАЙЛ (документ) ─────────────────────────────────────────────────
@dp.message(AddEntry.upload_file, F.document)
async def receive_doc(message: types.Message, state: FSMContext):
    # Читаем dept_id из FSM state (более надёжно, чем из DB)
    data = await state.get_data()
    dept_id = data.get('dept_id')
    if not dept_id:
        dept = await get_dept_by_user(message.from_user.id)
        dept_id = dept['id'] if dept else 0

    f = message.document
    file_id = f.file_id
    fname = f.file_name or f"doc_{file_id}.bin"

    dept_dir = FILES_DIR / str(dept_id)
    dept_dir.mkdir(parents=True, exist_ok=True)
    dest = dept_dir / fname

    file_obj = await bot.get_file(file_id)
    await bot.download_file(file_obj.file_path, destination=str(dest))
    await save_entry(message, state, file_path=str(dest), file_id=file_id)


# ─── ШАГ 4 — ФАЙЛ (фото) ─────────────────────────────────────────────────────
@dp.message(AddEntry.upload_file, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    # Читаем dept_id из FSM state (более надёжно, чем из DB)
    data = await state.get_data()
    dept_id = data.get('dept_id')
    if not dept_id:
        dept = await get_dept_by_user(message.from_user.id)
        dept_id = dept['id'] if dept else 0

    f = message.photo[-1]
    file_id = f.file_id
    fname = f"photo_{file_id}.jpg"

    dept_dir = FILES_DIR / str(dept_id)
    dept_dir.mkdir(parents=True, exist_ok=True)
    dest = dept_dir / fname

    file_obj = await bot.get_file(file_id)
    await bot.download_file(file_obj.file_path, destination=str(dest))
    await save_entry(message, state, file_path=str(dest), file_id=file_id)


# ─── ШАГ 4 — ПРОПУСТИТЬ ФАЙЛ ─────────────────────────────────────────────────
@dp.callback_query(F.data == "skip_file", AddEntry.upload_file)
async def skip_file(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await save_entry(cb.message, state, file_path='', file_id='', user_id=cb.from_user.id)
    await cb.answer()


# ─── ОТМЕНА ──────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "cancel")
async def cancel_action(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Бекор қилинди.")
    await cb.message.answer("Асосий меню 👇", reply_markup=main_kb(cb.from_user.id))
    await cb.answer()


# ─── СОХРАНЕНИЕ ЗАПИСИ ──────────────────────────────────────────────────────
async def save_entry(message: types.Message, state: FSMContext,
                     file_path: str, file_id: str, user_id: int = None):
    if user_id is None:
        user_id = message.from_user.id

    data = await state.get_data()
    dept_id = data.get('dept_id')
    dept_name = data.get('dept_name')
    head_name = data.get('head_name', '')

    if not dept_id:
        dept = await get_dept_by_user(user_id)
        if dept:
            dept_id = dept['id']
            dept_name = dept['name']
            head_name = dept['head_name'] or ''
        else:
            dept_id = 15
            dept_name = "Анатомия ва клиник анатомия кафедраси"
            head_name = "Кахаров Зафар Абдурахмонович"

    await state.clear()

    cat            = data.get('category', '')
    title          = data.get('title', '')
    authors        = data.get('authors', '')
    country        = data.get('country', '')
    journal_name   = data.get('journal_name', '')
    pub_date       = data.get('pub_date', '')
    url            = data.get('url', '')
    authors_count  = data.get('authors_count', '')
    specialty      = data.get('specialty', '')
    reg_number     = data.get('reg_number', '')
    publisher      = data.get('publisher', '')
    amount         = data.get('amount', '')

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
        # ── РЕЖИМ РЕДАКТИРОВАНИЯ: UPDATE существующей записи ──
        await update_entry_full(editing_id, entry_data)
        entry_id = editing_id
        is_edit = True
    else:
        # ── РЕЖИМ СОЗДАНИЯ: INSERT новой записи ──
        entry_id = await add_entry(
            dept_id=dept_id, category=cat, title=title, authors=authors, year=2026,
            doi=doi_val,
            file_path=file_path, file_id=file_id,
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
        "has_file": bool(file_path),
        "file_name": Path(file_path).name if file_path else "",
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

    # Аудит-канал: қўшимча майдонлар
    extra_lines = ""
    if cat == "patent":
        if country:   extra_lines += f"\n📋 <b>Патент тури:</b> {country}"
        if pub_date:  extra_lines += f"\n📅 <b>Олинган санаси:</b> {pub_date}"
    elif cat == "conferences":
        if country:       extra_lines += f"\n📋 <b>Шакли:</b> {country}"
        if journal_name:  extra_lines += f"\n🌐 <b>Қулами:</b> {journal_name}"
        if pub_date:      extra_lines += f"\n📅 <b>Санаси:</b> {pub_date}"
        if authors_count: extra_lines += f"\n👥 <b>Қатнашувчилар сони:</b> {authors_count}"
    else:
        if country:       extra_lines += f"\n🌍 <b>Давлат:</b> {country}"
        if journal_name:  extra_lines += f"\n📰 <b>Журнал/Буюртмачи:</b> {journal_name}"
        if pub_date:      extra_lines += f"\n📅 <b>Сана/Нашр:</b> {pub_date}"
        if url:           extra_lines += f"\n🔗 <b>URL:</b> {url}"
        if specialty:     extra_lines += f"\n🎓 <b>Ихтисослик:</b> {specialty}"
        if reg_number:    extra_lines += f"\n🔢 <b>Рег. рақам:</b> {reg_number}"
        if publisher:     extra_lines += f"\n🏢 <b>Нашриёт:</b> {publisher}"
        if amount:        extra_lines += f"\n💰 <b>Суммаси:</b> {amount} млн сўм"
        if authors_count: extra_lines += f"\n👥 <b>Муаллифлар сони:</b> {authors_count}"

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

    global _submission_count
    _submission_count += 1
    if _submission_count % 30 == 0:
        asyncio.create_task(backup_database_to_channel(reason=f"30 та янги ҳисобот тўпланди (#{entry_id})"))

    summary = await get_dept_summary(dept_id)
    label = INDICATOR_LABELS.get(cat, cat)
    total_cat = summary.get(cat, 0)
    has_file_str = "📎 Ҳужжат бириктирилди" if file_path or file_id else "📝 Файлсиз сақланди"
    header = "✏️ <b>ҲИСОБОТ МУВАФФАҚИЯТЛИ ЯНГИЛАНДИ!</b>" if is_edit else "✅ <b>ҲИСОБОТ МУВАФФАҚИЯТЛИ САҚЛАНДИ!</b>"

    await message.answer(
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📑 <b>Ҳисобот ID:</b> #{entry_id}\n"
        f"📂 <b>Кафедра:</b> {dept_name}\n"
        f"📌 <b>Категория:</b> {label}\n"
        f"📝 <b>Иш номи:</b> {title}\n"
        f"👥 <b>Муаллиф(лар):</b> {authors}\n"
        f"📄 <b>Ҳолати:</b> {has_file_str}{api_verify_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Ушбу йўналишдаги жами ишлар: <b>{total_cat} та</b>",
        reply_markup=main_kb(user_id),
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
        f"👤 <b>Мудир:</b> {dept['head_name'] or '—'}\n"
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

    # Муаллифлар рўйхати (шаффофлик учун)
    author_counts = {}
    for e in entries:
        auth_str = (e.get('authors') or '').strip()
        for a in auth_str.replace(";", ",").split(","):
            ac = a.strip()
            if ac and len(ac) > 2 and ac.lower() not in ["va boshqalar", "et al", "—"]:
                author_counts[ac] = author_counts.get(ac, 0) + 1

    if author_counts:
        top_auth = sorted(author_counts.items(), key=lambda x: -x[1])
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n👥 <b>Иш топширган муаллифлар (шаффофлик):</b>")
        for a_name, cnt in top_auth:
            lines.append(f"  • <b>{a_name}</b> — {cnt} та иш")
    # Отправка с разбивкой на части (лимит Telegram = 4096 символов)
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


# ─── ПРОСМОТР И УДАЛЕНИЕ СВОИХ ЗАПИСЕЙ ──────────────────────────────────────
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
            "ёки <b>🏛 Сводный ҳисобот</b> тугмасини босинг.",
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
        f"<i>(Агар хато киритган бўлсангиз, 🗑 тугмани босиб ўчиришингиз мумкин)</i>",
        parse_mode="HTML"
    )

    for e in entries:
        cat_lbl = INDICATOR_LABELS.get(e['category'], e['category'])
        has_f = "📎 Ҳужжат бор" if e['file_path'] or e['file_id'] else "📝 Файлсиз"
        title_str = e['title'].strip() if e['title'] and e['title'].strip() and e['title'].strip() != '—' else "⚠️ <i>(Мавзу киритилмаган)</i>"
        text = (
            f"┌ 📑 <b>Ҳисобот #{e['id']}</b>\n"
            f"├ 📌 <b>Категория:</b> {cat_lbl}\n"
            f"├ 📝 <b>Иш номи:</b> {title_str}\n"
            f"├ 👤 <b>Муаллиф:</b> {e['authors'] or '—'}\n"
            f"├ 📅 <b>Сана:</b> {str(e['created_at'])[:16]}\n"
            f"└ 📄 <b>Ҳолати:</b> {has_f}"
        )
        row_kb = InlineKeyboardBuilder()
        row_kb.button(text="✏️ Таҳрирлаш (барча майдонлар)", callback_data=f"edit_entry_{e['id']}")
        if e['file_id']:
            row_kb.button(text="📥 Ҳужжатни кўриш", callback_data=f"getfile_{e['id']}")
        row_kb.button(text=f"🗑 Ўчириш", callback_data=f"del_entry_{e['id']}")
        row_kb.adjust(1)
        await message.answer(text, reply_markup=row_kb.as_markup(), parse_mode="HTML")


# ─── ПОЛНОЕ РЕДАКТИРОВАНИЕ ЗАПИСИ ───────────────────────────────────────────
@dp.callback_query(F.data.startswith("edit_entry_"))
async def start_edit_entry_full(cb: types.CallbackQuery, state: FSMContext):
    """Запускает полный повторный ввод всех полей записи"""
    entry_id = int(cb.data.replace("edit_entry_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    entry = await get_entry_by_id(entry_id)
    if not entry:
        await cb.answer("❌ Ҳисобот топилмади", show_alert=True)
        return

    if not is_admin and dept and entry['dept_id'] != dept['id']:
        await cb.answer("❌ Бу ҳисоботни таҳрирлаш учун рухсатингиз йўқ", show_alert=True)
        return

    cat = entry['category']
    cat_lbl = INDICATOR_LABELS.get(cat, cat)
    current_title = entry['title'] or '—'

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

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Бекор қилиш", callback_data="cancel")

    await cb.message.answer(
        f"✏️ <b>#{entry_id}-РАҚАМЛИ ҲИСОБОТНИ ТАҲРИРЛАШ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Кафедра:</b> {entry.get('dept_name', '')}\n"
        f"📌 <b>Бўлим:</b> {cat_lbl}\n"
        f"📝 <b>Ҳозирги номи:</b> {current_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⬇️ Барча майдонларни қайта киритинг. Файлни ҳам алмаштириш мумкин.\n\n"
        f"📝 <b>[1/N-қадам] Иш номини (мавзусини) киритинг:</b>\n"
        f"<i>(Ёки аввалгисини: {current_title})</i>",
        reply_markup=cancel_kb.as_markup(),
        parse_mode="HTML"
    )
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
        asyncio.create_task(backup_database_to_channel(reason=f"Ўчирилди: #{entry_id} ({dept_name})"))
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
    """Инлайн-клавиатура выбора категории для просмотра файлов"""
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

    # Админ без кафедры видит всё; остальные — только свою
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
        title = e['title'] or '—'
        authors = e['authors'] or '—'
        dept_name = e['dept_name'] or '—'
        pub_date = e['pub_date'] or ''
        journal = e['journal_name'] or ''
        reg_num = e['reg_number'] or ''
        specialty = e['specialty'] or ''
        country = e['country'] or ''
        amount = e.get('amount') or ''

        details = []
        if country:   details.append(f"├ 🌍 <b>Давлат:</b> {country}")
        if journal:   details.append(f"├ 📰 <b>Журнал/Буюртмачи:</b> {journal}")
        if amount:    details.append(f"├ 💰 <b>Суммаси:</b> {amount} млн сўм")
        if pub_date:  details.append(f"├ 📅 <b>Сана/Бет:</b> {pub_date}")
        if reg_num:   details.append(f"├ 🔢 <b>Рег. рақам:</b> {reg_num}")
        if specialty: details.append(f"├ 🎓 <b>Ихтисослик:</b> {specialty}")
        det_str = ("\n" + "\n".join(details)) if details else ""

        title_str = title.strip() if title and title.strip() and title.strip() != '—' else "⚠️ <i>(Мавзу киритилмаган)</i>"
        card = (
            f"┌ 📑 <b>Ҳисобот #{e['id']}</b>\n"
            f"├ 📂 <b>Кафедра:</b> {dept_name}\n"
            f"├ 📝 <b>Иш номи:</b> {title_str}\n"
            f"├ 👥 <b>Муаллиф:</b> {authors}"
            f"{det_str}\n"
            f"└ 📅 <b>Вақт:</b> {str(e['created_at'])[:16]}"
        )

        card_kb = InlineKeyboardBuilder()
        card_kb.button(text="✏️ Таҳрирлаш (барча майдонлар)", callback_data=f"edit_entry_{e['id']}")
        if e['file_id']:
            card_kb.button(text="📥 Ҳужжатни юклаб олиш (PDF/Скан)", callback_data=f"getfile_{e['id']}")
        card_kb.adjust(1)
        await cb.message.answer(card, reply_markup=card_kb.as_markup(), parse_mode="HTML")

    await cb.answer()


@dp.callback_query(F.data.startswith("getfile_"))
async def send_entry_file(cb: types.CallbackQuery):
    """Отправляет файл по file_id из Telegram"""
    entry_id = int(cb.data.replace("getfile_", ""))
    user_id = cb.from_user.id
    dept = await get_dept_by_user(user_id)
    is_admin = user_id in ADMIN_IDS

    # Используем PostgreSQL через database.py
    from database import get_pg_pool, DB_PATH
    pool = await get_pg_pool()
    row = None

    if pool:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT file_id, dept_id, title FROM indicators WHERE id = $1",
                entry_id
            )
            row = dict(r) if r else None
    else:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT file_id, dept_id, title FROM indicators WHERE id = ?",
                (entry_id,)
            )
            r = await cur.fetchone()
            if r:
                row = dict(r)

    if not row:
        await cb.answer("❌ Ёзув топилмади", show_alert=True)
        return

    if not is_admin and dept and row['dept_id'] != dept['id']:
        await cb.answer("❌ Бу файлга кириш рухсати йўқ", show_alert=True)
        return

    if not row['file_id']:
        await cb.answer("❌ Файл сақланмаган", show_alert=True)
        return

    try:
        await cb.answer("⏳ Файл юборилмоқда...")
        await bot.send_document(
            cb.message.chat.id,
            row['file_id'],
            caption=f"📎 <b>{row['title'] or 'Ҳужжат'}</b>\n<i>ID: #{entry_id}</i>",
            parse_mode="HTML"
        )
    except Exception:
        try:
            await bot.send_photo(
                cb.message.chat.id,
                row['file_id'],
                caption=f"📎 <b>{row['title'] or 'Скан'}</b>\n<i>ID: #{entry_id}</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            await cb.message.answer(f"❌ Файлни юборишда хатолик: {e}")


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
                f"<b>{r['id']}. {name_short}</b>\n"
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


# ─── ADMIN: СКАЧАТЬ ZIP АРХИВ ВСЕХ ФАЙЛОВ ПО ПАПКАМ ──────────────────────
@dp.message(F.text.in_(["📦 Файллар ZIP архиви", "📦 Файллар ZIP архиви (.zip)"]))
async def prompt_zip_download(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    kb = InlineKeyboardBuilder()
    # Добавляем каждую категорию из INDICATORS как отдельный ZIP
    for key, label in INDICATORS:
        kb.button(text=f"{label} (.zip)", callback_data=f"zip_cat_{key}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="❌ Бекор қилиш", callback_data="cancel"))

    await message.answer(
        "📦 <b>Файллар ZIP архивини юклаб олиш</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Telegram 50 MB лимити сабабли барча файллар бирлаштирилмайди.</i>\n"
        "Ҳар бир категория алоҳида ZIP архив сифатида юкланади.\n\n"
        "Керакли бўлимни танланг 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("zip_"))
async def process_zip_download(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Рухсат йўқ", show_alert=True)
        return

    zip_type = cb.data.replace("zip_", "")
    await cb.message.edit_text("⏳ <b>Файллар Telegram серверидан юкланиб, ZIP архив яратилмоқда...</b>\n<i>Файллар сонига қараб 5-20 сония вақт олиши мумкин. Илтимос, кутинг.</i>", parse_mode="HTML")

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

    if not entries:
        await cb.message.edit_text(f"📭 <b>Ушбу бўлимда ҳали бирорта файл бириктирилмаган.</b>\n<i>({label_info})</i>", parse_mode="HTML")
        await cb.answer()
        return

    MAX_SIZE = 45 * 1024 * 1024  # 45 MB — Telegram лимити 50 МБ, запас 5 МБ

    try:
        buf, file_count = await generate_files_zip(bot, entries)

        if file_count == 0:
            await cb.message.edit_text("📭 Файлларни юклаб бўлмади ёки улар бўш.", parse_mode="HTML")
            await cb.answer()
            return

        zip_size = buf.getbuffer().nbytes

        if zip_size <= MAX_SIZE:
            # ── Обычная отправка — влезает в лимит ──
            await bot.send_document(
                cb.message.chat.id,
                types.BufferedInputFile(buf.getvalue(), filename=zip_name),
                caption=f"✅ <b>АДТИ 2026 — Файллар архиви (.zip)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 Бўлим: <b>{label_info}</b>\n"
                        f"📁 Жами архивланган файллар: <b>{file_count} та</b>\n"
                        f"📂 Барча файллар папкаларга чиройли тартибланган!",
                parse_mode="HTML"
            )
            await cb.message.delete()
        else:
            # ── ZIP слишком большой — автоматически делим по группам кафедр ──
            # Группы: 1-20, 21-40, 41-65
            dept_groups = [(1, 20), (21, 40), (41, 65)]
            await cb.message.edit_text(
                f"⚠️ <b>ZIP архив жуда катта ({zip_size // (1024*1024)} МБ).</b>\n"
                f"Кафедралар гуруҳлари бўйича <b>{len(dept_groups)} та</b> алоҳида архивга бўлиб юборилади...\n"
                f"<i>Илтимос, кутинг.</i>",
                parse_mode="HTML"
            )

            sent_parts = 0
            for start_dept, end_dept in dept_groups:
                part_entries = [e for e in entries if start_dept <= e['dept_id'] <= end_dept]
                if not part_entries:
                    continue

                part_buf, part_count = await generate_files_zip(bot, part_entries)
                if part_count == 0:
                    continue

                part_name = f"ADTI_2026_{zip_name.replace('.zip', '')}_Kaf{start_dept:02d}-{end_dept:02d}.zip"
                sent_parts += 1
                await bot.send_document(
                    cb.message.chat.id,
                    types.BufferedInputFile(part_buf.getvalue(), filename=part_name),
                    caption=f"📦 <b>{label_info}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🏫 Кафедралар: <b>{start_dept}–{end_dept}</b>\n"
                            f"📁 Файллар: <b>{part_count} та</b>",
                    parse_mode="HTML"
                )

            if sent_parts == 0:
                await cb.message.edit_text("📭 Файлларни юклаб бўлмади.", parse_mode="HTML")
            else:
                await cb.message.edit_text(
                    f"✅ <b>Барча файллар {sent_parts} та қисмга бўлиниб юборилди!</b>\n"
                    f"📌 Бўлим: <b>{label_info}</b>",
                    parse_mode="HTML"
                )

    except Exception as e:
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
                "📊 <b>1-варақ:</b> 65 та кафедра сводкаси (барча 17 индикатор)\n"
                "📝 <b>2-варақ:</b> Барча юборилган илмий ишлар базаси (муаллифлар билан)\n"
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


# ─── HELPER: УЗУН МАТННИ ЧИРОЙЛИ ВА ХАВФСИЗ ЮБОРИШ (Telegram HTML) ──────────
def format_telegram_html(text: str) -> str:
    """Markdown форматдаги AI жавобини чиройли ва бенуқсон Telegram HTML кўринишига ўтказади."""
    if not text:
        return ""
    import re
    # 1. Escape &
    text = text.replace("&", "&amp;")
    # 2. Headers ### Title -> <b>Title</b>
    text = re.sub(r'^(?:#{1,4})\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # 3. **bold** -> <b>bold</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # 4. *italic* -> <i>italic</i>
    text = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<i>\1</i>', text)
    # 5. Bullet points
    text = re.sub(r'^\s*[\*\-•]\s*', '• ', text, flags=re.MULTILINE)
    # 6. Horizontal rules
    text = re.sub(r'^\s*[-—_]{3,}\s*$', '━━━━━━━━━━━━━━━━━━━━━━━━━━━━', text, flags=re.MULTILINE)
    # 7. Clean leftover asterisks
    text = text.replace("**", "").replace("• •", "• ")
    # 8. Extra newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def send_long_text(chat_id: int, text: str, header: str = ""):
    """Узун AI таҳлил матнини Telegram лимитига мос бўлакларга бўлиб чиройли юборади."""
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
            # Fallback agar HTML parsingda belgi xatosi bo'lsa
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
        "⏳ <i>База маълумотлари ўқилмоқда ва расмий илмий таҳлил матни ёзилмоқда (10–25 сония)...</i>",
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
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    entered_pin = message.text.strip()
    data = await state.get_data()
    action = data.get("target_action", "download_passwords")
    await state.clear()

    import datetime
    now_str = datetime.datetime.now().strftime("%d.%m.%Y | %H:%M")
    admin_name = message.from_user.full_name or f"Admin #{message.from_user.id}"

    if entered_pin != ADMIN_MASTER_PIN:
        # Уведомляем аудит-канал о попытке несанкционированного доступа
        if AUDIT_CHANNEL_ID:
            try:
                alert_text = (
                    f"🚨 <b>#SECURITY_ALERT — НОТЎҒРИ PIN-КОД ТЕРИЛДИ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Фойдаланувчи:</b> {admin_name} (ID: <code>{message.from_user.id}</code>)\n"
                    f"⚠️ <b>Ҳаракат:</b> Кафедралар паролларини очишга уриниш\n"
                    f"❌ <b>Терилган код:</b> <code>{entered_pin}</code>\n"
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

    # Если ПИН-код правильный:
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

        # Логируем успешный вход в аудит-канал
        if AUDIT_CHANNEL_ID:
            try:
                log_text = (
                    f"🔐 <b>#MASTER_PIN — ПАРОЛЛАР МУВАФФАҚИЯТЛИ ЮКЛАНДИ</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Администратор:</b> {admin_name} (ID: <code>{message.from_user.id}</code>)\n"
                    f"📅 <b>Вақт:</b> {now_str}\n"
                    f"🛡 <i>Мастер ПИН-код тўғри терилди. Ҳужжат жўнатилди.</i>"
                )
                await bot.send_message(AUDIT_CHANNEL_ID, log_text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to log pin success: {e}")


# ─── ADMIN: ПРИНУДИТЕЛЬНАЯ СИНХРОНИЗАЦИЯ С GOOGLE SHEETS ────────────────────
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
                "head_name": r['head_name'],
                "dsc": r['dsc'], "phd": r['phd'],
                "monography": r['monography'], "patent": r['patent'],
                "oak_uz": r['oak_uz'], "oak_ru_if": r['oak_ru_if'],
                "thesis_uz": r['thesis_uz'], "thesis_foreign": r['thesis_foreign'],
                "scopus_wos": r['scopus_wos'],
                "rationalizer": r['rationalizer'],
                "implementation": r['implementation'],
                "conferences": r['conferences'],
                "contracts": r.get('contracts', 0) or 0,
                "grants": r.get('grants', 0) or 0,
                "total": r['total'],
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


# ─── ADMIN: ПРИНУДИТЕЛЬНАЯ СИНХРОНИЗАЦИЯ С GOOGLE SHEETS ────────────────────
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


# ─── KEEP-ALIVE HTTP СЕРВЕР ─────────────────────────────────────────────────
async def health_handler(request):
    return web.Response(
        text=json.dumps({"status": "ok", "bot": "ADTI Report Bot", "version": "2.1"}),
        content_type="application/json"
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
    logger.info("ADTI Bot v2.1 started. Polling...")

    # Запускаем фоновый цикл авто-бэкапа базы данных каждые 12 часов
    asyncio.create_task(periodic_backup_loop())

    # Первичная синхронизация с Google Таблицей
    asyncio.create_task(sync_sheets_background())

    await start_web_server()
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
