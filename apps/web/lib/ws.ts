"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { WS_BASE } from "@/lib/api";

export type WsEnvelope = {
  event_id: string;
  event_type: string;
  occurred_at: string;
  source: string;
  data: unknown;
};

export type WsState = "CONNECTING" | "OPEN" | "RECONNECTING" | "CLOSED";

const MAX_BACKOFF_MS = 30_000;
const BASE_BACKOFF_MS = 1_000;
const MAX_EVENTS = 200;

/**
 * Resilient WebSocket feed with exponential backoff + jitter.
 *
 * Design notes:
 * - A dropped socket must never silently stop the UI from updating, so `onEvent` is
 *   also driven by the caller's HTTP polling as a fallback — the socket only *speeds up*
 *   refresh, it is not the sole source of truth.
 * - Reconnect backoff is capped and jittered so N tabs cannot stampede the backend.
 * - The hook never throws: a failed connection surfaces as `state` for the UI to show.
 */
export function useWsFeed(
  channel: string,
  opts?: { onEvent?: (e: WsEnvelope) => void; enabled?: boolean; maxEvents?: number }
) {
  const { onEvent, enabled = true, maxEvents = MAX_EVENTS } = opts ?? {};
  const [state, setState] = useState<WsState>("CLOSED");
  const [events, setEvents] = useState<WsEnvelope[]>([]);
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [attempts, setAttempts] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const closedByUs = useRef(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const push = useCallback(
    (event: WsEnvelope) => {
      setEvents((prev) => {
        const next = [event, ...prev];
        return next.length > maxEvents ? next.slice(0, maxEvents) : next;
      });
      setLastEventAt(event.occurred_at);
      try {
        onEventRef.current?.(event);
      } catch {
        /* a consumer error must not break the feed */
      }
    },
    [maxEvents]
  );

  const connect = useCallback(() => {
    if (!enabled) return;
    if (socketRef.current && (socketRef.current.readyState === WebSocket.OPEN || socketRef.current.readyState === WebSocket.CONNECTING)) {
      return;
    }
    setState(attemptRef.current === 0 ? "CONNECTING" : "RECONNECTING");

    let socket: WebSocket;
    try {
      socket = new WebSocket(`${WS_BASE}/ws/${channel}`);
    } catch {
      scheduleReconnect();
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => {
      attemptRef.current = 0;
      setAttempts(0);
      setState("OPEN");
    };

    socket.onmessage = (raw) => {
      try {
        const parsed = JSON.parse(typeof raw.data === "string" ? raw.data : "");
        if (parsed && typeof parsed.event_type === "string") push(parsed as WsEnvelope);
      } catch {
        /* ignore non-JSON frames */
      }
    };

    socket.onerror = () => {
      /* onclose always follows; nothing to do here */
    };

    socket.onclose = () => {
      socketRef.current = null;
      if (closedByUs.current) {
        setState("CLOSED");
        return;
      }
      scheduleReconnect();
    };

    function scheduleReconnect() {
      attemptRef.current += 1;
      setAttempts(attemptRef.current);
      setState("RECONNECTING");
      const backoff = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** (attemptRef.current - 1));
      const jitter = Math.random() * 0.3 * backoff;
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => connect(), backoff + jitter);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, enabled, push]);

  useEffect(() => {
    closedByUs.current = false;
    connect();
    return () => {
      closedByUs.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close();
      }
    };
    // Re-connect when the channel changes; `connect` is stable for a given channel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, enabled]);

  const reconnect = useCallback(() => {
    closedByUs.current = false;
    attemptRef.current = 0;
    setAttempts(0);
    if (socketRef.current) {
      try {
        socketRef.current.close();
      } catch {
        /* already closing */
      }
      socketRef.current = null;
    }
    connect();
  }, [connect]);

  const clear = useCallback(() => {
    setEvents([]);
    setLastEventAt(null);
  }, []);

  return useMemo(
    () => ({
      state,
      events,
      lastEventAt,
      attempts,
      connected: state === "OPEN",
      reconnect,
      clear,
    }),
    [state, events, lastEventAt, attempts, reconnect, clear]
  );
}
