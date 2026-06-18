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
import {
  ArrowLeft,
  History,
  Loader2,
  MessageCircleIcon,
  Pencil,
  RefreshCw,
  SearchIcon,
  Send,
  Sparkles,
  Square,
  SquarePen,
  Trash2,
  X,
} from 'lucide-react';
import { usePathname } from 'next/navigation';
import { Presence } from '@radix-ui/react-presence';
import { Markdown } from '../markdown';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';
import { cn } from '../../lib/cn';
import {
  conversationSignature,
  type StoredConversation,
  useChatHistory,
} from '../../lib/chat-history';

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
type Conversation = StoredConversation<BookWikiChatMessage>;
type PanelView = 'chat' | 'history';

const EmptyMessages: BookWikiChatMessage[] = [];

// ---------------------------------------------------------------------------
// History context: conversation list + active selection (persisted)
// ---------------------------------------------------------------------------

type HistoryContextValue = {
  open: boolean;
  setOpen: (open: boolean) => void;
  view: PanelView;
  setView: (view: PanelView) => void;
  pagePath: string;
  hydrated: boolean;
  conversations: Conversation[];
  activeId: string;
  activeConversation: Conversation | null;
  startNewChat: () => void;
  openChat: (id: string) => void;
  deleteChat: (id: string) => void;
  renameChat: (id: string, title: string) => void;
  saveMessages: (id: string, pagePath: string, messages: BookWikiChatMessage[]) => void;
};

const HistoryContext = createContext<HistoryContextValue | null>(null);

function useHistory() {
  return use(HistoryContext)!;
}

export function AISearch({ children }: { children: ReactNode }) {
  const pagePath = usePathname();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<PanelView>('chat');
  const {
    activeConversation,
    activeId,
    conversations,
    deleteChat,
    hydrated,
    newChat,
    renameChat,
    saveMessages,
    selectChat,
  } = useChatHistory<BookWikiChatMessage>();

  const value = useMemo<HistoryContextValue>(
    () => ({
      open,
      setOpen,
      view,
      setView,
      pagePath,
      hydrated,
      conversations,
      activeId,
      activeConversation,
      startNewChat: () => {
        newChat();
        setView('chat');
      },
      openChat: (id: string) => {
        selectChat(id);
        setView('chat');
      },
      deleteChat,
      renameChat,
      saveMessages,
    }),
    [
      open,
      view,
      pagePath,
      hydrated,
      conversations,
      activeId,
      activeConversation,
      newChat,
      selectChat,
      deleteChat,
      renameChat,
      saveMessages,
    ],
  );

  return <HistoryContext value={value}>{children}</HistoryContext>;
}

// ---------------------------------------------------------------------------
// Session context: the live useChat instance for the active conversation
// ---------------------------------------------------------------------------

type SessionContextValue = {
  messages: BookWikiChatMessage[];
  status: ChatStatus;
  error: string | null;
  sendMessage: (question: string) => Promise<void>;
  retry: () => Promise<void>;
  stop: () => void;
};

const SessionContext = createContext<SessionContextValue | null>(null);

function useSession() {
  return use(SessionContext)!;
}

