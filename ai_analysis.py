"""
ai_analysis.py - Gemini API orqali ilmiy faoliyat tahlili
Gemini REST API (aiohttp orqali, qoshimcha SDK kerak emas)
"""

import aiohttp
import logging
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.6-flash:generateContent?key={key}"
)

CATEGORY_LABELS = {
    "dsc":            "DSc dissertatsiya himoyasi",
    "phd":            "PhD dissertatsiya himoyasi",
    "monography":     "Monografiya",
    "patent":         "Patent",
    "oak_uz":         "OAK jurnali maqolasi",
    "oak_ru_if":      "Rossiya OAK / Impact-faktor jurnali",
    "thesis_uz":      "Respublika konferensiya tezisi",
    "thesis_foreign": "Xorijiy konferensiya tezisi",
    "scopus_wos":     "Scopus / Web of Science maqolasi",
    "rationalizer":   "Ratsionalizatorlik taklifi",
    "implementation": "Amaliyotga tadbiq etilgan ish",
    "conferences":    "Kafedra o'tkazgan anjuman",
    "contracts":      "Xo'jalik shartnomasi",
    "grants":         "Grant",
}


async def _call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return (
            "Gemini API kaliti o'rnatilmagan.\n"
            "Render -> Environment -> GEMINI_API_KEY qiymatini kiriting."
        )

    url = GEMINI_URL.format(key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=90)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Gemini API error {resp.status}: {text[:300]}")
                    return f"Gemini xatosi ({resp.status}). Keyinroq urinib ko'ring."
                data = await resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return "Gemini javob bermadi. Keyinroq urinib ko'ring."
                return candidates[0]["content"]["parts"][0]["text"]
    except aiohttp.ClientError as e:
        logger.error(f"Gemini network error: {e}")
        return "Tarmoq xatosi. Keyinroq urinib ko'ring."
    except Exception as e:
        logger.error(f"Gemini unexpected error: {e}")
        return f"Kutilmagan xato: {type(e).__name__}"


def _build_dept_prompt(dept_name: str, head_name: str, summary: dict, entries: list) -> str:
    total = sum(summary.get(k, 0) for k in CATEGORY_LABELS)
    stats_lines = [
        f"  - {CATEGORY_LABELS[k]}: {summary[k]} ta"
        for k in CATEGORY_LABELS if summary.get(k, 0) > 0
    ]
    stats_text = "\n".join(stats_lines) or "  Ma'lumot yo'q"

    works_lines = []
    for e in entries[:25]:
        cat = CATEGORY_LABELS.get(e.get("category", ""), e.get("category", ""))
        title = e.get("title", "")[:80]
        authors = e.get("authors", "")[:50]
        works_lines.append(f"  - [{cat}] {title} ({authors})")
    works_text = "\n".join(works_lines) or "  Ma'lumot yo'q"

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi tahlilchisisiz.

Quyidagi kafedra ma'lumotlari asosida O'ZBEK TILIDA batafsil ilmiy-analitik matn yozing.

=== KAFEDRA ===
Nomi: {dept_name}
Mudiri: {head_name or "Ko'rsatilmagan"}
Jami topshirilgan ishlar: {total} ta

=== YO'NALISHLAR BO'YICHA STATISTIKA ===
{stats_text}

=== TOPSHIRILGAN ISHLAR (namuna) ===
{works_text}

=== TUZILMA ===
Quyidagi struktura bo'yicha O'ZBEKCHA rasmiy ilmiy tahlil yozing:

1. Umumiy baho - kafedra necha yo'nalishda faoliyat ko'rsatgan, jami ishlar soni va darajasi
2. Kuchli tomonlar - qaysi yo'nalishlar yaxshi rivojlangan va nima uchun
3. Mavjud muammolar - qaysi yo'nalishlar bo'sh yoki yo'q, nimaga e'tibor kam
4. Taklif va tavsiyalar - kafedra qaysi sohalarni rivojlantirishi zarur
5. Xulosa - kafedra ilmiy salohiyati va kelajakdagi imkoniyatlari

Matn rasmiy, ilmiy va aniq bo'lsin. Har bir band kamida 2-3 jumladan iborat bo'lsin.
Teglar, yulduzchalar ishlatma - oddiy paragraflar ko'rinishida yoz.
"""


def _build_full_prompt(all_data: list) -> str:
    total_all = 0
    dept_stats = []
    for row in all_data:
        name = row.get("name", "?")
        total = sum(row.get(k, 0) for k in CATEGORY_LABELS)
        total_all += total
        dept_stats.append((name, total, row))

    dept_stats.sort(key=lambda x: -x[1])
    top5 = "\n".join(f"  {i+1}. {n} - {c} ta" for i, (n, c, _) in enumerate(dept_stats[:5]))
    bot5 = "\n".join(f"  {i+1}. {n} - {c} ta" for i, (n, c, _) in enumerate(dept_stats[-5:]))

    dept_lines = []
    for n, c, row in dept_stats[:35]:
        parts = [f"{CATEGORY_LABELS[k]}: {row[k]}" for k in CATEGORY_LABELS if row.get(k, 0)]
        dept_lines.append(f"  {n}: jami {c} ta ({', '.join(parts[:4])})")
    dept_text = "\n".join(dept_lines)

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi bosh tahlilchisisiz.

Quyidagi barcha kafedralar ma'lumotlari asosida O'ZBEK TILIDA institut bo'yicha umumiy tahlil yozing.

=== UMUMIY STATISTIKA ===
Jami kafedralar: {len(all_data)} ta
Jami topshirilgan ishlar: {total_all} ta

=== TOP-5 FAOL KAFEDRALAR ===
{top5}

=== ENG KAM FAOL KAFEDRALAR ===
{bot5}

=== KAFEDRALAR BO'YICHA TAFSILOT ===
{dept_text}

=== TUZILMA ===
Quyidagi struktura bo'yicha O'ZBEKCHA rasmiy ilmiy tahlil yozing:

1. Institutning umumiy ilmiy faoliyati - jami ko'rsatkichlar, natijalar
2. Eng faol kafedralar - liderlar va ularning ko'rsatkichlari
3. Zaif kafedralar - kam faol kafedralar va muammolari
4. Yo'nalishlar bo'yicha tahlil - Scopus/WoS, patent, dissertatsiya va boshqalar holati
5. Strategik takliflar - institut ilmiy salohiyatini oshirish uchun choralar
6. Xulosa - 2026 yil yakuni va kelajakka istiqbol

Matn rasmiy, ilmiy va aniq bo'lsin. Har bir band 3-4 jumladan iborat bo'lsin.
Teglar, yulduzchalar ishlatma - oddiy paragraflar ko'rinishida yoz.
"""


async def generate_dept_analysis(dept_name: str, head_name: str,
                                  summary: dict, entries: list) -> str:
    """Bitta kafedra uchun AI tahlil matni."""
    prompt = _build_dept_prompt(dept_name, head_name, summary, entries)
    return await _call_gemini(prompt)


async def generate_full_analysis(all_data: list) -> str:
    """Barcha kafedralar bo'yicha umumiy AI tahlil matni."""
    prompt = _build_full_prompt(all_data)
    return await _call_gemini(prompt)