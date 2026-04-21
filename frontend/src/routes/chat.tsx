import { createFileRoute } from '@tanstack/react-router';
import { AgentChatPanel } from '../components/AgentChatPanel';

export const Route = createFileRoute('/chat')({
  component: ChatPage,
});

function ChatPage() {
  return (
    <div style={{ paddingTop: 16, maxWidth: 900, margin: '0 auto', height: 'calc(100vh - 120px)' }}>
      <div style={{ marginBottom: 12 }}>
        <h2 style={{ color: 'var(--text-primary)', margin: 0, fontSize: '1.15rem' }}>Agent chat</h2>
        <p style={{ color: 'var(--text-dim)', fontSize: '0.8rem', margin: '6px 0 0' }}>
          Talk to the Hermes agent running on Railway. Streams via the backend reverse proxy.
        </p>
      </div>
      <AgentChatPanel />
    </div>
  );
}