function ChatSession() {
  const { activeConversation, activeId, pagePath, saveMessages } = useHistory();

  // Captured once at mount (the subtree is keyed by activeId, so this is the
  // correct conversation and stays stable across re-renders).
  const [initialMessages] = useState(() => activeConversation?.messages ?? EmptyMessages);
  const [sessionPagePath] = useState(() => activeConversation?.pagePath ?? pagePath);
  const [lastQuestion, setLastQuestion] = useState('');

  const transport = useMemo(
    () =>
      new DefaultChatTransport<BookWikiChatMessage>({
        api: '/api/chat',
        prepareSendMessagesRequest: ({ id, messages }) => ({
          body: {
            id,
            messages,
            pagePath: sessionPagePath,
          },
        }),
      }),
    [sessionPagePath],
  );

  const { clearError, error, messages, regenerate, sendMessage: sendChatMessage, status, stop } =
    useChat<BookWikiChatMessage>({
      id: activeId,
      messages: initialMessages,
      transport,
    });

  // Persist the conversation whenever a turn settles.
  const signatureRef = useRef(conversationSignature(initialMessages));
  useEffect(() => {
    if (status !== 'ready' || messages.length === 0) return;
    const signature = conversationSignature(messages);
    if (signature === signatureRef.current) return;
    signatureRef.current = signature;
    saveMessages(activeId, sessionPagePath, messages);
  }, [status, messages, activeId, sessionPagePath, saveMessages]);

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

  const value: SessionContextValue = {
    messages,
    status,
    error: error ? messageFromError(error) : null,
    sendMessage,
    retry,
    stop,
  };

  return (
    <SessionContext value={value}>
      <ChatList className="flex-1" />
      <ChatComposer />
    </SessionContext>
  );
}

// ---------------------------------------------------------------------------
// Trigger + panel shell
// ---------------------------------------------------------------------------

export function AISearchTrigger({
  position = 'default',
  className,
  ...props
}: ComponentProps<'button'> & { position?: 'default' | 'float' }) {
  const { open, setOpen } = useHistory();

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
  const { activeId, open, setOpen, view } = useHistory();
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
            'fixed z-30 flex flex-col overflow-hidden bg-fd-card text-fd-card-foreground shadow-2xl [--ai-chat-width:400px] 2xl:[--ai-chat-width:460px]',
            'inset-x-2 inset-y-4 rounded-2xl border',
            'lg:inset-x-auto lg:inset-y-auto lg:bottom-4 lg:end-4 lg:w-(--ai-chat-width) lg:h-[min(680px,calc(100dvh-5rem))]',
            open ? 'animate-fd-dialog-in' : 'animate-fd-dialog-out',
          )}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="flex flex-col size-full">
            <PanelHeader />
            <div className="relative flex flex-1 flex-col min-h-0">
              <ChatSession key={activeId} />
              <Presence present={view === 'history'}>
                <div
                  className={cn(
                    'absolute inset-0 z-10 flex flex-col bg-fd-card',
                    view === 'history' ? 'animate-fd-fade-in' : 'animate-fd-fade-out',
                  )}
                >
                  <ConversationListView />
                </div>
              </Presence>
            </div>
          </div>
        </div>
      </Presence>
    </>
  );
}

function PanelHeader() {
  const { activeConversation, pagePath, setOpen, setView, startNewChat, view } = useHistory();

  if (view === 'history') {
    return (
      <header className="flex items-center gap-2 border-b px-3 py-2.5">
        <IconButton label="Back to chat" onClick={() => setView('chat')}>
          <ArrowLeft className="size-4" />
        </IconButton>
        <p className="flex-1 text-sm font-semibold">Chat history</p>
        <IconButton label="New chat" onClick={startNewChat}>
          <SquarePen className="size-4" />
        </IconButton>
        <IconButton label="Close" onClick={() => setOpen(false)}>
          <X className="size-4" />
        </IconButton>
      </header>
    );
  }

  const groundedIn = slugLabel(activeConversation?.pagePath ?? pagePath);

  return (
    <header className="flex items-center gap-2 border-b px-3 py-2.5">
      <span className="grid size-7 place-items-center rounded-lg bg-fd-primary/10 text-fd-primary">
        <Sparkles className="size-4" />
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold leading-tight">BookWiki Chat</p>
        <p className="truncate text-xs text-fd-muted-foreground" title={groundedIn}>
          Grounded in {groundedIn}
        </p>
      </div>
      <IconButton label="Chat history" onClick={() => setView('history')}>
        <History className="size-4" />
      </IconButton>
      <IconButton label="New chat" onClick={startNewChat}>
        <SquarePen className="size-4" />
      </IconButton>
      <IconButton label="Close" onClick={() => setOpen(false)}>
        <X className="size-4" />
      </IconButton>
    </header>
  );
}

