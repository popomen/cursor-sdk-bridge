/**
 * SSE 增量解码器 + Trae 上游事件流读取。
 *
 * 解码器严格实现 WHATWG event-stream 算法：LF / CRLF / 裸 CR 都终止一行，
 * 同一事件里多个 `data` 字段用单个 LF 折叠。
 *
 * `pendingParts` 数组累积是关键性能设计：不含换行的 chunk 以 O(1) 追加，只在遇到终止符时 join 一次。
 * 朴素的 `buffer += chunk` + 反复 indexOf 在大 base64 图片场景（单行几 MB、
 * 分几百个 chunk 到达）会退化成 O(n²) 并阻塞事件循环数秒。
 */

export interface DecodedSseEvent {
  data: string;
  event?: string;
  id?: string;
  retry?: number;
}

export class SseBlockDecoder {
  /** 单行上限。data 行可以合法地很大（内联 base64），但永不发换行的上游必须让流失败而不是无限缓冲 */
  private static readonly MAX_BUFFERED_LINE = 16 * 1024 * 1024;
  /** 单事件上限。否则上游可以用无穷多个小 data 行、永不发空行来绕过单行上限 */
  private static readonly MAX_EVENT_CHARACTERS = 16 * 1024 * 1024;
  private static readonly MAX_EVENT_LINES = 65_536;

  private readonly decoder = new TextDecoder();
  private pendingParts: string[] = [];
  private pendingLength = 0;
  /** 保留的残段以 CR 结尾时为真——它可能是 CRLF 的前半，LF 在下个 chunk 开头 */
  private pendingCr = false;
  private dataLines: string[] = [];
  private eventType: string | undefined;
  private lastEventId: string | undefined;
  private retry: number | undefined;
  private eventCharacters = 0;
  private eventLines = 0;

  push(chunk: Uint8Array): DecodedSseEvent[] {
    const decoded = this.decoder.decode(chunk, { stream: true });
    if (!decoded) return [];
    // 快路径：chunk 内没有任何行终止符，直接入数组，不 join 不扫描
    if (!this.pendingCr && !decoded.includes("\n") && !decoded.includes("\r")) {
      this.pendingParts.push(decoded);
      this.pendingLength += decoded.length;
      this.assertBounded();
      return [];
    }
    return this.drainText(this.takeBuffered() + decoded, false);
  }

  finish(): DecodedSseEvent[] {
    const events = this.drainText(this.takeBuffered() + this.decoder.decode(), true);
    // 兼容性扩展：部分上游在最后一行 data 后直接关闭，不发规范要求的空行。
    // 在 EOF 保留这个尾块而不是丢弃。
    const trailing = this.dispatchEvent();
    if (trailing) events.push(trailing);
    return events;
  }

  private takeBuffered(): string {
    if (this.pendingParts.length === 0) return "";
    const text = this.pendingParts.length === 1 ? this.pendingParts[0]! : this.pendingParts.join("");
    this.pendingParts = [];
    this.pendingLength = 0;
    return text;
  }

  private retainPartial(text: string): void {
    if (text) {
      this.pendingParts.push(text);
      this.pendingLength = text.length;
      this.pendingCr = text.endsWith("\r");
      this.assertBounded();
    } else {
      this.pendingCr = false;
    }
  }

  private assertBounded(): void {
    if (this.pendingLength > SseBlockDecoder.MAX_BUFFERED_LINE) {
      throw new Error(
        `SSE line exceeded ${SseBlockDecoder.MAX_BUFFERED_LINE} characters without a line terminator`,
      );
    }
  }

  private drainText(text: string, final: boolean): DecodedSseEvent[] {
    const events: DecodedSseEvent[] = [];
    let lineStart = 0;
    let index = 0;

    while (index < text.length) {
      const ch = text[index];
      if (ch === "\n") {
        this.processLine(text.slice(lineStart, index), events);
        index += 1;
        lineStart = index;
        continue;
      }
      if (ch === "\r") {
        if (index + 1 === text.length && !final) {
          // chunk 边界上的 CR 可能是 CRLF 的一半，等下个 chunk 再判
          break;
        }
        this.processLine(text.slice(lineStart, index), events);
        index += text[index + 1] === "\n" ? 2 : 1;
        lineStart = index;
        continue;
      }
      index += 1;
    }

    let remainder = text.slice(lineStart);
    if (final && remainder) {
      this.processLine(remainder, events);
      remainder = "";
    }
    this.retainPartial(remainder);
    return events;
  }

