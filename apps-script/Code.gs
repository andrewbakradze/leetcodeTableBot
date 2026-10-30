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
      return jsonReply_({ ok: false, error: 'Неверный токен. Скопируйте токен из журнала setup и запустите бота с --apps-script.' });
    }
    const spreadsheetId = properties.getProperty('SPREADSHEET_ID');
    require_(spreadsheetId && request.spreadsheet_id === spreadsheetId,
      'Ссылка бота указывает на другую таблицу. Запустите setup в Apps Script нужной таблицы.');
    const actions = ['metadata', 'read', 'initialize', 'append', 'add_notes'];
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
  if (request.action === 'metadata') return { title: sheet.getName(), col_count: sheet.getMaxColumns() };
  if (request.action === 'read') return { rows: sheet.getDataRange().getDisplayValues() };
  if (request.action === 'initialize') {
    const rows = sheet.getDataRange().getDisplayValues();
    require_(!rows.some(row => row.some(cell => cell.trim())), 'В листе уже есть данные. Настройте столбцы.');
    if (sheet.getMaxColumns() < 4) sheet.insertColumnsAfter(sheet.getMaxColumns(), 4 - sheet.getMaxColumns());
    sheet.getRange(1, 1, 1, 4).setValues([['problem number', 'problem name', 'my difficulties', 'link']]);
    return {};
  }
  const fields = ['number', 'title', 'notes', 'url'];
  const columns = request.columns;
  require_(columns && typeof columns === 'object' && !Array.isArray(columns) &&
    Object.keys(columns).length === 4 && fields.every(field => Number.isInteger(columns[field]) &&
    columns[field] >= 1 && columns[field] <= sheet.getMaxColumns()) &&
    new Set(fields.map(field => columns[field])).size === 4, 'Некорректные столбцы. Запустите --setup.');
  require_(Number.isInteger(request.header_row) && request.header_row >= 1 &&
    request.header_row <= sheet.getMaxRows(), 'Некорректная строка заголовков.');
  require_(Number.isInteger(request.number) && request.number >= 1 && request.number <= 9999999, 'Некорректный номер задачи.');
  require_(typeof request.notes === 'string' && request.notes.length <= 50000, 'Лимит заметки — 50 000 символов.');
  if (request.action === 'append') {
    require_(typeof request.title === 'string' && request.title.trim() && request.title.length <= 50000, 'Некорректное название задачи.');
    require_(typeof request.url === 'string' && /^https:\/\/leetcode\.com\/problems\/[a-z0-9]+(?:-[a-z0-9]+)*\/$/.test(request.url), 'Некорректная ссылка LeetCode.');
    const row = Math.max(sheet.getLastRow() + 1, request.header_row + 1);
    if (row > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), row - sheet.getMaxRows());
    const width = Math.max(...fields.map(field => columns[field]));
    const values = Array.from({ length: width }, () => literalText_(''));
    fields.forEach(field => {
      // RichTextValue записывает текст буквально, в том числе строки, начинающиеся с '='.
      values[columns[field] - 1] = literalText_(String(request[field]), field === 'title' || field === 'url' ? request.url : null);
    });
    sheet.getRange(row, 1, 1, width).setRichTextValues([values]);
    sheet.getRange(row, columns.number).setValue(request.number);
    return { row: row };
  }
  require_(Number.isInteger(request.row) && request.row > request.header_row &&
    request.row <= sheet.getLastRow(), 'Строка задачи изменилась. Введите номер заново; заметка есть в /draft.');
  const actualNumber = sheet.getRange(request.row, columns.number).getDisplayValue().trim();
  require_(/^[0-9]+(?:[.,]0+)?$/.test(actualNumber) &&
    Number(actualNumber.replace(',', '.')) === request.number,
    'Строка задачи изменилась. Введите номер заново; заметка есть в /draft.');
  const cell = sheet.getRange(request.row, columns.notes);
  const current = cell.getDisplayValue();
  const combined = current ? current + '\n\n' + request.notes : request.notes;
  require_(combined.length <= 50000, 'Заметка вместе с предыдущей превышает 50 000 символов.');
  cell.setRichTextValue(literalText_(combined));
  return { row: request.row };
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
