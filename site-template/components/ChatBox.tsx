'use client';

import { Send } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useState } from 'react';
import { Markdown } from './markdown';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  sources?: ChatSource[];
};

type ChatResponse = {
  answer?: unknown;
  sources?: unknown;
  error?: unknown;
};

export function ChatBox() {
  const pagePath = usePathname();
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function ask() {
    const trimmed = question.trim();
    if (!trimmed || loading) return;

    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: 'user', text: trimmed };
    setMessages((current) => [...current, userMessage]);
    setQuestion('');
    setError(null);
    setLoading(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmed, pagePath }),
      });
      const payload = (await response.json().catch(() => ({}))) as ChatResponse;
      if (!response.ok) throw new Error(messageFromPayload(payload, `Chat request failed: HTTP ${response.status}`));

      const answer = typeof payload.answer === 'string' ? payload.answer : '';
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          text: answer || 'No answer returned.',
          sources: parseSources(payload.sources),
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'chat request failed');
    } finally {
      setLoading(false);
    }
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
      {error ? <p className="tool-error">{error}</p> : null}
      {messages.map((message) => (
        <ChatMessageView key={message.id} message={message} />
      ))}
      {loading ? <div className="chat-answer"><p>BookWiki is answering...</p></div> : null}
    </section>
  );
}

function ChatMessageView({ message }: { message: ChatMessage }) {
  const sources = message.sources ?? [];

  return (
    <div className="chat-answer">
      <p>
        <strong>{message.role === 'user' ? 'You' : 'BookWiki'}:</strong>
      </p>
      <Markdown text={message.text} />
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

function parseSources(value: unknown): ChatSource[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is ChatSource => {
    return typeof item === 'object' && item !== null && typeof (item as ChatSource).ref_id === 'string';
  });
}

function messageFromPayload(payload: ChatResponse, fallback: string) {
  return typeof payload.error === 'string' ? payload.error : fallback;
}
