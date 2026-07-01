import { expect, test } from 'vitest';
import type { UIMessage } from 'ai';
import { stripFileParts } from './chat-history';

function msg(id: string, role: UIMessage['role'], parts: UIMessage['parts']): UIMessage {
  return { id, role, parts };
}

test('stripFileParts removes file parts but keeps text/reasoning', () => {
  const messages: UIMessage[] = [
    msg('u1', 'user', [
      { type: 'file', mediaType: 'image/png', url: 'data:image/png;base64,AAAA' },
      { type: 'text', text: '这道题怎么做' },
    ]),
    msg('a1', 'assistant', [
      { type: 'reasoning', text: 'think' },
      { type: 'text', text: '答案是…' },
    ]),
  ];

  const stripped = stripFileParts(messages);

  expect(stripped[0].parts).toEqual([{ type: 'text', text: '这道题怎么做' }]);
  expect(stripped[1].parts).toEqual([
    { type: 'reasoning', text: 'think' },
    { type: 'text', text: '答案是…' },
  ]);
});

test('stripFileParts returns the same reference when there is no file part', () => {
  const messages: UIMessage[] = [msg('u1', 'user', [{ type: 'text', text: 'hi' }])];
  const stripped = stripFileParts(messages);
  // No allocation for untouched messages.
  expect(stripped[0]).toBe(messages[0]);
});

test('stripFileParts can empty a message that was only an image', () => {
  const messages: UIMessage[] = [
    msg('u1', 'user', [{ type: 'file', mediaType: 'image/jpeg', url: 'data:image/jpeg;base64,BBBB' }]),
  ];
  const stripped = stripFileParts(messages);
  expect(stripped[0].parts).toEqual([]);
});
