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
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-flash-latest",
    "gemma-4-31b-it",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]

CATEGORY_LABELS = {
    "dsc":            "DSc dissertatsiya",
    "phd":            "PhD dissertatsiya",
    "monography":     "Monografiya",
    "patent":         "Patent",
    "oak_uz":         "OAK jurnali maqolasi",
    "oak_ru_if":      "Rossiya OAK / Impact-faktor",
    "thesis_uz":      "Respublika tezisi",
    "thesis_foreign": "Xorijiy tezis",
    "scopus_wos":     "Scopus / Web of Science",
    "rationalizer":   "Ratsionalizatorlik",
    "implementation": "Amaliyotga tadbiq",
    "conferences":    "Anjuman",
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
            "temperature": 0.5,
            "maxOutputTokens": 1500,
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
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=25)
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
                        logger.warning(f"Model {model} hit 429, trying next...")
                    elif resp.status == 503:
                        last_error = "High demand (503)"
                        logger.warning(f"Model {model} hit 503, trying next...")
                    else:
                        text = await resp.text()
                        last_error = f"{resp.status}"
                        logger.warning(f"Model {model} returned {resp.status}, trying next...")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Model {model} exception {e}, trying next...")

            await asyncio.sleep(0.3)

    return f"❌ Gemini xizmati vaqtincha band ({last_error}). Iltimos, 30 soniyadan so'ng qayta bosing."


def _build_dept_prompt(dept_name: str, head_name: str, summary: dict, entries: list) -> str:
    total = sum(summary.get(k, 0) for k in CATEGORY_LABELS)
    stats_lines = [
        f"  • {CATEGORY_LABELS[k]}: {summary[k]} ta"
        for k in CATEGORY_LABELS if summary.get(k, 0) > 0
    ]
    stats_text = "\n".join(stats_lines) or "  Ma'lumot yo'q"

    return f"""Siz ADTI ilmiy bo'limi tahlilchisisiz.

Quyidagi kafedra ma'lumotlari asosida O'ZBEK TILIDA JUDA QISQA, ANIQ va LO'NDA tahlil matnini yozing (ortiqcha suvsiz, ixcham):

=== KAFEDRA MA'LUMOTLARI ===
Nomi: {dept_name}
Mudiri: {head_name or "Ko'rsatilmagan"}
Jami ishlar: {total} ta

=== NATIJALAR ===
{stats_text}

=== STRUKTURA (QISQA VA ANIQ) ===
1. **Umumiy baho:** Kafedraning jami ko'rsatkichlari (1-2 jumla).
2. **Kuchli yo'nalishlar:** Qaysi sohalar yaxshi ishlagan (2-3 punkt).
3. **Kamchiliklar va e'tibor qaratish kerak bo'lgan sohalar:** (2-3 punkt).
4. **Takliflar:** Kafedra salohiyatini oshirish bo'yicha 2 ta aniq taklif.
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
    active_count = len([x for x in dept_stats if x[1] > 0])
    zero_count = len([x for x in dept_stats if x[1] == 0])

    top5 = "\n".join(f"  {i+1}. {n} — {c} ta ish" for i, (n, c, _) in enumerate(dept_stats[:5]))

    cat_lines = [
        f"  • {CATEGORY_LABELS[k]}: {v} ta"
        for k, v in category_totals.items() if v > 0
    ]
    cat_text = "\n".join(cat_lines)

    return f"""Siz ADTI ilmiy bo'limi tahlilchisisiz.

Quyidagi ma'lumotlar asosida O'ZBEK TILIDA JUDA QISQA, LO'NDA, ANIQ va SUVSIS tahlil xulosasini bering (faqat 4 ta ixcham blok):

=== MA'LUMOTLAR ===
• Jami kafedralar: {len(all_data)} ta (Faol: {active_count} ta, Ish topshirmagan: {zero_count} ta)
• Jami ilmiy ishlar: {total_all} ta

=== ASOSIY YO'NALISHLAR ===
{cat_text}

=== TOP-5 YETAKCHI KAFEDRALAR ===
{top5}

=== STRUKTURA (FAQAT 4 TA QISQA BLOK) ===
1. **Umumiy holat (Qisqacha svodka):** Jami ishlar soni, faollik foizi va asosiy yo'nalishlar (2-3 jumla).
2. **TOP-5 yetakchi kafedralar:** Eng faol 5 ta kafedra ro'yxati va ularning asosiy yutug'i.
3. **Asosiy muammolar va sust sohalar:** 0 ko'rsatkichli kafedralar, Scopus/patentlar kamligi (3-4 punkt).
4. **Rahbariyat uchun amaliy takliflar:** 3 ta aniq va lo'nda taklif.

Matn toza, aniq, rasmiy va suvsiz bo'lsin.
"""


async def generate_dept_analysis(dept_name: str, head_name: str,
                                  summary: dict, entries: list) -> str:
    """Bitta kafedra uchun qisqa va aniq AI tahlil matni."""
    prompt = _build_dept_prompt(dept_name, head_name, summary, entries)
    return await _call_gemini(prompt)


async def generate_full_analysis(all_data: list, detailed_entries: list = None) -> str:
    """Barcha kafedralar bo'yicha qisqa va lo'nda institut AI tahlil matni."""
    prompt = _build_full_prompt(all_data, detailed_entries)
    return await _call_gemini(prompt)