// ---------------------------------------------------------------------------
// Conversation history list
// ---------------------------------------------------------------------------

function ConversationListView() {
  const { activeId, conversations, deleteChat, hydrated, openChat, renameChat, startNewChat } =
    useHistory();
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <div className="flex flex-1 flex-col min-h-0">
      <div className="px-3 pt-3">
        <button
          type="button"
          onClick={startNewChat}
          className={cn(
            'flex w-full items-center justify-center gap-2 rounded-xl border border-dashed px-3 py-2.5 text-sm font-medium text-fd-muted-foreground transition-colors',
            'hover:border-fd-primary/40 hover:bg-fd-primary/5 hover:text-fd-primary',
          )}
        >
          <SquarePen className="size-4" />
          New chat
        </button>
      </div>

      {hydrated && conversations.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-sm text-fd-muted-foreground/80">
          <History className="size-6" />
          <p>No saved chats yet. Your conversations will appear here.</p>
        </div>
      ) : (
        <div className="fd-scroll-container mt-2 flex-1 overflow-y-auto px-2 pb-3">
          <ul className="flex flex-col gap-0.5">
            {conversations.map((conversation) => (
              <ConversationRow
                key={conversation.id}
                conversation={conversation}
                active={conversation.id === activeId}
                editing={editingId === conversation.id}
                onOpen={() => openChat(conversation.id)}
                onStartRename={() => setEditingId(conversation.id)}
                onCancelRename={() => setEditingId(null)}
                onRename={(title) => {
                  renameChat(conversation.id, title);
                  setEditingId(null);
                }}
                onDelete={() => deleteChat(conversation.id)}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ConversationRow({
  active,
  conversation,
  editing,
  onCancelRename,
  onDelete,
  onOpen,
  onRename,
  onStartRename,
}: {
  active: boolean;
  conversation: Conversation;
  editing: boolean;
  onCancelRename: () => void;
  onDelete: () => void;
  onOpen: () => void;
  onRename: (title: string) => void;
  onStartRename: () => void;
}) {
  const [draft, setDraft] = useState(conversation.title);

  useEffect(() => {
    if (editing) setDraft(conversation.title);
  }, [editing, conversation.title]);

  if (editing) {
    return (
      <li>
        <form
          className="flex items-center gap-1.5 rounded-lg border border-fd-primary/40 bg-fd-background px-2 py-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            onRename(draft);
          }}
        >
          <input
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') onCancelRename();
            }}
            className="min-w-0 flex-1 bg-transparent text-sm focus-visible:outline-none"
          />
          <button
            type="submit"
            className={cn(buttonVariants({ size: 'icon-sm', color: 'ghost' }), 'rounded-md')}
            aria-label="Save title"
          >
            <Send className="size-3.5" />
          </button>
        </form>
      </li>
    );
  }

  return (
    <li>
      <div
        className={cn(
          'group/row relative flex items-center gap-2 rounded-lg px-2.5 py-2 transition-colors',
          active ? 'bg-fd-primary/10' : 'hover:bg-fd-accent',
        )}
      >
        <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 flex-col text-left">
          <span
            className={cn(
              'truncate text-sm font-medium',
              active ? 'text-fd-primary' : 'text-fd-foreground',
            )}
          >
            {conversation.title}
          </span>
          <span className="truncate text-xs text-fd-muted-foreground">
            {slugLabel(conversation.pagePath)} · {formatRelativeTime(conversation.updatedAt)}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover/row:opacity-100 focus-within:opacity-100">
          <IconButton label="Rename chat" onClick={onStartRename}>
            <Pencil className="size-3.5" />
          </IconButton>
          <IconButton label="Delete chat" onClick={onDelete}>
            <Trash2 className="size-3.5" />
          </IconButton>
        </div>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Message list
// ---------------------------------------------------------------------------

function ChatList({ className, style, ...props }: ComponentProps<'div'>) {
  const { error, messages } = useSession();

  return (
    <List
      className={cn('overscroll-contain py-3', className)}
      style={{
        maskImage:
          'linear-gradient(to bottom, transparent, white 1rem, white calc(100% - 1rem), transparent 100%)',
        ...style,
      }}
      {...props}
    >
      {messages.length === 0 && !error ? (
        <EmptyState />
      ) : (
        <div className="flex flex-col gap-4 px-3">
          {error ? (
            <div className="rounded-xl border border-fd-error/30 bg-fd-error/5 p-3">
              <p className="mb-1 text-xs font-medium text-fd-error">Request failed</p>
              <p className="text-sm text-fd-foreground">{error}</p>
            </div>
          ) : null}
          {messages.map((message) => (
            <Message key={message.id} message={message} />
          ))}
        </div>
      )}
    </List>
  );
}

function EmptyState() {
  return (
    <div className="flex size-full flex-col items-center justify-center gap-3 px-6 text-center">
      <span className="grid size-12 place-items-center rounded-2xl bg-fd-primary/10 text-fd-primary">
        <MessageCircleIcon className="size-6" />
      </span>
      <div className="space-y-1">
        <p className="text-sm font-medium text-fd-foreground">Ask about this book</p>
        <p className="text-xs text-fd-muted-foreground/90">
          Answers are grounded in this book&apos;s SQLite index, with source references.
        </p>
      </div>
    </div>
  );
}

const roleLabel: Record<BookWikiChatMessage['role'], string> = {
  system: 'System',
  user: 'You',
  assistant: 'BookWiki',
};

function Message({ message }: { message: BookWikiChatMessage }) {
  const { status } = useSession();
  const sources = message.metadata?.sources ?? [];

  if (message.role === 'user') {
    return (
      <div className="flex justify-end" onClick={stopClick}>
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-fd-primary px-3.5 py-2.5 text-sm text-fd-primary-foreground shadow-sm">
          <p className="whitespace-pre-wrap break-words">{textContent(message)}</p>
        </div>
      </div>
    );
  }

  const hasVisibleParts = message.parts.some(isVisiblePart);

  return (
    <div className="flex flex-col gap-2" onClick={stopClick}>
      <div className="flex items-center gap-1.5 text-xs font-semibold text-fd-primary">
        <Sparkles className="size-3.5" />
        {roleLabel[message.role]}
      </div>
      <div className="flex flex-col gap-2">
        {message.parts.map((part, index) => (
          <MessagePart key={`${message.id}-${index}`} part={part} />
        ))}
        {!hasVisibleParts && isBusy(status) ? (
          <p className="flex items-center gap-2 text-sm text-fd-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            Searching the book…
          </p>
        ) : null}
      </div>
      {sources.length ? <SourceList sources={sources} /> : null}
    </div>
  );
}

function SourceList({ sources }: { sources: ChatSource[] }) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border bg-fd-secondary/60 p-2.5 text-xs text-fd-muted-foreground">
      <div className="flex items-center gap-1.5">
        <SearchIcon className="size-3.5" />
        <p className="font-medium text-fd-foreground">
          {sources.length} source {sources.length === 1 ? 'reference' : 'references'}
        </p>
      </div>
      <ul className="flex flex-wrap gap-1.5">
        {sources.map((source) => (
          <li
            key={`${source.ref_id}-${source.page ?? ''}-${source.heading ?? ''}`}
            className="rounded-md border bg-fd-background px-1.5 py-0.5"
            title={source.heading ?? source.page ?? undefined}
          >
            <code className="text-[0.7rem]">{source.ref_id}</code>
          </li>
        ))}
      </ul>
    </div>
  );
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
    <details className="rounded-lg border bg-fd-secondary/60 p-2 text-xs text-fd-muted-foreground">
      <summary className="flex cursor-pointer items-center justify-between gap-2">
        <span className="font-medium text-fd-foreground">Tool: {title}</span>
        <code>{state}</code>
      </summary>
      {input ? (
        <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words">Input: {input}</pre>
      ) : null}
      {output ? (
        <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words">Output: {output}</pre>
      ) : null}
      {errorText ? <p className="mt-2 text-fd-error">{errorText}</p> : null}
    </details>
  );
}

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------

