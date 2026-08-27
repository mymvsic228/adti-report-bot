import aiosqlite
import asyncpg
import hashlib
import logging
from config import DATABASE_URL, DB_PATH

logger = logging.getLogger(__name__)

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
    (9, "Даволаш факультети учун педиатрия кафедраси", "Арзибеков Абдукадир Гулямович"),
    (10, "2-Акушерлик ва гинекология кафедраси", "Негматшаева Хабибахон Набиевна"),
    (11, "Травматология, ортопедия ва нейрожарроҳлик кафедраси", "Худайбердиев Кобил Турсунович"),
    (12, "Психиатрия, наркология, тиббий психология ва психотерапия кафедраси", "Мирзаев Абдурахмон Алишерович"),
    (13, "Анестезиология- реаниматология ва тез тиббий ёрдам кафедраси", "Тошбоев Шерзод Олимович"),
    (14, "Урология кафедраси", "Шадманов Мирзамахмуд Алишерович"),
    (15, "Анатомия ва клиник анатомия кафедраси", "Кахаров Зафар Абдурахмонович"),
    (16, "Ижтимоий гигиена ва ССБ кафедраси", "Бабич Светлана Михайловна"),
    (17, "Превентив тиббиёт асослари кафедраси", "Салиева Манзура Хабибовна"),
    (18, "Болалар касалликлари пропедевтикаси ва поликлиник педиатрия кафедраси", "Абдуллаева Мавжуда Эргашевна"),
    (19, "Педиатрия факультети учун ички касалликлар пропедевтикаси кафедраси", "Артикова Сожидахон Гулямджановна"),
    (20, "Факультет педиатрия ва неонатология кафедраси", "Атаджанова Шаира Халиловна"),
    (21, "Госпитал педиатрия кафедраси", "Ганиева Марапатхон Шакировна"),
    (22, "Дерматовенерология кафедраси", "Пакирдинов Адхамжон Бегишевич"),
    (23, "Болалар жарроҳлиги кафедраси", "Мирзакаримов Баҳромжон Халимжонович"),
    (24, "1-Акушерлик ва гинекология кафедраси", "Асранкулова Дилорамхон Бахтияровна"),
    (25, "Болалар травматологияси, ортопедияси ва нейрожарроҳлик кафедраси", "Қулдашев Қахрамонжон Абдухалилович"),
    (26, "Офтальмология кафедраси", "Икрамов Азизбек Фазилович"),
    (27, "Юқумли касалликлар кафедраси", "Мирзакаримова Дилдора Баходировна"),
    (28, "Жарроҳлик касалликлари ва фуқаролар мухофазаси кафедраси", "Эгамов Юлдашали Сулаймонович"),
    (29, "Хорижий тиллар кафедраси", "Абдулхаирова Фируза Инваровна"),
    (30, "Нормал физиология кафедраси", "Кличева Икболхон Бахтиёровна"),
    (31, "Патологик физиология кафедраси", "Хамракулов Шарифжон Хошимович"),
    (32, "Патологик анатомия ва суд тиббиёти кафедраси", "Алибеков Омадбек Озодбекович"),
    (33, "Болалар стоматологияси кафедраси", "Жўраева Нигора Иминжоновна"),
    (34, "Пропедевтик стоматология кафедраси", "Хакимова Зилолахон Кахрамонжоновна"),
    (35, "Терапевтик стоматология кафедраси", "Усмонов Бахтиёржон Аробидин ўғли"),
    (36, "Ортопедик стоматология ва ортодонтия кафедраси", "Раимжонов Рустамбек Равшанбек ўғли"),
    (37, "Юз-жағ жарроҳлиги кафедраси", "Тешабоев Мухаммадяҳё Ғуломқодирович"),
    (38, "Реабилитология, спорт тиббиёти, халқ табобати ва жисмоний тарбия кафедраси", "Бутабаев Махмуджон Тухлибаевич"),
    (39, "Оториноларингология кафедраси", "Қосимов Кабил"),
    (40, "Фтизиатрия ва пульмонология, микробиология, иммунология ва вирусология кафедраси", "Исанова Дилфуза Турсуновна"),
    (41, "1-Факультет ва госпитал жарроҳлик кафедраси", "Джумабоев Эркин Саткулович"),
    (42, "Онкология кафедраси", "Мамарасулова Дилфуза Закиржоновна"),
    (43, "Ўзбек тили ва адабиёти, тиллар кафедраси", "Ахмедова Нигора Дадахановна"),
    (44, "Ижтимоий-гуманитар фанлар кафедраси", "Ўроқова Ойсулув Жамолиддиновна"),
    (45, "Биологик физика, информатика, тиббий технологиялар кафедраси", "Исманова Арофатхон Абдулхамидовна"),
    (46, "Биологик кимё кафедраси", "Маматова Иродахон Юсуповна"),
    (47, "Тиббий кимё кафедраси", "Холбоев Юсубжон Хакимович"),
    (48, "Тиббий биология ва гистология кафедраси", "Сайдуллаев Тайиржон"),
    (49, "1-Фармацевтик фанлар кафедраси", "Туйчиев Гафуржон Урмонович"),
    (50, "2-Фармацевтик фанлар кафедраси", "Пазлиддинов Абдулвохид Вохобжон ўғли"),
    (51, "Фармакология, клиник фармакология ва тиббиёт биотехнологиялар кафедраси", "Курбанова Дилорамхон Ибрагимджон қизи"),
    (52, "2-Факультет ва госпитал жарроҳлик кафедраси", "Нишанов Муроджон Фозилжонович"),
    (53, "Неврология кафедраси", "Бустанов Ойбек Якубович"),
    (54, "Тиббий радиология кафедраси", "Мадумарова Зарнигор Шухрат қизи"),
    (55, "Тиббий профилактика кафедраси", "Ахмадходжаева Муножатхон Муталибжановна"),
    (56, "ВМО ва ҚТ факультети кардиология, терапия ва тез тиббий ёрдам кафедраси", "Мамасалиев Нематжон"),
    (57, "ВМО ва ҚТ факультети педиатрия, эндокринология, болалар эндокринологияси, фтизиатрия, юқумли касалликлар ва эпидемиология кафедраси", "Худайбердиева Хамрохон Тиллавалдиевна"),
    (58, "ВМО ва ҚТ факультети умумий хирургия, болалар хирургияси, эндохирургия ва анестезиология-реаниматология, болалар анестезиологияси-реаниматология кафедраси", "Ходжиматов Гуломидин Минходжиевич"),
    (59, "ВМО ва ҚТ факультети тиббий радиология, интервенцион кардиология, нефрология-гемодиализ ва клиник лаборатор диагностика кафедраси", "Наджмитдинов Отабек Бахритдин ўғли"),
    (60, "ВМО ва ҚТ факультети оилавий шифокорларни малакасини ошириш ва қайта тайёрлаш, функционал диагностика, валеология, соғлиқни сақлашни бошқариш ва жамоат саломатлиги кафедраси", "Назарова Гулчехра Усмановна"),
    (61, "ВМО ва ҚТ факультети неонатология, неврология, болалар неврологияси, психиатрия, наркология ва тиббий психотерапия кафедраси", "Эргашбаева Дилрабохон Абдурасуловна"),
    (62, "ВМО ва ҚТ факультети акушерлик-гинекология, болалар ва ўсмир қизлар гинекологияси, дерматовенерология ва косметология кафедраси", "Якубова Олтиной Абдуганиевна"),
    (63, "ВМО ва ҚТ факультети офталмология, оториноларингология, онкология, урология ва эндоурология кафедраси", "Маматхужаева Гулнарахан Нажмидиновна"),
    (64, "ВМО ва ҚТ факультети травматология-ортопедия, нейрохирургия, стоматология, юз-жағ хирургияси, суд тиббиёт экспертизаси, реабилитология ва спорт тиббиёти кафедраси", "Холиқов Шавкатбек"),
    (65, "ВМО ва ҚТ факультети Аллергология ва клиник иммунология, пулмонология, ревмотология, гематология-трансфузиология ва гастроэнтерология кафедраси", ""),
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


