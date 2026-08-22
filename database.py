import aiosqlite
import hashlib
from config import DB_PATH

# 65 Кафедр с уникальными паролями доступа (Код: ADTI-{номер}-{хэш-код})
# Хэш генерируется стабильно, чтобы коды не менялись при перезапуске
def generate_code(dept_id: int) -> str:
    salt = f"adti_secret_2026_dept_{dept_id}"
    h = hashlib.sha256(salt.encode()).hexdigest().upper()[:4]
    return f"ADTI-{dept_id:02d}-{h}"

RAW_DEPARTMENTS = [
    (1, "Ички касалликлар пропедевтикаси кафедраси", "Мусашайхов Умиджон Хусанович"),
    (2, "Факультет терапия кафедраси", "Хужамбердиев Мамазаир"),
    (3, "Госпитал терапия ва эндокринология кафедраси", "Юсупова Шахноза Кадиржановна"),
    (4, "Оилавий шифокорлар тайёрлаш кафедраси", "Салохиддинов Зухриддин Салохиддинович"),
    (5, "Ички касалликлар кафедраси", "Эргашева Зумрад Абдуқаюмовна"),
    (6, "Умумий жарроҳлик ва трансплантология кафедраси", "Мусашайхов Хусанбой Таджибаевич"),
    (7, "Жарроҳлик касалликлари кафедраси", "Ботиров Акрамжон Кодиралиевич"),
    (8, "Даволаш факультети учун болалар жарроҳлиги кафедраси", "Гафуров Адхам Анварович"),
    (9, "Акушерлик ва гинекология кафедраси", "Рузиева Зилола Ботировна"),
    (10, "Болалар касалликлари кафедраси", "Маматхужаев Сарвар Алижонович"),
    (11, "Неврология кафедраси", "Хасанов Улугбек Абдурашидович"),
    (12, "Психиатрия ва наркология кафедраси", "Нурматов Шерзод"),
    (13, "Кўз касалликлари кафедраси", "Файзуллаев Шаҳриёр"),
    (14, "Оториноларингология кафедраси", "Мустафаев Азиз"),
    (15, "Тери ва таносил касалликлари кафедраси", "Юсупов Улугбек"),
    (16, "Травматология ва ортопедия кафедраси", "Комилов Шухрат"),
    (17, "Онкология кафедраси", "Ибрагимов Дилшод"),
    (18, "Юрак-томир жарроҳлиги кафедраси", "Тошматов Санжар"),
    (19, "Анестезиология ва реаниматология кафедраси", "Холиқов Зафар"),
    (20, "Инфекцион касалликлар кафедраси", "Мамажонов Акбар"),
    (21, "Фтизиатрия кафедраси", "Саидов Отабек"),
    (22, "Радиология ва функционал диагностика кафедраси", "Тошбоев Хуршид"),
    (23, "Ядровий тиббиёт ва радиация гигиенаси кафедраси", "Норматов Бахром"),
    (24, "Тиббий реабилитация ва спорт тиббиёти кафедраси", "Каримов Улугбек"),
    (25, "Умумий амалиёт шифокорлари тайёрлаш кафедраси", "Абдуллаев Санжар"),
    (26, "Патологик анатомия кафедраси", "Исмоилов Баходир"),
    (27, "Судтиббий экспертиза кафедраси", "Тошқўзиев Фирдавс"),
    (28, "Нормал физиология кафедраси", "Жалолов Ботир"),
    (29, "Патологик физиология кафедраси", "Маматқулов Сиёвуш"),
    (30, "Нормал анатомия кафедраси", "Мирзаев Санжар"),
    (31, "Биокимё кафедраси", "Алимов Хасан"),
    (32, "Микробиология ва вирусология кафедраси", "Расулов Акмал"),
    (33, "Гистология, цитология ва эмбриология кафедраси", "Холиқов Озод"),
    (34, "Тиббий биология ва генетика кафедраси", "Рахматов Улугбек"),
    (35, "Тиббий физика, биофизика ва информатика кафедраси", "Давронов Шухрат"),
    (36, "Тиббий кимё кафедраси", "Сидиқов Ботир"),
    (37, "Иноят тилид ва лотин тили кафедраси", "Тошматова Нилуфар"),
    (38, "Ижтимоий фанлар кафедраси", "Убайдуллаев Комил"),
    (39, "Жисмоний тарбия кафедраси", "Мирзаев Дониёр"),
    (40, "Педиатрия кафедраси", "Хасанов Алишер"),
    (41, "Болалар юқумли касалликлари кафедраси", "Тожибоев Равшан"),
    (42, "Болалар эндокринологияси кафедраси", "Юсупова Малика"),
    (43, "Болалар неврологияси кафедраси", "Камолов Баходир"),
    (44, "Болалар кардиоревматологияси кафедраси", "Исмонов Шохрух"),
    (45, "Педиатрик жарроҳлик кафедраси", "Нуриллаев Дилшод"),
    (46, "Неонатология кафедраси", "Собирова Шахло"),
    (47, "Стоматология кафедраси", "Расулов Мирзахид"),
    (48, "Тарапевтик стоматология кафедраси", "Садриев Отабек"),
    (49, "Хирургик стоматология кафедраси", "Аброров Машхур"),
    (50, "Ортопедик стоматология кафедраси", "Норматов Орзибой"),
    (51, "Болалар стоматологияси кафедраси", "Рустамов Жасур"),
    (52, "Фармакология ва клиник фармакология кафедраси", "Абдуллаев Комил"),
    (53, "Дори воситаларни технологияси кафедраси", "Юсупов Бахром"),
    (54, "Фармацевтик химия кафедраси", "Тошматов Ойбек"),
    (55, "Фармакогнозия кафедраси", "Мусаев Шокир"),
    (56, "Иммунология ва аллергология кафедраси", "Ҳусанов Равшан"),
    (57, "Гематология ва трансфузиология кафедраси", "Алиев Санжар"),
    (58, "Пульмонология кафедраси", "Холматов Хурсанд"),
    (59, "Ревматология кафедраси", "Маматов Достон"),
    (60, "Гастроэнтерология кафедраси", "Бобожонов Аброр"),
    (61, "ВМО ва ҚТ факультети оилавий шифокорларни малакасини ошириш ва қайта тайёрлаш, функционал диагностика, валеология, соғлиқни сақлашни бошқариш ва жамоат саломатлиги кафедраси", "Назарова Гулчехра Усмановна"),
    (62, "ВМО ва ҚТ факультети неонатология, неврология, болалар неврологияси, психиатрия, наркология ва тиббий психотерапия кафедраси", "Эргашбаева Дилрабохон Абдурасуловна"),
    (63, "ВМО ва ҚТ факультети акушерлик-гинекология, болалар ва ўсмир қизлар гинекологияси, дерматовенерология ва косметология кафедраси", "Якубова Олтиной Абдуганиевна"),
    (64, "ВМО ва ҚТ факультети офталмология, оториноларингология, онкология, урология ва эндоурология кафедраси", "Маматхужаева Гулнарахан Нажмидиновна"),
    (65, "ВМО ва ҚТ факультети травматология-ортопедия, нейрохирургия, стоматология, юз-жағ хирургияси, суд тиббиёт экспертизаси, реабилитология ва спорт тиббиёти кафедраси", "Холиқов Шавкатбек"),
]

