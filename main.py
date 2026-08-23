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

from config import BOT_TOKEN, ADMIN_IDS, FILES_DIR, SHEET_WEBHOOK_URL, AUDIT_CHANNEL_ID
from database import (
    init_db, get_department, get_dept_by_user, get_dept_by_code,
    bind_user_to_dept, unbind_user, add_entry, get_dept_summary, get_all_summary,
    get_all_detailed_entries, get_dept_entries, delete_entry, clear_all_test_data,
    DEPARTMENTS, INDICATORS, INDICATOR_LABELS
)
from report_gen import generate_report_docx, generate_codes_docx, generate_report_excel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ─── FSM STATES ─────────────────────────────────────────────────────────────
class AuthState(StatesGroup):
    waiting_for_code = State()

class AddEntry(StatesGroup):
    choose_category = State()
    enter_title     = State()
    enter_authors   = State()
    # Расширенные поля (собираются только для нужных категорий)
    enter_country      = State()   # Страна журнала (Scopus, WoS, хорижий ОАК)
    enter_journal      = State()   # Название журнала
    enter_pub_date     = State()   # Год, выпуск, страницы / дата выхода
    enter_url          = State()   # Ссылка / DOI
    enter_authors_count = State()  # Количество авторов
    enter_specialty    = State()   # Шифр и название специальности (дисс., монография)
    enter_reg_number   = State()   # Рег. номер (патент)
    enter_publisher    = State()   # Издательство (монография)
    enter_venue        = State()   # Место и дата проведения (конференция/тезис)
    upload_file        = State()