# ─── ASYNCPG POOL ───────────────────────────────────────────────────────────
_pg_pool = None

async def get_pg_pool():
    global _pg_pool
    if _pg_pool is None and DATABASE_URL:
        try:
            _pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
            logger.info("Connected to PostgreSQL pool successfully.")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            _pg_pool = None
    return _pg_pool


async def init_db():
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS departments (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    head_name TEXT,
                    access_code TEXT UNIQUE,
                    tg_user_id BIGINT DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS indicators (
                    id SERIAL PRIMARY KEY,
                    dept_id INTEGER NOT NULL REFERENCES departments(id),
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
                    country TEXT DEFAULT '',
                    journal_name TEXT DEFAULT '',
                    pub_date TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    authors_count TEXT DEFAULT '',
                    specialty TEXT DEFAULT '',
                    reg_number TEXT DEFAULT '',
                    publisher TEXT DEFAULT '',
                    amount TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS dept_users (
                    tg_user_id BIGINT NOT NULL,
                    dept_id INTEGER NOT NULL,
                    role TEXT DEFAULT 'staff',
                    PRIMARY KEY (tg_user_id, dept_id)
                );
            """)
            await conn.execute("ALTER TABLE indicators ADD COLUMN IF NOT EXISTS amount TEXT DEFAULT '';")
            for dep_id, name, head, code in DEPARTMENTS:
                await conn.execute("""
                    INSERT INTO departments (id, name, head_name, access_code)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        head_name = EXCLUDED.head_name,
                        access_code = EXCLUDED.access_code;
                """, dep_id, name, head, code)
        logger.info("PostgreSQL database initialized with 65 departments.")
        return

    # Fallback to SQLite
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

        new_columns = [
            ("country",       "TEXT DEFAULT ''"),
            ("journal_name",  "TEXT DEFAULT ''"),
            ("pub_date",      "TEXT DEFAULT ''"),
            ("url",           "TEXT DEFAULT ''"),
            ("authors_count", "TEXT DEFAULT ''"),
            ("specialty",     "TEXT DEFAULT ''"),
            ("reg_number",    "TEXT DEFAULT ''"),
            ("publisher",     "TEXT DEFAULT ''"),
            ("amount",        "TEXT DEFAULT ''"),
        ]
        for col_name, col_def in new_columns:
            try:
                await db.execute(f"ALTER TABLE indicators ADD COLUMN {col_name} {col_def}")
                await db.commit()
            except Exception:
                pass

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
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM departments ORDER BY id")
            return [dict(r) for r in rows]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM departments ORDER BY id")
        return await cur.fetchall()


async def get_department(dept_id: int):
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM departments WHERE id = $1", dept_id)
            return dict(r) if r else None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,))
        return await cur.fetchone()


async def get_dept_by_code(code: str):
    clean_code = code.strip().upper()
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM departments WHERE UPPER(TRIM(access_code)) = $1",
                clean_code
            )
            return dict(r) if r else None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM departments WHERE UPPER(TRIM(access_code)) = ?",
            (clean_code,)
        )
        return await cur.fetchone()


async def get_dept_by_user(tg_user_id: int):
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            r = await conn.fetchrow("""
                SELECT d.* FROM departments d
                JOIN dept_users du ON d.id = du.dept_id
                WHERE du.tg_user_id = $1
                LIMIT 1
            """, tg_user_id)
            return dict(r) if r else None

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
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM dept_users WHERE tg_user_id = $1;", tg_user_id)
            await conn.execute("""
                INSERT INTO dept_users (tg_user_id, dept_id, role)
                VALUES ($1, $2, $3);
            """, tg_user_id, dept_id, role)
            return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM dept_users WHERE tg_user_id = ?", (tg_user_id,))
        await db.execute(
            "INSERT INTO dept_users (tg_user_id, dept_id, role) VALUES (?, ?, ?)",
            (tg_user_id, dept_id, role)
        )
        await db.commit()


async def unbind_user(tg_user_id: int):
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM dept_users WHERE tg_user_id = $1", tg_user_id)
            return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM dept_users WHERE tg_user_id = ?", (tg_user_id,))
        await db.commit()


async def add_entry(dept_id: int, category: str, title: str = '', authors: str = '',
                    year: int = 2026, journal: str = '', doi: str = '',
                    file_path: str = '', file_id: str = '', notes: str = '',
                    country: str = '', journal_name: str = '', pub_date: str = '',
                    url: str = '', authors_count: str = '', specialty: str = '',
                    reg_number: str = '', publisher: str = '', amount: str = '') -> int:
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO indicators
                    (dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes,
                     country, journal_name, pub_date, url, authors_count, specialty, reg_number, publisher, amount)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19)
                RETURNING id;
            """, dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes,
                  country, journal_name, pub_date, url, authors_count, specialty, reg_number, publisher, amount)
            return row['id']

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO indicators
                (dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes,
                 country, journal_name, pub_date, url, authors_count, specialty, reg_number, publisher, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (dept_id, category, title, authors, year, journal, doi, file_path, file_id, notes,
              country, journal_name, pub_date, url, authors_count, specialty, reg_number, publisher, amount))
        await db.commit()
        return cur.lastrowid


async def get_dept_summary(dept_id: int) -> dict:
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT category, COUNT(*) as cnt
                FROM indicators WHERE dept_id = $1
                GROUP BY category
            """, dept_id)
            return {r['category']: r['cnt'] for r in rows}

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
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
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
                    SUM(CASE WHEN i.category='contracts' THEN 1 ELSE 0 END) as contracts,
                    SUM(CASE WHEN i.category='grants' THEN 1 ELSE 0 END) as grants,
                    COUNT(i.id) as total
                FROM departments d
                LEFT JOIN indicators i ON d.id = i.dept_id
                GROUP BY d.id
                ORDER BY d.id
            """)
            return [dict(r) for r in rows]

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
                SUM(CASE WHEN i.category='contracts' THEN 1 ELSE 0 END) as contracts,
                SUM(CASE WHEN i.category='grants' THEN 1 ELSE 0 END) as grants,
                COUNT(i.id) as total
            FROM departments d
            LEFT JOIN indicators i ON d.id = i.dept_id
            GROUP BY d.id
            ORDER BY d.id
        """)
        return await cur.fetchall()


async def get_all_detailed_entries() -> list:
    """Возвращает все добавленные отчёты со всеми деталями"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT i.id, i.created_at, d.id as dept_id, d.name as dept_name, d.head_name,
                       i.category, i.title, i.authors, i.year, i.journal, i.doi, i.file_path, i.file_id, i.notes,
                       i.country, i.journal_name, i.pub_date, i.url,
                       i.authors_count, i.specialty, i.reg_number, i.publisher, i.amount
                FROM indicators i
                JOIN departments d ON i.dept_id = d.id
                ORDER BY i.created_at DESC
            """)
            return [dict(r) for r in rows]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.id, i.created_at, d.id as dept_id, d.name as dept_name, d.head_name,
                   i.category, i.title, i.authors, i.year, i.journal, i.doi, i.file_path, i.file_id, i.notes,
                   i.country, i.journal_name, i.pub_date, i.url,
                   i.authors_count, i.specialty, i.reg_number, i.publisher, i.amount
            FROM indicators i
            JOIN departments d ON i.dept_id = d.id
            ORDER BY i.created_at DESC
        """)
        return await cur.fetchall()


