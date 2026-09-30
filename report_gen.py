import io
import asyncio
import logging
import re
from pathlib import Path
from docx import Document
from docx.shared import Pt
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from database import INDICATORS, INDICATOR_KEYS, INDICATOR_LABELS, RAW_DEPARTMENTS

logger = logging.getLogger(__name__)


def normalize_work_date(pub_date_str: str, year: int = 2026, created_at = None):
    """
    Returns (year: int, month: int, day: int, ym_str: str, source: str)
    Priority:
    1. Exact date (DD.MM.YYYY, YYYY-MM-DD, etc.)
    2. Month name in Uzbek / Russian / English
    3. Issue / number in journal (№1..12, No. 1..12, Issue 1..12, Son 1..12)
    4. Fallback to created_at (date submitted to bot)
    """
    s = str(pub_date_str or '').strip()
    s_lower = s.lower()
    
    # 1. Exact date DD.MM.YYYY, DD/MM/YYYY, DD-MM-YYYY
    m = re.search(r'\b(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{4})\b', s)
    if m:
        d, mth, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mth <= 12 and 1 <= d <= 31:
            return yr, mth, d, f"{yr:04d}-{mth:02d}", "exact_date"
        elif 1 <= d <= 12 and 1 <= mth <= 31:
            return yr, d, mth, f"{yr:04d}-{d:02d}", "exact_date"

    # YYYY.MM.DD
    m2 = re.search(r'\b(\d{4})[\.\/\-](\d{1,2})[\.\/\-](\d{1,2})\b', s)
    if m2:
        yr, mth, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        if 1 <= mth <= 12 and 1 <= d <= 31:
            return yr, mth, d, f"{yr:04d}-{mth:02d}", "exact_date"

    # YYYY.MM
    m3 = re.search(r'\b(\d{4})[\.\/\-](\d{1,2})\b', s)
    if m3:
        yr, mth = int(m3.group(1)), int(m3.group(2))
        if 1 <= mth <= 12:
            return yr, mth, 1, f"{yr:04d}-{mth:02d}", "exact_month"

    # 2. Month name
    months_map = {
        'yanvar': 1, 'январ': 1, 'jan': 1,
        'fevral': 2, 'феврал': 2, 'feb': 2,
        'mart': 3, 'март': 3, 'mar': 3,
        'aprel': 4, 'апрел': 4, 'apr': 4,
        'may': 5, 'май': 5,
        'iyun': 6, 'июн': 6, 'jun': 6,
        'iyul': 7, 'июл': 7, 'jul': 7,
        'avgust': 8, 'август': 8, 'aug': 8,
        'sentyabr': 9, 'sentabr': 9, 'сентябр': 9, 'sep': 9,
        'oktyabr': 10, 'oktabr': 10, 'октябр': 10, 'oct': 10,
        'noyabr': 11, 'ноябр': 11, 'nov': 11,
        'dekabr': 12, 'декабр': 12, 'dec': 12
    }
    for name, mth_num in months_map.items():
        if name in s_lower:
            y_m = re.search(r'\b(202[0-9]|201[0-9])\b', s)
            yr = int(y_m.group(1)) if y_m else (year or 2026)
            d_m = re.search(r'\b(\d{1,2})[\s\-]+' + name, s_lower)
            d = int(d_m.group(1)) if (d_m and 1 <= int(d_m.group(1)) <= 31) else 1
            return yr, mth_num, d, f"{yr:04d}-{mth_num:02d}", "month_name"

    # 3. Issue / Number in journal (№1..12, No. 1..12, Issue 1..12, Son 1..12)
    m_iss = re.search(r'(?:no\.?|№|issue|сон|выпуск|вып\.?)\s*(\d{1,2})\b', s_lower)
    if not m_iss:
        m_iss = re.search(r'\b(\d{1,2})\s*-\s*сон\b', s_lower)
    if not m_iss:
        m_iss = re.search(r'\(\s*(\d{1,2})\s*\)', s)
        
    if m_iss:
        iss_num = int(m_iss.group(1))
        if 1 <= iss_num <= 12:
            y_m = re.search(r'\b(202[0-9]|201[0-9])\b', s)
            yr = int(y_m.group(1)) if y_m else (year or 2026)
            return yr, iss_num, 1, f"{yr:04d}-{iss_num:02d}", "issue_number"

    # 4. Fallback: created_at date
    if created_at:
        try:
            yr = created_at.year
            mth = created_at.month
            d = created_at.day
            return yr, mth, d, f"{yr:04d}-{mth:02d}", "fallback_created_at"
        except Exception:
            pass

    yr = year or 2026
    return yr, 1, 1, f"{yr:04d}-01", "fallback_default"


