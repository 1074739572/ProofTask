import assert from 'node:assert/strict';
import {test} from 'node:test';

import * as interaction from '../src-open/interaction.ts';
import {
  applyCompletionResult,
  completionContext,
  moveCompletionSelection,
  shouldHandleAutocompleteKey,
  type CompletionMenuState,
} from '../src/autocomplete.js';

// 结构化可滚动补全菜单（CM1-CM7）与页脚交互（FT3）的聚焦回归测试。
//
// 分层：全部为 pure_logic ——
// - CM4/CM5/CM6/CM7 走既有边界 src/autocomplete.ts 的纯函数（上下文、合并、
//   键路由语义必须保留）；
// - FT3 走既有边界 src-open/interaction.ts 的 footerHint（completionOpen）；
// - CM1/CM2/CM3 属于“结构化选项 + 滚动窗口 + 目录浏览”的新行为，按本仓库
//   composer-keybindings 契约测试的先例：在既有模块 src-open/interaction.ts
//   上查找缺失的纯函数，缺失时抛出描述性行为失败（而不是裸导入错误）。

type CompletionOption = {
  label: string;
  description?: string;
  icon?: string;
  isDirectory?: boolean;
};

type CompletionWindow = {
  options: CompletionOption[];
  start: number;
  total: number;
  selected: number;
  selectedVisible: boolean;
};

type CompletionOptionRow = {
  label: string;
  description: string;
  icon: string;
  directory: boolean;
};

type DirectoryTraversal = {
  text: string;
  cursor: number;
  path: string;
};

function menuSeams() {
  const mod = interaction as any;
  const completionMenuWindow = mod.completionMenuWindow;
  const completionOptionRow = mod.completionOptionRow;
  const enterCompletionDirectory = mod.enterCompletionDirectory;
  const missing = [
    typeof completionMenuWindow !== 'function' && 'completionMenuWindow',
    typeof completionOptionRow !== 'function' && 'completionOptionRow',
    typeof enterCompletionDirectory !== 'function' && 'enterCompletionDirectory',
  ].filter(Boolean).join(' / ');
  if (missing) {
    throw new Error(
      `src-open/interaction.ts 尚未导出 ${missing}：结构化滚动补全菜单（CM1-CM3）行为缺失 —— ` +
        '补全选项必须携带 label/description/icon/目录状态；超过 6 个候选时菜单可滚动到任意候选；' +
        '接受目录选项后追加遍历分隔符并请求子目录补全',
    );
  }
  return {
    completionMenuWindow: completionMenuWindow as (
      options: CompletionOption[],
      selected: number,
      maxRows?: number,
    ) => CompletionWindow,
    completionOptionRow: completionOptionRow as (option: CompletionOption) => CompletionOptionRow,
    enterCompletionDirectory: enterCompletionDirectory as (
      option: CompletionOption,
      text: string,
      start: number,
      end: number,
    ) => DirectoryTraversal | null,
  };
}

function openMenu(requestId: number, options: unknown[], selected = 0): CompletionMenuState {
  return {
    mode: 'mention',
    start: 0,
    end: 2,
    query: 'm',
    requestId,
    options: options as string[],
    selected,
  };
}

const footerBase = {
  width: 120,
  running: false,
  phase: 'idle',
  elapsed: '0s',
  pending: 0,
  toolsDone: 0,
  toolsTotal: 0,
  backend: 'connected' as const,
  composerLines: 1,
};

// ---------- FT3（跨任务交互：补全菜单打开时页脚输出导航/选择提示） ----------

test('补全菜单打开时页脚显示补全导航与选择提示', () => {
  assert.equal(
    interaction.footerHint({...footerBase, completionOpen: true}),
    '↑↓ select · Tab/Enter apply · Esc close',
  );
  // 打开补全菜单时该提示优先于运行状态页脚，导航与选择键始终可见
  assert.equal(
    interaction.footerHint({
      ...footerBase,
      completionOpen: true,
      running: true,
      phase: 'working',
      currentTool: 'read_file',
      toolsDone: 2,
      toolsTotal: 5,
    }),
    '↑↓ select · Tab/Enter apply · Esc close',
  );
  // 空闲状态默认仍是发送提示，两者可区分
  assert.notEqual(interaction.footerHint({...footerBase, completionOpen: true}), interaction.footerHint(footerBase));
});

// ---------- CM4（光标移动重算补全上下文） ----------

test('光标移动时补全菜单按新光标位置重算候选上下文', () => {
  assert.deepEqual(completionContext('@ma', 3), {mode: 'mention', start: 0, end: 3, query: 'ma'});
  // 光标左移：候选范围收窄，end 与 query 同步重算
  assert.deepEqual(completionContext('@ma', 1), {mode: 'mention', start: 0, end: 1, query: 'm'});
  // 光标移过空白：当前上下文失效，菜单应关闭并重新请求
  assert.equal(completionContext('@ma x', 5), null);
});

// ---------- CM5（迟到的补全响应不能覆盖更新的菜单） ----------

test('迟到的补全响应不能覆盖更新的菜单状态', () => {
  const latest = openMenu(8, ['/model', '/mode']);
  assert.equal(applyCompletionResult(latest, 7, ['/goal']), latest);
  // 结构化候选同样受请求序号保护，旧响应不得改写已显示的菜单
  const structured = openMenu(8, [{label: '/model', icon: 'command'}]);
  assert.equal(applyCompletionResult(structured, 7, [{label: '/goal'}]), structured);
});

