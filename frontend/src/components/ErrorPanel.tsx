interface Props {
  readonly errors: readonly string[];
}

const TIMESTAMP_PREFIX = /^\[(\d{2}:\d{2}:\d{2})\]\s+(.*)$/s;

// Plain-English explanation of well-known error codes the bot can hit so
// the user sees *why* something failed, not just the raw API string.
function explain(raw: string): string | null {
  const text = raw.toLowerCase();

  // Order matters: Binance overloads code -2010 for BOTH the post-only
  // "would immediately match" reject AND the genuine "insufficient
  // balance" reject. We must match on the descriptive message keyword
  // first, otherwise every -2010 lands on the post-only explanation
  // even when the real cause is locked balance.
  if (
    text.includes('would immediately match') ||
    text.includes('post only') ||
    text.includes('post-only') ||
    text.includes('limit_maker')
  ) {
    return (
      'The exchange rejected a post-only (LIMIT_MAKER) order because the ' +
      'orderbook moved between the tick read and the POST, so the price ' +
      'would have crossed the spread and traded as a taker. The bot ' +
      'pauses 5s on the same price, re-anchors to the last traded price, ' +
      'and the next tick computes a fresh target safely below the ask — ' +
      'no capital is at risk; the order simply was not placed.'
    );
  }
  if (
    text.includes('-2019') ||
    text.includes('insufficient balance') ||
    text.includes('insufficient') ||
    (text.includes('-2010') && text.includes('balance'))
  ) {
    return (
      'Exchange refused the order because the funds are already locked ' +
      "— usually by another open order covering the same bag (state-loss " +
      'after a redeploy). The bot first tries to adopt that existing ' +
      "order in-band; if no qty/price match is found the bag enters a " +
      "2-min cooldown and is retried on every tick afterwards until the " +
      'orphan clears or gets adopted. No capital is at risk.'
    );
  }
  if (text.includes('-1021') || text.includes('timestamp')) {
    return (
      'Server clock drift — the request signature window expired. Will ' +
      'auto-retry; usually self-resolves within a minute.'
    );
  }
  if (text.includes('429') || text.includes('rate limit')) {
    return (
      'Rate limited by the exchange. Polling will back off automatically.'
    );
  }
  if (text.includes('403') || text.includes('forbidden') || text.includes('permission')) {
    return (
      'API key permission error. Confirm the key has Spot Trading enabled ' +
      'and that your IP is whitelisted in the exchange dashboard.'
    );
  }
  if (text.includes('min_notional') || text.includes('< min')) {
    return (
      "Order skipped because its USD value falls below the exchange's " +
      'minimum-order threshold. Increase tranche size or wait for a price ' +
      'level where the lot is large enough.'
    );
  }
  if (text.includes('skip sell')) {
    return (
      'A take-profit sell was skipped due to a precision/notional guard. ' +
      'The bag remains held; the bot will retry on the next reconciliation tick.'
    );
  }
  return null;
}

// Pull leading "[HH:MM:SS] " timestamp so we can render it as a side label.
function splitTimestamp(raw: string): { ts: string | null; body: string } {
  const m = TIMESTAMP_PREFIX.exec(raw);
  return m ? { ts: m[1], body: m[2] } : { ts: null, body: raw };
}

export function ErrorPanel({ errors }: Props) {
  if (errors.length === 0) return null;

  // Newest errors first — the most actionable info is at the top of the panel.
  const ordered = [...errors].reverse();

  return (
    <div className="card error-card">
      <div className="card-header" style={{ borderBottomColor: 'rgba(248, 113, 113, 0.1)' }}>
        <span className="card-title" style={{ color: 'var(--red-400)' }}>
          Errors ({errors.length})
        </span>
      </div>
      <div className="error-list">
        {ordered.map((raw, i) => {
          const { ts, body } = splitTimestamp(raw);
          const note = explain(body);
          return (
            <div key={`${i}-${raw.slice(0, 16)}`} className="error-row">
              <div className="error-row-header">
                {ts && <span className="error-ts">{ts}</span>}
                <span className="error-msg">{body}</span>
              </div>
              {note && <div className="error-explain">↳ {note}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
