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

    # Mualliflar bo'yicha hisob-kitob (shaffoflik)
    author_counts = {}
    works_lines = []
    for idx, e in enumerate(entries[:50], 1):
        cat = CATEGORY_LABELS.get(e.get("category", ""), e.get("category", ""))
        title = (e.get("title") or "—").strip()
        authors = (e.get("authors") or "Ko'rsatilmagan").strip()

        # Mualliflar chastotasi
        for raw_author in authors.replace(";", ",").split(","):
            a_clean = raw_author.strip()
            if a_clean and len(a_clean) > 2 and a_clean.lower() not in ["va boshqalar", "et al", "—"]:
                author_counts[a_clean] = author_counts.get(a_clean, 0) + 1

        works_lines.append(f"  {idx}. [{cat}] «{title}» — Muallif(lar): {authors}")

    works_text = "\n".join(works_lines) or "  Ma'lumot yo'q"

    # Mualliflar reytingi
    top_authors = sorted(author_counts.items(), key=lambda x: -x[1])
    authors_summary = "\n".join(f"  • {name} — {cnt} ta ish" for name, cnt in top_authors[:15]) or "  Ma'lumot yo'q"

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi tahlilchisisiz.

Quyidagi kafedra ma'lumotlari asosida O'ZBEK TILIDA rasmiy ilmiy-analitik tahlil matnini yozing.
MUHIM TALAB: Barcha hisobotlar va tahlillar shaffof bo'lishi uchun MUALLIFLARNING ISM-SHARIFLARINI (F.I.Sh.) matnda alohida qayd etib, kim qanday ish bajarganini aniq ko'rsating.

=== KAFEDRA MA'LUMOTLARI ===
Nomi: {dept_name}
Mudiri: {head_name or "Ko'rsatilmagan"}
Jami topshirilgan ishlar: {total} ta

=== YO'NALISHLAR BO'YICHA STATISTIKA ===
{stats_text}

