import assert from 'node:assert/strict';
import {test} from 'node:test';
import * as App from '../src-open/App.tsx';
import * as Interaction from '../src-open/interaction.ts';

// kill-ring 编辑命令（Ctrl+U/Ctrl+W/Ctrl+Y）的契约测试。
// 实现方需导出以下纯函数（其余键盘逻辑保持纯直通）：
// src-open/App.tsx:
//   resolveComposerKeyBinding(event) 扩展:
//     - Ctrl+U => 'kill-line-backward'
//     - Ctrl+W => 'kill-word-backward'
//     - Ctrl+Y => 'yank'
//     - 其余按键（含普通 u/w/y 与带 meta/alt 的组合）=> null，保持直通不被劫持
//   killLineBackward(state) -> {state, killed}
//     - 删除光标到当前行行首的文本并返回被杀文本 killed；光标移到行首
//     - 光标已在行首：原样返回，killed: ''
//   killWordBackward(state) -> {state, killed}
//     - shell 风格（unix-word-rubout）：只有空格/制表符分隔词，'/' 等不算分隔符
//     - 删除光标前的词；若光标后紧跟分隔空白则连同空白一起删除（词+其后空白）
//     - 换行是硬边界：不跨行删除；光标在行首：原样返回，killed: ''
//     - 光标位于词中间：只删除光标前的部分词（readline 语义）
//   applyComposerKillAction(state, ring, action) -> {state, ring}
//     - state = {text, cursor}，cursor 为字符偏移，范围 [0, text.length]
//     - 'kill-line-backward' / 'kill-word-backward'：执行删除并把 killed 存入 ring
//     - 'yank'：把 ring 顶部文本插入光标处；ring 为空则原样返回
// src-open/interaction.ts:
//   type KillRing = {entries: string[]; lastOp: 'kill' | 'yank' | null}
//   createKillRing() -> KillRing            // {entries: [], lastOp: null}
//   killRingPush(ring, killed) -> KillRing
//     - killed 为空串：原样返回（不产生空条目，也不打断累积）
//     - 连续 kill（lastOp === 'kill'）时把新文本前插到 entries[0] 合并，
//       使 yank 按原序还原整段（shell 兼容：Ctrl+W、Ctrl+W、Ctrl+U 后 Ctrl+Y 还原整行）
//     - 否则在头部插入新条目并把 lastOp 置 'kill'
//   killRingYank(ring) -> {ring, text}
//     - ring 为空：返回 {ring 原样, text: ''}，不抛错
//     - 否则：返回 {ring: {...ring, lastOp: 'yank'}, text: entries[0]}

type ComposerKeyEvent = {
  name?: string;
  ctrl?: boolean;
  shift?: boolean;
  meta?: boolean;
  alt?: boolean;
};

type ComposerKillAction = 'kill-line-backward' | 'kill-word-backward' | 'yank';
type ComposerEditState = {text: string; cursor: number};
type KillRing = {entries: string[]; lastOp: 'kill' | 'yank' | null};

function contract() {
  const app = App as any;
  const interaction = Interaction as any;
  const missing: string[] = [];
  if (typeof app.resolveComposerKeyBinding !== 'function') missing.push('resolveComposerKeyBinding');
  if (typeof app.killLineBackward !== 'function') missing.push('killLineBackward');
  if (typeof app.killWordBackward !== 'function') missing.push('killWordBackward');
  if (typeof app.applyComposerKillAction !== 'function') missing.push('applyComposerKillAction');
  if (typeof interaction.createKillRing !== 'function') missing.push('createKillRing');
  if (typeof interaction.killRingPush !== 'function') missing.push('killRingPush');
  if (typeof interaction.killRingYank !== 'function') missing.push('killRingYank');
  if (missing.length) {
    throw new Error(
      'src-open/App.tsx 与 src-open/interaction.ts 尚未导出 kill-ring 契约函数：' +
        missing.join(', ') + '，Ctrl+U/Ctrl+W/Ctrl+Y 行为缺失',
    );
  }
  return {
    resolve: app.resolveComposerKeyBinding as (event: ComposerKeyEvent) => string | null,
    killLine: app.killLineBackward as (state: ComposerEditState) => {state: ComposerEditState; killed: string},
    killWord: app.killWordBackward as (state: ComposerEditState) => {state: ComposerEditState; killed: string},
    applyKill: app.applyComposerKillAction as (
      state: ComposerEditState,
      ring: KillRing,
      action: ComposerKillAction,
    ) => {state: ComposerEditState; ring: KillRing},
    createKillRing: interaction.createKillRing as () => KillRing,
    killRingPush: interaction.killRingPush as (ring: KillRing, killed: string) => KillRing,
    killRingYank: interaction.killRingYank as (ring: KillRing) => {ring: KillRing; text: string},
  };
}

