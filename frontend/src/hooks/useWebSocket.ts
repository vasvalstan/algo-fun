/* ── WebSocket hook with automatic reconnect ── */

import { useEffect, useRef } from 'react';
import { useBotStore, isV2Channel, type WsChannel } from './useBotState';
import type { BotState, V2BotState } from '../lib/types';

const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_DELAY = 30000;

export type { WsChannel };

export function useWebSocket(channel: WsChannel) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelay = useRef(RECONNECT_DELAY);

  const setState = useBotStore((s) => s.setState);
  const setV2State = useBotStore((s) => s.setV2State);
  const setConnectionStatus = useBotStore((s) => s.setConnectionStatus);
  const setLastReceived = useBotStore((s) => s.setLastReceived);
  const addDebugLog = useBotStore((s) => s.addDebugLog);
  const clearDebugLogs = useBotStore((s) => s.clearDebugLogs);

  const isMounted = useRef(true);

  useEffect(() => {
    isMounted.current = true;
    const v2 = isV2Channel(channel);

    const stamp = () => new Date().toLocaleTimeString();
    clearDebugLogs();
    addDebugLog(`[${stamp()}] init channel=${channel}`);

    const connect = () => {
      const backendUrl = import.meta.env.VITE_BACKEND_URL || '';
      let url: string;
      if (backendUrl) {
        const wsBase = backendUrl.replace(/^http/, 'ws');
        url = `${wsBase}/ws/${channel}`;
      } else {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        url = `${protocol}//${window.location.host}/ws/${channel}`;
      }
      addDebugLog(`[${stamp()}] connect ${url}`);

      if (isMounted.current) {
        setConnectionStatus('connecting');
        useBotStore.setState({ state: null, v2State: null });
      }

      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!isMounted.current) return;
        setConnectionStatus('connected');
        reconnectDelay.current = RECONNECT_DELAY;
        addDebugLog(`[${stamp()}] open channel=${channel}`);
      };

      ws.onmessage = (event) => {
        if (!isMounted.current) return;
        try {
          const data = JSON.parse(event.data);
          if (v2) {
            setV2State(data as V2BotState);
          } else {
            setState(data as BotState);
          }
          setLastReceived(Date.now());
        } catch { /* malformed frame */ }
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (!isMounted.current) return;

        setConnectionStatus('disconnected');
        addDebugLog(
          `[${stamp()}] close channel=${channel} retry=${Math.round(reconnectDelay.current / 1000)}s`
        );
        reconnectTimer.current = setTimeout(() => {
          reconnectDelay.current = Math.min(
            reconnectDelay.current * 1.5,
            MAX_RECONNECT_DELAY
          );
          connect();
        }, reconnectDelay.current);
      };

      ws.onerror = () => {
        if (!isMounted.current) return;
        setConnectionStatus('error');
        addDebugLog(`[${stamp()}] error channel=${channel}`);
        ws.close();
      };
    };

    connect();

    return () => {
      isMounted.current = false;
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
      }
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
      addDebugLog(`[${stamp()}] cleanup channel=${channel}`);
    };
  }, [
    channel,
    setState,
    setV2State,
    setConnectionStatus,
    setLastReceived,
    addDebugLog,
    clearDebugLogs,
  ]);
}