# Маппинг категорий → список доп-шагов после enter_authors
# Каждый элемент = (state, prompt_text, field_name)
CATEGORY_EXTRA_STEPS = {
    "scopus_wos": [
        ("enter_country",       "🌍 <b>Журнал нашр этилган давлат номини</b> киритинг:\n<i>(Масалан: Germany, USA, Eron, Xitoy)</i>"),
        ("enter_journal",       "📰 <b>Журнал номини</b> киритинг:\n<i>(Масалан: Journal of Nanostructures)</i>"),
        ("enter_pub_date",      "📅 <b>Нашр йили, сони, бетлари</b>ни киритинг:\n<i>(Масалан: 2026, №2, 66-72 ёки 16.03.2026, 431-437)</i>"),
        ("enter_url",           "🔗 <b>Мақола ҳавола (URL/DOI)</b>сини киритинг:\n<i>(Масалан: https://doi.org/...)</i>"),
        ("enter_authors_count", "👥 <b>Муаллифлар сонини</b> киритинг:\n<i>(Масалан: 5)</i>"),
    ],
    "oak_ru_if": [
        ("enter_country",       "🌍 <b>Журнал нашр этилган давлат номини</b> киритинг:\n<i>(Масалан: Russia, Germany)</i>"),
        ("enter_journal",       "📰 <b>Журнал номини</b> киритинг:"),
        ("enter_pub_date",      "📅 <b>Нашр йили, сони, бетлари</b>ни киритинг:\n<i>(Масалан: Vol. 6 No. 02 (2026))</i>"),
        ("enter_url",           "🔗 <b>Мақола ҳавола (URL/DOI)</b>сини киритинг:"),
        ("enter_authors_count", "👥 <b>Муаллифлар сонини</b> киритинг:"),
    ],
    "oak_uz": [
        ("enter_journal",   "📰 <b>Журнал номини</b> киритинг:\n<i>(Масалан: Profilaktik tibbiyot va salomatlik)</i>"),
        ("enter_pub_date",  "📅 <b>Нашр санаси / йили</b>ни киритинг:\n<i>(Масалан: 05.02.2026 ёки 2026, №1)</i>"),
        ("enter_url",       "🔗 <b>Мақола ҳавола (URL)</b>сини киритинг:"),
    ],
    "dsc": [
        ("enter_specialty", "🎓 <b>Ихтисослик шифри ва номи</b>ни киритинг:\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",  "📅 <b>ВАК томонидан тасдиқланган сана</b>ни киритинг:\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "phd": [
        ("enter_specialty", "🎓 <b>Ихтисослик шифри ва номи</b>ни киритинг:\n<i>(Масалан: 14.00.06 — Кардиология)</i>"),
        ("enter_pub_date",  "📅 <b>ВАК томонидан тасдиқланган сана</b>ни киритинг:\n<i>(Масалан: 15.03.2026)</i>"),
    ],
    "monography": [
        ("enter_publisher", "🏢 <b>Нашриёт номини</b> киритинг:\n<i>(Масалан: Fan va texnologiya)</i>"),
        ("enter_pub_date",  "📅 <b>Нашр йили ва санаси</b>ни киритинг:\n<i>(Масалан: 2026)</i>"),
    ],
    "patent": [
        ("enter_pub_date",   "📅 <b>Патент берилган сана</b>сини киритинг:\n<i>(Масалан: 23.02.2026)</i>"),
        ("enter_reg_number", "🔢 <b>Патент рақами</b>ни киритинг:\n<i>(Масалан: IAP 8489 ёки DGU 60217)</i>"),
    ],
    "thesis_foreign": [
        ("enter_venue",    "📍 <b>Ўтказилган жой ва санаси</b>ни киритинг:\n<i>(Масалан: Toshkent, 15.03.2026)</i>"),
    ],
    "thesis_uz": [
        ("enter_venue",    "📍 <b>Ўтказилган жой ва санаси</b>ни киритинг:\n<i>(Масалан: Andijon, 10.04.2026)</i>"),
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
    "enter_venue":         "pub_date",   # место+дата → сохраняем в pub_date
}


# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb(user_id: int):
    buttons = [
        [KeyboardButton(text="📁 Ҳисобот қўшиш")],
        [KeyboardButton(text="📋 Юборилган ишлар (Ўчириш)"), KeyboardButton(text="📊 Кафедрам статистикаси")],
        [KeyboardButton(text="🚪 Кафедрадан чиқиш")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="🏛 Сводный ҳисобот")])
        buttons.append([KeyboardButton(text="📊 Excel ҳисобот юклаш (.xlsx)")])
        buttons.append([KeyboardButton(text="📥 Word ҳисобот юклаш")])
        buttons.append([KeyboardButton(text="🔑 Кафедралар пароллари (Word)")])
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



# ─── /start ─────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if dept:
        await message.answer(
            f"👋 <b>Ассалому алайкум!</b>\n\n"
            f"📂 <b>Кафедрангиз:</b>\n<i>{dept['name']}</i>\n"
            f"👤 Мудир: <b>{dept['head_name'] or '—'}</b>\n\n"
            f"Қуйидаги тугмалардан фойдаланинг 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    elif user_id in ADMIN_IDS:
        await message.answer(
            f"👑 <b>Ассалому алайкум, Админ!</b>\n\n"
            f"Сиз бошқарув панелига кирдингиз. Барча 65 та кафедра ҳисоботларини кўришингиз, "
            f"Word файлини юклаб олишингиз ва кафедралар паролларини тарқатишингиз мумкин 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🔒 <b>Ассалому алайкум! АДТИ Илмий ҳисоботлар тизими</b>\n\n"
            "Кафедрангизга кириш учун илмий бўлим томонидан берилган <b>МАХСУС ПАРОЛни (кодни)</b> киритинг:\n\n"
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
            "❌ <b>Нотўғри парол!</b>\n\n"
            "Илтимос, кодни қайта текшириб тўғри киритинг ёки Илмий бўлимга мурожаат қилинг:",
            parse_mode="HTML"
        )
        return

    # Привязываем пользователя к кафедре
    await bind_user_to_dept(message.from_user.id, dept['id'])
    await state.clear()

    await message.answer(
        f"✅ <b>Кафедра тасдиқланди!</b>\n\n"
        f"📂 <b>{dept['name']}</b>\n"
        f"👤 Кафедра мудири: <b>{dept['head_name'] or '—'}</b>\n\n"
        f"Энди ҳисоботларни юборишингиз мумкин 👇",
        reply_markup=main_kb(message.from_user.id),
        parse_mode="HTML"
    )


# ─── ВЫХОД ИЗ КАФЕДРЫ ───────────────────────────────────────────────────────
@dp.message(F.text == "🚪 Кафедрадан чиқиш")
async def logout_dept(message: types.Message, state: FSMContext):
    await unbind_user(message.from_user.id)
    await state.clear()
    await message.answer(
        "🔓 Сиз кафедрадан чиқдингиз.\n\n"
        "Бошқа кафедрага кириш учун махсус код-паролни киритинг:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )
    await state.set_state(AuthState.waiting_for_code)


# ─── ДОБАВЛЕНИЕ ЗАПИСИ: ШАГ 1 — КАТЕГОРИЯ ───────────────────────────────────
@dp.message(F.text == "📁 Ҳисобот қўшиш")
async def add_report_start(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    if not dept and message.from_user.id not in ADMIN_IDS:
        await message.answer("⚠️ Аввал кафедра кодини киритинг: /start")
        return

    dept_name = dept['name'] if dept else "Администратор"
    await state.clear()
    await message.answer(
        f"📂 <b>{dept_name}</b>\n\nКатегорияни танланг:",
        reply_markup=categories_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.choose_category)


@dp.callback_query(F.data.startswith("cat_"), AddEntry.choose_category)
async def choose_category(cb: types.CallbackQuery, state: FSMContext):
    cat = cb.data[4:]
    await state.update_data(category=cat)
    label = INDICATOR_LABELS.get(cat, cat)
    await cb.message.edit_text(
        f"✅ <b>{label}</b> танланди.\n\n"
        f"📝 <b>Иш номини</b> киритинг:\n"
        f"<i>(мақола, монография, патент, диссертация номи)</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.enter_title)
    await cb.answer()


# ─── ШАГ 2 — НАЗВАНИЕ ────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_title, F.text)
async def enter_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "👤 <b>Муаллифларни</b> киритинг:\n<i>(Ф.И.Ш., вергул билан)</i>",
        parse_mode="HTML"
    )
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
        await message.answer(
            "📎 <b>Тасдиқловчи ҳужжатни юборинг</b>\n"
            "<i>(PDF, Word ёки фото скан)</i>\n\n"
            "Ёки 👇 тугмани босинг:",
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


# ─── ШАГ 3 — АВТОРЫ ──────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_authors, F.text)
async def enter_authors(message: types.Message, state: FSMContext):
    await state.update_data(authors=message.text.strip(), extra_step_index=0)
    await go_to_next_extra_step(message, state)


# ─── ШАГ 4 — ФАЙЛ (документ) ─────────────────────────────────────────────────
@dp.message(AddEntry.upload_file, F.document)
async def receive_doc(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    dept_id = dept['id'] if dept else 1
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
    dept = await get_dept_by_user(message.from_user.id)
    dept_id = dept['id'] if dept else 1
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
    dept = await get_dept_by_user(user_id)
    dept_id = dept['id'] if dept else 1
    dept_name = dept['name'] if dept else "Админ"
    head_name = dept['head_name'] if dept else ""
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

    entry_id = await add_entry(
        dept_id=dept_id,
        category=cat,
        title=title,
        authors=authors,
        year=2026,
        file_path=file_path,
        file_id=file_id,
        country=country,
        journal_name=journal_name,
        pub_date=pub_date,
        url=url,
        authors_count=authors_count,
        specialty=specialty,
        reg_number=reg_number,
        publisher=publisher,
    )

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
    }))

    # Аудит-канал: добавляем новые поля в уведомление
    extra_lines = ""
    if country:       extra_lines += f"\n🌍 <b>Давлат:</b> {country}"
    if journal_name:  extra_lines += f"\n📰 <b>Журнал:</b> {journal_name}"
    if pub_date:      extra_lines += f"\n📅 <b>Сана/Нашр:</b> {pub_date}"
    if url:           extra_lines += f"\n🔗 <b>URL:</b> {url}"
    if specialty:     extra_lines += f"\n🎓 <b>Ихтисослик:</b> {specialty}"
    if reg_number:    extra_lines += f"\n🔢 <b>Рег. рақам:</b> {reg_number}"
    if publisher:     extra_lines += f"\n🏢 <b>Нашриёт:</b> {publisher}"
    if authors_count: extra_lines += f"\n👥 <b>Муаллифлар сони:</b> {authors_count}"

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

    summary = await get_dept_summary(dept_id)
    label = INDICATOR_LABELS.get(cat, cat)
    total_cat = summary.get(cat, 0)

    await message.answer(
        f"✅ <b>Қабул қилинди!</b>\n\n"
        f"📂 <i>{dept_name}</i>\n"
        f"📌 {label}: <b>{total_cat} та</b>\n"
        f"{'📎 Файл сақланди.' if file_path else '📝 Файлсиз сақланди.'}\n"
        f"{'📊 Google Таблицага юборилди.' if SHEET_WEBHOOK_URL else ''}",
        reply_markup=main_kb(user_id),
        parse_mode="HTML"
    )



# ─── СТАТИСТИКА КАФЕДРЫ ─────────────────────────────────────────────────────
@dp.message(F.text == "📊 Кафедрам статистикаси")
async def my_stats(message: types.Message):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if not dept:
        if user_id in ADMIN_IDS:
            await message.answer(
                "👑 <b>Сиз Администраторсиз.</b>\n\n"
                "Ўз кафедрангиз статистикасини кўриш учун аввал кафедра кодини киритинг.\n"
                "Ёки барча кафедраларнинг сводный ҳисоботини кўриш учун:\n\n"
                "👉 <b>🏛 Сводный ҳисобот</b> тугмасини босинг.",
                reply_markup=main_kb(user_id),
                parse_mode="HTML"
            )
        else:
            await message.answer("⚠️ Аввал кафедра паролини киритинг: /start")
        return

    summary = await get_dept_summary(dept['id'])
    lines = [f"📂 <b>{dept['name']}</b>\n<i>Мудир: {dept['head_name'] or '—'}</i>\n"]

    total = 0
    for key, label in INDICATORS:
        cnt = summary.get(key, 0)
        if cnt:
            lines.append(f"  {label}: <b>{cnt}</b>")
            total += cnt

    if total == 0:
        lines.append("  <i>Ҳали ёзувлар йўқ</i>")

    lines.append(f"\n📊 <b>Жами: {total} та ёзув</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── ПРОСМОТР И УДАЛЕНИЕ СВОИХ ЗАПИСЕЙ ──────────────────────────────────────
@dp.message(F.text == "📋 Юборилган ишлар (Ўчириш)")
async def list_dept_submissions(message: types.Message):
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)
    if not dept and user_id not in ADMIN_IDS:
        await message.answer("⚠️ Аввал кафедра кодини киритинг: /start")
        return

    dept_id = dept['id'] if dept else 1
    dept_name = dept['name'] if dept else "Барча кафедралар"

    entries = await get_dept_entries(dept_id, limit=10)
    if not entries:
        await message.answer(
            f"📂 <b>{dept_name}</b>\n\n"
            f"Ҳозирча юборилган илмий ишлар йўқ.",
            parse_mode="HTML"
        )
        return

    await message.answer(
        f"📋 <b>{dept_name}</b>\n"
        f"Сўнгги юборилган ишлар рўйхати:\n"
        f"<i>(Агар хато киритган бўлсангиз, пастдаги 🗑 тугмани босиб ўчиришингиз мумкин)</i>",
        parse_mode="HTML"
    )

    for e in entries:
        cat_lbl = INDICATOR_LABELS.get(e['category'], e['category'])
        has_f = "📎 Файл бор" if e['file_path'] else "📝 Файлсиз"
        text = (
            f"🔹 <b>#{e['id']} — {cat_lbl}</b>\n"
            f"📝 <b>Номи:</b> {e['title'] or '—'}\n"
            f"👤 <b>Муаллифлар:</b> {e['authors'] or '—'}\n"
            f"📅 Сана: {str(e['created_at'])[:16]} | {has_f}"
        )
        del_kb = InlineKeyboardBuilder()
        del_kb.button(text=f"🗑 Ўчириш (#{e['id']})", callback_data=f"del_entry_{e['id']}")
        await message.answer(text, reply_markup=del_kb.as_markup(), parse_mode="HTML")


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
    await cb.message.edit_text("✅ <b>Барча тест маълумотлар муваффақиятли тозаланди!</b>\nБаза бўш ва расмий қабулга тайёр.", parse_mode="HTML")
    await cb.answer()


# ─── ADMIN: СВОДНАЯ ТАБЛИЦА ─────────────────────────────────────────────────
@dp.message(F.text == "🏛 Сводный ҳисобот")
async def admin_summary(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    rows = await get_all_summary()
    lines = ["📊 <b>Сводный ҳисобот — барча 65 та кафедра</b>\n"]
    total_scopus = total_phd = total_dsc = total_all = 0

    for r in rows:
        if r['total'] == 0:
            continue
        name_short = r['name'][:32] + '…' if len(r['name']) > 32 else r['name']
        lines.append(
            f"<b>{r['id']}.</b> {name_short}\n"
            f"   DSc:{r['dsc']} PhD:{r['phd']} Scopus:{r['scopus_wos']} "
            f"Патент:{r['patent']} Жами:<b>{r['total']}</b>\n"
        )
        total_scopus += r['scopus_wos']
        total_phd += r['phd']
        total_dsc += r['dsc']
        total_all += r['total']

    lines.append(
        f"\n<b>═══ ЖАМИ по институту ═══</b>\n"
        f"DSc: {total_dsc} | PhD: {total_phd} | Scopus: {total_scopus}\n"
        f"Барча ёзувлар: <b>{total_all} та</b>"
    )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="HTML")


# ─── ADMIN: EXCEL ОТЧЁТ ПО ХИСАБОТУ (.xlsx) ──────────────────────────────
@dp.message(F.text == "📊 Excel ҳисобот юклаш (.xlsx)")
async def download_excel_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ Excel ҳисобот яратиляпти (.xlsx)...")
    summary_rows = await get_all_summary()
    detailed_rows = await get_all_detailed_entries()
    buf = await generate_report_excel(summary_rows, detailed_rows)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_2026_hisobot.xlsx"),
        caption="✅ <b>АДТИ 2026 йил Excel ҳисоботи (.xlsx)</b>\n\n"
                "📊 <b>1-варақ:</b> 65 та кафедра сводкаси (барча 17 индикатор + жами суммалар)\n"
                "📝 <b>2-варақ:</b> Барча юборилган илмий ишлар базаси (муаллифлар, номлар)",
        parse_mode="HTML"
    )


# ─── ADMIN: WORD ОТЧЁТ ПО ХИСАБОТУ ──────────────────────────────────────────
@dp.message(F.text == "📥 Word ҳисобот юклаш")
async def download_report(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ Word ҳисобот яратиляпти...")
    rows = await get_all_summary()
    buf = await generate_report_docx(rows)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_2026_hisobot.docx"),
        caption="✅ <b>АДТИ 2026 йил ҳисоботи</b> — барча 65 та кафедра",
        parse_mode="HTML"
    )


# ─── ADMIN: СКАЧАТЬ СПИСОК ПАРОЛЕЙ ВСЕХ 65 КАФЕДР ──────────────────────────
@dp.message(F.text == "🔑 Кафедралар пароллари (Word)")
async def download_passwords(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer("⏳ Пароллар ҳужжати тайёрланмоқда...")
    buf = await generate_codes_docx(DEPARTMENTS)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_Kafedralar_Parollari.docx"),
        caption="🔑 <b>АДТИ: Барча 65 та кафедра учун кириш пароллари (кодлари)</b>\n\n"
                "Ушбу ҳужжатни чиқариб ёки мудирларга тарқатишингиз мумкин. Ҳар бир кафедра фақат ўз пароли орқали киради!",
        parse_mode="HTML"
    )


# ─── ADMIN: ПРИНУДИТЕЛЬНАЯ СИНХРОНИЗАЦИЯ С GOOGLE SHEETS ────────────────────
@dp.message(F.text == "📋 Google Таблица янгилаш")
async def force_sync_sheets(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    if not SHEET_WEBHOOK_URL:
        await message.answer("⚠️ SHEET_WEBHOOK_URL созланмаган. Render Environment Variables га қўшинг.")
        return

    await message.answer("⏳ Барча маълумотлар Google Таблицага юборилмоқда...")

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
            "total": r['total'],
            "action": "full_sync"
        })

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                SHEET_WEBHOOK_URL,
                json={"action": "full_sync", "departments": payload},
                timeout=aiohttp.ClientTimeout(total=30)
            )
        await message.answer(f"✅ Google Таблицага муваффақиятли юборилди!\n{len(payload)} та кафедра.")
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

    await start_web_server()
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
