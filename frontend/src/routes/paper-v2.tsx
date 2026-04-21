import { createFileRoute, useNavigate } from '@tanstack/react-router';
import { useEffect } from 'react';
import { useBotStore } from '../hooks/useBotState';

export const Route = createFileRoute('/paper-v2')({
  component: PaperV2Redirect,
});

/** Legacy URL: switch to Binance Demo and open the unified trading page. */
function PaperV2Redirect() {
  const navigate = useNavigate();
  const setTradingContext = useBotStore((s) => s.setTradingContext);

  useEffect(() => {
    setTradingContext({ platform: 'binance', accountMode: 'demo' });
    navigate({ to: '/', replace: true });
  }, [navigate, setTradingContext]);

  return (
    <div className="center-page" style={{ minHeight: '60vh' }}>
      <p className="loading-text">Redirecting…</p>
    </div>
  );
}
