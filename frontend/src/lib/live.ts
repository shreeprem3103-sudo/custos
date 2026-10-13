/**
 * Live feed client for `/api/v1/stream`.
 *
 * A control-room console is judged by what it does when the network is unkind:
 *
 * * It reconnects with bounded, jittered backoff — never in a tight loop that
 *   would finish off a saturated field link.
 * * It resumes from the last sequence number it saw, so a two-second dropout
 *   costs two seconds of events, not the whole picture.
 * * It notices *gaps*. The server's heartbeat carries the bus window; when the
 *   console's position falls out of it, the console refetches state rather than
 *   quietly rendering a hole.
 * * It reports its own liveness and the API's snapshot age, so "no updates" is
 *   never confused with "the city is quiet".
 *
 * Wire protocol (backend/custos/api/ws.py):
 *   server → hello | event | snapshot | heartbeat | pong | subscribed | replayed | drill | error
 *   client → {action: "ping" | "subscribe" | "replay" | "drill"}
 */

import { streamUrl } from "./api";
import type { StreamEvent } from "./types";

export interface FeedStatePatch {
  connected?: boolean;
  reconnects?: number;
  gapDetected?: boolean;
  error?: string | null;
  lastSeq?: number;
  snapshotAgeS?: number | null;
  degraded?: boolean;
}

export interface LiveFeedHandlers {
  onEvent(event: StreamEvent): void;
  onState(patch: FeedStatePatch): void;
  /** The console was away long enough that it must refetch, not just resume. */
  onResync(reason: string): void;
}

type ServerMessage = {
  type?: string;
  [key: string]: unknown;
};

const PING_INTERVAL_MS = 25_000;

export class LiveFeed {
  private socket: WebSocket | null = null;
  private closed = false;
  private attempt = 0;
  private reconnects = 0;
  private lastSeq = 0;
  private ringFloor = 0;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private subscribedTopics: string[] = [];

  constructor(private readonly handlers: LiveFeedHandlers) {}

  connect(fromSeq = 0) {
    this.closed = false;
    this.lastSeq = Math.max(this.lastSeq, fromSeq);
    this.ringFloor = this.lastSeq;

    let socket: WebSocket;
    try {
      socket = new WebSocket(streamUrl(this.lastSeq, this.subscribedTopics.length ? this.subscribedTopics : undefined));
    } catch (error) {
      this.handlers.onState({ connected: false, error: `cannot open the live feed: ${(error as Error).message}` });
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.handlers.onState({ connected: true, error: null, reconnects: this.reconnects, lastSeq: this.lastSeq });
      this.startPings();
    };

    socket.onmessage = (raw: MessageEvent<string>) => {
      let message: ServerMessage;
      try {
        message = JSON.parse(raw.data) as ServerMessage;
      } catch {
        return; // a malformed frame must never take the console down
      }
      this.handle(message);
    };

    socket.onclose = (event: CloseEvent) => {
      this.stopPings();
      this.socket = null;
      if (this.closed) return;
      const reason =
        event.code === 1008
          ? "the command API refused the live feed - your session may have expired"
          : event.code === 1000
            ? null
            : `live feed disconnected (${event.code})`;
      this.handlers.onState({ connected: false, error: reason });
      if (event.code !== 1008) this.scheduleReconnect();
    };

    socket.onerror = () => this.handlers.onState({ connected: false, error: "live feed error" });
  }

  private handle(message: ServerMessage) {
    const type = String(message.type ?? "");
    switch (type) {
      case "hello": {
        const replayed = Number(message.replayed ?? 0);
        if (replayed === 0) {
          // We asked to resume but the ring had nothing: the console is behind.
          this.handlers.onResync("the live feed had no buffered events for this session");
        }
        return;
      }
      case "event": {
        const seq = Number(message.seq ?? 0);
        if (seq && this.ringFloor && seq > this.ringFloor + 1) {
          // A gap we can still repair by replaying from our own position.
          this.requestReplay(this.ringFloor);
        }
        this.lastSeq = Math.max(this.lastSeq, seq);
        this.handlers.onState({ lastSeq: this.lastSeq });
        this.handlers.onEvent(message as unknown as StreamEvent);
        return;
      }
      case "snapshot": {
        this.handlers.onState({ degraded: Boolean(message.degraded) });
        return;
      }
      case "heartbeat": {
        const bus = (message.bus ?? {}) as { oldest_seq?: number | null };
        const oldest = Number(bus.oldest_seq ?? 0);
        if (oldest && this.lastSeq && oldest > this.lastSeq + 1) {
          // Our resume point has aged out of the ring: silently skipping the
          // missing minutes would be the worst possible behaviour here.
          this.handlers.onState({ gapDetected: true });
          this.handlers.onResync(`events ${this.lastSeq + 1}..${oldest - 1} are no longer buffered`);
          this.lastSeq = Math.max(this.lastSeq, oldest - 1);
        }
        this.handlers.onState({
          snapshotAgeS: message.snapshot_age_s === null || message.snapshot_age_s === undefined ? null : Number(message.snapshot_age_s),
          degraded: Boolean(message.degraded),
        });
        return;
      }
      case "replayed": {
        this.handlers.onState({ gapDetected: false });
        return;
      }
      case "error": {
        this.handlers.onState({ error: String(message.detail ?? "live feed error") });
        return;
      }
      case "pong":
      case "subscribed":
      case "drill":
      default:
        return;
    }
  }

  private startPings() {
    this.stopPings();
    // A client-side ping keeps intermediaries from idling the socket out, and
    // gives the console a heartbeat of its own to measure the API against.
    this.pingTimer = setInterval(() => this.send({ action: "ping" }), PING_INTERVAL_MS);
  }

  private stopPings() {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private scheduleReconnect() {
    if (this.closed) return;
    this.attempt += 1;
    this.reconnects += 1;
    const base = Math.min(15_000, 700 * 2 ** Math.min(this.attempt, 4));
    const delay = base + Math.random() * 500;
    this.handlers.onState({ reconnects: this.reconnects });
    setTimeout(() => {
      if (!this.closed) this.connect(this.lastSeq);
    }, delay);
  }

  private send(payload: Record<string, unknown>): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(payload));
    return true;
  }

  /** Ask the server to resend everything after ``seq`` (gap repair). */
  requestReplay(seq: number) {
    this.send({ action: "replay", since_seq: seq });
  }

  subscribe(topics: string[]) {
    this.subscribedTopics = topics;
    this.send({ action: "subscribe", topics });
  }

  /** Drive the synthetic telemetry replay (training drills) from the console. */
  setDrillRainfall(mmH: number) {
    this.send({ action: "drill", rainfall_mm_h: mmH });
  }

  disconnect() {
    this.closed = true;
    this.stopPings();
    this.socket?.close(1000, "console closed");
    this.socket = null;
  }

  get position(): number {
    return this.lastSeq;
  }
}