def recompute_summary_for_period(filtered_detailed_rows: list) -> list:
    cats = ['scopus_wos', 'phd', 'dsc', 'monography', 'patent', 'oak_uz', 'oak_ru_if', 
            'thesis_uz', 'thesis_foreign', 'rationalizer', 'implementation', 'conferences', 'contracts', 'grants']
    
    summary_map = {}
    for d_id, d_name, d_head in RAW_DEPARTMENTS:
        summary_map[d_id] = {
            'id': d_id,
            'name': d_name,
            'head_name': d_head,
            'total': 0
        }
        for c in cats:
            summary_map[d_id][c] = 0
            
    for r in filtered_detailed_rows:
        did = r['dept_id']
        cat = r.get('category', '')
        if did in summary_map:
            summary_map[did]['total'] += 1
            if cat in summary_map[did]:
                summary_map[did][cat] += 1
                
    return list(summary_map.values())


async def generate_report_docx(summary_rows: list, detailed_rows: list = None, start_ym: str = None, end_ym: str = None) -> io.BytesIO:
    """
    Генерирует официальный Word-отчёт АДТИ по форме «2026 йил хисобот»
    Опционально фильтрует по интервалу start_ym .. end_ym (например: 2026-01 .. 2026-06)
    """
    doc = Document()

    # Enrich rows with normalized dates
    if detailed_rows:
        for d in detailed_rows:
            if '_norm_ym' not in d:
                yr, mth, day, ym, src = normalize_work_date(d.get('pub_date'), d.get('year'), d.get('created_at'))
                d['_norm_yr'] = yr
                d['_norm_mth'] = mth
                d['_norm_day'] = day
                d['_norm_ym'] = ym
                d['_norm_src'] = src

    period_title = ""
    if start_ym or end_ym:
        if start_ym and not end_ym: end_ym = start_ym
        if end_ym and not start_ym: start_ym = end_ym
        if detailed_rows:
            detailed_rows = [d for d in detailed_rows if start_ym <= d['_norm_ym'] <= end_ym]
        summary_rows = recompute_summary_for_period(detailed_rows or [])
        period_title = f"{start_ym} ДАВРИ" if start_ym == end_ym else f"{start_ym} — {end_ym} ДАВРИ"

    # Сортировка детальных строк по дате (свежие сверху)
    if detailed_rows:
        detailed_rows.sort(key=lambda d: (d.get('_norm_ym', ''), d.get('_norm_day', 0), d.get('id', 0)), reverse=True)

    # ─── 1-ҚИСМ: СВОДКА ТАБЛИЦАСИ ────────────────────────────────────────────
    title = doc.add_paragraph()
    title.alignment = 1  # CENTER
    run = title.add_run("АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ КАФЕДРАЛАРИ ТОМОНИДАН\n")
    run.bold = True
    run.font.size = Pt(12)
    header_subtitle = f"2026 ЙИЛ ({period_title}) ХИСОБОТИ" if period_title else "2026 ЙИЛ ХИСОБОТИ"
    run2 = title.add_run(f"{header_subtitle} — ИЛМИЙ ТАДҚИҚОТ ИШЛАРИ ТЎҒРИСИДА МАЪЛУМОТ")
    run2.bold = True
    run2.font.size = Pt(12)

    doc.add_paragraph()

    # Таблица: 17 столбцов как в оригинале
    col_headers = [
        "Т/Р", "Кафедра номи", "Кафедра мудири",
        "DSc", "PhD", "Монография", "Патент",
        "ЎзОАК", "Россия ОАК/IF",
        "Тезис (Ўз)", "Тезис (Хор)",
        "Scopus/WoS",
        "Рац.таклиф", "Амалиётга тадбиқ",
        "Анжуман", "Хўж.шартн.", "Грант"
    ]

    table = doc.add_table(rows=1, cols=len(col_headers))
    table.style = "Table Grid"

    # Заголовки
    hdr = table.rows[0].cells
    for i, h in enumerate(col_headers):
        hdr[i].text = h
        hdr[i].paragraphs[0].runs[0].bold = True

    # Данные
    totals = [0] * (len(col_headers) - 3)

    for r in summary_rows:
        row = table.add_row().cells
        row[0].text = str(r['id'])
        row[1].text = r['name']
        row[2].text = r['head_name'] or ''

        vals = [
            r['dsc'], r['phd'], r['monography'], r['patent'],
            r['oak_uz'], r['oak_ru_if'],
            r['thesis_uz'], r['thesis_foreign'],
            r['scopus_wos'],
            r['rationalizer'], r['implementation'],
            r['conferences'], '', ''
        ]

        for i, v in enumerate(vals):
            row[i + 3].text = str(v) if v else ''
            if isinstance(v, int):
                totals[i] += v

    # Итоговая строка
    total_row = table.add_row().cells
    total_row[0].text = ''
    total_row[1].text = 'ЖАМИ (Итого):'
    total_row[2].text = ''
    for i, t in enumerate(totals):
        total_row[i + 3].text = str(t) if t else ''

    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(8)

    # ─── 2-ҚИСМ: МУАЛЛИФЛАР ВА ИШЛАР БАТАФСИЛ РЎЙХАТИ (ИЛОВА) ─────────────────
    if detailed_rows:
        doc.add_page_break()

        p2 = doc.add_paragraph()
        p2.alignment = 1
        r_app = p2.add_run("ИЛОВА — 2-ҚИСМ\n")
        r_app.bold = True
        r_app.font.size = Pt(11)
        r_app2 = p2.add_run("КАФЕДРАЛАР КЕСИМИДА ИЛМИЙ ИШЛАР ВА УЛАРНИНГ МУАЛЛИФЛАРИ БАТАФСИЛ РЎЙХАТИ\n(ШАФФОФЛИК ҲИСОБОТИ)")
        r_app2.bold = True
        r_app2.font.size = Pt(12)

        p_info = doc.add_paragraph()
        sub_text = f" ({period_title})" if period_title else ""
        p_info.add_run(f"Жами рўйхатга олинган ишлар: {len(detailed_rows)} та{sub_text}\n").italic = True

        det_cols = [
            "Т/Р", "Кафедра номи", "Йўналиш",
            "Иш номи / Мавзу", "Муаллифлар (Ф.И.Ш.)", "Қўшимча маълумот (Журнал/Сана)"
        ]

        det_table = doc.add_table(rows=1, cols=len(det_cols))
        det_table.style = "Table Grid"

        det_hdr = det_table.rows[0].cells
        for i, h in enumerate(det_cols):
            det_hdr[i].text = h
            det_hdr[i].paragraphs[0].runs[0].bold = True

        for idx, d in enumerate(detailed_rows, 1):
            row = det_table.add_row().cells
            row[0].text = str(idx)
            row[1].text = str(d.get('dept_name', '') or f"Кафедра #{d.get('dept_id', '')}")
            cat_k = d.get('category', '')
            row[2].text = INDICATOR_LABELS.get(cat_k, cat_k)
            row[3].text = str(d.get('title', '') or '—')
            
            row[4].text = str(d.get('authors', '') or '—')
            if row[4].paragraphs and row[4].paragraphs[0].runs:
                row[4].paragraphs[0].runs[0].bold = True

            extra_parts = []
            if d.get('_norm_ym'): extra_parts.append(f"Давр: {d['_norm_ym']}")
            if d.get('country'): extra_parts.append(f"Давлат: {d['country']}")
            if d.get('journal_name'): extra_parts.append(f"Журнал: {d['journal_name']}")
            if d.get('pub_date'): extra_parts.append(f"Сана: {d['pub_date']}")
            if d.get('amount'): extra_parts.append(f"Сумма: {d['amount']} млн")
            if d.get('reg_number'): extra_parts.append(f"Рег: {d['reg_number']}")
            row[5].text = "; ".join(extra_parts) if extra_parts else "—"

        for row in det_table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(8)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


