import assert from 'node:assert/strict';
import test from 'node:test';
import {footerHint} from '../src-open/interaction.ts';

// 前端消息队列自动排空（frontend message queue with auto-drain）：
// agent 忙碌时，用户提交的消息不立即发送，而是进入客户端本地待发队列；
// agent 转为空闲后，由空闲转换 effect 自动按 FIFO 顺序补发，每条恰好发送一次；
// 页脚反映本地待发数量与队列状态，而不是只依赖后端 queue 事件。
//
// 实现契约（批准改动范围：src-open/App.tsx、src-open/interaction.ts）：
// src-open/interaction.ts 需导出纯函数 createMessageQueue(send)，返回一个
// 队列对象 {setBusy, submit, pendingCount, pending}：
//   - submit(command)   空闲时立即交给 send；忙碌时只入队，不调用 send；
//   - setBusy(false)    空闲转换触发点：按 FIFO 顺序排空队列，每条只发送一次；
//   - pendingCount()    返回本地待发数量（页脚与 toast 使用）；
//   - pending()         返回当前待发消息快照（FIFO 队头在前）。
// App.tsx 在 running() 为真时用 submit 入队（不立即 send），并在 running()
// 翻转为 false 的 effect 中调用 setBusy(false) 触发自动排空。
//
// 注意：createMessageQueue 在实现前尚不存在，因此这里使用动态导入并在断言中
// 明确“缺少导出”，保证实现前的基线失败是行为缺失断言，而不是模块加载错误，
// 从而让测试名始终可被机器收集。
async function loadQueueFactory(): Promise<(send: (command: Record<string, unknown>) => boolean) => any> {
  const interaction = await import('../src-open/interaction.ts');
  const factory = (interaction as any).createMessageQueue;
  assert.equal(typeof factory, 'function', 'src-open/interaction.ts 缺少 createMessageQueue(send) 导出');
  return factory;
}

test('忙碌时提交的消息不立即发送，而是暂存到本地待发队列', async () => {
  const createMessageQueue = await loadQueueFactory();
  const sent: Record<string, unknown>[] = [];
  const queue = createMessageQueue((command: Record<string, unknown>) => {
    sent.push(command);
    return true;
  });

  // given：后端 agent 正在运行（busy）
  queue.setBusy(true);
  // when：用户提交消息
  queue.submit({type: 'user_message', text: 'first'});
  queue.submit({type: 'user_message', text: 'second'});
  // then：消息不立即发送，而是存入本地待发队列
  assert.equal(sent.length, 0);
  assert.equal(queue.pendingCount(), 2);

  // 对照：转为空闲后新提交的消息应立即发送，队列只在忙碌时启用
  queue.setBusy(false);
  queue.submit({type: 'user_message', text: 'third'});
  assert.equal(sent.length, 3);
  assert.equal(queue.pendingCount(), 0);
});

test('转入空闲时按 FIFO 顺序自动补发本地队列，且每条只发送一次', async () => {
  const createMessageQueue = await loadQueueFactory();
  const sent: string[] = [];
  const queue = createMessageQueue((command: Record<string, unknown>) => {
    sent.push(String(command.text ?? ''));
    return true;
  });

  // given：本地队列存在多条待发消息
  queue.setBusy(true);
  queue.submit({type: 'user_message', text: 'one'});
  queue.submit({type: 'user_message', text: 'two'});
  queue.submit({type: 'user_message', text: 'three'});
  assert.equal(queue.pendingCount(), 3);

  // when：agent 转为空闲，空闲转换 effect 触发
  queue.setBusy(false);
  // then：按 FIFO 顺序自动发送，每条恰好一次，队列清空
  assert.deepEqual(sent, ['one', 'two', 'three']);
  assert.equal(queue.pendingCount(), 0);

  // 重复的空闲转换不得重发旧消息
  queue.setBusy(false);
  assert.deepEqual(sent, ['one', 'two', 'three']);

  // 新一轮 忙碌→空闲 只补发新增消息，且保持顺序
  queue.setBusy(true);
  queue.submit({type: 'user_message', text: 'four'});
  queue.submit({type: 'user_message', text: 'five'});
  queue.setBusy(false);
  assert.deepEqual(sent, ['one', 'two', 'three', 'four', 'five']);
});

test('页脚渲染本地待发数量与队列状态', async () => {
  const createMessageQueue = await loadQueueFactory();
  const queue = createMessageQueue(() => true);

  // given：本地待发队列存在
  queue.setBusy(true);
  queue.submit({type: 'user_message', text: 'one'});
  queue.submit({type: 'user_message', text: 'two'});
  assert.equal(queue.pendingCount(), 2);

  // when：页脚渲染，pending 来自本地队列计数
  const footer = footerHint({
    width: 120, running: true, phase: 'working', elapsed: '4s', pending: queue.pendingCount(),
    toolsDone: 0, toolsTotal: 0, backend: 'connected', composerLines: 1,
  });
  // then：页脚展示本地待发数量与队列状态（明确标注本地队列，而非仅后端 queue 事件）
  assert.match(footer, /2/);
  assert.match(footer, /本地队列|local queue|local pending/i);
});