async def get_files_for_zip(category: str = None, dept_id: int = None) -> list:
    """Возвращает все записи, у которых прикреплён файл (file_id не пустой)"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            query = """
                SELECT i.id, i.dept_id, d.name as dept_name, i.category,
                       i.title, i.authors, i.file_id, i.file_path, i.created_at
                FROM indicators i
                JOIN departments d ON i.dept_id = d.id
                WHERE (i.file_id IS NOT NULL AND i.file_id != '')
            """
            params = []
            if category:
                params.append(category)
                query += f" AND i.category = ${len(params)}"
            if dept_id:
                params.append(dept_id)
                query += f" AND i.dept_id = ${len(params)}"
            query += " ORDER BY i.category, i.dept_id, i.id"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT i.id, i.dept_id, d.name as dept_name, i.category,
                   i.title, i.authors, i.file_id, i.file_path, i.created_at
            FROM indicators i
            JOIN departments d ON i.dept_id = d.id
            WHERE (i.file_id IS NOT NULL AND i.file_id != '')
        """
        params = []
        if category:
            query += " AND i.category = ?"
            params.append(category)
        if dept_id:
            query += " AND i.dept_id = ?"
            params.append(dept_id)
        query += " ORDER BY i.category, i.dept_id, i.id"
        cur = await db.execute(query, tuple(params))
        return await cur.fetchall()