test('Ctrl+U/W/Y 绑定 kill 动作且普通键入与 meta/alt 组合不被劫持', () => {
  const {resolve} = contract();
  assert.equal(resolve({name: 'u', ctrl: true}), 'kill-line-backward');
  assert.equal(resolve({name: 'w', ctrl: true}), 'kill-word-backward');
  assert.equal(resolve({name: 'y', ctrl: true}), 'yank');
  // 普通字符继续正常输入
  assert.equal(resolve({name: 'u'}), null);
  assert.equal(resolve({name: 'w'}), null);
  assert.equal(resolve({name: 'y'}), null);
  // 带 meta/alt 的组合继续直通（不劫持终端快捷键）
  assert.equal(resolve({name: 'u', ctrl: true, meta: true}), null);
  assert.equal(resolve({name: 'y', ctrl: true, alt: true}), null);
  // 既有绑定不受影响
  assert.equal(resolve({name: 'a', ctrl: true}), 'beginning-of-line');
  assert.equal(resolve({name: 'k', ctrl: true}), 'interrupt');
});

test('Ctrl+U 删除光标到行首并把文本存入 kill ring (KR1)', () => {
  const {applyKill, killLine} = contract();
  // 光标位于行中、前面有文本：删除到行首
  assert.deepEqual(killLine({text: 'hello world', cursor: 6}), {state: {text: 'world', cursor: 0}, killed: 'hello '});
  // 多行输入只删当前行，不跨行
  assert.deepEqual(killLine({text: 'ab\ncd', cursor: 4}), {state: {text: 'ab\nd', cursor: 3}, killed: 'c'});
  // 光标已在行首：安全空操作
  assert.deepEqual(killLine({text: 'ab\ncd', cursor: 3}), {state: {text: 'ab\ncd', cursor: 3}, killed: ''});
  // 集成路径：删除文本同时写入 ring
  const out = applyKill({text: 'hello world', cursor: 6}, {entries: [], lastOp: null}, 'kill-line-backward');
  assert.deepEqual(out.state, {text: 'world', cursor: 0});
  assert.deepEqual(out.ring, {entries: ['hello '], lastOp: 'kill'});
  // 空删除不产生 ring 条目
  const noop = applyKill({text: 'ab\ncd', cursor: 3}, out.ring, 'kill-line-backward');
  assert.deepEqual(noop.state, {text: 'ab\ncd', cursor: 3});
  assert.deepEqual(noop.ring, out.ring);
});

test('Ctrl+W 删除前一个词及其分隔空白并存入 kill ring (KR2)', () => {
  const {applyKill, killWord} = contract();
  // 光标跟在词边界后：删除前一个词
  assert.deepEqual(killWord({text: 'echo hello world', cursor: 15}), {state: {text: 'echo hello ', cursor: 10}, killed: 'world'});
  // 光标在词后的分隔空白之后：词连同其分隔空白一起删除
  assert.deepEqual(killWord({text: 'hello world ', cursor: 12}), {state: {text: 'hello ', cursor: 6}, killed: 'world '});
  // '/' 不是词分隔符（shell 风格：只有空白分隔词）
  assert.deepEqual(killWord({text: 'cd src/open', cursor: 11}), {state: {text: 'cd ', cursor: 3}, killed: 'src/open'});
  // 多行输入不跨过换行边界
  assert.deepEqual(killWord({text: 'ab\ncd', cursor: 3}), {state: {text: 'ab\ncd', cursor: 3}, killed: ''});
  assert.deepEqual(killWord({text: 'ab\ncd', cursor: 5}), {state: {text: 'ab\n', cursor: 3}, killed: 'cd'});
  // 集成路径：删除文本同时写入 ring
  const out = applyKill({text: 'echo hello world', cursor: 15}, {entries: [], lastOp: null}, 'kill-word-backward');
  assert.deepEqual(out.state, {text: 'echo hello ', cursor: 10});
  assert.deepEqual(out.ring, {entries: ['world'], lastOp: 'kill'});
});

