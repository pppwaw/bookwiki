'use client';

import {
  type ComponentProps,
  createContext,
  type ReactNode,
  type SyntheticEvent,
  use,
  useEffect,
  useEffectEvent,
  useRef,
  useState,
} from 'react';
import { Loader2, MessageCircleIcon, RefreshCw, SearchIcon, Send, X } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { Presence } from '@radix-ui/react-presence';
import { Markdown } from '../markdown';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';
import { cn } from '../../lib/cn';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

type ChatMetadata = {
  sources?: ChatSource[];
};

type ChatStatus = 'idle' | 'submitted';

type BookWikiChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  parts: Array<{ type: 'text'; text: string }>;
  metadata?: ChatMetadata;
};

type BookWikiChatPart = BookWikiChatMessage['parts'][number];

type ChatResponse = {
  answer?: unknown;
  sources?: unknown;
  error?: unknown;
};

const Context = createContext<{
  open: boolean;
  setOpen: (open: boolean) => void;
  messages: BookWikiChatMessage[];
  status: ChatStatus;
  error: string | null;
  sendMessage: (question: string) => Promise<void>;
  retry: () => Promise<void>;
  clear: () => void;
} | null>(null);

export function AISearch({ children }: { children: ReactNode }) {
  const pagePath = usePathname();
  const [open, setOpen] = useState(false);
  const [lastQuestion, setLastQuestion] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [messages, setMessages] = useState<BookWikiChatMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>('idle');

  async function sendMessage(question: string) {
    const trimmed = question.trim();
    if (!trimmed || isBusy(status)) return;

    setLastQuestion(trimmed);
    setError(null);
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: 'user', parts: [{ type: 'text', text: trimmed }] },
    ]);
    setStatus('submitted');

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmed, pagePath }),
      });
      const payload = (await response.json().catch(() => ({}))) as ChatResponse;
      if (!response.ok) throw new Error(messageFromPayload(payload, `Chat request failed: HTTP ${response.status}`));

      const answer = typeof payload.answer === 'string' ? payload.answer : 'No answer returned.';
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          parts: [{ type: 'text', text: answer }],
          metadata: { sources: parseSources(payload.sources) },
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'chat request failed');
    } finally {
      setStatus('idle');
    }
  }

  async function retry() {
    if (isBusy(status)) return;

    if (lastQuestion) await sendMessage(lastQuestion);
  }

  function clear() {
    setMessages([]);
    setError(null);
    setLastQuestion('');
  }

  return (
    <Context
      value={{
        clear,
        error,
        messages,
        open,
        retry,
        sendMessage,
        setOpen,
        status,
      }}
    >
      {children}
    </Context>
  );
}

export function AISearchPanelHeader({ className, ...props }: ComponentProps<'div'>) {
  const { setOpen } = useAISearchContext();

  return (
    <div
      className={cn(
        'sticky top-0 flex items-start gap-2 border rounded-xl bg-fd-secondary text-fd-secondary-foreground shadow-sm',
        className,
      )}
      {...props}
    >
      <div className="px-3 py-2 flex-1">
        <p className="text-sm font-medium mb-2">BookWiki Chat</p>
        <p className="text-xs text-fd-muted-foreground">
          Answers are grounded in this book&apos;s SQLite index.
        </p>
      </div>

      <button
        aria-label="Close"
        tabIndex={-1}
        className={cn(
          buttonVariants({
            size: 'icon-sm',
            color: 'ghost',
            className: 'text-fd-muted-foreground rounded-full',
          }),
        )}
        onClick={() => setOpen(false)}
      >
        <X />
      </button>
    </div>
  );
}

export function AISearchInputActions() {
  const { clear, messages, retry, status } = useAISearchContext();
  const isLoading = isBusy(status);

  if (messages.length === 0) return null;

  return (
    <>
      {!isLoading && messages.at(-1)?.role === 'assistant' && (
        <button
          type="button"
          className={cn(
            buttonVariants({
              color: 'secondary',
              size: 'sm',
              className: 'rounded-full gap-1.5',
            }),
          )}
          onClick={() => void retry()}
        >
          <RefreshCw className="size-4" />
          Retry
        </button>
      )}
      <button
        type="button"
        className={cn(
          buttonVariants({
            color: 'secondary',
            size: 'sm',
            className: 'rounded-full',
          }),
        )}
        onClick={clear}
      >
        Clear Chat
      </button>
    </>
  );
}

const StorageKeyInput = '__bookwiki_ai_search_input';