async def generate_codes_docx(departments: list) -> io.BytesIO:
    doc = Document()
    title = doc.add_paragraph()
    title.alignment = 1
    run = title.add_run("АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ\n")
    run.bold = True
    run.font.size = Pt(13)
    run2 = title.add_run("КАФЕДРАЛАР УЧУН ТЕЛЕГРАМ БОТГА КИРИШ МАХСУС ПАРОЛЛАРИ (КОДЛАРИ)")
    run2.bold = True
    run2.font.size = Pt(11)

    doc.add_paragraph("Ушбу махсус кодлар ҳар бир кафедра мудири ёки масъул ходимига берилади. Ботга биринчи марта кирганда ушбу код киритилади.\n")

    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    hdr = table.rows[0].cells
    hdr[0].text = "Т/Р"
    hdr[1].text = "Кафедра номи"
    hdr[2].text = "Кафедра мудири"
    hdr[3].text = "КИРИШ КОДИ (ПАРОЛ)"

    for i in range(4):
        hdr[i].paragraphs[0].runs[0].bold = True

    for dep_id, name, head, code in departments:
        row = table.add_row().cells
        row[0].text = str(dep_id)
        row[1].text = name
        row[2].text = head or ''
        row[3].text = code
        row[3].paragraphs[0].runs[0].bold = True

    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


