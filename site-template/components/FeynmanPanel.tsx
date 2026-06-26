'use client';

import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport, type ChatStatus, type UIMessage } from 'ai';
import { CheckCircle2, MessageCircleQuestion, RotateCcw, Send, SquareStop } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';
import { Markdown } from '@/components/markdown';
import { cn } from '@/lib/cn';
import {
  conversationSignature,
  FeynmanChatStorageKey,
  useChatHistory,
} from '@/lib/chat-history';
import {
  feynmanContinuePrompt,
  feynmanProbePrompt,
  feynmanReviewPrompt,
} from '@/lib/feynman-prompts';

type FeynmanPanelProps = {
  scope: string;
  keypoints: string[];
  summary?: string;
};

type Phase = 'idle' | 'probing' | 'reviewing' | 'done';

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

const EmptyMessages: BookWikiChatMessage[] = [];
const ReviewRequest = '请对本段讲解给出总评。';

export function FeynmanPanel({ scope, keypoints, summary }: FeynmanPanelProps) {
  const pagePath = usePathname();
  const history = useChatHistory<BookWikiChatMessage>(FeynmanChatStorageKey);
  const [initialMessages] = useState(() => history.activeConversation?.messages ?? EmptyMessages);
  const [phase, setPhase] = useState<Phase>(() => phaseFromMessages(initialMessages));
  const [explanation, setExplanation] = useState('');
  const [answer, setAnswer] = useState('');
  const [pendingInitial, setPendingInitial] = useState<string | null>(null);
  const [pendingReview, setPendingReview] = useState(false);
  const [messageCount, setMessageCount] = useState(initialMessages.length);

  const transport = useMemo(() => {
    let systemPrompt = '';
    if (phase === 'probing') {
      systemPrompt = (messageCount === 0 ? feynmanProbePrompt : feynmanContinuePrompt)({
        keypoints,
        scope,
      });
    } else if (phase === 'reviewing') {
      systemPrompt = feynmanReviewPrompt({ keypoints, scope });
    }
    return new DefaultChatTransport<BookWikiChatMessage>({
      api: '/api/chat',
      prepareSendMessagesRequest: ({ id, messages }) => ({
        body: { id, messages, pagePath, system: systemPrompt },
      }),
    });
  }, [pagePath, phase, keypoints, scope, messageCount]);

  const { clearError, error, messages, sendMessage, setMessages, status, stop } =
    useChat<BookWikiChatMessage>({
      id: `feynman:${pagePath}`,
      messages: initialMessages,
      transport,
    });
  const loading = isBusy(status);
  const hasSubmitted = messages.length > 0 || pendingInitial !== null;
  const hasAssistantProbe = messages.some((message) => message.role === 'assistant');
  const canReview = phase === 'probing' && hasAssistantProbe && !loading;
  const showDialogueComposer = phase === 'probing' && hasSubmitted;

  const signatureRef = useRef(conversationSignature(initialMessages));
  const hydratedRef = useRef(false);

  useEffect(() => {
    setMessageCount(messages.length);
  }, [messages.length]);

  useEffect(() => {
    if (!history.hydrated || hydratedRef.current || messages.length > 0) return;
    hydratedRef.current = true;
    const storedMessages = history.activeConversation?.messages ?? EmptyMessages;
    if (storedMessages.length === 0) return;
    setMessages(storedMessages);
    setPhase(phaseFromMessages(storedMessages));
    signatureRef.current = conversationSignature(storedMessages);
  }, [history.hydrated, history.activeConversation?.messages, messages.length, setMessages]);

  useEffect(() => {
    if (status !== 'ready' || messages.length === 0) return;
    const signature = conversationSignature(messages);
    if (signature === signatureRef.current) return;
    signatureRef.current = signature;
    history.saveMessages(history.activeId, pagePath, messages);
  }, [status, messages, history, pagePath]);

  const sendFeynmanMessage = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || isBusy(status)) return;

    clearError();
    await sendMessage({ text: trimmed });
  }, [clearError, sendMessage, status]);

  useEffect(() => {
    if (phase !== 'probing' || pendingInitial === null) return;
    const text = pendingInitial;
    setPendingInitial(null);
    void sendFeynmanMessage(text);
  }, [phase, pendingInitial, sendFeynmanMessage]);

  useEffect(() => {
    if (phase !== 'reviewing' || !pendingReview) return;
    setPendingReview(false);
    void sendFeynmanMessage(ReviewRequest);
  }, [phase, pendingReview, sendFeynmanMessage]);

  useEffect(() => {
    if (phase !== 'reviewing' || status !== 'ready') return;
    const last = messages.at(-1);
    if (last?.role === 'assistant' && isReviewAssistantMessage(messages, messages.length - 1)) {
      setPhase('done');
    }
  }, [phase, status, messages]);

  async function submitExplanation() {
    const trimmed = explanation.trim();
    if (!trimmed || loading) return;

    setExplanation('');
    setPendingInitial(trimmed);
    setPhase('probing');
  }

  async function submitAnswer() {
    const trimmed = answer.trim();
    if (!trimmed || loading || phase !== 'probing') return;

    setAnswer('');
    await sendFeynmanMessage(trimmed);
  }

  function requestReview() {
    if (!canReview) return;

    setPendingReview(true);
    setPhase('reviewing');
  }

  function restart() {
    history.newChat();
    setMessages([]);
    setPhase('idle');
    setExplanation('');
    setAnswer('');
    setPendingInitial(null);
    setPendingReview(false);
    signatureRef.current = conversationSignature([]);
  }

  return (
    <section className="feynman-panel">
      <div className="feynman-heading">
        <span className="feynman-heading-icon" aria-hidden="true">
          <MessageCircleQuestion size={20} />
        </span>
        <div>
          <h2>费曼学习法</h2>
          <p>先用自己的话讲一遍，再让困惑同学追问你没讲清的地方。</p>
        </div>
      </div>

      <div className="feynman-keypoints">
        <h3>要点清单</h3>
        {keypoints.length ? (
          <ol>
            {keypoints.map((point, index) => (
              <li key={`${index}-${point}`}>
                <Markdown text={point} inline />
              </li>
            ))}
          </ol>
        ) : summary ? (
          <Markdown text={summary} />
        ) : (
          <p>请围绕本页核心概念，用自己的话讲清楚它是什么、为什么重要、如何使用。</p>
        )}
      </div>

      <form
        className="feynman-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submitExplanation();
        }}
      >
        <textarea
          aria-label="输入你的讲解"
          disabled={hasSubmitted}
          onChange={(event) => setExplanation(event.target.value)}
          placeholder="把你对这一页的理解讲给一个没学过的同学听……"
          value={explanation}
        />
        <button
          className={cn(buttonVariants({ color: 'primary' }))}
          disabled={!explanation.trim() || loading || hasSubmitted}
          type="submit"
        >
          <Send aria-hidden="true" size={16} />
          <span>{loading && !hasSubmitted ? '提交中' : '提交讲解'}</span>
        </button>
      </form>

      {error ? <p className="tool-error">{messageFromError(error)}</p> : null}

      {messages.length ? (
        <div className="feynman-messages">
          {messages.map((message, index) => (
            <ChatMessage
              key={message.id}
              message={message}
              status={status}
              label={messageLabel(messages, index, phase)}
            />
          ))}
        </div>
      ) : null}

      {showDialogueComposer ? (
        <form
          className="feynman-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submitAnswer();
          }}
        >
          <textarea
            aria-label="回答困惑同学的问题"
            disabled={loading || phase !== 'probing'}
            onChange={(event) => setAnswer(event.target.value)}
            placeholder="继续回答同学的追问……"
            value={answer}
          />
          <button
            className={cn(buttonVariants({ color: 'primary' }))}
            disabled={!answer.trim() || loading || phase !== 'probing'}
            type="submit"
          >
            <Send aria-hidden="true" size={16} />
            <span>{loading ? '发送中' : '继续回答'}</span>
          </button>
        </form>
      ) : null}

      {hasSubmitted ? (
        <div className="feynman-actions">
          {loading ? (
            <button className={cn(buttonVariants({ color: 'secondary' }))} onClick={stop} type="button">
              <SquareStop aria-hidden="true" size={16} />
              停止生成
            </button>
          ) : null}
          <button
            className={cn(buttonVariants({ color: 'secondary' }))}
            disabled={!canReview}
            onClick={requestReview}
            type="button"
          >
            <CheckCircle2 aria-hidden="true" size={16} />
            结束并评价
          </button>
          <button className={cn(buttonVariants({ color: 'ghost' }))} onClick={restart} type="button">
            <RotateCcw aria-hidden="true" size={16} />
            重新开始
          </button>
        </div>
      ) : null}
    </section>
  );
}

