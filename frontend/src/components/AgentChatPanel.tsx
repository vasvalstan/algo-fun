import { useEffect, useRef, useState } from 'react';
import {
  getAgentToken,
  setAgentToken,
  resetSession,
  getOrCreateSessionId,
} from '../lib/agentChat';
import { streamChat, type ChatMessage } from '../lib/agentStream';

type DisplayMessage = ChatMessage & { id: string; toolCalls?: ToolCallView[] };

type ToolCallView = {
  index: number;
  name: string;
  argumentsText: string;
};

const SYSTEM_HINT =
  'You are the algo-fun trading assistant (Hermes). Use the algo-fun-trading MCP tools to inspect bot state and propose trades. Always summarise risk before acting.';

export function AgentChatPanel() {
  const [tokenInput, setTokenInput] = useState('');
  const [hasToken, setHasToken] = useState(false);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = getAgentToken();
    if (t) {
      setHasToken(true);
      setTokenInput(t);
    }
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  function saveToken() {
    setAgentToken(tokenInput.trim());
    setHasToken(Boolean(tokenInput.trim()));
    setError(null);
  }

  function clearToken() {
    setAgentToken('');
    setHasToken(false);
    setMessages([]);
    setTokenInput('');
  }

  function newSession() {
    abortRef.current?.abort();
    resetSession();
    setMessages([]);
    setError(null);
  }

  async function send() {
    const text = input.trim();
    if (!text || streaming) return;
    setError(null);

    const userMsg: DisplayMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
    };
    const assistantMsg: DisplayMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '',
      toolCalls: [],
    };
    const nextMessages = [...messages, userMsg, assistantMsg];
    setMessages(nextMessages);
    setInput('');
    setStreaming(true);

    const wirePayload: ChatMessage[] = [
      { role: 'system', content: SYSTEM_HINT },
      ...nextMessages
        .filter((m) => m.id !== assistantMsg.id)
        .map<ChatMessage>(({ role, content }) => ({ role, content })),
    ];

    abortRef.current = new AbortController();

    try {
      await streamChat(wirePayload, {
        signal: abortRef.current.signal,
        onEvent: (e) => {
          if (e.type === 'delta') {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id ? { ...m, content: m.content + e.content } : m,
              ),
            );
          } else if (e.type === 'tool_call_delta') {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantMsg.id) return m;
                const calls = [...(m.toolCalls ?? [])];
                const existing = calls.find((c) => c.index === e.index);
                if (existing) {
                  if (e.name) existing.name = e.name;
                  if (e.argumentsDelta) existing.argumentsText += e.argumentsDelta;
                } else {
                  calls.push({
                    index: e.index,
                    name: e.name ?? '(tool)',
                    argumentsText: e.argumentsDelta ?? '',
                  });
                }
                return { ...m, toolCalls: calls };
              }),
            );
          } else if (e.type === 'error') {
            setError(e.message);
          }
        },
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg === 'UNAUTHORIZED') {
        setError('Token rejected. Update it below.');
        clearToken();
      } else if (!/aborted/i.test(msg)) {
        setError(msg);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  if (!hasToken) {
    return (
      <div style={panelStyle}>
        <h2 style={headingStyle}>Hermes Agent</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: 14, marginTop: 0 }}>
          Paste the AGENT_CHAT_TOKEN configured on the backend &amp; agent service. Stored locally
          in this browser only.
        </p>
        <input
          type="password"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          placeholder="AGENT_CHAT_TOKEN"
          style={inputStyle}
          autoComplete="off"
        />
        <button type="button" onClick={saveToken} style={primaryBtn} disabled={!tokenInput.trim()}>
          Unlock chat
        </button>
      </div>
    );
  }

  return (
    <div style={panelStyle}>
      <header style={headerRow}>
        <h2 style={headingStyle}>Hermes Agent</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" onClick={newSession} style={ghostBtn} title="Forget conversation history on the agent">
            New session
          </button>
          <button type="button" onClick={clearToken} style={ghostBtn}>
            Sign out
          </button>
        </div>
      </header>
      <p style={{ color: 'var(--text-dim)', fontSize: 12, margin: '0 0 12px' }}>
        Session id: <code>{getOrCreateSessionId().slice(0, 8)}…</code>
      </p>

      <div ref={scrollRef} style={scrollStyle}>
        {messages.length === 0 && (
          <div style={{ color: 'var(--text-dim)', fontSize: 14 }}>
            Ask Hermes about the bot, e.g. <em>“What's the current BTC market state?”</em>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} msg={m} />
        ))}
      </div>

      {error && (
        <div style={errorBoxStyle} role="alert">
          {error}
        </div>
      )}

      <div style={composerRow}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Message Hermes (Shift+Enter = newline)…"
          style={textareaStyle}
          rows={2}
          disabled={streaming}
        />
        {streaming ? (
          <button type="button" onClick={stop} style={primaryBtn}>
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void send()}
            style={primaryBtn}
            disabled={!input.trim()}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}

