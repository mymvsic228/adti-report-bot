import logging
import asyncio
import aiohttp
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

from config import BOT_TOKEN, ADMIN_IDS, FILES_DIR, SHEET_WEBHOOK_URL
from database import (
    init_db, get_department, get_dept_by_user,
    bind_user_to_dept, add_entry, get_dept_summary, get_all_summary,
    DEPARTMENTS, INDICATORS, INDICATOR_LABELS
)
from report_gen import generate_report_docx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ─── FSM ────────────────────────────────────────────────────────────────────
class SelectDept(StatesGroup):
    browsing = State()

class AddEntry(StatesGroup):
    choose_category = State()
    enter_title     = State()
    enter_authors   = State()
    upload_file     = State()


# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb(user_id: int):
    buttons = [
        [KeyboardButton(text="📁 Ҳисобот қўшиш")],
        [KeyboardButton(text="📊 Кафедрам статистикаси")],
        [KeyboardButton(text="🔄 Кафедрани ўзгартириш")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="🏛 Сводный ҳисобот")])
        buttons.append([KeyboardButton(text="📥 Word ҳисобот юклаш")])
        buttons.append([KeyboardButton(text="📋 Google Таблица янгилаш")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def dept_list_kb(page: int = 0):
    PAGE_SIZE = 8
    total = len(DEPARTMENTS)
    start = page * PAGE_SIZE
    chunk = DEPARTMENTS[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for dep_id, name, head in chunk:
        short = name[:40] + '…' if len(name) > 40 else name
        kb.button(text=f"{dep_id}. {short}", callback_data=f"dept_{dep_id}")
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Орқа", callback_data=f"page_{page-1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Кейинги ▶", callback_data=f"page_{page+1}"))
    if nav:
        kb.row(*nav)

    return kb.as_markup()


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
    """Отправляет новую запись в Google Таблицу через Apps Script вебхук"""
    if not SHEET_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                SHEET_WEBHOOK_URL,
                json=entry,
                timeout=aiohttp.ClientTimeout(total=10)
            )
        logger.info(f"Synced entry to Google Sheets: dept={entry.get('dept_id')}, cat={entry.get('category')}")
    except Exception as e:
        logger.warning(f"Google Sheets sync failed: {e}")


# ─── /start ─────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if dept:
        await message.answer(
            f"👋 <b>Хуш келибсиз!</b>\n\n"
            f"📂 Сизнинг кафедрангиз:\n<i>{dept['name']}</i>\n\n"
            f"Қуйидаги тугмалардан фойдаланинг 👇",
            reply_markup=main_kb(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "👋 <b>АДТИ Илмий ҳисоботлар тизимига хуш келибсиз!</b>\n\n"
            "Аввал кафедрангизни танланг 👇",
            reply_markup=dept_list_kb(0),
            parse_mode="HTML"
        )
        await state.set_state(SelectDept.browsing)


# ─── ВЫБОР КАФЕДРЫ ──────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("page_"))
async def paginate_depts(cb: types.CallbackQuery):
    page = int(cb.data.split("_")[1])
    await cb.message.edit_reply_markup(reply_markup=dept_list_kb(page))
    await cb.answer()


@dp.callback_query(F.data.startswith("dept_"))
async def select_dept(cb: types.CallbackQuery, state: FSMContext):
    dept_id = int(cb.data.split("_")[1])
    await bind_user_to_dept(cb.from_user.id, dept_id)
    dept = await get_department(dept_id)
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Кафедра танланди:</b>\n<i>{dept['name']}</i>",
        parse_mode="HTML"
    )
    await cb.message.answer(
        "Энди ҳисобот қўшишингиз мумкин! 👇",
        reply_markup=main_kb(cb.from_user.id)
    )
    await cb.answer()


# ─── СМЕНА КАФЕДРЫ ──────────────────────────────────────────────────────────
@dp.message(F.text == "🔄 Кафедрани ўзгартириш")
async def change_dept(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Янги кафедрани танланг:",
        reply_markup=dept_list_kb(0)
    )
    await state.set_state(SelectDept.browsing)


# ─── ДОБАВЛЕНИЕ ЗАПИСИ: ШАГ 1 — КАТЕГОРИЯ ───────────────────────────────────
@dp.message(F.text == "📁 Ҳисобот қўшиш")
async def add_report_start(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    if not dept:
        await message.answer("⚠️ Аввал кафедрангизни танланг: /start")
        return
    await state.clear()
    await message.answer(
        f"📂 <b>{dept['name']}</b>\n\nКатегорияни танланг:",
        reply_markup=categories_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.choose_category)


@dp.callback_query(F.data.startswith("cat_"), AddEntry.choose_category)
async def choose_category(cb: types.CallbackQuery, state: FSMContext):
    cat = cb.data[4:]  # убираем "cat_"
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


# ─── ШАГ 3 — АВТОРЫ ──────────────────────────────────────────────────────────
@dp.message(AddEntry.enter_authors, F.text)
async def enter_authors(message: types.Message, state: FSMContext):
    await state.update_data(authors=message.text.strip())
    await message.answer(
        "📎 <b>Тасдиқловчи ҳужжатни юборинг</b>\n"
        "<i>(PDF, Word ёки фото скан)</i>\n\n"
        "Ёки 👇 тугмани босинг:",
        reply_markup=skip_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.upload_file)


# ─── ШАГ 4 — ФАЙЛ (документ) ─────────────────────────────────────────────────
@dp.message(AddEntry.upload_file, F.document)
async def receive_doc(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    f = message.document
    file_id = f.file_id
    fname = f.file_name or f"doc_{file_id}.bin"

    dept_dir = FILES_DIR / str(dept['id'])
    dept_dir.mkdir(parents=True, exist_ok=True)
    dest = dept_dir / fname

    file_obj = await bot.get_file(file_id)
    await bot.download_file(file_obj.file_path, destination=str(dest))
    await save_entry(message, state, file_path=str(dest), file_id=file_id)


# ─── ШАГ 4 — ФАЙЛ (фото) ─────────────────────────────────────────────────────
@dp.message(AddEntry.upload_file, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    f = message.photo[-1]
    file_id = f.file_id
    fname = f"photo_{file_id}.jpg"

    dept_dir = FILES_DIR / str(dept['id'])
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


# ─── ОТМЕНА В ЛЮБОЙ МОМЕНТ ──────────────────────────────────────────────────
@dp.callback_query(F.data == "cancel")
async def cancel_action(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Бекор қилинди.")
    await cb.message.answer("Асосий меню 👇", reply_markup=main_kb(cb.from_user.id))
    await cb.answer()


# ─── СОХРАНЕНИЕ ЗАПИСИ + СИНХРОНИЗАЦИЯ ──────────────────────────────────────
async def save_entry(message: types.Message, state: FSMContext,
                     file_path: str, file_id: str, user_id: int = None):
    if user_id is None:
        user_id = message.from_user.id

    data = await state.get_data()
    dept = await get_dept_by_user(user_id)
    await state.clear()

    cat = data.get('category', '')
    title = data.get('title', '')
    authors = data.get('authors', '')

    entry_id = await add_entry(
        dept_id=dept['id'],
        category=cat,
        title=title,
        authors=authors,
        year=2026,
        file_path=file_path,
        file_id=file_id
    )

    # Синхронизация с Google Таблицей
    asyncio.create_task(sync_to_sheets({
        "dept_id": dept['id'],
        "dept_name": dept['name'],
        "head_name": dept['head_name'],
        "category": cat,
        "category_label": INDICATOR_LABELS.get(cat, cat),
        "title": title,
        "authors": authors,
        "year": 2026,
        "has_file": bool(file_path),
        "file_name": Path(file_path).name if file_path else "",
        "entry_id": entry_id
    }))

    summary = await get_dept_summary(dept['id'])
    label = INDICATOR_LABELS.get(cat, cat)
    total_cat = summary.get(cat, 0)

    await message.answer(
        f"✅ <b>Қабул қилинди!</b>\n\n"
        f"📂 <i>{dept['name']}</i>\n"
        f"📌 {label}: <b>{total_cat} та</b>\n"
        f"{'📎 Файл сақланди.' if file_path else '📝 Файлсиз сақланди.'}\n"
        f"{'📊 Google Таблицага юборилди.' if SHEET_WEBHOOK_URL else ''}",
        reply_markup=main_kb(user_id),
        parse_mode="HTML"
    )


# ─── СТАТИСТИКА КАФЕДРЫ ─────────────────────────────────────────────────────
@dp.message(F.text == "📊 Кафедрам статистикаси")
async def my_stats(message: types.Message):
    dept = await get_dept_by_user(message.from_user.id)
    if not dept:
        await message.answer("⚠️ Аввал кафедрангизни танланг: /start")
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


# ─── ADMIN: WORD ОТЧЁТ ──────────────────────────────────────────────────────
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


# ─── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    logger.info("ADTI Bot v2 started. Polling...")
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
