'use client';

import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport, type ChatStatus, type UIMessage } from 'ai';
import { Send } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useMemo, useState } from 'react';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

type ChatMetadata = {
  sources?: ChatSource[];
};

type BookWikiChatMessage = UIMessage<ChatMetadata>;

export function ChatBox() {
  const pagePath = usePathname();
  const [question, setQuestion] = useState('');
  const transport = useMemo(
    () =>
      new DefaultChatTransport<BookWikiChatMessage>({
        api: '/api/chat',
        prepareSendMessagesRequest: ({ id, messages }) => ({
          body: {
            id,
            message: messages.at(-1),
            pagePath,
          },
        }),
      }),
    [pagePath],
  );
  const { clearError, error, messages, sendMessage, status } = useChat<BookWikiChatMessage>({
    id: `bookwiki-inline:${pagePath}`,
    transport,
  });
  const loading = isBusy(status);

  async function ask() {
    const trimmed = question.trim();
    if (!trimmed || loading) return;

    clearError();
    setQuestion('');
    await sendMessage({ text: trimmed });
  }

  return (
    <section className="tool-panel">
      <form
        className="chat-form"
        onSubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
      >
        <textarea
          aria-label="Ask this book"
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask a question about this book"
          value={question}
        />
        <button disabled={!question.trim() || loading} title="Ask" type="submit">
          <Send aria-hidden="true" size={18} />
          <span>{loading ? 'Asking' : 'Ask'}</span>
        </button>
      </form>
      {error ? <p className="tool-error">{messageFromError(error)}</p> : null}
      {messages.map((message) => (
        <ChatMessage key={message.id} message={message} status={status} />
      ))}
    </section>
  );
}

function ChatMessage({
  message,
  status,
}: {
  message: BookWikiChatMessage;
  status: ChatStatus;
}) {
  const text = textFromMessage(message);
  const sources = message.metadata?.sources ?? [];

  return (
    <div className="chat-answer">
      <p>
        <strong>{message.role === 'user' ? 'You' : 'BookWiki'}:</strong>{' '}
        {text || (message.role === 'assistant' && isBusy(status) ? 'Searching...' : '')}
      </p>
      {sources.length ? (
        <ul>
          {sources.map((source) => (
            <li key={`${source.ref_id}-${source.page ?? ''}-${source.heading ?? ''}`}>
              <code>{source.ref_id}</code>
              {source.heading ? ` ${source.heading}` : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function isBusy(status: ChatStatus) {
  return status === 'submitted' || status === 'streaming';
}

function messageFromError(error: Error) {
  try {
    const payload = JSON.parse(error.message) as { error?: unknown };
    if (typeof payload.error === 'string') return payload.error;
  } catch {
    // The transport may already expose a plain text error.
  }

  return error.message;
}

function textFromMessage(message: BookWikiChatMessage) {
  return message.parts
    .filter(
      (part): part is Extract<BookWikiChatMessage['parts'][number], { type: 'text' }> =>
        part.type === 'text',
    )
    .map((part) => part.text)
    .join('');
}
