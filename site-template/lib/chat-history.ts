'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { UIMessage } from 'ai';

const StorageKey = 'bookwiki:chat:v1';
const MaxConversations = 100;
const TitleMaxLength = 48;

export type StoredConversation<M extends UIMessage = UIMessage> = {
  id: string;
  title: string;
  /** The article path the conversation is grounded in (captured at creation). */
  pagePath: string;
  messages: M[];
  createdAt: number;
  updatedAt: number;
};

type ChatStore<M extends UIMessage = UIMessage> = {
  conversations: StoredConversation<M>[];
  activeId: string;
};

export type ChatHistory<M extends UIMessage = UIMessage> = {
  hydrated: boolean;
  conversations: StoredConversation<M>[];
  activeId: string;
  activeConversation: StoredConversation<M> | null;
  newChat: () => string;
  selectChat: (id: string) => void;
  deleteChat: (id: string) => void;
  renameChat: (id: string, title: string) => void;
  saveMessages: (id: string, pagePath: string, messages: M[]) => void;
};

export function useChatHistory<M extends UIMessage = UIMessage>(): ChatHistory<M> {
  const [store, setStore] = useState<ChatStore<M>>(() => ({
    conversations: [],
    activeId: newId(),
  }));
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const loaded = loadStore<M>();
    setStore({
      conversations: loaded.conversations,
      activeId: loaded.activeId || newId(),
    });
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    saveStore(store);
  }, [store, hydrated]);

  const newChat = useCallback(() => {
    const id = newId();
    setStore((prev) => ({ ...prev, activeId: id }));
    return id;
  }, []);

  const selectChat = useCallback((id: string) => {
    setStore((prev) => (prev.activeId === id ? prev : { ...prev, activeId: id }));
  }, []);

  const deleteChat = useCallback((id: string) => {
    setStore((prev) => {
      const conversations = prev.conversations.filter((conversation) => conversation.id !== id);
      const activeId = prev.activeId === id ? (conversations[0]?.id ?? newId()) : prev.activeId;
      return { conversations, activeId };
    });
  }, []);

  const renameChat = useCallback((id: string, title: string) => {
    const clean = title.trim().slice(0, TitleMaxLength) || 'Untitled';
    setStore((prev) => ({
      ...prev,
      conversations: prev.conversations.map((conversation) =>
        conversation.id === id ? { ...conversation, title: clean } : conversation,
      ),
    }));
  }, []);

  const saveMessages = useCallback((id: string, pagePath: string, messages: M[]) => {
    setStore((prev) => {
      const now = Date.now();
      const existing = prev.conversations.find((conversation) => conversation.id === id);
      const rest = prev.conversations.filter((conversation) => conversation.id !== id);

      const conversation: StoredConversation<M> = existing
        ? { ...existing, messages, updatedAt: now }
        : {
            id,
            title: deriveTitle(messages),
            pagePath,
            messages,
            createdAt: now,
            updatedAt: now,
          };

      return {
        ...prev,
        conversations: [conversation, ...rest].slice(0, MaxConversations),
      };
    });
  }, []);

  const activeConversation = useMemo(
    () => store.conversations.find((conversation) => conversation.id === store.activeId) ?? null,
    [store.conversations, store.activeId],
  );

  return {
    hydrated,
    conversations: store.conversations,
    activeId: store.activeId,
    activeConversation,
    newChat,
    selectChat,
    deleteChat,
    renameChat,
    saveMessages,
  };
}

export function conversationSignature(messages: UIMessage[]): string {
  const last = messages.at(-1);
  return `${messages.length}:${last?.id ?? ''}:${textOf(last)?.length ?? 0}`;
}

export function deriveTitle(messages: UIMessage[]): string {
  for (const message of messages) {
    if (message.role !== 'user') continue;
    const text = textOf(message);
    if (!text) continue;
    return text.length > TitleMaxLength ? `${text.slice(0, TitleMaxLength)}…` : text;
  }
  return 'New chat';
}

function textOf(message: UIMessage | undefined): string {
  if (!message) return '';
  return message.parts
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('')
    .trim();
}

function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function loadStore<M extends UIMessage = UIMessage>(): ChatStore<M> {
  if (typeof window === 'undefined') return { conversations: [], activeId: '' };

  try {
    const raw = window.localStorage.getItem(StorageKey);
    if (!raw) return { conversations: [], activeId: '' };

    const parsed = JSON.parse(raw) as Partial<ChatStore<M>>;
    const conversations = Array.isArray(parsed.conversations)
      ? parsed.conversations.filter(isStoredConversation)
      : [];
    const activeId = typeof parsed.activeId === 'string' ? parsed.activeId : '';

    return { conversations: conversations as StoredConversation<M>[], activeId };
  } catch {
    return { conversations: [], activeId: '' };
  }
}

function saveStore<M extends UIMessage = UIMessage>(store: ChatStore<M>): void {
  if (typeof window === 'undefined') return;

  try {
    window.localStorage.setItem(StorageKey, JSON.stringify(store));
  } catch {
    // Ignore quota or serialization errors; chat history is best-effort.
  }
}

function isStoredConversation(value: unknown): value is StoredConversation {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<StoredConversation>;
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.title === 'string' &&
    Array.isArray(candidate.messages)
  );
}