function ChatComposer() {
  const { messages, retry, sendMessage, status, stop } = useSession();
  const [input, setInput] = useState('');
  const isLoading = isBusy(status);
  const canRetry = !isLoading && messages.at(-1)?.role === 'assistant';

  const submit = (event?: SyntheticEvent) => {
    event?.preventDefault();
    const message = input.trim();
    if (!message || isLoading) return;

    void sendMessage(message);
    setInput('');
  };

  return (
    <div className="flex flex-col gap-1.5 p-2 lg:p-3">
      {isLoading || canRetry ? (
        <div className="flex items-center justify-center gap-1.5">
          {isLoading ? (
            <button
              type="button"
              onClick={stop}
              className={cn(
                buttonVariants({ color: 'secondary', size: 'sm', className: 'gap-1.5 rounded-full' }),
              )}
            >
              <Square className="size-3.5" />
              Stop
            </button>
          ) : null}
          {canRetry ? (
            <button
              type="button"
              onClick={() => void retry()}
              className={cn(
                buttonVariants({ color: 'secondary', size: 'sm', className: 'gap-1.5 rounded-full' }),
              )}
            >
              <RefreshCw className="size-3.5" />
              Retry
            </button>
          ) : null}
        </div>
      ) : null}

      <form
        onSubmit={submit}
        className="flex items-end gap-2 rounded-2xl border bg-fd-secondary p-1.5 shadow-sm transition-shadow has-focus-visible:shadow-md"
      >
        <Input
          value={input}
          placeholder={isLoading ? 'BookWiki is answering…' : 'Ask this book'}
          autoFocus
          className="px-2.5 py-1.5"
          disabled={isLoading}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (!event.shiftKey && event.key === 'Enter') submit(event);
          }}
        />
        <button
          type="submit"
          aria-label="Send"
          className={cn(
            buttonVariants({
              color: isLoading ? 'secondary' : 'primary',
              size: 'icon',
              className: 'rounded-full transition-colors',
            }),
          )}
          disabled={input.trim().length === 0 || isLoading}
        >
          {isLoading ? (
            <Loader2 className="size-4 animate-spin text-fd-muted-foreground" />
          ) : (
            <Send className="size-4" />
          )}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