# Полный список с паролями
DEPARTMENTS = [
    (d[0], d[1], d[2], generate_code(d[0]))
    for d in RAW_DEPARTMENTS
]

# Категории научных показателей (соответствуют столбцам в Word-отчёте)
INDICATORS = [
    ("dsc",             "🎓 DSc диссертация ҳимояси"),
    ("phd",             "🎓 PhD диссертация ҳимояси"),
    ("monography",      "📗 Монография"),
    ("patent",          "💡 Патент"),
    ("oak_uz",          "📄 ЎзОАК журнали мақоласи"),
    ("oak_ru_if",       "📄 Россия ОАК / Импакт-фактор журнали"),
    ("thesis_uz",       "📋 Республика конференция тезиси"),
    ("thesis_foreign",  "📋 Хорижий конференция тезиси"),
    ("scopus_wos",      "🌐 Scopus / Web of Science мақоласи"),
    ("rationalizer",    "🔬 Рационализаторлик таклифи"),
    ("implementation",  "🏥 Амалиётга тадбиқ этилган иш"),
    ("conferences",     "🗓 Кафедра ўтказган анжуман"),
    ("contracts",       "💰 Хўжалик шартномаси"),
    ("grants",          "🏆 Грант"),
]

INDICATOR_KEYS = [k for k, _ in INDICATORS]
INDICATOR_LABELS = {k: v for k, v in INDICATORS}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                head_name TEXT,
                access_code TEXT UNIQUE,
                tg_user_id INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS indicators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dept_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                title TEXT,
                authors TEXT,
                year INTEGER,
                journal TEXT,
                doi TEXT,
                file_path TEXT,
                file_id TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (dept_id) REFERENCES departments(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dept_users (
                tg_user_id INTEGER NOT NULL,
                dept_id INTEGER NOT NULL,
                role TEXT DEFAULT 'staff',
                PRIMARY KEY (tg_user_id, dept_id)
            )
        """)
        await db.commit()

        # Добавляем или обновляем пароли кафедр
        for dep_id, name, head, code in DEPARTMENTS:
            await db.execute("""
                INSERT INTO departments (id, name, head_name, access_code)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    head_name=excluded.head_name,
                    access_code=excluded.access_code
            """, (dep_id, name, head, code))
        await db.commit()


async def get_all_departments():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM departments ORDER BY id")
        return await cur.fetchall()


async def get_department(dept_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,))
        return await cur.fetchone()


async def get_dept_by_code(code: str):
    """Поиск кафедры по секретному коду (регистронезависимо)"""
    clean_code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM departments WHERE UPPER(TRIM(access_code)) = ?",
            (clean_code,)
        )
        return await cur.fetchone()


async def get_dept_by_user(tg_user_id: int):
    """Возвращает кафедру, к которой привязан пользователь"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT d.* FROM departments d
            JOIN dept_users du ON d.id = du.dept_id
            WHERE du.tg_user_id = ?
            LIMIT 1
        """, (tg_user_id,))
        return await cur.fetchone()


async def bind_user_to_dept(tg_user_id: int, dept_id: int, role: str = 'staff'):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO dept_users (tg_user_id, dept_id, role) VALUES (?, ?, ?)",
            (tg_user_id, dept_id, role)
        )
        await db.commit()


async def unbind_user(tg_user_id: int):
    """Отвязывает пользователя от кафедры при выходе"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM dept_users WHERE tg_user_id = ?", (tg_user_id,))
        await db.commit()