test('更新的补全响应替换结构化候选并重置选中项到第一项', () => {
  const updated = applyCompletionResult(
    openMenu(2, ['a.py'], 1),
    3,
    [{label: 'mail.py', description: 'Python 脚本', icon: 'file', isDirectory: false}],
  );
  assert.equal(updated.requestId, 3);
  assert.equal(updated.selected, 0);
  const options = updated.options as unknown as CompletionOption[];
  assert.deepEqual(options, [
    {label: 'mail.py', description: 'Python 脚本', icon: 'file', isDirectory: false},
  ]);
});

// ---------- CM6 / CM7（菜单打开时的键路由语义） ----------

test('补全菜单打开时普通输入键不被菜单消费', () => {
  for (const key of ['a', 'b', 'backspace', 'left', 'right', 'space']) {
    assert.equal(shouldHandleAutocompleteKey(key), false, key);
  }
});

test('补全菜单只消费导航与选择键', () => {
  for (const key of ['up', 'down', 'tab', 'return', 'escape']) {
    assert.equal(shouldHandleAutocompleteKey(key), true, key);
  }
});

// ---------- CM1（超过 6 个候选时可滚动选择任意候选） ----------

test('超过六个候选时可滚动到任意候选且选中项始终在窗口内', () => {
  const {completionMenuWindow} = menuSeams();
  const options = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'].map(label => ({label}));

  // 初始窗口展示前 6 项
  const first = completionMenuWindow(options, 0);
  assert.equal(first.total, 9);
  assert.equal(first.options.length, 6);
  assert.equal(first.start, 0);
  assert.equal(first.selectedVisible, true);

  // 逐项向下滚动：窗口最多 6 行，选中项永远可见且不会截断到六项以内
  for (let selected = 1; selected <= 8; selected++) {
    const win = completionMenuWindow(options, selected);
    assert.equal(win.options.length, 6, `selected=${selected}`);
    assert.equal(win.selectedVisible, true, `selected=${selected}`);
    assert.ok(win.start >= 0 && win.start + win.options.length <= options.length, `selected=${selected}`);
    assert.ok(
      win.start <= selected && selected < win.start + win.options.length,
      `selected=${selected} 不在可见窗口内`,
    );
    // 可见切片必须是候选列表的连续区间
    assert.deepEqual(win.options, options.slice(win.start, win.start + win.options.length));
  }

  // 滚动到底后最后一项可见，窗口滑到尾部
  const last = completionMenuWindow(options, 8);
  assert.equal(last.start, 3);
  assert.deepEqual(last.options.map(option => option.label), ['d', 'e', 'f', 'g', 'h', 'i']);

  // 回到第一项时窗口回到开头
  assert.equal(completionMenuWindow(options, 0).start, 0);
});

test('候选不超过六项时窗口展示全部候选而不截断', () => {
  const {completionMenuWindow} = menuSeams();
  const options = ['a.py', 'b.py', 'c.py'].map(label => ({label}));
  const win = completionMenuWindow(options, 2);
  assert.equal(win.total, 3);
  assert.equal(win.start, 0);
  assert.equal(win.selectedVisible, true);
  assert.deepEqual(win.options, options);
  // 空候选窗口保持安全
  const empty = completionMenuWindow([], 0);
  assert.equal(empty.total, 0);
  assert.equal(empty.selectedVisible, false);
});

test('既有选择接口在超过六个候选时仍能到达最后一项', () => {
  const options = Array.from({length: 10}, (_, i) => `candidate-${i}`);
  let state: CompletionMenuState = {
    mode: 'mention',
    start: 0,
    end: 1,
    query: '',
    requestId: 1,
    options,
    selected: 0,
  };
  for (let i = 0; i < 9; i++) state = moveCompletionSelection(state, 1);
  assert.equal(state.selected, 9);
  // 向下越过末尾回到第一项，向上越过开头回到最后一项：选择不截断
  assert.equal(moveCompletionSelection(state, 1).selected, 0);
  assert.equal(moveCompletionSelection({...state, selected: 0}, -1).selected, 9);
});

// ---------- CM2（补全选项携带元数据：标签、描述、图标、目录状态） ----------

test('补全选项渲染保留标签、描述、图标与目录状态', () => {
  const {completionOptionRow} = menuSeams();
  assert.deepEqual(
    completionOptionRow({label: 'src/main.py', description: 'Python 模块', icon: 'file', isDirectory: false}),
    {label: 'src/main.py', description: 'Python 模块', icon: 'file', directory: false},
  );
  assert.deepEqual(
    completionOptionRow({label: 'src', description: '目录', icon: 'directory', isDirectory: true}),
    {label: 'src', description: '目录', icon: 'directory', directory: true},
  );
  // 稀疏选项有安全默认值，不因缺少元数据而崩溃
  assert.deepEqual(completionOptionRow({label: 'a.py'}), {
    label: 'a.py',
    description: '',
    icon: '',
    directory: false,
  });
});

// ---------- CM3（接受目录选项：追加遍历分隔符并请求子级补全） ----------

test('接受目录补全选项后追加遍历分隔符并返回子目录请求路径', () => {
  const {enterCompletionDirectory} = menuSeams();
  const dir = {label: 'src', isDirectory: true};
  assert.deepEqual(enterCompletionDirectory(dir, 'src', 0, 3), {
    text: 'src/',
    cursor: 4,
    path: 'src',
  });
  assert.deepEqual(enterCompletionDirectory(dir, 'ls src', 3, 6), {
    text: 'ls src/',
    cursor: 7,
    path: 'src',
  });
});

test('接受非目录补全选项时不触发目录遍历', () => {
  const {enterCompletionDirectory} = menuSeams();
  assert.equal(enterCompletionDirectory({label: 'main.py', isDirectory: false}, 'main.py', 0, 7), null);
});
