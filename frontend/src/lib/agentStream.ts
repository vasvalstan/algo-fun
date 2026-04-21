import { apiUrl } from './apiBase';
import { getAgentToken, getOrCreateSessionId } from './agentChat';

export type ChatMessage = {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
};

export type StreamEvent =
  | { type: 'delta'; content: string }
  | { type: 'tool_call_delta'; index: number; name?: string; argumentsDelta?: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

type StreamHandlers = {
  onEvent: (e: StreamEvent) => void;
  signal?: AbortSignal;
};

type OpenAIDelta = {
  content?: string;
  tool_calls?: Array<{
    index?: number;
    function?: { name?: string; arguments?: string };
  }>;
};

type OpenAIChoice = {
  delta?: OpenAIDelta;
  finish_reason?: string | null;
};

type OpenAIChunk = {
  choices?: OpenAIChoice[];
  error?: unknown;
};

/**
 * Parse one OpenAI-style SSE chunk into our normalized StreamEvent list.
 * Returns an empty list for chunks that carry no user-visible content
 * (e.g. role-only deltas).
 */
function parseChunk(json: OpenAIChunk): StreamEvent[] {
  const out: StreamEvent[] = [];
  const choice = json.choices?.[0];
  if (!choice) return out;
  const delta = choice.delta ?? {};
  if (typeof delta.content === 'string' && delta.content.length > 0) {
    out.push({ type: 'delta', content: delta.content });
  }
  if (Array.isArray(delta.tool_calls)) {
    for (const tc of delta.tool_calls) {
      out.push({
        type: 'tool_call_delta',
        index: typeof tc.index === 'number' ? tc.index : 0,
        name: tc.function?.name,
        argumentsDelta: tc.function?.arguments,
      });
    }
  }
  if (choice.finish_reason) {
    out.push({ type: 'done' });
  }
  return out;
}

/**
 * POST a chat completion request and stream events back via onEvent.
 * Throws Error('UNAUTHORIZED') on 401 so callers can clear the saved token.
 */
export async function streamChat(
  messages: ChatMessage[],
  handlers: StreamHandlers,
): Promise<void> {
  const token = getAgentToken();
  if (!token) throw new Error('No agent token set.');

  const body = {
    model: 'hermes',
    stream: true,
    messages,
    session_id: getOrCreateSessionId(),
  };

  const resp = await fetch(apiUrl('/api/agent/chat/completions'), {
    method: 'POST',
    signal: handlers.signal,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
  });

  if (resp.status === 401) {
    throw new Error('UNAUTHORIZED');
  }
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '');
    throw new Error(`Upstream error ${resp.status}: ${text || 'no body'}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  // SSE frames are separated by a blank line (\n\n). Each frame is one
  // or more "field: value" lines; we only consume `data:` lines (OpenAI
  // doesn't use named events here).
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep = buffer.indexOf('\n\n');
    while (sep !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      const data = dataLines.join('\n').trim();
      if (data) {
        if (data === '[DONE]') {
          handlers.onEvent({ type: 'done' });
          return;
        }
        try {
          const parsed = JSON.parse(data) as OpenAIChunk;
          if (parsed.error) {
            handlers.onEvent({ type: 'error', message: JSON.stringify(parsed.error) });
          } else {
            for (const ev of parseChunk(parsed)) handlers.onEvent(ev);
          }
        } catch {
          // Tolerate occasional partial frames — they'll be retried by
          // the next read tick.
        }
      }
      sep = buffer.indexOf('\n\n');
    }
  }
  handlers.onEvent({ type: 'done' });
}