function IconButton({
  className,
  label,
  ...props
}: ComponentProps<'button'> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      tabIndex={-1}
      className={cn(
        buttonVariants({
          size: 'icon-sm',
          color: 'ghost',
          className: 'rounded-lg text-fd-muted-foreground hover:text-fd-foreground',
        }),
        className,
      )}
      {...props}
    >
      {props.children}
    </button>
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
          'resize-none bg-transparent text-sm placeholder:text-fd-muted-foreground focus-visible:outline-none',
          shared,
        )}
      />
      <div ref={ref} className={cn(shared, 'break-all invisible')}>
        {`${props.value?.toString() ?? ''}\n`}
      </div>
    </div>
  );
}

export function useHotKey() {
  const { open, setOpen } = useHistory();

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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function stopClick(event: SyntheticEvent) {
  event.stopPropagation();
}

function textContent(message: BookWikiChatMessage) {
  return message.parts
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('')
    .trim();
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

function slugLabel(pagePath: string) {
  const clean = pagePath.split(/[?#]/, 1)[0]?.replace(/\/+$/, '') ?? '';
  if (!clean || clean === '/docs') return 'index';
  return clean.replace(/^\/docs\//, '') || 'index';
}

function formatRelativeTime(timestamp: number) {
  const diff = Date.now() - timestamp;
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return 'just now';

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;

  return new Date(timestamp).toLocaleDateString();
}