async def get_dept_entries(dept_id: int, limit: int = 15) -> list:
    """Возвращает список последних записей конкретной кафедры"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM indicators 
                WHERE dept_id = $1 
                ORDER BY created_at DESC 
                LIMIT $2
            """, dept_id, limit)
            return [dict(r) for r in rows]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM indicators 
            WHERE dept_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (dept_id, limit))
        return await cur.fetchall()


async def get_entries_by_category(category: str, dept_id: int = None, limit: int = 30) -> list:
    """Возвращает записи по категории. Если dept_id задан — только по кафедре."""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            if dept_id:
                rows = await conn.fetch("""
                    SELECT i.id, i.created_at, i.title, i.authors, i.file_id, i.file_path,
                           i.pub_date, i.journal_name, i.country, i.url,
                           i.specialty, i.reg_number, i.publisher, i.authors_count, i.amount,
                           d.name as dept_name
                    FROM indicators i
                    JOIN departments d ON i.dept_id = d.id
                    WHERE i.category = $1 AND i.dept_id = $2
                    ORDER BY i.created_at DESC
                    LIMIT $3
                """, category, dept_id, limit)
            else:
                rows = await conn.fetch("""
                    SELECT i.id, i.created_at, i.title, i.authors, i.file_id, i.file_path,
                           i.pub_date, i.journal_name, i.country, i.url,
                           i.specialty, i.reg_number, i.publisher, i.authors_count, i.amount,
                           d.name as dept_name
                    FROM indicators i
                    JOIN departments d ON i.dept_id = d.id
                    WHERE i.category = $1
                    ORDER BY i.created_at DESC
                    LIMIT $2
                """, category, limit)
            return [dict(r) for r in rows]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if dept_id:
            cur = await db.execute("""
                SELECT i.id, i.created_at, i.title, i.authors, i.file_id, i.file_path,
                       i.pub_date, i.journal_name, i.country, i.url,
                       i.specialty, i.reg_number, i.publisher, i.authors_count, i.amount,
                       d.name as dept_name
                FROM indicators i
                JOIN departments d ON i.dept_id = d.id
                WHERE i.category = ? AND i.dept_id = ?
                ORDER BY i.created_at DESC
                LIMIT ?
            """, (category, dept_id, limit))
        else:
            cur = await db.execute("""
                SELECT i.id, i.created_at, i.title, i.authors, i.file_id, i.file_path,
                       i.pub_date, i.journal_name, i.country, i.url,
                       i.specialty, i.reg_number, i.publisher, i.authors_count, i.amount,
                       d.name as dept_name
                FROM indicators i
                JOIN departments d ON i.dept_id = d.id
                WHERE i.category = ?
                ORDER BY i.created_at DESC
                LIMIT ?
            """, (category, limit))
        return await cur.fetchall()


