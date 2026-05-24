'use client';

import { Send } from 'lucide-react';
import { useState } from 'react';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

export function ChatBox() {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [sources, setSources] = useState<ChatSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function ask() {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch("/api/chat", {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const payload = (await response.json()) as {
        answer?: string;
        sources?: ChatSource[];
        error?: string;
      };

      if (!response.ok) {
        throw new Error(payload.error ?? 'chat failed');
      }

      setAnswer(payload.answer ?? '');
      setSources(payload.sources ?? []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'chat failed');
      setAnswer('');
      setSources([]);
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
      {answer ? (
        <div className="chat-answer">
          <p>{answer}</p>
          {sources.length ? (
            <ul>
              {sources.map((source) => (
                <li key={`${source.ref_id}-${source.page ?? ''}`}>
                  <code>{source.ref_id}</code>
                  {source.heading ? ` ${source.heading}` : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