async def add_entry(dept_id: int, category: str, title: str = '', authors: str = '',
                    year: int = 2026, journal: str = '', doi: str = '',
                    file_path: str = '', file_id: str = '', notes: str = '') -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO indicators (dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes))
        await db.commit()
        return cur.lastrowid


async def get_dept_summary(dept_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT category, COUNT(*) as cnt
            FROM indicators WHERE dept_id = ?
            GROUP BY category
        """, (dept_id,))
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}


async def get_all_summary() -> list:
    """Сводная таблица по всем кафедрам"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT d.id, d.name, d.head_name, d.access_code,
                SUM(CASE WHEN i.category='scopus_wos' THEN 1 ELSE 0 END) as scopus_wos,
                SUM(CASE WHEN i.category='phd' THEN 1 ELSE 0 END) as phd,
                SUM(CASE WHEN i.category='dsc' THEN 1 ELSE 0 END) as dsc,
                SUM(CASE WHEN i.category='monography' THEN 1 ELSE 0 END) as monography,
                SUM(CASE WHEN i.category='patent' THEN 1 ELSE 0 END) as patent,
                SUM(CASE WHEN i.category='oak_uz' THEN 1 ELSE 0 END) as oak_uz,
                SUM(CASE WHEN i.category='oak_ru_if' THEN 1 ELSE 0 END) as oak_ru_if,
                SUM(CASE WHEN i.category='thesis_uz' THEN 1 ELSE 0 END) as thesis_uz,
                SUM(CASE WHEN i.category='thesis_foreign' THEN 1 ELSE 0 END) as thesis_foreign,
                SUM(CASE WHEN i.category='rationalizer' THEN 1 ELSE 0 END) as rationalizer,
                SUM(CASE WHEN i.category='implementation' THEN 1 ELSE 0 END) as implementation,
                SUM(CASE WHEN i.category='conferences' THEN 1 ELSE 0 END) as conferences,
                COUNT(i.id) as total
            FROM departments d
            LEFT JOIN indicators i ON d.id = i.dept_id
            GROUP BY d.id
            ORDER BY d.id
        """)
        return await cur.fetchall()


async def get_all_detailed_entries() -> list:
    """Возвращает все добавленные отчёты со всеми деталями"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.id, i.created_at, d.id as dept_id, d.name as dept_name, d.head_name,
                   i.category, i.title, i.authors, i.year, i.journal, i.file_path, i.notes
            FROM indicators i
            JOIN departments d ON i.dept_id = d.id
            ORDER BY i.created_at DESC
        """)
        return await cur.fetchall()

