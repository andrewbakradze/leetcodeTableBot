const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../apps-script/Code.gs'), 'utf8');
const headers = ['problem number', 'problem name', 'my difficulties', 'link'];
const token = 'a'.repeat(64);

function harness(initial = [headers.slice()]) {
  const rows = initial.map(row => row.slice());
  const writes = [];
  const state = { opens: 0, releases: 0, available: true, maxColumns: 26, maxRows: 1000 };
  const properties = { BOT_TOKEN: token, SPREADSHEET_ID: 'example' };
  const display = value => value === undefined ? '' : String(value);
  function range(row, col, height = 1, width = 1) {
    const put = (values, method) => {
      writes.push(method);
      values.forEach((cells, i) => cells.forEach((value, j) => {
        rows[row - 1 + i] ||= [];
        rows[row - 1 + i][col - 1 + j] = value;
      }));
    };
    return {
      getDisplayValue: () => display((rows[row - 1] || [])[col - 1]),
      setValue: value => put([[value]], 'setValue'),
      setValues: values => put(values, 'setValues'),
      setRichTextValue: value => put([[value.text]], 'setRichTextValue'),
      setRichTextValues: values => put(values.map(row => row.map(value => value.text)), 'setRichTextValues'),
    };
  }
  const sheet = {
    getSheetId: () => 0,
    getName: () => 'Лист1',
    getMaxColumns: () => state.maxColumns,
    getMaxRows: () => state.maxRows,
    getLastRow: () => rows.reduce((last, row, i) => row.some(value => display(value) !== '') ? i + 1 : last, 0),
    getDataRange: () => ({ getDisplayValues: () => rows.map(row => row.map(display)) }),
    getRange: range,
    insertRowsAfter: (after, count) => { state.maxRows += count; },
    insertColumnsAfter: (after, count) => { state.maxColumns += count; },
  };
  const spreadsheet = { getSheets: () => [sheet], getId: () => 'example', getName: () => 'LeetCode' };
  const logs = [];
  const context = vm.createContext({
    PropertiesService: { getScriptProperties: () => ({
      getProperty: key => properties[key],
      setProperties: values => Object.assign(properties, values),
    }) },
    Utilities: { getUuid: () => '12345678-1234-1234-1234-123456789abc' },
    Logger: { log: text => logs.push(text) },
    LockService: { getScriptLock: () => ({
      tryLock: () => state.available,
      releaseLock: () => { state.releases++; },
    }) },
    SpreadsheetApp: {
      getActiveSpreadsheet: () => spreadsheet,
      openById: id => { assert.equal(id, 'example'); state.opens++; return spreadsheet; },
      flush: () => {},
      newRichTextValue: () => {
        const builder = {
          text: '', url: null,
          setText(text) { this.text = text; return this; },
          setLinkUrl(url) { this.url = url; return this; },
          build() { return { text: this.text, url: this.url }; },
        };
        return builder;
      },
    },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: text => ({ text, setMimeType() { return this; } }),
    },
  });
  vm.runInContext(source, context);
  const request = (extra = {}) => JSON.parse(context.doPost({ postData: { contents: JSON.stringify({
    token, spreadsheet_id: 'example', worksheet_id: 0, action: 'metadata',
    columns: { number: 1, title: 2, notes: 3, url: 4 }, header_row: 1,
    number: 228, title: 'Summary Ranges', notes: 'заметка', url: 'https://leetcode.com/problems/summary-ranges/',
    ...extra,
  }) } }).text);
  return { context, rows, writes, state, properties, logs, request };
}

test('unauthenticated request cannot open or modify the spreadsheet', () => {
  const app = harness();
  assert.equal(app.request({ token: 'wrong', action: 'read' }).ok, false);
  assert.equal(app.state.opens, 0);
  assert.deepEqual(app.writes, []);
});

test('token cannot access a different spreadsheet', () => {
  const app = harness();
  const reply = app.request({ spreadsheet_id: 'another' });
  assert.equal(reply.ok, false);
  assert.match(reply.error, /другую таблицу/);
  assert.equal(app.state.opens, 0);
});

test('metadata and read preserve the template and release locks', () => {
  const app = harness();
  assert.deepEqual(app.request().data, { title: 'Лист1', col_count: 26 });
  assert.deepEqual(app.request({ action: 'read' }).data.rows, [headers]);
  assert.equal(app.state.releases, 2);
  assert.deepEqual(app.writes, []);
});

test('append writes literal multiline text without executing formulas', () => {
  const app = harness([headers, [1, 'Two Sum', 'existing', 'link']]);
  const reply = app.request({ action: 'append', notes: '=SUM(A1:A2)\nстрока' });
  assert.equal(reply.ok, true);
  assert.deepEqual(app.rows[2], [228, 'Summary Ranges', '=SUM(A1:A2)\nстрока', 'https://leetcode.com/problems/summary-ranges/']);
  assert.equal(app.rows[1][2], 'existing');
  assert.ok(app.writes.includes('setRichTextValues'));
  assert.ok(!app.writes.includes('setValues'));
});

test('update preserves old note and only changes note cell', () => {
  const app = harness([headers, [228, 'Summary Ranges', 'старая', 'link']]);
  assert.equal(app.request({ action: 'add_notes', row: 2, notes: 'новая' }).ok, true);
  assert.deepEqual(app.rows[1], [228, 'Summary Ranges', 'старая\n\nновая', 'link']);
  assert.deepEqual(app.writes, ['setRichTextValue']);
});

test('moved row is rejected without changing another task', () => {
  const app = harness([headers, [1, 'Two Sum', 'не менять', 'link']]);
  assert.equal(app.request({ action: 'add_notes', row: 2 }).ok, false);
  assert.deepEqual(app.writes, []);
  assert.equal(app.state.releases, 1);
});

test('initialization does not overwrite existing rows', () => {
  const app = harness([[], [228]]);
  assert.equal(app.request({ action: 'initialize' }).ok, false);
  assert.deepEqual(app.writes, []);
});

test('empty sheet is initialized with exact template', () => {
  const app = harness([]);
  assert.equal(app.request({ action: 'initialize' }).ok, true);
  assert.deepEqual(app.rows[0], headers);
});

test('invalid columns and overlong notes do not write', () => {
  const app = harness();
  assert.equal(app.request({ action: 'append', columns: { number: 1, title: 1, notes: 3, url: 4 } }).ok, false);
  assert.equal(app.request({ action: 'append', notes: 'x'.repeat(50001) }).ok, false);
  assert.deepEqual(app.writes, []);
});

test('concurrent write lock failure is readable', () => {
  const app = harness();
  app.state.available = false;
  const reply = app.request({ action: 'append' });
  assert.equal(reply.ok, false);
  assert.match(reply.error, /занята/);
  assert.equal(app.state.opens, 0);
});

test('setup binds current sheet and keeps previous token', () => {
  const app = harness();
  app.context.setup();
  assert.equal(app.properties.SPREADSHEET_ID, 'example');
  assert.equal(app.properties.BOT_TOKEN, token);
  assert.ok(app.logs[0].includes(token));
  assert.deepEqual(app.writes, []);
});

test('GET and malformed requests never return spreadsheet rows', () => {
  const app = harness();
  assert.equal(JSON.parse(app.context.doGet().text).ok, false);
  assert.equal(JSON.parse(app.context.doPost({ postData: { contents: 'not json' } }).text).ok, false);
  assert.equal(app.state.opens, 0);
});
