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

    # Mualliflar bo'yicha hisob-kitob (shaffoflik)
    author_counts = {}
    works_lines = []
    for idx, e in enumerate(entries[:50], 1):
        cat = CATEGORY_LABELS.get(e.get("category", ""), e.get("category", ""))
        title = (e.get("title") or "—").strip()
        authors = (e.get("authors") or "Ko'rsatilmagan").strip()

        for raw_author in authors.replace(";", ",").split(","):
            a_clean = raw_author.strip()
            if a_clean and len(a_clean) > 2 and a_clean.lower() not in ["va boshqalar", "et al", "—"]:
                author_counts[a_clean] = author_counts.get(a_clean, 0) + 1

        works_lines.append(f"  {idx}. [{cat}] «{title}» — Muallif(lar): {authors}")

    works_text = "\n".join(works_lines) or "  Ma'lumot yo'q"

    top_authors = sorted(author_counts.items(), key=lambda x: -x[1])
    authors_summary = "\n".join(f"  • {name} — {cnt} ta ish" for name, cnt in top_authors[:15]) or "  Ma'lumot yo'q"

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi tahlilchisisiz.

Quyidagi kafedra ma'lumotlari asosida O'ZBEK TILIDA QISQA, LAKONIK, ANIQ va OPTIMAL tahlil matnini yozing.
ORTIQCHA SUV VA UZUN GAPLAR BO'LMASIN. FAQAT ANIQ FAKTLAR, MUALLIFLAR VA NATIJALAR BO'LSIN.

=== KAFEDRA MA'LUMOTLARI ===
Nomi: {dept_name}
Mudiri: {head_name or "Ko'rsatilmagan"}
Jami topshirilgan ishlar: {total} ta

=== YO'NALISHLAR BO'YICHA NATIJALAR ===
{stats_text}

=== MUALLIFLAR VA ULARNING ISHLARI SONI (SHAFFOFLIK) ===
{authors_summary}

=== TOPSHIRILGAN ISHLAR VA MUALLIFLAR RO'YXATI ===
{works_text}

