import type { ConnectionStatus as ConnStatus } from '../lib/types';

interface Props {
  status: ConnStatus;
  lastReceived: number;
}

export function ConnectionStatus({ status, lastReceived }: Props) {
  const dotClass = status === 'connected'
    ? 'connected'
    : status === 'connecting'
      ? 'connecting'
      : 'disconnected';

  const label = status === 'connected'
    ? 'Live'
    : status === 'connecting'
      ? 'Connecting…'
      : 'Disconnected';

  return (
    <div className="connection-bar">
      <span className={`dot ${dotClass}`} />
      <span>{label}</span>
      {lastReceived > 0 && status === 'connected' && (
        <span>· {new Date(lastReceived).toLocaleTimeString()}</span>
      )}
    </div>
  );
}