export function AISearchInput(props: ComponentProps<'form'>) {
  const { sendMessage, status } = useAISearchContext();
  const [input, setInput] = useState(() => localStorage.getItem(StorageKeyInput) ?? '');
  const isLoading = isBusy(status);

  const onStart = (event?: SyntheticEvent) => {
    event?.preventDefault();
    const message = input.trim();
    if (!message) return;

    void sendMessage(message);
    setInput('');
    localStorage.removeItem(StorageKeyInput);
  };

  useEffect(() => {
    if (isLoading) document.getElementById('nd-ai-input')?.focus();
  }, [isLoading]);

  return (
    <form {...props} className={cn('flex items-start pe-2', props.className)} onSubmit={onStart}>
      <Input
        value={input}
        placeholder={isLoading ? 'BookWiki is answering...' : 'Ask this book'}
        autoFocus
        className="p-3"
        disabled={isLoading}
        onChange={(event) => {
          setInput(event.target.value);
          localStorage.setItem(StorageKeyInput, event.target.value);
        }}
        onKeyDown={(event) => {
          if (!event.shiftKey && event.key === 'Enter') {
            onStart(event);
          }
        }}
      />
      <button
        type="submit"
        className={cn(
          buttonVariants({
            color: isLoading ? 'secondary' : 'primary',
            className: 'transition-all rounded-full mt-2',
          }),
        )}
        disabled={input.length === 0 || isLoading}
      >
        {isLoading ? (
          <Loader2 className="size-4 animate-spin text-fd-muted-foreground" />
        ) : (
          <Send className="size-4" />
        )}
      </button>
    </form>
  );
}

function List(props: Omit<ComponentProps<'div'>, 'dir'>) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function callback() {
      containerRef.current?.scrollTo({
        top: containerRef.current.scrollHeight,
        behavior: 'instant',
      });
    }

    const observer = new ResizeObserver(callback);
    callback();

    const element = containerRef.current?.firstElementChild;
    if (element) observer.observe(element);

    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      {...props}
      className={cn('fd-scroll-container overflow-y-auto min-w-0 flex flex-col', props.className)}
    >
      {props.children}
    </div>
  );
}

function Input(props: ComponentProps<'textarea'>) {
  const ref = useRef<HTMLDivElement>(null);
  const shared = cn('col-start-1 row-start-1', props.className);

  return (
    <div className="grid flex-1">
      <textarea
        id="nd-ai-input"
        {...props}
        className={cn(
          'resize-none bg-transparent placeholder:text-fd-muted-foreground focus-visible:outline-none',
          shared,
        )}
      />
      <div ref={ref} className={cn(shared, 'break-all invisible')}>
        {`${props.value?.toString() ?? ''}\n`}
      </div>
    </div>
  );
}

function isBusy(status: ChatStatus) {
  return status === 'submitted';
}

function isVisiblePart(part: BookWikiChatPart) {
  return part.type === 'text';
}