=== TALAB QILINADIGAN STRUKTURA (QISQA VA ANIQ) ===
1. **Kafedra ilmiy natijalari qisqacha** (Jami ishlar va asosiy yo'nalishlar)
2. **Mualliflar va ularning ilmiy hissasi (Shaffoflik)** (Kafedrada ish topshirgan mualliflarning ism-shariflari va hissalari)
3. **Kuchli va zaif yo'nalishlar** (2-3 ta aniq punkt)
4. **Takliflar va xulosa** (2-3 ta aniq amaliy punkt)

Matn toza, tushunarli, optimal bo'lsin.
"""


def _build_full_prompt(all_data: list, detailed_entries: list = None) -> str:
    total_all = 0
    dept_stats = []
    for row in all_data:
        name = row.get("name", "?")
        total = sum(row.get(k, 0) for k in CATEGORY_LABELS)
        total_all += total
        if total > 0:
            dept_stats.append((name, total, row))

    # Reyting: eng ko'p ish topshirgan kafedralar
    dept_stats.sort(key=lambda x: -x[1])
    leaderboard_lines = []
    for i, (n, c, row) in enumerate(dept_stats, 1):
        parts = [f"{CATEGORY_LABELS[k]}: {row[k]}" for k in CATEGORY_LABELS if row.get(k, 0) > 0]
        leaderboard_lines.append(f"  {i}. {n} — {c} ta ish ({', '.join(parts[:3])})")
    leaderboard_text = "\n".join(leaderboard_lines) or "  Hozircha ishlar topshirilmagan"

    # Ish kiritmagan kafedralar
    zero_depts = [row.get("name", "?") for row in all_data if sum(row.get(k, 0) for k in CATEGORY_LABELS) == 0]
    zero_depts_text = f"  Jami {len(zero_depts)} ta kafedrada hali ishlar kiritilmagan."

    # Institut bo'yicha eng faol mualliflar reytingi (shaffoflik)
    authors_ranking_text = "  Ma'lumot yo'q"
    if detailed_entries:
        inst_authors = {}
        for d in detailed_entries:
            auth_str = (d.get("authors") or "").strip()
            dept_name = d.get("dept_name") or ""
            for raw_author in auth_str.replace(";", ",").split(","):
                a_clean = raw_author.strip()
                if a_clean and len(a_clean) > 2 and a_clean.lower() not in ["va boshqalar", "et al", "—"]:
                    if a_clean not in inst_authors:
                        inst_authors[a_clean] = {"count": 0, "depts": set(), "cats": set()}
                    inst_authors[a_clean]["count"] += 1
                    if dept_name:
                        inst_authors[a_clean]["depts"].add(dept_name)
                    if d.get("category"):
                        inst_authors[a_clean]["cats"].add(CATEGORY_LABELS.get(d["category"], d["category"]))

        sorted_authors = sorted(inst_authors.items(), key=lambda x: -x[1]["count"])
        lines = []
        for i, (author, info) in enumerate(sorted_authors[:15], 1):
            d_name = next(iter(info["depts"])) if info["depts"] else "Kafedra"
            lines.append(f"  {i}. {author} ({d_name}) — {info['count']} ta ish ({', '.join(list(info['cats'])[:2])})")
        if lines:
            authors_ranking_text = "\n".join(lines)

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi tahlilchisisiz.

Quyidagi ma'lumotlar asosida O'ZBEK TILIDA QISQA, LAKONIK, ANIQ va OPTIMAL tahlil hisobotini yozing.
ORTIQCHA SUV VA UZUN GAPLAR BO'LMASIN. FAQAT ANIQ RAQAMLAR, ENG KO'P ISH TOPSHIRGAN KAFEDRALAR RO'YXATI, MUALLIFLAR VA XULOSALAR BO'LSIN.

=== UMUMIY KO'RSATKICHLAR ===
Jami kafedralar: {len(all_data)} ta
Faol (ish topshirgan) kafedralar: {len(dept_stats)} ta
Jami topshirilgan ilmiy ishlar: {total_all} ta

=== ENG KO'P ISH TOPSHIRGAN KAFEDRALAR RO'YXATI (REYTING) ===
{leaderboard_text}

=== ENG FAOL MUALLIFLAR VA OLIMLAR (SHAFFOFLIK) ===
{authors_ranking_text}

=== ISH TOPSHIRMAGAN KAFEDRALAR ===
{zero_depts_text}

=== TALAB QILINADIGAN STRUKTURA (QISQA VA ANIQ) ===
1. **Umumiy statistika** (1-2 gap: jami ishlar, faol kafedralar soni)
2. **Eng ko'p ish topshirgan kafedralar ro'yxati (Reyting)** (Eng ko'p ish qo'shgan kafedralarning to'liq reytingi va topshirgan ishlari soni)
3. **Eng faol mualliflar (Shaffoflik)** (Institutning eng ko'p ish topshirgan yetakchi mualliflari ism-shariflari)
4. **Asosiy kamchiliklar va passiv kafedralar** (Ish topshirmagan yoki kam topshirgan kafedralar haqida qisqa)
5. **Rahbariyat uchun takliflar va xulosa** (3-4 ta aniq punkt)

Matn toza, lo'nda va professional bo'lsin.
"""


async def generate_dept_analysis(dept_name: str, head_name: str,
                                  summary: dict, entries: list) -> str:
    """Bitta kafedra uchun optimal AI tahlil matni."""
    prompt = _build_dept_prompt(dept_name, head_name, summary, entries)
    return await _call_gemini(prompt)


async def generate_full_analysis(all_data: list, detailed_entries: list = None) -> str:
    """Barcha kafedralar bo'yicha optimal AI tahlil matni (kafedralar reytingi bilan)."""
    prompt = _build_full_prompt(all_data, detailed_entries)
    return await _call_gemini(prompt)