async def generate_report_excel(summary_rows: list, detailed_rows: list, start_ym: str = None, end_ym: str = None) -> io.BytesIO:
    """
    Генерирует официальный Excel (.xlsx) отчёт с тремя листами:
    1. «Сводка 65 кафедр» — с авто-суммами и автофильтрами
    2. «Барча ҳисоботлар (База)» — со столбцом «Давр (Йил-Ой)», автофильтрами и сортировкой
    3. «Кафедралар рейтинги» — рейтинг кафедр и топ-30 авторов
    Опционально фильтрует по интервалу start_ym .. end_ym (например: 2026-01 .. 2026-06).
    """
    wb = openpyxl.Workbook()

    # Стили
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    total_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    bold_font = Font(name="Calibri", size=10, bold=True)
    regular_font = Font(name="Calibri", size=10)
    
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9")
    )
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # Enrich rows with normalized dates
    for d in detailed_rows:
        if '_norm_ym' not in d:
            yr, mth, day, ym, src = normalize_work_date(d.get('pub_date'), d.get('year'), d.get('created_at'))
            d['_norm_yr'] = yr
            d['_norm_mth'] = mth
            d['_norm_day'] = day
            d['_norm_ym'] = ym
            d['_norm_src'] = src

    period_title = ""
    if start_ym or end_ym:
        if start_ym and not end_ym: end_ym = start_ym
        if end_ym and not start_ym: start_ym = end_ym
        detailed_rows = [d for d in detailed_rows if start_ym <= d['_norm_ym'] <= end_ym]
        summary_rows = recompute_summary_for_period(detailed_rows)
        period_title = f"{start_ym} ДАВРИ" if start_ym == end_ym else f"{start_ym} — {end_ym} ДАВРИ"

    # Сортировка детальных строк по дате (свежие сверху)
    detailed_rows.sort(key=lambda d: (d.get('_norm_ym', ''), d.get('_norm_day', 0), d.get('id', 0)), reverse=True)

    # ─── ЛИСТ 1: СВОДКА ──────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Сводка 65 кафедр"
    ws1.views.sheetView[0].showGridLines = True

    # Заголовок
    ws1.merge_cells("A1:R1")
    title_cell = ws1["A1"]
    title_header_text = f"АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ — {period_title} ИЛМИЙ ХИСОБОТИ" if period_title else "АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ — 2026 ЙИЛ ИЛМИЙ ХИСОБОТИ"
    title_cell.value = title_header_text
    title_cell.font = Font(name="Calibri", size=14, bold=True, color="1F4E79")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws1.row_dimensions[1].height = 30

    col_headers = [
        "Т/Р", "Кафедра номи", "Кафедра мудири",
        "DSc", "PhD", "Монография", "Патент",
        "ЎзОАК", "Россия ОАК/IF",
        "Тезис (Ўз)", "Тезис (Хор)",
        "Scopus/WoS",
        "Рац.таклиф", "Амалиётга тадбиқ",
        "Анжуман", "Хўж.шартн.", "Грант", "Жами"
    ]

    ws1.append([])  # Строка 2 пустая
    ws1.append(col_headers)  # Строка 3 заголовки
    ws1.row_dimensions[3].height = 28

    for col_num in range(1, len(col_headers) + 1):
        c = ws1.cell(row=3, column=col_num)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center_align
        c.border = thin_border

    totals = [0] * (len(col_headers) - 3)

    for r in summary_rows:
        row_vals = [
            r['id'], r['name'], r['head_name'] or '',
            r['dsc'] or 0, r['phd'] or 0, r['monography'] or 0, r['patent'] or 0,
            r['oak_uz'] or 0, r['oak_ru_if'] or 0,
            r['thesis_uz'] or 0, r['thesis_foreign'] or 0,
            r['scopus_wos'] or 0,
            r['rationalizer'] or 0, r['implementation'] or 0,
            r['conferences'] or 0, r.get('contracts', 0) or 0, r.get('grants', 0) or 0,
            r['total'] or 0
        ]
        ws1.append(row_vals)
        curr_row = ws1.max_row
        ws1.row_dimensions[curr_row].height = 20

        for idx, val in enumerate(row_vals):
            cell = ws1.cell(row=curr_row, column=idx + 1)
            cell.font = regular_font
            cell.border = thin_border
            if idx == 1:
                cell.alignment = left_align
            else:
                cell.alignment = center_align

            if idx >= 3 and isinstance(val, (int, float)):
                totals[idx - 3] += val

    # Итоговая строка
    total_row_vals = ["", "ЖАМИ (Институт бўйича):", ""] + totals
    ws1.append(total_row_vals)
    tot_row_num = ws1.max_row
    ws1.row_dimensions[tot_row_num].height = 24

    for col_num in range(1, len(total_row_vals) + 1):
        cell = ws1.cell(row=tot_row_num, column=col_num)
        cell.font = bold_font
        cell.fill = total_fill
        cell.border = thin_border
        cell.alignment = left_align if col_num == 2 else center_align

    # Автофильтр Лист 1
    ws1.auto_filter.ref = f"A3:R{tot_row_num - 1}"

    # ─── ЛИСТ 2: ДЕТАЛЬНЫЕ ЗАПИСИ ────────────────────────────────────────────
    ws2 = wb.create_sheet(title="Барча ҳисоботлар (База)")
    ws2.views.sheetView[0].showGridLines = True

    det_headers = [
        "ID", "Давр (Йил-Ой)", "Киритилган вақт", "Кафедра ID", "Кафедра номи", "Кафедра мудири",
        "Категория", "Иш номи / Диссертация мавзуси", "Муаллифлар (Ф.И.Ш.)",
        "Шартнома/Грант суммаси (млн)", "Нашр давлати", "Журнал/Буюртмачи", "Нашр йили / Сана / Бетлар",
        "URL / DOI", "Муаллифлар сони", "Ихтисослик шифри ва номи",
        "Рег. рақам (Патент)", "Нашриёт (Монография)", "Файл борми?"
    ]
    ws2.append(det_headers)
    ws2.row_dimensions[1].height = 26

    for col_num in range(1, len(det_headers) + 1):
        c = ws2.cell(row=1, column=col_num)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center_align
        c.border = thin_border

    for d in detailed_rows:
        has_f = "✅ Ҳа" if d['file_path'] else "❌ Йўқ"
        cat_lbl = INDICATOR_LABELS.get(d['category'], d['category'])
        doi_or_url = d.get('doi', '') or d.get('url', '') or ''
        r_data = [
            d['id'],
            d.get('_norm_ym', ''),
            str(d.get('created_at', ''))[:16],
            d['dept_id'],
            d.get('dept_name', ''),
            d.get('head_name', '') or '',
            cat_lbl,
            d.get('title', '') or '',
            d.get('authors', '') or '',
            d.get('amount', '') or '',
            d.get('country', '') or '',
            d.get('journal_name', '') or '',
            d.get('pub_date', '') or '',
            doi_or_url,
            d.get('authors_count', '') or '',
            d.get('specialty', '') or '',
            d.get('reg_number', '') or '',
            d.get('publisher', '') or '',
            has_f
        ]
        ws2.append(r_data)
        curr = ws2.max_row
        for col_idx in range(1, len(r_data) + 1):
            cell = ws2.cell(row=curr, column=col_idx)
            cell.font = regular_font
            cell.border = thin_border
            if col_idx in (2,):
                cell.font = bold_font
            cell.alignment = left_align if col_idx in (5, 8, 9, 10, 11, 12, 13, 14, 16) else center_align

    # Автофильтр Лист 2
    if ws2.max_row > 1:
        ws2.auto_filter.ref = f"A1:S{ws2.max_row}"

    # ─── ЛИСТ 3: КАФЕДРАЛАР РЕЙТИНГИ ВА ЛИДЕРЛАР ────────────────────────────
    ws3 = wb.create_sheet(title="Кафедралар рейтинги")
    ws3.views.sheetView[0].showGridLines = True

    ws3.merge_cells("A1:N1")
    title_r = ws3["A1"]
    rating_header_text = f"АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ — КАФЕДРАЛАР РЕЙТИНГИ ({period_title})" if period_title else "АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ — КАФЕДРАЛАРНИНГ ИЛМИЙ ФАОЛЛИК РЕЙТИНГИ (2026 ЙИЛ)"
    title_r.value = rating_header_text
    title_r.font = Font(name="Calibri", size=13, bold=True, color="1F4E79")
    title_r.alignment = Alignment(horizontal="center", vertical="center")
    ws3.row_dimensions[1].height = 28

    rank_headers = [
        "Ўрни (Рейтинг)", "Кафедра ID", "Кафедра номи", "Кафедра мудири",
        "ЖАМИ ИШЛАР", "Scopus / WoS", "ЎзОАК мақола", "Россия ОАК / IF",
        "Патентлар", "DSc / PhD", "Монография", "Тезислар (Ўз/Хор)",
        "Шартнома / Грант", "Фаоллик ҳолати"
    ]
    ws3.append([])
    ws3.append(rank_headers)
    ws3.row_dimensions[3].height = 26

    rank_header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")

    for col_num in range(1, len(rank_headers) + 1):
        c = ws3.cell(row=3, column=col_num)
        c.font = header_font
        c.fill = rank_header_fill
        c.alignment = center_align
        c.border = thin_border

    sorted_depts = sorted(summary_rows, key=lambda r: -(r.get('total') or 0))

    for rank, r in enumerate(sorted_depts, 1):
        tot = r.get('total') or 0
        diss = (r.get('dsc') or 0) + (r.get('phd') or 0)
        theses = (r.get('thesis_uz') or 0) + (r.get('thesis_foreign') or 0)
        grants = (r.get('contracts') or 0) + (r.get('grants') or 0)

        if tot >= 30:
            status_txt = "🔥 Юқори фаол (Лидер)"
        elif tot >= 10:
            status_txt = "✅ Фаол"
        elif tot > 0:
            status_txt = "⚠️ Паст кўрсаткич"
        else:
            status_txt = "❌ Иш топширмаган (0)"

        rank_row_vals = [
            f"#{rank}", r['id'], r['name'], r['head_name'] or '',
            tot, r.get('scopus_wos') or 0, r.get('oak_uz') or 0, r.get('oak_ru_if') or 0,
            r.get('patent') or 0, diss, r.get('monography') or 0, theses,
            grants, status_txt
        ]
        ws3.append(rank_row_vals)
        curr = ws3.max_row
        ws3.row_dimensions[curr].height = 20

        for col_idx, val in enumerate(rank_row_vals):
            cell = ws3.cell(row=curr, column=col_idx + 1)
            cell.font = bold_font if col_idx in (0, 4) else regular_font
            cell.border = thin_border
            cell.alignment = left_align if col_idx in (2, 3) else center_align

    # Автофильтр Лист 3
    if sorted_depts:
        ws3.auto_filter.ref = f"A3:N{len(sorted_depts) + 3}"

    # ТОП-30 АВТОРОВ
    ws3.append([])
    ws3.append([])
    auth_title_row = ws3.max_row
    ws3.merge_cells(f"A{auth_title_row}:H{auth_title_row}")
    auth_title_cell = ws3[f"A{auth_title_row}"]
    auth_title_cell.value = "ИНСТИТУТНИНГ ЭНГ ФАОЛ 30 ТА ОЛИМ ВА МУАЛЛИФЛАРИ (РЕЙТИНГ)"
    auth_title_cell.font = Font(name="Calibri", size=12, bold=True, color="1F4E79")
    auth_title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws3.row_dimensions[auth_title_row].height = 26

    auth_headers = [
        "Ўрни", "Муаллифнинг Ф.И.Ш.", "Кафедра номи",
        "ЖАМИ ИШЛАРИ", "Scopus / WoS", "Патентлар", "ЎзОАК / ОАК", "Диссертация / Тезислар"
    ]
    ws3.append(auth_headers)
    hdr_row_idx = ws3.max_row
    ws3.row_dimensions[hdr_row_idx].height = 24
    for col_num in range(1, len(auth_headers) + 1):
        c = ws3.cell(row=hdr_row_idx, column=col_num)
        c.font = header_font
        c.fill = rank_header_fill
        c.alignment = center_align
        c.border = thin_border

    authors_data = {}
    for d in detailed_rows:
        auth_str = (d.get('authors') or '').strip()
        dept_name = d.get('dept_name') or ''
        cat = d.get('category') or ''
        for raw_a in auth_str.replace(";", ",").split(","):
            a = raw_a.strip()
            if a and len(a) > 2 and a.lower() not in ["va boshqalar", "et al", "—"]:
                if a not in authors_data:
                    authors_data[a] = {"dept": dept_name, "total": 0, "scopus": 0, "patent": 0, "oak": 0, "other": 0}
                authors_data[a]["total"] += 1
                if not authors_data[a]["dept"] and dept_name:
                    authors_data[a]["dept"] = dept_name
                if cat == "scopus_wos":
                    authors_data[a]["scopus"] += 1
                elif cat == "patent":
                    authors_data[a]["patent"] += 1
                elif cat in ("oak_uz", "oak_ru_if"):
                    authors_data[a]["oak"] += 1
                else:
                    authors_data[a]["other"] += 1

    sorted_authors = sorted(authors_data.items(), key=lambda x: -x[1]["total"])

    for a_rank, (a_name, a_info) in enumerate(sorted_authors[:30], 1):
        a_row = [
            f"#{a_rank}", a_name, a_info["dept"] or "—",
            a_info["total"], a_info["scopus"], a_info["patent"],
            a_info["oak"], a_info["other"]
        ]
        ws3.append(a_row)
        curr = ws3.max_row
        ws3.row_dimensions[curr].height = 20
        for col_idx in range(1, len(a_row) + 1):
            cell = ws3.cell(row=curr, column=col_idx)
            cell.font = bold_font if col_idx in (1, 4) else regular_font
            cell.border = thin_border
            cell.alignment = left_align if col_idx in (2, 3) else center_align

    # Автоширина колонок
    for ws in (ws1, ws2, ws3):
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = str(cell.value or '')
                if len(val) > max_len and '\n' not in val:
                    max_len = len(val)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 10), 50)

    ws1.column_dimensions["B"].width = 38
    ws1.column_dimensions["C"].width = 28
    ws2.column_dimensions["B"].width = 16
    ws2.column_dimensions["C"].width = 18
    ws2.column_dimensions["E"].width = 38
    ws2.column_dimensions["H"].width = 40
    ws2.column_dimensions["I"].width = 30

    ws3.column_dimensions["A"].width = 16
    ws3.column_dimensions["B"].width = 12
    ws3.column_dimensions["C"].width = 40
    ws3.column_dimensions["D"].width = 28
    ws3.column_dimensions["E"].width = 16
    ws3.column_dimensions["N"].width = 24

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