  private processLine(line: string, events: DecodedSseEvent[]): void {
    if (line === "") {
      const event = this.dispatchEvent();
      if (event) events.push(event);
      return;
    }

    // `;` 是上游心跳行（非 WHATWG 注释），与 `:` 注释同等对待：
    // 不计入事件行数/字符数上限，也不产生字段。否则深队列里的心跳会把上限打满。
    if (line.startsWith(":") || line.startsWith(";")) return;

    this.eventLines += 1;
    this.eventCharacters += line.length + 1;
    if (
      this.eventLines > SseBlockDecoder.MAX_EVENT_LINES ||
      this.eventCharacters > SseBlockDecoder.MAX_EVENT_CHARACTERS
    ) {
      throw new Error(
        `SSE event exceeded ${SseBlockDecoder.MAX_EVENT_CHARACTERS} characters or ` +
          `${SseBlockDecoder.MAX_EVENT_LINES} lines without an event terminator`,
      );
    }

    const colonIndex = line.indexOf(":");
    const field = colonIndex < 0 ? line : line.slice(0, colonIndex);
    let value = colonIndex < 0 ? "" : line.slice(colonIndex + 1);
    if (value.startsWith(" ")) value = value.slice(1); // 只剥一个前导空格

    if (field === "data") {
      this.dataLines.push(value);
    } else if (field === "event") {
      this.eventType = value;
    } else if (field === "id") {
      if (!value.includes("\0")) this.lastEventId = value;
    } else if (field === "retry" && /^[0-9]+$/.test(value)) {
      this.retry = Number(value);
    }
  }

  private dispatchEvent(): DecodedSseEvent | null {
    if (this.dataLines.length === 0) {
      this.eventType = undefined;
      this.eventCharacters = 0;
      this.eventLines = 0;
      return null;
    }
    const event: DecodedSseEvent = {
      data: this.dataLines.join("\n"),
      ...(this.eventType ? { event: this.eventType } : {}),
      ...(this.lastEventId !== undefined ? { id: this.lastEventId } : {}),
      ...(this.retry !== undefined ? { retry: this.retry } : {}),
    };
    this.dataLines = [];
    this.eventType = undefined;
    this.eventCharacters = 0;
    this.eventLines = 0;
    return event;
  }
}

/** 上游流事件：要么是一个解码好的 SSE 事件，要么是一个读取错误 */
export interface UpstreamStreamEvent {
  event?: DecodedSseEvent;
  error?: Error;
}

/** Trae 上游的两个终止事件名：`done` 是当前版本，`finish_reason` 是旧版本 */
export function isTerminalEventName(name: string | undefined): boolean {
  if (!name) return false;
  const n = name.trim().toLowerCase();
  return n === "done" || n === "finish_reason";
}

/**
 * 读取上游 SSE 流。
 *
 * 两条 Trae 特有的规则：
 *  1. 收到 `done` 或 `finish_reason` 事件后**立即停止读取**，不再消费后续字节。
 *  2. idle timeout —— 两次数据之间超过 `idleMs` 就判定流卡死。排队期间上游会持续
 *     发 queue 事件，本身就是 keepalive，所以这个值可以设得比较大。
 *
 * 注意调用方必须区分「收到终止事件」与「EOF 但没收到终止事件」：后者是不完整的流，
 * 必须当作失败，不能当成功返回。
 */
export async function* readUpstreamSSE(
  body: ReadableStream<Uint8Array>,
  idleMs: number,
  signal?: AbortSignal,
): AsyncGenerator<UpstreamStreamEvent, void, void> {
  const reader = body.getReader();
  const decoder = new SseBlockDecoder();

  let idleTimer: NodeJS.Timeout | null = null;
  let idleFired = false;
  const armIdle = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleFired = true;
      void reader.cancel(new Error("sse idle timeout")).catch(() => {});
    }, idleMs);
    // 别让这个定时器拖住进程退出
    idleTimer.unref?.();
  };

  const onAbort = () => {
    void reader.cancel(new Error("aborted")).catch(() => {});
  };
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();

  try {
    armIdle();
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (e) {
        if (idleFired) {
          yield { error: new Error(`sse idle timeout after ${idleMs}ms`) };
        } else if (signal?.aborted) {
          return; // 下游取消，静默退出
        } else {
          yield { error: e as Error };
        }
        return;
      }

      if (chunk.done) {
        if (signal?.aborted) return;
        if (idleFired) {
          yield { error: new Error(`sse idle timeout after ${idleMs}ms`) };
          return;
        }
        let tail: DecodedSseEvent[];
        try {
          tail = decoder.finish();
        } catch (e) {
          yield { error: e as Error };
          return;
        }
        for (const ev of tail) yield { event: ev };
        return;
      }

      armIdle();

      let events: DecodedSseEvent[];
      try {
        events = decoder.push(chunk.value);
      } catch (e) {
        yield { error: e as Error };
        return;
      }

      for (const ev of events) {
        yield { event: ev };
        // 终止事件之后立即停读——上游不保证会关闭连接
        if (isTerminalEventName(ev.event)) return;
      }
    }
  } finally {
    if (idleTimer) clearTimeout(idleTimer);
    signal?.removeEventListener("abort", onAbort);
    try {
      await reader.cancel();
    } catch {
      /* 已经关了 */
    }
    try {
      reader.releaseLock();
    } catch {
      /* 已经释放 */
    }
  }
}