=== ENG FAOL MUALLIFLAR (KAFEDRA BO'YICHA) ===
{authors_summary}

=== TOPSHIRILGAN ISHLAR VA MUALLIFLAR RO'YXATI ===
{works_text}

=== TUZILMA (O'ZBEK TILIDA ILMIY USLUBDA) ===
Quyidagi 6 ta bo'lim bo'yicha batafsil yozing:

1. **Umumiy baho** — kafedraning 2026 yildagi ilmiy faolligi, jami ishlar soni va darajasi
2. **Mualliflar va ularning ilmiy hissasi (Shaffoflik)** — kafedrada eng ko'p natija ko'rsatgan mualliflarning (F.I.Sh.) ism-shariflarini birma-bir sanab, ularning maqola, patent yoki dissertatsiyalarini ochiq tahlil qiling
3. **Kuchli yo'nalishlar** — Scopus/WoS, OAK maqolalari, patentlar, monografiyalar qaysi mualliflar tomonidan samarali bajarilgan
4. **Mavjud muammolar va kamchiliklar** — qaysi yo'nalishlarda ishlar yetarli emas, qaysi sohalarga e'tibor qaratilmagan
5. **Amaliy taklif va tavsiyalar** — kafedra ilmiy salohiyatini oshirish, mualliflarni xalqaro jurnallarga jalb qilish bo'yicha tavsiyalar
6. **Xulosa** — kafedraning umumiy ilmiy salohiyatiga yakuniy xulosa

Matn rasmiy, ilmiy va aniq bo'lsin. Har bir band kamida 2-3 jumladan iborat bo'lsin.
Teglar, yulduzchalar ishlatma - toza paragraflar ko'rinishida yoz.
"""


def _build_full_prompt(all_data: list, detailed_entries: list = None) -> str:
    total_all = 0
    dept_stats = []
    for row in all_data:
        name = row.get("name", "?")
        total = sum(row.get(k, 0) for k in CATEGORY_LABELS)
        total_all += total
        dept_stats.append((name, total, row))

    dept_stats.sort(key=lambda x: -x[1])
    top5 = "\n".join(f"  {i+1}. {n} — {c} ta" for i, (n, c, _) in enumerate(dept_stats[:5]))
    bot5 = "\n".join(f"  {i+1}. {n} — {c} ta" for i, (n, c, _) in enumerate(dept_stats[-5:]))

    dept_lines = []
    for n, c, row in dept_stats[:30]:
        parts = [f"{CATEGORY_LABELS[k]}: {row[k]}" for k in CATEGORY_LABELS if row.get(k, 0)]
        dept_lines.append(f"  • {n}: jami {c} ta ({', '.join(parts[:4])})")
    dept_text = "\n".join(dept_lines)

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
        for i, (author, info) in enumerate(sorted_authors[:20], 1):
            d_name = next(iter(info["depts"])) if info["depts"] else "Kafedra"
            lines.append(f"  {i}. {author} ({d_name}) — {info['count']} ta ish ({', '.join(list(info['cats'])[:2])})")
        if lines:
            authors_ranking_text = "\n".join(lines)

    return f"""Siz ADTI (Andijon davlat tibbiyot instituti) ilmiy bo'limi bosh tahlilchisisiz.

Quyidagi barcha 65 ta kafedra ma'lumotlari asosida O'ZBEK TILIDA institut bo'yicha umumiy tahlil hisobotini yozing.
MUHIM TALAB: Hisobot to'liq shaffof va ochiq bo'lishi uchun INSTITUTNING YETAKCHI VA ENG FAOL MUALLIFLARINING ISM-SHARIFLARINI (F.I.Sh.) va ularning ilmiy natijalarini matnda aniq ko'rsating.

=== UMUMIY STATISTIKA ===
Jami kafedralar: {len(all_data)} ta
Jami topshirilgan ilmiy ishlar: {total_all} ta

=== TOP-5 YETAKCHI KAFEDRALAR ===
{top5}

=== ENG KAM FAOL KAFEDRALAR ===
{bot5}

=== INSTITUTNING ENG FAOL MUALLIFLARI VA OLIMLARI (SHAFFOFLIK) ===
{authors_ranking_text}

=== KAFEDRALAR BO'YICHA QISQA TAFSILOT ===
{dept_text}

=== TUZILMA (O'ZBEK TILIDA ILMIY USLUBDA) ===
Quyidagi 6 ta bo'lim bo'yicha batafsil yozing:

1. **Institutning umumiy ilmiy faoliyati** — jami ko'rsatkichlar, ilmiy dinamika va natijalar
2. **Yetakchi kafedralar va ularning ko'rsatkichlari** — reytingda yuqori o'rinlarni egallagan kafedralar tahlili
3. **Institutning eng faol mualliflari va tadqiqotchilari (Shaffoflik)** — ilmiy faoliyatda eng ko'p maqola, patent va monografiyalar muallifi bo'lgan olimlar va xodimlarning ism-shariflarini (F.I.Sh.) va ularning kafedralarini ochiq qayd eting
4. **Zaif kafedralar va mavjud muammolar** — past ko'rsatkichga ega kafedralar, yosh olimlar yetishmasligi va Scopus/WoS nashrlari kamligi
5. **Yo'nalishlar bo'yicha tahlil** — Scopus/WoS, patentlar, dissertatsiyalar (DSc/PhD), xo'jalik shartnomalari holati
6. **Strategik takliflar va xulosa** — institut ilmiy salohiyatini oshirish, mualliflarni rag'batlantirish bo'yicha rahbariyatga xulosa

Matn rasmiy, ilmiy va aniq bo'lsin. Har bir band 3-4 jumladan iborat bo'lsin.
Teglar, yulduzchalar ishlatma - toza paragraflar ko'rinishida yoz.
"""


async def generate_dept_analysis(dept_name: str, head_name: str,
                                  summary: dict, entries: list) -> str:
    """Bitta kafedra uchun AI tahlil matni (mualliflar shaffofligi bilan)."""
    prompt = _build_dept_prompt(dept_name, head_name, summary, entries)
    return await _call_gemini(prompt)


async def generate_full_analysis(all_data: list, detailed_entries: list = None) -> str:
    """Barcha kafedralar bo'yicha umumiy AI tahlil matni (mualliflar shaffofligi bilan)."""
    prompt = _build_full_prompt(all_data, detailed_entries)
    return await _call_gemini(prompt)