CATEGORY_FOLDERS = {
    "scopus_wos":       "01_Scopus_va_Web_of_Science",
    "dsc":              "02_DSc_Dissertatsiyalar",
    "phd":              "03_PhD_Dissertatsiyalar",
    "patent":           "04_Patentlar_IAP_FAP",
    "monography":       "05_Monografiyalar",
    "oak_uz":           "06_OzOAK_maqolalari",
    "oak_ru_if":        "07_Rossiya_va_Xorijiy_OAK",
    "thesis_uz":        "08_Respublika_konferensiya_tezislari",
    "thesis_foreign":   "09_Xorijiy_konferensiya_tezislari",
    "rationalizer":     "10_Ratsionalizatorlik_takliflari",
    "implementation":   "11_Amaliyotga_tadbiq_qilingan_ishlar",
    "conferences":      "12_Kafedra_otkazgan_anjumanlar",
    "contracts":        "13_Xojalik_shartnomalari",
    "grants":           "14_Grantlar",
}


def sanitize_filename(text: str, max_len: int = 50) -> str:
    """Очищает строку от недопустимых символов для имен файлов"""
    if not text:
        return "hujjat"
    clean = re.sub(r'[\\/*?:"<>|\n\r\t]', '_', str(text))
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:max_len]


async def fetch_file_content(bot, entry: dict, sem: asyncio.Semaphore):
    file_id = entry.get('file_id')
    cat_folder = CATEGORY_FOLDERS.get(entry.get('category'), entry.get('category', 'other'))
    dept_id = entry.get('dept_id', 0)
    author = sanitize_filename(entry.get('authors') or '', 25)
    title = sanitize_filename(entry.get('title') or '', 35)
    entry_id = entry.get('id', 0)

    def make_stub(reason: str):
        stub_path = f"{cat_folder}/[Kaf_{dept_id:02d}]_{author}_{title}_id{entry_id}_YUKLAB_OLINMADI.txt"
        stub_text = (
            f"Fayl yuklab olinmadi.\n"
            f"Sababi: {reason}\n\n"
            f"Maqola: {entry.get('title', '')}\n"
            f"Mualliflar: {entry.get('authors', '')}\n"
            f"Kafedra ID: {dept_id}\n"
            f"Yozuv ID: #{entry_id}\n"
        ).encode('utf-8')
        return (stub_path, stub_text)

    if not file_id:
        return None

    async with sem:
        last_error = "Noma'lum xato"
        for attempt in range(1, 4):
            try:
                tg_file = await bot.get_file(file_id)

                if tg_file.file_size and tg_file.file_size > 20 * 1024 * 1024:
                    size_mb = tg_file.file_size // (1024 * 1024)
                    return make_stub(f"Fayl hajmi {size_mb} MB > 20 MB (Telegram API cheklovi)")

                f_stream = await bot.download_file(tg_file.file_path)
                if hasattr(f_stream, 'getvalue'):
                    content = f_stream.getvalue()
                elif hasattr(f_stream, 'read'):
                    if hasattr(f_stream, 'seek'):
                        f_stream.seek(0)
                    content = f_stream.read()
                else:
                    content = bytes(f_stream)

                if not content:
                    last_error = "Yuklab olingan kontent bo'sh"
                    await asyncio.sleep(1.5 * attempt)
                    continue

                ext = Path(tg_file.file_path).suffix.lower() or ".pdf"
                if not ext.startswith("."):
                    ext = "." + ext

                if ext == ".pdf":
                    if not content.startswith(b'%PDF'):
                        last_error = (
                            f"PDF sarlavhasi topilmadi (fayl buzilgan, "
                            f"boshlanishi: {content[:8].hex()}, urinish {attempt}/3)"
                        )
                        logger.warning(f"Entry #{entry_id}: {last_error}")
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    if len(content) < 100:
                        last_error = f"PDF hajmi juda kichik ({len(content)} bayt)"
                        await asyncio.sleep(1)
                        continue

                zip_path = f"{cat_folder}/[Kaf_{dept_id:02d}]_{author}_{title}_id{entry_id}{ext}"
                return (zip_path, content)

            except Exception as ex:
                last_error = str(ex)[:120]
                logger.warning(f"Entry #{entry_id} fetch attempt {attempt}/3 failed: {ex}")
                await asyncio.sleep(1.5 * attempt)

        logger.error(f"Entry #{entry_id}: all 3 fetch attempts failed. Last: {last_error}")
        return make_stub(f"3 urinishdan keyin ham yuklab bo'lmadi: {last_error}")


