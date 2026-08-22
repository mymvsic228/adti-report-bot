/**
 * ADTI Report Bot — Google Apps Script
 * 
 * Инструкция по установке:
 * 1. Откройте Google Таблицу
 * 2. Расширения → Apps Script
 * 3. Вставьте этот код
 * 4. Нажмите "Развернуть" → "Новое развёртывание" → Тип: Web App
 * 5. Доступ: "Все" (Anyone)
 * 6. Скопируйте URL и вставьте в Render как SHEET_WEBHOOK_URL
 */

const SHEET_NAME_ENTRIES = "Ҳисоботлар";    // Все записи построчно
const SHEET_NAME_SUMMARY = "Сводка";          // Итоги по кафедрам
const SHEET_NAME_LOG     = "Лог";             // Системный лог

// ─── ЗАГОЛОВКИ ───────────────────────────────────────────────────────────────
const ENTRY_HEADERS = [
  "ID", "Сана", "Кафедра ID", "Кафедра номи", "Мудир Ф.И.Ш.",
  "Категория", "Иш номи", "Муаллифлар", "Йил", "Файл борми?"
];

const SUMMARY_HEADERS = [
  "Т/Р", "Кафедра номи", "Мудир", 
  "DSc", "PhD", "Монография", "Патент",
  "ЎзОАК", "РФ ОАК/IF", "Тезис (Ўз)", "Тезис (Хор)",
  "Scopus/WoS", "Рац.таклиф", "Амалиётга тадбиқ",
  "Анжуман", "Шартнома", "Грант", "Жами"
];

// ─── MAIN WEBHOOK ────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    
    if (data.action === "full_sync") {
      return handleFullSync(data.departments);
    } else {
      return handleNewEntry(data);
    }
  } catch (err) {
    logError(err.toString());
    return ContentService.createTextOutput(
      JSON.stringify({ status: "error", message: err.toString() })
    ).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput("ADTI Bot webhook is running ✅")
    .setMimeType(ContentService.MimeType.TEXT);
}

// ─── НОВАЯ ЗАПИСЬ ────────────────────────────────────────────────────────────
function handleNewEntry(data) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getOrCreateSheet(ss, SHEET_NAME_ENTRIES, ENTRY_HEADERS);
  
  const now = new Date().toLocaleString("uz-UZ", {timeZone: "Asia/Tashkent"});
  
  sheet.appendRow([
    data.entry_id || "",
    now,
    data.dept_id,
    data.dept_name,
    data.head_name || "",
    data.category_label || data.category,
    data.title || "",
    data.authors || "",
    data.year || 2026,
    data.has_file ? "✅ Ҳа" : "❌ Йўқ"
  ]);
  
  // Форматируем новую строку
  const lastRow = sheet.getLastRow();
  sheet.getRange(lastRow, 1, 1, ENTRY_HEADERS.length)
    .setFontSize(10)
    .setVerticalAlignment("middle");
  
  log(`Янги ёзув: ${data.dept_name} — ${data.category_label}`);
  
  return ContentService.createTextOutput(
    JSON.stringify({ status: "ok", message: "Entry added" })
  ).setMimeType(ContentService.MimeType.JSON);
}

// ─── ПОЛНАЯ СИНХРОНИЗАЦИЯ ────────────────────────────────────────────────────
function handleFullSync(departments) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getOrCreateSheet(ss, SHEET_NAME_SUMMARY, SUMMARY_HEADERS);
  
  // Очищаем данные (кроме заголовков)
  const lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    sheet.getRange(2, 1, lastRow - 1, SUMMARY_HEADERS.length).clearContent();
  }
  
  let grandTotal = 0;
  const rows = [];
  
  departments.forEach(d => {
    const total = d.total || 0;
    grandTotal += total;
    rows.push([
      d.dept_id, d.dept_name, d.head_name || "",
      d.dsc || 0, d.phd || 0, d.monography || 0, d.patent || 0,
      d.oak_uz || 0, d.oak_ru_if || 0,
      d.thesis_uz || 0, d.thesis_foreign || 0,
      d.scopus_wos || 0,
      d.rationalizer || 0, d.implementation || 0,
      d.conferences || 0, 0, 0,  // contracts, grants — пока 0
      total
    ]);
  });
  
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, SUMMARY_HEADERS.length).setValues(rows);
  }
  
  // Итоговая строка
  const totalRow = sheet.getLastRow() + 1;
  sheet.getRange(totalRow, 1).setValue("ЖАМИ:");
  sheet.getRange(totalRow, 18).setValue(grandTotal);
  sheet.getRange(totalRow, 1, 1, SUMMARY_HEADERS.length)
    .setFontWeight("bold")
    .setBackground("#d9ead3");
  
  // Форматирование
  sheet.autoResizeColumns(1, SUMMARY_HEADERS.length);
  sheet.getRange(1, 1, 1, SUMMARY_HEADERS.length)
    .setBackground("#4a86e8")
    .setFontColor("white")
    .setFontWeight("bold");
  
  log(`Full sync: ${departments.length} та кафедра, ${grandTotal} та ёзув`);
  
  return ContentService.createTextOutput(
    JSON.stringify({ status: "ok", message: `Synced ${departments.length} departments` })
  ).setMimeType(ContentService.MimeType.JSON);
}

// ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────────────────
function getOrCreateSheet(ss, name, headers) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(headers);
    sheet.getRange(1, 1, 1, headers.length)
      .setBackground("#4a86e8")
      .setFontColor("white")
      .setFontWeight("bold")
      .setFontSize(10);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function log(msg) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getOrCreateSheet(ss, SHEET_NAME_LOG, ["Вақт", "Хабар"]);
  const now = new Date().toLocaleString("uz-UZ", {timeZone: "Asia/Tashkent"});
  sheet.appendRow([now, msg]);
}

function logError(msg) {
  log("❌ ERROR: " + msg);
}
