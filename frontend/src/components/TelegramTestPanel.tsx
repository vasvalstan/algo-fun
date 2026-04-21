import { useState } from 'react';
import { apiUrl } from '../lib/apiBase';
import { useBotStore } from '../hooks/useBotState';

export function TelegramTestPanel() {
  const [secret, setSecret] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const addDebugLog = useBotStore((s) => s.addDebugLog);

  const send = async () => {
    setLoading(true);
    setStatus(null);
    const stamp = () => new Date().toLocaleTimeString();
    addDebugLog(`[${stamp()}] POST /api/test-telegram …`);
    try {
      const res = await fetch(apiUrl('/api/test-telegram'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(secret.trim() ? { secret: secret.trim() } : {}),
      });
      const data = (await res.json().catch(() => ({}))) as { detail?: string; ok?: boolean };
      if (!res.ok) {
        const msg = typeof data.detail === 'string' ? data.detail : res.statusText;
        setStatus(`Error ${res.status}: ${msg}`);
        addDebugLog(`[${stamp()}] test-telegram failed: ${res.status} ${msg}`);
        return;
      }
      setStatus('Sent — check Telegram.');
      addDebugLog(`[${stamp()}] test-telegram ok`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'request failed';
      setStatus(msg);
      addDebugLog(`[${stamp()}] test-telegram error: ${msg}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="debug-panel telegram-test-panel">
      <div className="debug-title">Telegram test</div>
      <input
        type="password"
        className="telegram-test-secret"
        placeholder="Test secret (only if TELEGRAM_TEST_SECRET is set)"
        value={secret}
        onChange={(e) => setSecret(e.target.value)}
        autoComplete="off"
      />
      <button
        type="button"
        className="telegram-test-btn"
        onClick={send}
        disabled={loading}
      >
        {loading ? 'Sending…' : 'Send test message'}
      </button>
      {status && <div className="telegram-test-status">{status}</div>}
    </div>
  );
}