async def stream_files_zip(bot, entries: list, max_zip_bytes: int = 35 * 1024 * 1024):
    import zipfile

    if not entries:
        return

    sem = asyncio.Semaphore(6)
    BATCH_DOWNLOAD_SIZE = 12

    current_buf = io.BytesIO()
    current_zf = zipfile.ZipFile(current_buf, "w", compression=zipfile.ZIP_DEFLATED)
    current_count = 0

    for i in range(0, len(entries), BATCH_DOWNLOAD_SIZE):
        batch = entries[i:i + BATCH_DOWNLOAD_SIZE]
        tasks = [fetch_file_content(bot, e, sem) for e in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if not isinstance(res, tuple) or res is None:
                continue
            zip_path, content = res
            if not content:
                continue

            if (current_buf.tell() + len(content)) > max_zip_bytes and current_count > 0:
                current_zf.close()
                current_buf.seek(0)
                yield current_buf, current_count

                current_buf = io.BytesIO()
                current_zf = zipfile.ZipFile(current_buf, "w", compression=zipfile.ZIP_DEFLATED)
                current_count = 0

            current_zf.writestr(zip_path, content)
            current_count += 1

    if current_count > 0:
        current_zf.close()
        current_buf.seek(0)
        yield current_buf, current_count
    else:
        current_zf.close()


async def generate_files_zip(bot, entries: list) -> tuple[io.BytesIO, int]:
    async for buf, count in stream_files_zip(bot, entries):
        return buf, count
    return io.BytesIO(), 0