async def delete_entry(entry_id: int, dept_id: int = None) -> bool:
    """Удаляет ошибочно добавленную запись"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            if dept_id:
                res = await conn.execute("DELETE FROM indicators WHERE id = $1 AND dept_id = $2", entry_id, dept_id)
            else:
                res = await conn.execute("DELETE FROM indicators WHERE id = $1", entry_id)
            return res.endswith(" 1") or (not res.endswith(" 0"))

    async with aiosqlite.connect(DB_PATH) as db:
        if dept_id:
            cur = await db.execute("DELETE FROM indicators WHERE id = ? AND dept_id = ?", (entry_id, dept_id))
        else:
            cur = await db.execute("DELETE FROM indicators WHERE id = ?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


async def clear_all_test_data():
    """Сбрасывает все тестовые данные (для админа перед официальным стартом)"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM indicators")
            return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM indicators")
        await db.commit()


async def get_entry_by_id(entry_id: int):
    """Возвращает запись отчёта по её ID вместе с данными кафедры"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            r = await conn.fetchrow("""
                SELECT i.*, d.name as dept_name, d.head_name
                FROM indicators i
                JOIN departments d ON i.dept_id = d.id
                WHERE i.id = $1
            """, entry_id)
            return dict(r) if r else None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.*, d.name as dept_name, d.head_name
            FROM indicators i
            JOIN departments d ON i.dept_id = d.id
            WHERE i.id = ?
        """, (entry_id,))
        r = await cur.fetchone()
        return dict(r) if r else None