function ChatMessage({
  label,
  message,
  status,
}: {
  label: string;
  message: BookWikiChatMessage;
  status: ChatStatus;
}) {
  const hasVisibleParts = message.parts.some(isVisiblePart);
  const sources = message.metadata?.sources ?? [];

  return (
    <div className={cn('feynman-msg', message.role === 'user' ? 'feynman-msg-user' : 'feynman-msg-assistant')}>
      <p>
        <strong>{label}:</strong>
      </p>
      {message.parts.map((part, index) => (
        <ChatMessagePart key={`${message.id}-${index}`} part={part} />
      ))}
      {!hasVisibleParts && message.role === 'assistant' && isBusy(status) ? <p>正在思考...</p> : null}
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
        <summary>推理过程</summary>
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

function phaseFromMessages(messages: BookWikiChatMessage[]): Phase {
  if (messages.length === 0) return 'idle';
  const last = messages.at(-1);
  if (last?.role === 'assistant' && isReviewAssistantMessage(messages, messages.length - 1)) return 'done';
  if (last?.role === 'user' && textOf(last) === ReviewRequest) return 'reviewing';
  return 'probing';
}

function messageLabel(messages: BookWikiChatMessage[], index: number, phase: Phase): string {
  const message = messages[index];
  if (message?.role === 'user') return '我';
  if (phase === 'reviewing' && index === messages.length - 1) return '评价';
  return isReviewAssistantMessage(messages, index) ? '评价' : '困惑同学';
}

function isReviewAssistantMessage(messages: BookWikiChatMessage[], index: number): boolean {
  const message = messages[index];
  const previous = messages[index - 1];
  return message?.role === 'assistant' && previous?.role === 'user' && textOf(previous) === ReviewRequest;
}

function textOf(message: BookWikiChatMessage | undefined): string {
  if (!message) return '';
  return message.parts
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('')
    .trim();
}
