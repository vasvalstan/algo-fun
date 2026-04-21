import { useEffect, useMemo, useState } from 'react';
import { apiUrl } from '../lib/apiBase';
import type { V2BotState } from '../lib/types';

interface LayerOpt {
  id: string;
  label: string;
}

interface StrategyRow {
  id: string;
  name: string;
  short: string;
  layer_options?: LayerOpt[];
}

interface Props {
  readonly v2: V2BotState;
}

const STRATEGY_SECRET_STORAGE_KEY = 'algo_fun_strategy_api_secret';

export function StrategyConfigChat({ v2 }: Props) {
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState('bitcoin_sandbox');
  const [selectedLayers, setSelectedLayers] = useState<string[]>([]);
  const [message, setMessage] = useState('');
  const [secret, setSecret] = useState(() => {
    try {
      return sessionStorage.getItem(STRATEGY_SECRET_STORAGE_KEY) ?? '';
    } catch {
      return '';
    }
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastReply, setLastReply] = useState<{
    summary: string;
    applied: boolean;
    effective?: Record<string, unknown>;
    param_patch?: Record<string, unknown>;
  } | null>(null);

  useEffect(() => {
    fetch(apiUrl('/api/strategies'))
      .then((r) => r.json())
      .then((d: { strategies?: StrategyRow[] }) => {
        const list = d.strategies ?? [];
        setStrategies(list);
        if (list.length) {
          setStrategyId((prev) => (list.some((s) => s.id === prev) ? prev : list[0].id));
        }
      })
      .catch(() => setStrategies([]));
  }, []);

  const layerOptions = useMemo(
    () => strategies.find((s) => s.id === strategyId)?.layer_options ?? [],
    [strategies, strategyId],
  );

  const liveParams = v2.strategy_params?.[strategyId];

  function toggleLayer(id: string) {
    setSelectedLayers((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  async function submitChat() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(apiUrl('/api/strategy-config/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_id: strategyId,
          message: message.trim(),
          selected_layers: selectedLayers,
          secret: secret.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        throw new Error(detail || res.statusText);
      }
      setLastReply({
        summary: data.summary,
        applied: Boolean(data.applied),
        effective: data.effective,
        param_patch: data.param_patch,
      });
      if (secret.trim()) {
        try {
          sessionStorage.setItem(STRATEGY_SECRET_STORAGE_KEY, secret.trim());
        } catch {
          /* ignore */
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function resetParams() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(apiUrl(`/api/strategy-config/${encodeURIComponent(strategyId)}/reset`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret: secret.trim() || undefined }),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        throw new Error(detail || res.statusText);
      }
      setLastReply({
        summary: 'Parameters reset to defaults.',
        applied: true,
        effective: data.effective,
      });
      if (secret.trim()) {
        try {
          sessionStorage.setItem(STRATEGY_SECRET_STORAGE_KEY, secret.trim());
        } catch {
          /* ignore */
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="card-header">
        <span className="card-title">Strategy config (chat)</span>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>V2 paper · Gemini or OpenAI</span>
      </div>

      <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 12 }}>
        Describe what to tune (e.g. &ldquo;widen Keltner to 2.0× ATR&rdquo;). Optionally focus on specific
        layers. Changes apply on the next paper tick. If your Railway/backend has{' '}
        <code style={{ fontSize: '0.75rem' }}>STRATEGY_CHAT_SECRET</code> or{' '}
        <code style={{ fontSize: '0.75rem' }}>TRADE_API_SECRET</code> set, you must paste the{' '}
        <strong>same value</strong> into the API secret field below (same as trade approval, if you use one
        secret for both).
      </p>

      <div style={{ display: 'grid', gap: 10 }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>Strategy</span>
          <select
            className="mono"
            value={strategyId}
            onChange={(e) => {
              setStrategyId(e.target.value);
              setSelectedLayers([]);
              setLastReply(null);
            }}
            style={{
              padding: '8px 10px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(0,0,0,0.25)',
              color: 'var(--text-primary)',
            }}
          >
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.short} — {s.name}
              </option>
            ))}
            {strategies.length === 0 && <option value="bitcoin_sandbox">Sandbox — BTC grid</option>}
          </select>
        </label>

        {layerOptions.length > 0 && (
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginBottom: 6 }}>
              Focus layers (optional)
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {layerOptions.map((lo) => (
                <label
                  key={lo.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    fontSize: '0.8rem',
                    cursor: 'pointer',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selectedLayers.includes(lo.id)}
                    onChange={() => toggleLayer(lo.id)}
                  />
                  {lo.label}
                </label>
              ))}
            </div>
          </div>
        )}

        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>Instructions</span>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="e.g. Make mean reversion less strict: RSI oversold 32, allow BULL_RUN regime"
            rows={4}
            style={{
              padding: '10px 12px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(0,0,0,0.2)',
              color: 'var(--text-primary)',
              fontFamily: 'inherit',
              fontSize: '0.85rem',
            }}
          />
        </label>

        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>
            API secret (required when server has STRATEGY_CHAT_SECRET or TRADE_API_SECRET)
          </span>
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            autoComplete="off"
            style={{
              padding: '8px 10px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(0,0,0,0.25)',
              color: 'var(--text-primary)',
            }}
          />
        </label>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          <button
            type="button"
            disabled={loading}
            onClick={submitChat}
            style={{
              padding: '8px 16px',
              borderRadius: 'var(--radius-sm)',
              border: 'none',
              background: 'linear-gradient(135deg, #34d399, #22d3ee)',
              color: '#0f172a',
              fontWeight: 600,
              fontSize: '0.85rem',
              cursor: loading ? 'wait' : 'pointer',
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? 'Sending…' : 'Apply via chat'}
          </button>
          <button
            type="button"
            disabled={loading}
            onClick={resetParams}
            style={{
              padding: '8px 16px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid rgba(255,255,255,0.15)',
              background: 'transparent',
              color: 'var(--text-secondary)',
              fontSize: '0.85rem',
              cursor: loading ? 'wait' : 'pointer',
            }}
          >
            Reset to defaults
          </button>
        </div>

        {error && (
          <div
            style={{
              padding: '10px 12px',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(239,68,68,0.12)',
              color: '#fca5a5',
              fontSize: '0.85rem',
            }}
          >
            {error}
          </div>
        )}

        {lastReply && (
          <div
            style={{
              padding: '12px',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(52,211,153,0.08)',
              border: '1px solid rgba(52,211,153,0.2)',
            }}
          >
            <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 6 }}>
              {lastReply.applied ? 'Applied' : 'No change'}
            </div>
            <div style={{ fontSize: '0.9rem' }}>{lastReply.summary}</div>
            {lastReply.param_patch && Object.keys(lastReply.param_patch).length > 0 && (
              <pre
                className="mono"
                style={{
                  marginTop: 10,
                  fontSize: '0.7rem',
                  overflow: 'auto',
                  maxHeight: 120,
                  opacity: 0.9,
                }}
              >
                {JSON.stringify(lastReply.param_patch, null, 2)}
              </pre>
            )}
          </div>
        )}

        <div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginBottom: 6 }}>
            Live effective params (WebSocket)
          </div>
          <pre
            className="mono"
            style={{
              fontSize: '0.68rem',
              padding: 10,
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(0,0,0,0.35)',
              overflow: 'auto',
              maxHeight: 200,
              margin: 0,
            }}
          >
            {liveParams
              ? JSON.stringify(liveParams, null, 2)
              : '— waiting for strategy_params in stream —'}
          </pre>
        </div>
      </div>
    </div>
  );
}
