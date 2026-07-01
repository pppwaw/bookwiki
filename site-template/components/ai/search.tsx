'use client';

import {
  type ClipboardEvent,
  type ComponentProps,
  createContext,
  type DragEvent,
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
import { DefaultChatTransport, type ChatStatus, type FileUIPart, type UIMessage } from 'ai';
import {
  ArrowLeft,
  History,
  ImagePlus,
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
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Presence } from '@radix-ui/react-presence';
import { Markdown } from '../markdown';
import { buttonVariants } from 'fumadocs-ui/components/ui/button';
import { cn } from '../../lib/cn';
import { decodeSlug } from '../../lib/slug';
import {
  conversationSignature,
  type StoredConversation,
  useChatHistory,
} from '../../lib/chat-history';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
  url?: string;
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
// Image attachments (vision input)
// ---------------------------------------------------------------------------

const AcceptedImageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const AcceptedImageAttr = 'image/png,image/jpeg,image/webp,image/gif';
const MaxImageBytes = 10 * 1024 * 1024;
const MaxImageBytesLabel = '10MB';
const MaxImages = 4;

type Attachment = { id: string; file: File; url: string };

function newAttachmentId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `a_${Date.now().toString(36)}`;
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error ?? new Error('failed to read image'));
    reader.readAsDataURL(file);
  });
}

function imageFilesFromDataTransfer(data: DataTransfer | null): File[] {
  if (!data) return [];
  return Array.from(data.items)
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null);
}

function isImagePart(part: BookWikiChatPart): part is FileUIPart {
  return (
    part.type === 'file' &&
    typeof (part as FileUIPart).url === 'string' &&
    ((part as FileUIPart).mediaType ?? '').startsWith('image/')
  );
}

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
  /** Whether image (vision) input is enabled for the configured model. */
  visionEnabled: boolean;
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