function MessageBubble({ msg }: { readonly msg: DisplayMessage }) {
  const isUser = msg.role === 'user';
  return (
    <div
      style={{
        alignSelf: isUser ? 'flex-end' : 'flex-start',
        maxWidth: '85%',
        background: isUser ? 'rgba(167, 139, 250, 0.18)' : 'var(--bg-secondary)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 10,
        padding: '10px 12px',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontSize: 14,
        lineHeight: 1.45,
      }}
    >
      {msg.content || (msg.role === 'assistant' && <span style={{ opacity: 0.5 }}>…</span>)}
      {msg.toolCalls && msg.toolCalls.length > 0 && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {msg.toolCalls.map((tc) => (
            <div key={tc.index} style={toolCallStyle}>
              <strong style={{ color: 'var(--accent, #a78bfa)' }}>{tc.name}</strong>
              {tc.argumentsText && (
                <pre style={{ margin: '4px 0 0', fontSize: 11, opacity: 0.85 }}>
                  {tc.argumentsText}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const panelStyle = {
  background: 'var(--bg-primary, #11131a)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 14,
  padding: 18,
  display: 'flex',
  flexDirection: 'column' as const,
  gap: 8,
  height: '100%',
  minHeight: 480,
};

const headerRow = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 4,
};

const headingStyle = {
  margin: 0,
  fontSize: 18,
  color: 'var(--text-primary)',
};

const scrollStyle = {
  flex: 1,
  overflowY: 'auto' as const,
  display: 'flex',
  flexDirection: 'column' as const,
  gap: 10,
  padding: 8,
  background: 'var(--bg-secondary)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 10,
  minHeight: 240,
};

const composerRow = {
  display: 'flex',
  gap: 8,
  alignItems: 'flex-end',
};

const textareaStyle = {
  flex: 1,
  resize: 'vertical' as const,
  background: 'var(--bg-secondary)',
  color: 'var(--text-primary)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: '8px 10px',
  fontSize: 14,
  fontFamily: 'inherit',
  minHeight: 44,
};

const inputStyle = {
  ...textareaStyle,
  minHeight: 'unset',
  resize: 'none' as const,
  width: '100%',
  marginBottom: 12,
};

const baseBtn = {
  border: '1px solid var(--border-subtle)',
  borderRadius: 8,
  padding: '8px 14px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  transition: 'background 0.15s, opacity 0.15s',
} as const;

const primaryBtn = {
  ...baseBtn,
  background: 'rgba(167, 139, 250, 0.25)',
  color: 'var(--text-primary)',
};

const ghostBtn = {
  ...baseBtn,
  background: 'transparent',
  color: 'var(--text-dim)',
  padding: '6px 10px',
  fontSize: 12,
};

const toolCallStyle = {
  background: 'rgba(255,255,255,0.03)',
  border: '1px dashed var(--border-subtle)',
  borderRadius: 6,
  padding: '6px 8px',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 12,
};

const errorBoxStyle = {
  background: 'rgba(239, 68, 68, 0.12)',
  border: '1px solid rgba(239, 68, 68, 0.4)',
  color: '#fecaca',
  borderRadius: 8,
  padding: '8px 10px',
  fontSize: 13,
};
