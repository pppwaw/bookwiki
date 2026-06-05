'use client';

import {
  type ComponentProps,
  createContext,
  type ReactNode,
  type SyntheticEvent,
  use,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport, type ChatStatus, type UIMessage } from 'ai';
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

type BookWikiChatMessage = UIMessage<ChatMetadata>;
type BookWikiChatPart = BookWikiChatMessage['parts'][number];
type ToolMessagePart = BookWikiChatPart & { type: `tool-${string}` };

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
  const {
    clearError,
    error,
    messages,
    regenerate,
    sendMessage: sendChatMessage,
    setMessages,
    status,
  } = useChat<BookWikiChatMessage>({
    id: `bookwiki:${pagePath}`,
    transport,
  });

  async function sendMessage(question: string) {
    const trimmed = question.trim();
    if (!trimmed || isBusy(status)) return;

    setLastQuestion(trimmed);
    clearError();
    await sendChatMessage({ text: trimmed });
  }

  async function retry() {
    if (isBusy(status)) return;

    if (messages.at(-1)?.role === 'assistant') {
      await regenerate();
    } else if (lastQuestion) {
      await sendChatMessage({ text: lastQuestion });
    }
  }

  function clear() {
    setMessages([]);
    clearError();
    setLastQuestion('');
  }

  return (
    <Context
      value={{
        clear,
        error: error ? messageFromError(error) : null,
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

function isVisiblePart(part: BookWikiChatPart) {
  return part.type === 'text' || part.type === 'reasoning' || isToolPart(part);
}

function isToolPart(part: BookWikiChatPart): part is ToolMessagePart {
  return part.type.startsWith('tool-');
}

function MessagePart({ part }: { part: BookWikiChatPart }) {
  if (part.type === 'text') {
    return (
      <div className="prose text-sm">
        <Markdown text={part.text} />
      </div>
    );
  }

  if (part.type === 'reasoning') {
    if (!part.text) return null;

    return (
      <details className="rounded-lg border bg-fd-secondary/60 p-2 text-xs text-fd-muted-foreground">
        <summary className="cursor-pointer font-medium text-fd-foreground">Reasoning</summary>
        <div className="mt-2">
          <Markdown text={part.text} />
        </div>
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
    <div className="rounded-lg border bg-fd-secondary/60 p-2 text-xs text-fd-muted-foreground">
      <div className="flex items-center justify-between gap-2">
        <p className="font-medium text-fd-foreground">Tool: {title}</p>
        <code>{state}</code>
      </div>
      {input ? (
        <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words">Input: {input}</pre>
      ) : null}
      {output ? (
        <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words">Output: {output}</pre>
      ) : null}
      {errorText ? <p className="mt-2 text-fd-error">{errorText}</p> : null}
    </div>
  );
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

const roleName: Record<BookWikiChatMessage['role'], string> = {
  system: 'system',
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
            'fixed z-30 flex flex-col overflow-hidden bg-fd-card text-fd-card-foreground shadow-xl [--ai-chat-width:400px] 2xl:[--ai-chat-width:460px]',
            'inset-x-2 inset-y-4 rounded-2xl border',
            'lg:inset-x-auto lg:inset-y-auto lg:bottom-4 lg:end-4 lg:w-(--ai-chat-width) lg:h-[min(640px,calc(100dvh-5rem))]',
            open ? 'animate-fd-dialog-in' : 'animate-fd-dialog-out',
          )}
        >
          <div className="flex flex-col size-full p-2 lg:p-3">
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