function MessagePart({ part }: { part: BookWikiChatPart }) {
  if (part.type === 'text') {
    return (
      <div className="prose text-sm">
        <Markdown text={part.text} />
      </div>
    );
  }

  return null;
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

const roleName: Record<BookWikiChatMessage['role'], string> = {
  user: 'you',
  assistant: 'bookwiki',
};

function Message({ message, ...props }: { message: BookWikiChatMessage } & ComponentProps<'div'>) {
  const hasVisibleParts = message.parts.some(isVisiblePart);
  const sources = message.metadata?.sources ?? [];

  return (
    <div onClick={(event) => event.stopPropagation()} {...props}>
      <p
        className={cn(
          'mb-1 text-sm font-medium text-fd-muted-foreground',
          message.role === 'assistant' && 'text-fd-primary',
        )}
      >
        {roleName[message.role]}
      </p>
      <div className="flex flex-col gap-2">
        {message.parts.map((part, index) => (
          <MessagePart key={`${message.id}-${index}`} part={part} />
        ))}
        {!hasVisibleParts ? <p className="text-fd-muted-foreground">Searching...</p> : null}
      </div>

      {sources.length ? (
        <div className="flex flex-col gap-2 mt-3 rounded-lg border bg-fd-secondary text-fd-muted-foreground text-xs p-2">
          <div className="flex flex-row gap-2 items-center">
            <SearchIcon className="size-4" />
            <p>{sources.length} source refs</p>
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {sources.map((source) => (
              <li key={`${source.ref_id}-${source.page ?? ''}-${source.heading ?? ''}`}>
                <code>{source.ref_id}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export function AISearchTrigger({
  position = 'default',
  className,
  ...props
}: ComponentProps<'button'> & { position?: 'default' | 'float' }) {
  const { open, setOpen } = useAISearchContext();

  return (
    <button
      data-state={open ? 'open' : 'closed'}
      className={cn(
        position === 'float' && [
          'fixed bottom-4 gap-3 w-24 inset-e-[calc(--spacing(4)+var(--removed-body-scroll-bar-size,0px))] shadow-lg z-20 transition-[translate,opacity]',
          open && 'translate-y-10 opacity-0',
        ],
        className,
      )}
      onClick={() => setOpen(!open)}
      {...props}
    >
      {props.children}
    </button>
  );
}

export function AISearchPanel() {
  const { open, setOpen } = useAISearchContext();
  useHotKey();

  return (
    <>
      <style>
        {`
        @keyframes ask-ai-open {
          from { translate: 100% 0; }
          to { translate: 0 0; }
        }
        @keyframes ask-ai-close {
          from { width: var(--ai-chat-width); }
          to { width: 0px; }
        }`}
      </style>
      <Presence present={open}>
        <div
          className={cn(
            'fixed inset-0 z-30 backdrop-blur-xs bg-fd-overlay lg:hidden',
            open ? 'animate-fd-fade-in' : 'animate-fd-fade-out',
          )}
          onClick={() => setOpen(false)}
        />
      </Presence>
      <Presence present={open}>
        <div
          className={cn(
            'overflow-hidden z-30 bg-fd-card text-fd-card-foreground [--ai-chat-width:400px] 2xl:[--ai-chat-width:460px]',
            'max-lg:fixed max-lg:inset-x-2 max-lg:inset-y-4 max-lg:border max-lg:rounded-2xl max-lg:shadow-xl',
            'lg:sticky lg:top-0 lg:h-dvh lg:border-s lg:ms-auto lg:in-[#nd-docs-layout]:[grid-area:toc] lg:in-[#nd-notebook-layout]:row-span-full lg:in-[#nd-notebook-layout]:col-start-5',
            open
              ? 'animate-fd-dialog-in lg:animate-[ask-ai-open_200ms]'
              : 'animate-fd-dialog-out lg:animate-[ask-ai-close_200ms]',
          )}
        >
          <div className="flex flex-col size-full p-2 lg:p-3 lg:w-(--ai-chat-width)">
            <AISearchPanelHeader />
            <AISearchPanelList className="flex-1" />
            <div className="rounded-xl border bg-fd-secondary text-fd-secondary-foreground shadow-sm has-focus-visible:shadow-md">
              <AISearchInput />
              <div className="flex items-center gap-1.5 p-1 empty:hidden">
                <AISearchInputActions />
              </div>
            </div>
          </div>
        </div>
      </Presence>
    </>
  );
}

export function AISearchPanelList({ className, style, ...props }: ComponentProps<'div'>) {
  const { error, messages } = useAISearchContext();

  return (
    <List
      className={cn('py-4 overscroll-contain', className)}
      style={{
        maskImage:
          'linear-gradient(to bottom, transparent, white 1rem, white calc(100% - 1rem), transparent 100%)',
        ...style,
      }}
      {...props}
    >
      {messages.length === 0 ? (
        <div className="text-sm text-fd-muted-foreground/80 size-full flex flex-col items-center justify-center text-center gap-2">
          <MessageCircleIcon fill="currentColor" stroke="none" />
          <p onClick={(event) => event.stopPropagation()}>Ask a question about this book.</p>
        </div>
      ) : (
        <div className="flex flex-col px-3 gap-4">
          {error ? (
            <div className="p-2 bg-fd-secondary text-fd-secondary-foreground border rounded-lg">
              <p className="text-xs text-fd-muted-foreground mb-1">Request Failed</p>
              <p className="text-sm">{error}</p>
            </div>
          ) : null}
          {messages.map((item) => (
            <Message key={item.id} message={item} />
          ))}
        </div>
      )}
    </List>
  );
}

export function useHotKey() {
  const { open, setOpen } = useAISearchContext();

  const onKeyPress = useEffectEvent((event: KeyboardEvent) => {
    if (event.key === 'Escape' && open) {
      setOpen(false);
      event.preventDefault();
    }

    if (event.key === '/' && (event.metaKey || event.ctrlKey) && !open) {
      setOpen(true);
      event.preventDefault();
    }
  });

  useEffect(() => {
    window.addEventListener('keydown', onKeyPress);
    return () => window.removeEventListener('keydown', onKeyPress);
  }, []);
}

export function useAISearchContext() {
  return use(Context)!;
}
