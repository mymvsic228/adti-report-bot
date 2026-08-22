import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import BOT_TOKEN, ADMIN_IDS, FILES_DIR
from database import (
    init_db, get_all_departments, get_department, get_dept_by_user,
    bind_user_to_dept, add_entry, get_dept_summary, get_all_summary,
    get_dept_entries, DEPARTMENTS, INDICATORS, INDICATOR_LABELS
)
from report_gen import generate_report_docx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ─── FSM ────────────────────────────────────────────────────────────────────
class SelectDept(StatesGroup):
    searching = State()

class AddEntry(StatesGroup):
    choose_category = State()
    enter_title     = State()
    enter_authors   = State()
    enter_journal   = State()
    upload_file     = State()


# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb(is_admin: bool = False):
    buttons = [
        [KeyboardButton(text="📁 Ҳисобот қўшиш")],
        [KeyboardButton(text="📊 Кафедрам статистикаси")],
        [KeyboardButton(text="🔄 Кафедрани ўзгартириш")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="🏛 Ягона сводный ҳисобот (Admin)")])
        buttons.append([KeyboardButton(text="📥 Word ҳисобот юклаш (Admin)")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def dept_list_kb(page: int = 0, search: str = ""):
    """Клавиатура выбора кафедры с пагинацией"""
    PAGE_SIZE = 8
    depts = DEPARTMENTS
    if search:
        depts = [d for d in DEPARTMENTS if search.lower() in d[1].lower() or search.lower() in d[2].lower()]

    total = len(depts)
    start = page * PAGE_SIZE
    chunk = depts[start:start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for dep_id, name, head in chunk:
        short = name[:38] + '…' if len(name) > 38 else name
        kb.button(text=f"{dep_id}. {short}", callback_data=f"select_dept_{dep_id}")
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Орқа", callback_data=f"dept_page_{page-1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Кейинги ▶", callback_data=f"dept_page_{page+1}"))
    if nav:
        kb.row(*nav)

    return kb.as_markup()


def categories_kb():
    kb = InlineKeyboardBuilder()
    for key, label in INDICATORS:
        kb.button(text=label, callback_data=f"cat_{key}")
    kb.adjust(1)
    kb.button(text="❌ Бекор қилиш", callback_data="cancel")
    return kb.as_markup()


# ─── HELPERS ────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── /start ─────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    dept = await get_dept_by_user(user_id)

    if dept:
        await message.answer(
            f"👋 Хуш келибсиз!\n\n"
            f"📂 <b>Сизнинг кафедрангиз:</b>\n<i>{dept['name']}</i>\n\n"
            f"Қуйидаги тугмалардан фойдаланинг 👇",
            reply_markup=main_kb(is_admin(user_id)),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "👋 <b>АДТИ Илмий ҳисоботлар тизимига хуш келибсиз!</b>\n\n"
            "Биринчи навбатда кафедрангизни танланг:\n\n"
            "🔍 Кафедра номини ёзинг <i>(мас: «терапия» ёки «жарроҳлик»)</i> "
            "ёки рўйхатдан танланг:",
            reply_markup=dept_list_kb(page=0),
            parse_mode="HTML"
        )
        await state.set_state(SelectDept.searching)


@dp.message(SelectDept.searching)
async def search_dept(message: types.Message, state: FSMContext):
    q = message.text.strip()
    await message.answer(
        f"🔍 <b>«{q}»</b> бўйича натижалар:",
        reply_markup=dept_list_kb(search=q),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("dept_page_"))
async def page_dept(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.replace("dept_page_", ""))
    await callback.message.edit_reply_markup(reply_markup=dept_list_kb(page=page))


@dp.callback_query(F.data.startswith("select_dept_"))
async def select_dept(callback: types.CallbackQuery, state: FSMContext):
    dept_id = int(callback.data.replace("select_dept_", ""))
    await bind_user_to_dept(callback.from_user.id, dept_id)
    dept = await get_department(dept_id)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Кафедра танланди:</b>\n<i>{dept['name']}</i>\n\n"
        f"Кафедра мудири: <b>{dept['head_name'] or '—'}</b>",
        parse_mode="HTML"
    )
    await callback.message.answer(
        "Энди ҳисобот қўшишингиз мумкин! 👇",
        reply_markup=main_kb(is_admin(callback.from_user.id))
    )


# ─── ДОБАВЛЕНИЕ ЗАПИСИ ──────────────────────────────────────────────────────
@dp.message(F.text == "📁 Ҳисобот қўшиш")
async def add_report_start(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)
    if not dept:
        await message.answer("⚠️ Аввал кафедрангизни танланг: /start")
        return
    await message.answer(
        f"📂 <b>{dept['name']}</b>\n\nКатегорияни танланг:",
        reply_markup=categories_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.choose_category)


@dp.callback_query(F.data.startswith("cat_"), AddEntry.choose_category)
async def choose_category(callback: types.CallbackQuery, state: FSMContext):
    cat = callback.data.replace("cat_", "")
    await state.update_data(category=cat)
    label = INDICATOR_LABELS.get(cat, cat)
    await callback.message.edit_text(
        f"✅ <b>{label}</b> танланди.\n\n"
        f"📝 <b>Иш номини</b> киритинг (мақола, монография, патент номи ва ҳ.к.):",
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.enter_title)


@dp.callback_query(F.data == "cancel")
async def cancel_action(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Бекор қилинди.")
    await callback.message.answer("Асосий меню 👇", reply_markup=main_kb(is_admin(callback.from_user.id)))


@dp.message(AddEntry.enter_title)
async def enter_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("👤 <b>Муаллифларни</b> киритинг (Ф.И.Ш., вергул билан):", parse_mode="HTML")
    await state.set_state(AddEntry.enter_authors)


@dp.message(AddEntry.enter_authors)
async def enter_authors(message: types.Message, state: FSMContext):
    await state.update_data(authors=message.text.strip())
    await message.answer(
        "📎 <b>Тасдиқловчи ҳужжатни юборинг</b> (PDF, Word ёки фото сканни)\n\n"
        "Ёки <b>/skip</b> — ҳужжатсиз давом этиш:",
        parse_mode="HTML"
    )
    await state.set_state(AddEntry.upload_file)


@dp.message(Command("skip"), AddEntry.upload_file)
async def skip_file(message: types.Message, state: FSMContext):
    await save_entry(message, state, file_path='', file_id='')


@dp.message(AddEntry.upload_file, F.document | F.photo)
async def receive_file(message: types.Message, state: FSMContext):
    dept = await get_dept_by_user(message.from_user.id)

    # Определяем файл
    if message.document:
        f = message.document
        file_id = f.file_id
        fname = f.file_name or f"file_{file_id}.bin"
    elif message.photo:
        f = message.photo[-1]
        file_id = f.file_id
        fname = f"photo_{file_id}.jpg"

    # Скачиваем и сохраняем
    dept_dir = FILES_DIR / str(dept['id'])
    dept_dir.mkdir(exist_ok=True)
    file_path = str(dept_dir / fname)

    file_obj = await bot.get_file(file_id)
    await bot.download_file(file_obj.file_path, destination=file_path)

    await save_entry(message, state, file_path=file_path, file_id=file_id)


async def save_entry(message: types.Message, state: FSMContext, file_path: str, file_id: str):
    data = await state.get_data()
    dept = await get_dept_by_user(message.from_user.id)

    await add_entry(
        dept_id=dept['id'],
        category=data.get('category', ''),
        title=data.get('title', ''),
        authors=data.get('authors', ''),
        year=2026,
        file_path=file_path,
        file_id=file_id
    )
    await state.clear()

    summary = await get_dept_summary(dept['id'])
    cat = data.get('category', '')
    label = INDICATOR_LABELS.get(cat, cat)
    total_cat = summary.get(cat, 0)

    await message.answer(
        f"✅ <b>Қабул қилинди!</b>\n\n"
        f"📂 Кафедра: <i>{dept['name']}</i>\n"
        f"📌 Категория: <b>{label}</b>\n"
        f"📊 Жами бу категорияда: <b>{total_cat} та</b>\n"
        f"{'📎 Файл сақланди.' if file_path else ''}",
        reply_markup=main_kb(is_admin(message.from_user.id)),
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

    lines.append(f"\n📊 <b>Жами: {total} та ёзув</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── СМЕНА КАФЕДРЫ ──────────────────────────────────────────────────────────
@dp.message(F.text == "🔄 Кафедрани ўзгартириш")
async def change_dept(message: types.Message, state: FSMContext):
    await message.answer(
        "Янги кафедрани танланг:",
        reply_markup=dept_list_kb(page=0)
    )
    await state.set_state(SelectDept.searching)


# ─── ADMIN: СВОДНАЯ ТАБЛИЦА ─────────────────────────────────────────────────
@dp.message(F.text == "🏛 Ягона сводный ҳисобот (Admin)")
async def admin_summary(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    rows = await get_all_summary()
    lines = ["📊 <b>Сводный ҳисобот — барча кафедралар</b>\n"]
    total_scopus = total_phd = total_dsc = 0

    for r in rows:
        scopus = r['scopus_wos']
        phd = r['phd']
        dsc = r['dsc']
        total = r['total']
        if total == 0:
            continue
        name_short = r['name'][:35] + ('…' if len(r['name']) > 35 else '')
        lines.append(
            f"<b>{r['id']}.</b> {name_short}\n"
            f"   Scopus: {scopus} | PhD: {phd} | DSc: {dsc} | Жами: {total}\n"
        )
        total_scopus += scopus
        total_phd += phd
        total_dsc += dsc

    lines.append(f"\n<b>ЖАМИ по институту:</b> Scopus: {total_scopus} | PhD: {total_phd} | DSc: {total_dsc}")

    # Длинное сообщение — делим на части
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="HTML")


# ─── ADMIN: СКАЧАТЬ WORD ОТЧЁТ ──────────────────────────────────────────────
@dp.message(F.text == "📥 Word ҳисобот юклаш (Admin)")
async def download_report(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer("⏳ Word ҳисобот яратиляпти, бир дақиқа...")
    rows = await get_all_summary()
    buf = await generate_report_docx(rows)

    await bot.send_document(
        message.chat.id,
        types.BufferedInputFile(buf.read(), filename="ADTI_2026_hisobot.docx"),
        caption="✅ <b>АДТИ 2026 йил ҳисоботи</b>\nBарча 65 та кафедра бўйича автоматик яратилди.",
        parse_mode="HTML"
    )


# ─── MAIN ───────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    logger.info("ADTI Bot started. Polling...")
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
