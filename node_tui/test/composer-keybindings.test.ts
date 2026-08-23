import assert from 'node:assert/strict';
import {test} from 'node:test';
import * as App from '../src-open/App.tsx';

// P1-01 光标与字符快捷键（Ctrl+A/D，Ctrl+E 改绑到行尾）的契约测试。
// 实现方需在 src-open/App.tsx 中导出两个纯函数（其余键盘逻辑保持纯直通）：
//   resolveComposerKeyBinding(event) -> action | null
//     - Ctrl+A        => 'beginning-of-line'
//     - Ctrl+E        => 'end-of-line'（effort 选择器不再占用 Ctrl+E）
//     - Ctrl+Shift+E  => 'open-effort'（effort 选择器的新无冲突绑定）
//     - Ctrl+D        => 'delete-char-forward'
//     - Ctrl+P/N/R/O/L/K => 'history-previous' / 'history-next' / 'history-search'
//                           / 'toggle-paste' / 'clear-screen' / 'interrupt'
//     - 其余按键（含 Ctrl+C、普通字符）=> null，保持直通不被劫持
//   applyComposerKeyAction(state, action) -> state
//     - state = {text, cursor}，cursor 为字符偏移，范围 [0, text.length]
//     - beginning-of-line：移到当前行行首（上一个 '\n' 之后，无则 0）
//     - end-of-line：移到当前行行尾（下一个 '\n' 之前，无则 text.length）
//     - delete-char-forward：光标后有字符则删除一个字符，否则原样返回（安全空操作）
//     - 其它 action（既有快捷键）不改动编辑缓冲，原样返回

type ComposerKeyEvent = {
  name?: string;
  ctrl?: boolean;
  shift?: boolean;
  meta?: boolean;
  alt?: boolean;
};

type ComposerKeyAction =
  | 'beginning-of-line'
  | 'end-of-line'
  | 'delete-char-forward'
  | 'open-effort'
  | 'history-previous'
  | 'history-next'
  | 'history-search'
  | 'toggle-paste'
  | 'clear-screen'
  | 'interrupt';

type ComposerEditState = {text: string; cursor: number};

function keybindings() {
  const resolve = (App as any).resolveComposerKeyBinding;
  const apply = (App as any).applyComposerKeyAction;
  if (typeof resolve !== 'function' || typeof apply !== 'function') {
    throw new Error(
      'src-open/App.tsx 尚未导出 resolveComposerKeyBinding / applyComposerKeyAction，' +
        'P1-01 光标快捷键行为缺失（Ctrl+A/E/D 与 effort 选择器新绑定）',
    );
  }
  return {
    resolve: resolve as (event: ComposerKeyEvent) => ComposerKeyAction | null,
    apply: apply as (state: ComposerEditState, action: ComposerKeyAction) => ComposerEditState,
  };
}

test('Ctrl+A 把光标移动到行首', () => {
  const {resolve, apply} = keybindings();
  assert.equal(resolve({name: 'a', ctrl: true}), 'beginning-of-line');
  // 光标不在行首时，Ctrl+A 移到行首
  assert.deepEqual(apply({text: 'hello', cursor: 3}, 'beginning-of-line'), {text: 'hello', cursor: 0});
  // 多行输入：移到当前行（第二行）的行首，而不是整个 buffer 开头
  assert.deepEqual(apply({text: 'ab\ncd', cursor: 4}, 'beginning-of-line'), {text: 'ab\ncd', cursor: 3});
});

test('Ctrl+E 把光标移动到行尾且不再打开 effort 选择器', () => {
  const {resolve, apply} = keybindings();
  // Ctrl+E 现在是行尾，不再是 effort 选择器
  assert.equal(resolve({name: 'e', ctrl: true}), 'end-of-line');
  assert.deepEqual(apply({text: 'hello', cursor: 1}, 'end-of-line'), {text: 'hello', cursor: 5});
  // 多行输入：移到当前行（第一行）的行尾
  assert.deepEqual(apply({text: 'ab\ncd', cursor: 0}, 'end-of-line'), {text: 'ab\ncd', cursor: 2});
});

test('effort 选择器使用无冲突的新绑定 Ctrl+Shift+E', () => {
  const {resolve} = keybindings();
  // 新绑定命中 effort 选择器动作
  assert.equal(resolve({name: 'e', ctrl: true, shift: true}), 'open-effort');
  // 普通键入 'e' 不受影响
  assert.equal(resolve({name: 'e'}), null);
});

test('Ctrl+D 删除光标后的字符', () => {
  const {resolve, apply} = keybindings();
  assert.equal(resolve({name: 'd', ctrl: true}), 'delete-char-forward');
  // 光标后有字符：删除光标后的那个字符，光标位置不变
  assert.deepEqual(apply({text: 'hello', cursor: 2}, 'delete-char-forward'), {text: 'helo', cursor: 2});
});

test('空输入下 Ctrl+D 是安全的空操作', () => {
  const {resolve, apply} = keybindings();
  assert.equal(resolve({name: 'd', ctrl: true}), 'delete-char-forward');
  // 输入为空：安全空操作，不崩溃、不改变状态
  assert.deepEqual(apply({text: '', cursor: 0}, 'delete-char-forward'), {text: '', cursor: 0});
  // 光标已在行尾：同样保持原状
  assert.deepEqual(apply({text: 'hi', cursor: 2}, 'delete-char-forward'), {text: 'hi', cursor: 2});
});

test('既有快捷键 Ctrl+P/N/R/O/L/K 语义保持不变', () => {
  const {resolve, apply} = keybindings();
  const legacy: Array<[ComposerKeyEvent, ComposerKeyAction]> = [
    [{name: 'p', ctrl: true}, 'history-previous'],
    [{name: 'n', ctrl: true}, 'history-next'],
    [{name: 'r', ctrl: true}, 'history-search'],
    [{name: 'o', ctrl: true}, 'toggle-paste'],
    [{name: 'l', ctrl: true}, 'clear-screen'],
    [{name: 'k', ctrl: true}, 'interrupt'],
  ];
  for (const [event, action] of legacy) {
    assert.equal(resolve(event), action);
    // 既有快捷键不改动编辑缓冲，仍走原有处理路径
    assert.deepEqual(apply({text: 'hello', cursor: 2}, action), {text: 'hello', cursor: 2});
  }
});

test('未绑定的按键（Ctrl+C 复制等）保持直通不被劫持', () => {
  const {resolve} = keybindings();
  // 复制选区等终端级组合键必须继续放行
  assert.equal(resolve({name: 'c', ctrl: true}), null);
  assert.equal(resolve({name: 'c', ctrl: true, shift: true}), null);
  // 无修饰的普通字符继续正常输入
  assert.equal(resolve({name: 'a'}), null);
  assert.equal(resolve({name: 'd'}), null);
});
