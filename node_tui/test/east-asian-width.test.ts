import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import * as App from '../src-open/App.tsx';

// 东亚宽度（EW1–EW4）与依赖元数据（EW5）的契约测试。
// 实现方需在 src-open/App.tsx 中导出既有纯函数，并接入 get-east-asian-width：
//   terminalColumns(text, options?) -> number
//     - options = {ambiguousAsWide?: boolean}；默认按窄处理（UAX#11 建议：
//       上下文不可靠时 ambiguous 字符按窄处理，get-east-asian-width 默认即 narrow）。
//     - emoji / 宽字符计 2；combining / 零宽字符计 0（不受 profile 影响）；其余计 1。
//   truncateTerminalText(text, maxColumns) -> string
//     - 按东亚列宽预算截断，预算用尽即停并追加 '...'。
//   composerVisualLines(text, width) -> number
//     - 视觉行数基于 terminalColumns 计算。
// package.json:
//   dependencies["get-east-asian-width"] 必须声明为非空版本号。

type EastAsianWidthOptions = {ambiguousAsWide?: boolean};

function widthContract() {
  const app = App as any;
  const missing: string[] = [];
  if (typeof app.terminalColumns !== 'function') missing.push('terminalColumns');
  if (typeof app.truncateTerminalText !== 'function') missing.push('truncateTerminalText');
  if (typeof app.composerVisualLines !== 'function') missing.push('composerVisualLines');
  if (missing.length) {
    throw new Error(
      `src-open/App.tsx 尚未导出 ${missing.join(' / ')}，东亚宽度计算行为缺失（需接入 get-east-asian-width）`,
    );
  }
  return {
    terminalColumns: app.terminalColumns as (text: string, options?: EastAsianWidthOptions) => number,
    truncateTerminalText: app.truncateTerminalText as (text: string, maxColumns: number) => string,
    composerVisualLines: app.composerVisualLines as (text: string, width: number) => number,
  };
}

// EW1：emoji 占两个终端列。
test('emoji contributes width 2 to the terminal column count', () => {
  const {terminalColumns} = widthContract();
  assert.equal(terminalColumns('😀'), 2);
  assert.equal(terminalColumns('a😀b'), 4);
  // 混合 CJK：1 + 1 + 2 + 2 = 6
  assert.equal(terminalColumns('hi😀界'), 6);
});

// EW2：CJK 表意文字占两个终端列。
test('CJK ideographs contribute width 2 to the terminal column count', () => {
  const {terminalColumns} = widthContract();
  assert.equal(terminalColumns('字'), 2);
  assert.equal(terminalColumns('a字b'), 4);
  // 全角拉丁字符（U+FF21）同样占两列。
  assert.equal(terminalColumns('\uFF21'), 2);
});

// EW3：ambiguous 字符宽度遵循配置的东亚宽度 profile。
test('ambiguous-width characters follow the configured East Asian width profile', () => {
  const {terminalColumns} = widthContract();
  // 默认 narrow profile：希腊字母 α（U+03B1）与省略号 …（U+2026）各占一列。
  assert.equal(terminalColumns('α'), 1);
  assert.equal(terminalColumns('…'), 1);
  // wide profile：同一字符占两列。
  assert.equal(terminalColumns('α', {ambiguousAsWide: true}), 2);
  assert.equal(terminalColumns('…', {ambiguousAsWide: true}), 2);
});

// EW4：combining 与零宽字符不占列。
test('combining and zero-width characters contribute width 0', () => {
  const {terminalColumns} = widthContract();
  // 组合重音（U+0301）挂在 e 上：总宽仍是 1。
  assert.equal(terminalColumns('e\u0301'), 1);
  // CJK + 组合重音：仍是 2。
  assert.equal(terminalColumns('字\u0301'), 2);
  // 零宽连接符（U+200D）在 ASCII 字符之间不占列。
  assert.equal(terminalColumns('a\u200Db'), 2);
  // 家庭 emoji ZWJ 序列：3 × 2 = 6，两个 ZWJ 各计 0。
  assert.equal(terminalColumns('👨\u200D👩\u200D👧'), 6);
  // 零宽属性与 ambiguous profile 无关：wide profile 下组合重音仍计 0。
  assert.equal(terminalColumns('a\u0301', {ambiguousAsWide: true}), 1);
});

// 截断按东亚列宽预算消费（EW1/EW4 的截断边界）。
test('truncation consumes the East Asian column budget', () => {
  const {truncateTerminalText} = widthContract();
  // 预算 8：极限 5，两个 CJK 字符（4 列）后再放第三个会超（6 > 5）。
  assert.equal(truncateTerminalText('你好世界', 8), '你好...');
  // 组合重音不占列：'a' + 重音仍在 1 列预算内，可保留。
  assert.equal(truncateTerminalText('a\u0301b', 4), 'a\u0301...');
});

// composer 视觉行数使用东亚宽度（EW2/EW4 的行数边界）。
test('composer visual lines use East Asian widths', () => {
  const {composerVisualLines} = widthContract();
  // 20 个 ASCII + 组合重音：宽度 20，20 列内恰好一行。
  assert.equal(composerVisualLines('a'.repeat(20) + '\u0301', 20), 1);
  // 11 个 CJK 字符：22 列，需要两行。
  assert.equal(composerVisualLines('字'.repeat(11), 20), 2);
});

// EW5：get-east-asian-width 已声明为运行时依赖。
test('get-east-asian-width is declared as a runtime dependency', () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as {
    dependencies?: Record<string, string>;
  };
  const version = pkg.dependencies?.['get-east-asian-width'];
  assert.equal(typeof version, 'string');
  assert.match(String(version ?? ''), /^\d/);
});