export function AISearch({
  children,
  visionEnabled = false,
}: {
  children: ReactNode;
  visionEnabled?: boolean;
}) {
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
      visionEnabled,
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
      visionEnabled,
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
  sendMessage: (question: string, files?: FileUIPart[]) => Promise<void>;
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

  async function sendMessage(question: string, files?: FileUIPart[]) {
    const trimmed = question.trim();
    const hasFiles = !!files && files.length > 0;
    if ((!trimmed && !hasFiles) || isBusy(status)) return;

    setLastQuestion(trimmed);
    clearError();
    await sendChatMessage({ text: trimmed, files });
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
    const images = message.parts.filter(isImagePart);
    const text = textContent(message);

    return (
      <div className="flex justify-end" onClick={stopClick}>
        <div className="flex max-w-[85%] flex-col gap-2 rounded-2xl rounded-br-md bg-fd-primary px-3.5 py-2.5 text-sm text-fd-primary-foreground shadow-sm">
          {images.length ? (
            <div className="flex flex-wrap justify-end gap-1.5">
              {images.map((part, index) => (
                <a
                  key={`${message.id}-img-${index}`}
                  href={part.url}
                  target="_blank"
                  rel="noreferrer"
                  className="block"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={part.url}
                    alt={part.filename ?? 'attached image'}
                    className="max-h-44 rounded-lg object-contain"
                  />
                </a>
              ))}
            </div>
          ) : null}
          {text ? <p className="whitespace-pre-wrap break-words">{text}</p> : null}
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
            {source.url ? (
              <Link href={source.url} className="text-[0.7rem] text-fd-primary hover:underline">
                {source.heading ?? source.ref_id}
              </Link>
            ) : (
              <code className="text-[0.7rem]">{source.ref_id}</code>
            )}
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
  const { visionEnabled } = useHistory();
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [notice, setNotice] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);

  const isLoading = isBusy(status);
  const canRetry = !isLoading && messages.at(-1)?.role === 'assistant';
  const canSend = (input.trim().length > 0 || attachments.length > 0) && !isLoading;

  // Revoke preview object URLs on unmount so blob URLs don't leak.
  const attachmentsRef = useRef(attachments);
  attachmentsRef.current = attachments;
  useEffect(
    () => () => {
      for (const attachment of attachmentsRef.current) URL.revokeObjectURL(attachment.url);
    },
    [],
  );

  function addFiles(incoming: FileList | File[] | null | undefined) {
    if (!visionEnabled || !incoming) return;
    const accepted: Attachment[] = [];
    let rejection = '';

    for (const file of Array.from(incoming)) {
      if (!AcceptedImageTypes.has(file.type)) {
        rejection = '仅支持 PNG / JPEG / WebP / GIF 图片';
        continue;
      }
      if (file.size > MaxImageBytes) {
        rejection = `单张图片需小于 ${MaxImageBytesLabel}`;
        continue;
      }
      accepted.push({ id: newAttachmentId(), file, url: URL.createObjectURL(file) });
    }

    setAttachments((prev) => {
      const room = Math.max(0, MaxImages - prev.length);
      if (accepted.length > room) {
        rejection = `最多添加 ${MaxImages} 张图片`;
        for (const extra of accepted.slice(room)) URL.revokeObjectURL(extra.url);
      }
      return [...prev, ...accepted.slice(0, room)];
    });

    setNotice(rejection);
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => {
      const target = prev.find((attachment) => attachment.id === id);
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((attachment) => attachment.id !== id);
    });
  }

  const submit = async (event?: SyntheticEvent) => {
    event?.preventDefault();
    if (!canSend) return;

    const text = input.trim();
    const current = attachments;
    const files: FileUIPart[] | undefined = current.length
      ? await Promise.all(
          current.map(async (attachment) => ({
            type: 'file' as const,
            mediaType: attachment.file.type,
            filename: attachment.file.name,
            url: await fileToDataUrl(attachment.file),
          })),
        )
      : undefined;

    void sendMessage(text, files);

    for (const attachment of current) URL.revokeObjectURL(attachment.url);
    setAttachments([]);
    setInput('');
    setNotice('');
  };

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (!visionEnabled) return;
    const files = imageFilesFromDataTransfer(event.clipboardData);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  }

  function onDragEnter(event: SyntheticEvent) {
    if (!visionEnabled) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }

  function onDragOver(event: SyntheticEvent) {
    if (!visionEnabled) return;
    event.preventDefault();
  }

  function onDragLeave(event: SyntheticEvent) {
    if (!visionEnabled) return;
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    if (!visionEnabled) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    addFiles(imageFilesFromDataTransfer(event.dataTransfer));
  }

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

      {attachments.length ? (
        <div className="flex flex-col gap-1 px-1">
          <div className="flex flex-wrap gap-2">
            {attachments.map((attachment) => (
              <div key={attachment.id} className="relative">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={attachment.url}
                  alt={attachment.file.name}
                  className="size-14 rounded-lg border object-cover"
                />
                <button
                  type="button"
                  aria-label="Remove image"
                  onClick={() => removeAttachment(attachment.id)}
                  className="absolute -end-1.5 -top-1.5 grid size-5 place-items-center rounded-full border bg-fd-background text-fd-muted-foreground shadow-sm hover:text-fd-foreground"
                >
                  <X className="size-3" />
                </button>
              </div>
            ))}
          </div>
          <p className="text-[0.7rem] text-fd-muted-foreground">
            图片仅本次会话有效,刷新或重开对话后会丢失
          </p>
        </div>
      ) : null}

      {notice ? <p className="px-1 text-[0.7rem] text-fd-error">{notice}</p> : null}

      <form
        onSubmit={submit}
        onDragEnter={visionEnabled ? onDragEnter : undefined}
        onDragOver={visionEnabled ? onDragOver : undefined}
        onDragLeave={visionEnabled ? onDragLeave : undefined}
        onDrop={visionEnabled ? onDrop : undefined}
        className={cn(
          'relative flex items-center gap-2 rounded-2xl border bg-fd-secondary p-1.5 shadow-sm transition-shadow has-focus-visible:shadow-md',
          dragging && 'ring-2 ring-fd-primary ring-offset-1',
        )}
      >
        {visionEnabled ? (
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept={AcceptedImageAttr}
              multiple
              className="hidden"
              onChange={(event) => {
                addFiles(event.target.files);
                event.target.value = '';
              }}
            />
            <button
              type="button"
              aria-label="Add image"
              title="添加图片"
              onClick={() => fileInputRef.current?.click()}
              disabled={isLoading || attachments.length >= MaxImages}
              className={cn(
                buttonVariants({
                  color: 'ghost',
                  size: 'icon',
                  className:
                    'shrink-0 rounded-full text-fd-muted-foreground hover:text-fd-foreground',
                }),
              )}
            >
              <ImagePlus className="size-4" />
            </button>
          </>
        ) : null}

        <Input
          value={input}
          placeholder={isLoading ? 'BookWiki is answering…' : 'Ask this book'}
          autoFocus
          className="px-2.5 py-1.5"
          disabled={isLoading}
          onChange={(event) => setInput(event.target.value)}
          onPaste={visionEnabled ? onPaste : undefined}
          onKeyDown={(event) => {
            if (!event.shiftKey && event.key === 'Enter') void submit(event);
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
          disabled={!canSend}
        >
          {isLoading ? (
            <Loader2 className="size-4 animate-spin text-fd-muted-foreground" />
          ) : (
            <Send className="size-4" />
          )}
        </button>

        {dragging ? (
          <div className="pointer-events-none absolute inset-0 grid place-items-center rounded-2xl bg-fd-primary/10 text-xs font-medium text-fd-primary">
            松开以添加图片
          </div>
        ) : null}
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
  const slug = clean.replace(/^\/docs\//, '') || 'index';
  // `usePathname()` hands back percent-encoded segments; decode so Chinese
  // slugs render as text instead of `%E6%84…`.
  return decodeSlug(slug);
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
