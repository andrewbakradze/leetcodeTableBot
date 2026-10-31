/**
 * LeetCode Table Bot. Вставьте весь файл в Apps Script, открытый из вашей таблицы.
 * Один раз выполните setup(), затем разверните Web app: Execute as Me, access Anyone.
 * Каждый запрос требует секретный токен. Без него данные не читаются и не изменяются.
 */

function setup() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Откройте Apps Script из таблицы: Расширения → Apps Script.');
  const properties = PropertiesService.getScriptProperties();
  const token = properties.getProperty('BOT_TOKEN') ||
    (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '');
  properties.setProperties({ BOT_TOKEN: token, SPREADSHEET_ID: spreadsheet.getId() });
  Logger.log('Токен бота (скопируйте в Python-программу): ' + token);
  Logger.log('Таблица подключена: ' + spreadsheet.getName());
}

function doGet() {
  return jsonReply_({ ok: false, error: 'Это адрес для Python-бота. Используйте POST с токеном.' });
}

function doPost(event) {
  try {
    const body = event && event.postData && event.postData.contents;
    if (!body || body.length > 500000) throw new Error('Некорректный запрос.');
    const request = JSON.parse(body);
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('Некорректный запрос.');
    const properties = PropertiesService.getScriptProperties();
    const token = properties.getProperty('BOT_TOKEN');
    if (!token || typeof request.token !== 'string' || request.token !== token) {
      return jsonReply_({ ok: false, error: 'Неверный токен. Скопируйте токен из журнала setup и запустите бота с --setup.' });
    }
    const spreadsheetId = properties.getProperty('SPREADSHEET_ID');
    require_(spreadsheetId && request.spreadsheet_id === spreadsheetId,
      'Ссылка бота указывает на другую таблицу. Запустите setup в Apps Script нужной таблицы.');
    const actions = ['metadata', 'read', 'initialize', 'append'];
    require_(actions.includes(request.action), 'Неизвестная команда.');
    // Блокировка сериализует изменения от нескольких экземпляров бота.
    const lock = LockService.getScriptLock();
    require_(lock.tryLock(10000), 'Таблица сейчас занята. Повторите позже.');
    try {
      const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
      const sheets = spreadsheet.getSheets();
      const sheet = request.worksheet_id === null ? sheets[0] :
        sheets.find(item => item.getSheetId() === request.worksheet_id);
      require_(sheet, 'Лист не найден. Проверьте gid в ссылке таблицы.');
      const data = handleRequest_(sheet, request);
      SpreadsheetApp.flush();
      return jsonReply_({ ok: true, data: data });
    } finally {
      lock.releaseLock();
    }
  } catch (error) {
    // Не возвращаем стек, тело запроса или внутренние ошибки Google с данными аккаунта.
    const message = error && error.botMessage ? error.botMessage :
      'Не удалось выполнить запрос. Проверьте настройку скрипта, разрешения и лист таблицы.';
    return jsonReply_({ ok: false, error: message });
  }
}

function handleRequest_(sheet, request) {
  if (request.action === 'metadata') return { title: sheet.getName(), col_count: sheet.getMaxColumns(), supports_resolve: true };
  if (request.action === 'read') return { rows: sheet.getDataRange().getDisplayValues() };
  if (request.action === 'initialize') {
    const rows = sheet.getDataRange().getDisplayValues();
    require_(!rows.some(row => row.some(cell => cell.trim())), 'В листе уже есть данные. Настройте столбцы.');
    if (sheet.getMaxColumns() < 5) sheet.insertColumnsAfter(sheet.getMaxColumns(), 5 - sheet.getMaxColumns());
    sheet.getRange(1, 1, 1, 5).setValues([['problem number', 'problem name', 'my difficulties', 'link', 'Resolve']]);
    return {};
  }
  const fields = ['number', 'title', 'notes', 'url', 'resolve'];
  const columns = request.columns;
  require_(columns && typeof columns === 'object' && !Array.isArray(columns) &&
    Object.keys(columns).length === fields.length && fields.every(field => Number.isInteger(columns[field]) &&
    columns[field] >= 1 && columns[field] <= sheet.getMaxColumns()) &&
    new Set(fields.map(field => columns[field])).size === fields.length, 'Некорректные столбцы. Проверьте заголовок Resolve.');
  require_(Number.isInteger(request.header_row) && request.header_row >= 1 &&
    request.header_row <= sheet.getMaxRows(), 'Некорректная строка заголовков.');
  require_(Number.isInteger(request.number) && request.number >= 1 && request.number <= 9999999, 'Некорректный номер задачи.');
  require_(typeof request.notes === 'string' && request.notes.length <= 50000, 'Лимит заметки — 50 000 символов.');
  require_(['ДА', 'может быть', 'нет'].includes(request.resolve), 'Resolve: выберите ДА, может быть или нет.');
  if (request.action === 'append') {
    require_(typeof request.title === 'string' && request.title.trim() && request.title.length <= 50000, 'Некорректное название задачи.');
    require_(typeof request.url === 'string' && /^https:\/\/leetcode\.com\/problems\/[a-z0-9]+(?:-[a-z0-9]+)*\/$/.test(request.url), 'Некорректная ссылка LeetCode.');
    const row = Math.max(sheet.getLastRow() + 1, request.header_row + 1);
    if (row > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), row - sheet.getMaxRows());
    const resolveValue = resolveText_(sheet.getRange(row, columns.resolve), request.resolve);
    const width = Math.max(...fields.map(field => columns[field]));
    const values = Array.from({ length: width }, () => literalText_(''));
    fields.forEach(field => {
      // RichTextValue записывает текст буквально, в том числе строки, начинающиеся с '='.
      const text = field === 'resolve' ? resolveValue : String(request[field]);
      values[columns[field] - 1] = literalText_(text, field === 'title' || field === 'url' ? request.url : null);
    });
    sheet.getRange(row, 1, 1, width).setRichTextValues([values]);
    sheet.getRange(row, columns.number).setValue(request.number);
    return { row: row };
  }
}

function resolveText_(cell, value) {
  const rule = cell.getDataValidation();
  if (!rule) return value;
  const type = rule.getCriteriaType();
  const criteria = SpreadsheetApp.DataValidationCriteria;
  let options;
  if (type === criteria.VALUE_IN_LIST) options = rule.getCriteriaValues()[0];
  else if (type === criteria.VALUE_IN_RANGE) options = rule.getCriteriaValues()[0].getDisplayValues().flat();
  else return value;
  // Сохраняем написание уже настроенного списка, например «Нет» вместо «нет».
  const match = options.find(option => String(option).trim().toLowerCase() === value.toLowerCase());
  require_(match !== undefined, 'В списке Resolve нет выбранного варианта. Нужны ДА, может быть и нет.');
  return String(match);
}

function literalText_(text, url) {
  const value = SpreadsheetApp.newRichTextValue().setText(text);
  if (url) value.setLinkUrl(url);
  return value.build();
}

function require_(condition, message) {
  if (!condition) {
    const error = new Error(message);
    error.botMessage = message;
    throw error;
  }
}

function jsonReply_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
