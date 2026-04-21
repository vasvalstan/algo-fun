/**
 * Token + session id helpers for the agent chat UI.
 *
 * Token  : shared secret entered once on the chat page, persisted in
 *          localStorage. The browser sends it as
 *          `Authorization: Bearer <token>` on each request to
 *          /api/agent/chat/completions.
 * Session: stable opaque id per browser tab so Hermes can keep
 *          conversation continuity across messages
 *          (X-Hermes-Session-Id upstream).
 */

const TOKEN_KEY = 'agentChatToken';
const SESSION_KEY = 'agentSessionId';

export function getAgentToken(): string {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setAgentToken(token: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export function getOrCreateSessionId(): string {
  let sid = localStorage.getItem(SESSION_KEY);
  if (!sid) {
    sid = crypto.randomUUID?.() ?? Math.random().toString(36).slice(2);
    localStorage.setItem(SESSION_KEY, sid);
  }
  return sid;
}

export function resetSession(): void {
  localStorage.removeItem(SESSION_KEY);
}