async def update_entry_title(entry_id: int, new_title: str, dept_id: int = None) -> bool:
    """Обновляет название/тему работы в базе данных"""
    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            if dept_id:
                await conn.execute(
                    "UPDATE indicators SET title = $1 WHERE id = $2 AND dept_id = $3",
                    new_title, entry_id, dept_id
                )
            else:
                await conn.execute(
                    "UPDATE indicators SET title = $1 WHERE id = $2",
                    new_title, entry_id
                )
            return True

    async with aiosqlite.connect(DB_PATH) as db:
        if dept_id:
            await db.execute(
                "UPDATE indicators SET title = ? WHERE id = ? AND dept_id = ?",
                (new_title, entry_id, dept_id)
            )
        else:
            await db.execute(
                "UPDATE indicators SET title = ? WHERE id = ?",
                (new_title, entry_id)
            )
        await db.commit()
        return True


async def update_entry_full(entry_id: int, data: dict) -> bool:
    """Полностью обновляет все поля записи отчёта по её ID.
    Файл обновляется только если в data есть непустой 'file_id'."""
    fields = ['title', 'authors', 'doi', 'country', 'journal_name', 'pub_date',
              'url', 'authors_count', 'specialty', 'reg_number', 'publisher', 'amount']

    pool = await get_pg_pool()
    if pool:
        async with pool.acquire() as conn:
            sets = ", ".join(f"{f} = ${i+1}" for i, f in enumerate(fields))
            params = [data.get(f, '') for f in fields]

            if data.get('file_id'):
                n = len(params)
                sets += f", file_id = ${n+1}, file_path = ${n+2}"
                params += [data['file_id'], data.get('file_path', '')]

            params.append(entry_id)
            await conn.execute(
                f"UPDATE indicators SET {sets} WHERE id = ${len(params)}",
                *params
            )
            return True

    async with aiosqlite.connect(DB_PATH) as db:
        sets = ", ".join(f"{f} = ?" for f in fields)
        params = [data.get(f, '') for f in fields]

        if data.get('file_id'):
            sets += ", file_id = ?, file_path = ?"
            params += [data['file_id'], data.get('file_path', '')]

        params.append(entry_id)
        await db.execute(f"UPDATE indicators SET {sets} WHERE id = ?", tuple(params))
        await db.commit()
        return True
