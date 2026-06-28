'use client';

import './ChatBox.css';

import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport, type ChatStatus, type UIMessage } from 'ai';
import { Send } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useMemo, useState } from 'react';
import { Markdown } from './markdown';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

type ChatMetadata = {
  sources?: ChatSource[];
};

type BookWikiChatMessage = UIMessage<ChatMetadata>;
type BookWikiChatPart = BookWikiChatMessage['parts'][number];
type ToolMessagePart = BookWikiChatPart & { type: `tool-${string}` };

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
            messages,
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
  const hasVisibleParts = message.parts.some(isVisiblePart);
  const sources = message.metadata?.sources ?? [];

  return (
    <div className="chat-answer">
      <p>
        <strong>{message.role === 'user' ? 'You' : 'BookWiki'}:</strong>
      </p>
      {message.parts.map((part, index) => (
        <ChatMessagePart key={`${message.id}-${index}`} part={part} />
      ))}
      {!hasVisibleParts && message.role === 'assistant' && isBusy(status) ? <p>Searching...</p> : null}
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

function isVisiblePart(part: BookWikiChatPart) {
  return part.type === 'text' || part.type === 'reasoning' || isToolPart(part);
}

function isToolPart(part: BookWikiChatPart): part is ToolMessagePart {
  return part.type.startsWith('tool-');
}

function ChatMessagePart({ part }: { part: BookWikiChatPart }) {
  if (part.type === 'text') {
    return <Markdown text={part.text} />;
  }

  if (part.type === 'reasoning') {
    if (!part.text) return null;

    return (
      <details>
        <summary>Reasoning</summary>
        <Markdown text={part.text} />
      </details>
    );
  }

  if (isToolPart(part)) {
    return <ToolPart part={part} />;
  }

  return null;
}

function ToolPart({ part }: { part: ToolMessagePart }) {
  const toolName = part.type.slice('tool-'.length);
  const title = 'title' in part && typeof part.title === 'string' ? part.title : toolName;
  const state = 'state' in part && typeof part.state === 'string' ? part.state : 'pending';
  const input = 'input' in part ? compactJson(part.input) : null;
  const output = 'output' in part ? summarizeToolOutput(part.output) : null;
  const errorText = 'errorText' in part && typeof part.errorText === 'string' ? part.errorText : null;

  return (
    <details>
      <summary>
        Tool: {title} ({state})
      </summary>
      {input ? <pre>Input: {input}</pre> : null}
      {output ? <pre>Output: {output}</pre> : null}
      {errorText ? <p className="tool-error">{errorText}</p> : null}
    </details>
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

function summarizeToolOutput(value: unknown) {
  if (value === undefined || value === null) return null;

  if (typeof value === 'object' && value !== null) {
    const record = value as Record<string, unknown>;

    if (Array.isArray(record.chunks)) {
      return compactJson({ chunks: record.chunks.length });
    }

    if ('slug' in record || 'sourceRefs' in record || 'found' in record) {
      return compactJson({
        found: record.found,
        slug: record.slug,
        title: record.title,
        sourceRefs: record.sourceRefs,
      });
    }
  }

  return compactJson(value);
}

function compactJson(value: unknown) {
  try {
    const json = JSON.stringify(value, null, 2);
    if (!json) return null;
    return json.length > 800 ? `${json.slice(0, 800)}...` : json;
  } catch {
    return String(value);
  }
}
