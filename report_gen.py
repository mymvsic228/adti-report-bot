import io
from docx import Document
from docx.shared import Pt
from database import INDICATORS, INDICATOR_KEYS


async def generate_report_docx(summary_rows: list) -> io.BytesIO:
    """
    Генерирует официальный Word-отчёт АДТИ по форме «2026 йил хисобот»
    """
    doc = Document()

    # Заголовок
    title = doc.add_paragraph()
    title.alignment = 1  # CENTER
    run = title.add_run("АНДИЖОН ДАВЛАТ ТИББИЁТ ИНСТИТУТИ КАФЕДРАЛАРИ ТОМОНИДАН\n")
    run.bold = True
    run.font.size = Pt(12)
    run2 = title.add_run("2026 ЙИЛ ХИСОБОТИ — ИЛМИЙ ТАДҚИҚОТ ИШЛАРИ ТЎҒРИСИДА МАЪЛУМОТ")
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
    totals = [0] * (len(col_headers) - 3)  # для итоговой строки

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
            r['conferences'], '', ''  # contracts и grants — текстовые поля
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

    # Формат шрифтов
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(8)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


async def generate_codes_docx(departments: list) -> io.BytesIO:
    """
    Генерирует Word-файл с индивидуальными кодами доступа для всех 65 кафедр
    (для передачи завкафедрами)
    """
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

