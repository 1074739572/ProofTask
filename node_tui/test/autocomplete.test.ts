import assert from 'node:assert/strict';
import {test} from 'node:test';
import {
  applyCompletionResult,
  completionContext,
  shouldHandleAutocompleteKey,
  type CompletionMenuState,
} from '../src/autocomplete.js';

// Contract for the non-modal composer autocomplete.
// This is deliberately pure: the OpenTUI component should call these helpers,
// while the tests prove that an open menu never steals ordinary typing.

const empty: CompletionMenuState = {
  mode: null,
  start: 0,
  end: 0,
  query: '',
  requestId: 0,
  options: [],
  selected: 0,
};

test('@ mention context stays active as the user continues typing', () => {
  assert.deepEqual(completionContext('@m', 2), {
    mode: 'mention', start: 0, end: 2, query: 'm',
  });
  assert.deepEqual(completionContext('@mai', 4), {
    mode: 'mention', start: 0, end: 4, query: 'mai',
  });
});

test('slash command context only activates for the first token', () => {
  assert.deepEqual(completionContext('/mo', 3), {
    mode: 'command', start: 0, end: 3, query: 'mo',
  });
  assert.equal(completionContext('/model x', 8), null);
  assert.equal(completionContext('explain /mo', 11), null);
});

test('ordinary typing keys are never consumed by an open autocomplete menu', () => {
  for (const key of ['a', 'backspace', 'left', 'right', 'space']) {
    assert.equal(shouldHandleAutocompleteKey(key), false, key);
  }
});

test('only navigation and selection keys are consumed by autocomplete', () => {
  for (const key of ['up', 'down', 'tab', 'return', 'escape']) {
    assert.equal(shouldHandleAutocompleteKey(key), true, key);
  }
});

test('a later completion response replaces candidates in an already visible menu', () => {
  const visible: CompletionMenuState = {
    mode: 'mention',
    start: 0,
    end: 2,
    query: 'm',
    requestId: 2,
    options: ['main.py', 'models.py'],
    selected: 1,
  };

  const updated = applyCompletionResult(visible, 3, ['mail.py']);
  assert.deepEqual(updated, {
    mode: 'mention',
    start: 0,
    end: 2,
    query: 'm',
    requestId: 3,
    options: ['mail.py'],
    selected: 0,
  });
});

test('an out-of-order completion response cannot overwrite a newer query', () => {
  const latest: CompletionMenuState = {
    ...empty,
    mode: 'command',
    start: 0,
    end: 3,
    query: 'mo',
    requestId: 8,
    options: ['/model', '/mode'],
  };

  assert.equal(applyCompletionResult(latest, 7, ['/goal']), latest);
});