test('连续 kill 按 shell 顺序累积合并，yank 后另起新条目 (KR3)', () => {
  const {applyKill, createKillRing, killRingPush, killRingYank} = contract();
  let ring = createKillRing();
  assert.deepEqual(ring, {entries: [], lastOp: null});
  // 第一次 Ctrl+W：删除 'baz'
  let out = applyKill({text: 'foo bar baz', cursor: 11}, ring, 'kill-word-backward');
  assert.deepEqual(out.state, {text: 'foo bar ', cursor: 8});
  assert.deepEqual(out.ring, {entries: ['baz'], lastOp: 'kill'});
  // 连续 Ctrl+W：'bar ' 前插合并到顶部条目
  out = applyKill(out.state, out.ring, 'kill-word-backward');
  assert.deepEqual(out.state, {text: 'foo ', cursor: 4});
  assert.deepEqual(out.ring, {entries: ['bar baz'], lastOp: 'kill'});
  // 连续 Ctrl+U：'foo ' 继续前插合并，yank 按原序还原整句
  out = applyKill(out.state, out.ring, 'kill-line-backward');
  assert.deepEqual(out.state, {text: '', cursor: 0});
  assert.deepEqual(out.ring, {entries: ['foo bar baz'], lastOp: 'kill'});
  assert.equal(killRingYank(out.ring).text, 'foo bar baz');
  // yank 打断累积：之后的 kill 另起新条目，旧内容仍保留
  const afterYank = killRingYank(out.ring);
  assert.equal(afterYank.ring.lastOp, 'yank');
  const next = killRingPush(afterYank.ring, 'x ');
  assert.deepEqual(next, {entries: ['x ', 'foo bar baz'], lastOp: 'kill'});
  // 空 kill 不产生空条目，也不打断累积
  assert.deepEqual(killRingPush(out.ring, ''), out.ring);
});

test('Ctrl+Y 把最近一次 kill 的文本插回光标处 (KR4)', () => {
  const {applyKill, createKillRing, killRingPush, killRingYank} = contract();
  const ring = killRingPush(createKillRing(), 'world');
  const out = applyKill({text: 'foo', cursor: 1}, ring, 'yank');
  assert.deepEqual(out.state, {text: 'fworldoo', cursor: 6});
  assert.deepEqual(out.ring, {entries: ['world'], lastOp: 'yank'});
  // 多条目时插入最近一次 kill（entries[0]），旧条目保留
  const first = killRingPush(createKillRing(), 'older');
  const multi = killRingPush(killRingYank(first).ring, 'recent ');
  assert.deepEqual(multi, {entries: ['recent ', 'older'], lastOp: 'kill'});
  const out2 = applyKill({text: 'ab', cursor: 2}, multi, 'yank');
  assert.deepEqual(out2.state, {text: 'abrecent ', cursor: 9});
  assert.deepEqual(out2.ring, {entries: ['recent ', 'older'], lastOp: 'yank'});
});

test('kill ring 为空时 Ctrl+Y 安全空操作且不报错 (KR5)', () => {
  const {applyKill, createKillRing, killRingYank} = contract();
  const empty = createKillRing();
  assert.deepEqual(empty, {entries: [], lastOp: null});
  // 纯 helper：空 ring 的 yank 返回空文本且不抛错
  assert.deepEqual(killRingYank(empty), {ring: empty, text: ''});
  // 集成路径：光标与文本保持不变
  const out = applyKill({text: 'abc', cursor: 2}, empty, 'yank');
  assert.deepEqual(out.state, {text: 'abc', cursor: 2});
  assert.deepEqual(out.ring, {entries: [], lastOp: null});
});

test('Ctrl+W 只以空白为词分隔并支持制表符、行首与词中间边界', () => {
  const {killWord} = contract();
  // 制表符与空格一样是词分隔符，且随词后的分隔空白一起删除
  assert.deepEqual(killWord({text: 'a\tb ', cursor: 4}), {state: {text: 'a\t', cursor: 2}, killed: 'b '});
  // 光标位于词中间：只删除光标前的部分词（readline 语义）
  assert.deepEqual(killWord({text: 'hello world', cursor: 9}), {state: {text: 'hello ld', cursor: 6}, killed: 'wor'});
  // 行首/缓冲开头：安全空操作
  assert.deepEqual(killWord({text: 'hello', cursor: 0}), {state: {text: 'hello', cursor: 0}, killed: ''});
  // 只有前导空白时全部删除
  assert.deepEqual(killWord({text: '   ', cursor: 3}), {state: {text: '', cursor: 0}, killed: '   '});
});
