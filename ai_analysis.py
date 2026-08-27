"""
ai_analysis.py - Gemini API orqali ilmiy faoliyat tahlili
Gemini REST API (aiohttp orqali, qoshimcha SDK kerak emas)
"""

import asyncio
import aiohttp
import logging
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

AVAILABLE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
    "gemini-flash-latest",
]

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

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 4096,
        }
    }

    last_error = ""
    async with aiohttp.ClientSession() as session:
        for model in AVAILABLE_MODELS:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={GEMINI_API_KEY}"
            )
            try:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=50)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if (
                            candidates
                            and "content" in candidates[0]
                            and "parts" in candidates[0]["content"]
                        ):
                            text = candidates[0]["content"]["parts"][0].get("text", "")
                            if text:
                                return text
                    elif resp.status == 429:
                        last_error = "Rate limit (429)"
                        logger.warning(f"Model {model} hit 429, trying next model in pool...")
                    else:
                        text = await resp.text()
                        last_error = f"{resp.status}: {text[:120]}"
                        logger.warning(f"Model {model} returned {resp.status}, trying next...")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Model {model} exception {e}, trying next...")

            await asyncio.sleep(0.5)

    return f"❌ Gemini xatosi ({last_error}). Iltimos, 1 daqiqadan so'ng qayta urinib ko'ring."
def _build_dept_prompt(dept_name: str, head_name: str, summary: dict, entries: list) -> str:
    total = sum(summary.get(k, 0) for k in CATEGORY_LABELS)
    stats_lines = [
        f"  • {CATEGORY_LABELS[k]}: {summary[k]} ta"
        for k in CATEGORY_LABELS if summary.get(k, 0) > 0
    ]
    stats_text = "\n".join(stats_lines) or "  Ma'lumot yo'q"

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi tahlilchisisiz.

Quyidagi kafedra ma'lumotlari asosida O'ZBEK TILIDA rasmiy ilmiy-tahliliy hisobot matnini yozing:

=== KAFEDRA MA'LUMOTLARI ===
Nomi: {dept_name}
Mudiri: {head_name or "Ko'rsatilmagan"}
Jami topshirilgan ilmiy ishlar: {total} ta

=== YO'NALISHLAR BO'YICHA NATIJALAR ===
{stats_text}

=== TALAB QILINADIGAN TUZILMA ===
Quyidagi 4 ta bo'lim bo'yicha ravon, mantiqiy va rasmiy tahliliy matn yozing:

1. **Umumiy baho** — kafedraning 2026 yildagi ilmiy faolligi, umumiy salohiyati va natijalari
2. **Kuchli yo'nalishlar** — yuqori ko'rsatkichga erishilgan ilmiy yo'nalishlar tahlili
3. **Mavjud kamchiliklar** — e'tibor qaratish kerak bo'lgan, sust rivojlangan sohalar
4. **Tavsiyalar va yakuniy xulosa** — kafedra ilmiy salohiyatini oshirish bo'yicha amaliy takliflar va xulosa
"""


def _build_full_prompt(all_data: list, detailed_entries: list = None) -> str:
    total_all = 0
    dept_stats = []
    category_totals = {k: 0 for k in CATEGORY_LABELS}

    for row in all_data:
        name = row.get("name", "?")
        total = sum(row.get(k, 0) for k in CATEGORY_LABELS)
        total_all += total
        for k in CATEGORY_LABELS:
            category_totals[k] += (row.get(k, 0) or 0)
        dept_stats.append((name, total, row))

    dept_stats.sort(key=lambda x: -x[1])
    top5 = "\n".join(f"  {i+1}. {n} — {c} ta ish" for i, (n, c, _) in enumerate(dept_stats[:5]))
    bot5 = "\n".join(f"  {i+1}. {n} — {c} ta ish" for i, (n, c, _) in enumerate(dept_stats[-5:]))

    cat_lines = [
        f"  • {CATEGORY_LABELS[k]}: {v} ta"
        for k, v in category_totals.items() if v > 0
    ]
    cat_text = "\n".join(cat_lines)

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi bosh tahlilchisisiz.

Quyidagi barcha 65 ta kafedra ma'lumotlari asosida O'ZBEK TILIDA institut bo'yicha umumiy ilmiy-tahliliy hisobot yozing.

=== INSTITUTNING UMUMIY STATISTIKASI ===
Jami kafedralar: {len(all_data)} ta
Jami topshirilgan ilmiy ishlar: {total_all} ta

=== YO'NALISHLAR BO'YICHA UMUMIY NATIJALAR ===
{cat_text}

=== TOP-5 YETAKCHI KAFEDRALAR ===
{top5}

=== ENG KAM FAOL KAFEDRALAR ===
{bot5}

=== TALAB QILINADIGAN TUZILMA ===
Quyidagi 5 ta bo'lim bo'yicha rasmiy, ravon, ilmiy va tushunarli umumiy tahliliy matn yozing:

1. **Institutning umumiy ilmiy faoliyati** — 2026 yildagi umumiy ko'rsatkichlar, ilmiy dinamika va institutning umumiy ilmiy faolligi
2. **Yetakchi kafedralar va asosiy yutuqlar** — TOP-5 yetakchi kafedralar faoliyati va ularning institut ilmiy salohiyatidagi hissasi
3. **Mavjud muammolar va sust sohalar** — past ko'rsatkichga ega kafedralar, xalqaro nashrlar (Scopus/WoS) va patentlar yetishmasligi
4. **Ilmiy yo'nalishlar bo'yicha tahlil** — Scopus/WoS maqolalari, dissertatsiyalar (DSc/PhD), patentlar, monografiyalar va xo'jalik shartnomalari holati
5. **Strategik tavsiyalar va yakuniy xulosa** — institut ilmiy nufuzini yanada oshirish bo'yicha rahbariyatga amaliy tavsiyalar va yakuniy xulosa
"""


async def generate_dept_analysis(dept_name: str, head_name: str,
                                  summary: dict, entries: list) -> str:
    """Bitta kafedra uchun umumiy AI tahlil matni."""
    prompt = _build_dept_prompt(dept_name, head_name, summary, entries)
    return await _call_gemini(prompt)


async def generate_full_analysis(all_data: list, detailed_entries: list = None) -> str:
    """Barcha kafedralar bo'yicha umumiy institut AI tahlil matni."""
    prompt = _build_full_prompt(all_data, detailed_entries)
    return await _call_gemini(prompt)