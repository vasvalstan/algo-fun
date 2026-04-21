import { useState } from 'react';
import { apiUrl } from '../lib/apiBase';
import { useBotStore } from '../hooks/useBotState';

interface Props {
  logs: string[];
}

export function DebugPanel({ logs }: Props) {
  const [secret, setSecret] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const addDebugLog = useBotStore((s) => s.addDebugLog);

  const sendTest = async () => {
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
      const data = (await res.json().catch(() => ({}))) as { detail?: string };
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
    <div className="debug-panel">
      <div className="debug-title">Debug</div>
      <div className="debug-log-list">
        {logs.length === 0 ? (
          <div className="debug-log-item">Waiting for connection events…</div>
        ) : (
          logs.map((line, idx) => (
            <div key={`${idx}-${line}`} className="debug-log-item">
              {line}
            </div>
          ))
        )}
      </div>

      <div className="debug-telegram-row">
        <input
          type="password"
          className="telegram-test-secret"
          placeholder="Secret (optional)"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          autoComplete="off"
        />
        <button
          type="button"
          className="telegram-test-btn"
          onClick={sendTest}
          disabled={loading}
        >
          {loading ? 'Sending…' : 'Test Telegram'}
        </button>
      </div>
      {status && <div className="telegram-test-status">{status}</div>}
    </div>
  );
}
