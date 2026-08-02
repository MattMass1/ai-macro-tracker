"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import type { ChatPayload } from "@/lib/types";

type ChatMessage = { id: number; role: "user" | "assistant"; text: string };

export default function ChatLog({ onLogged }: { onLogged: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [rendered, setRendered] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: 0, role: "assistant", text: "Tell me what you ate and I’ll log it." },
  ]);
  const endRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function showSheet() {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setRendered(true);
    requestAnimationFrame(() => requestAnimationFrame(() => setOpen(true)));
  }

  function hideSheet() {
    setOpen(false);
    closeTimer.current = setTimeout(() => setRendered(false), 300);
  }

  useEffect(() => {
    return () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  async function send(event: FormEvent) {
    event.preventDefault();
    const message = input.trim();
    if (!message || sending) return;

    setInput("");
    setSending(true);
    setMessages((current) => [
      ...current,
      { id: Date.now(), role: "user", text: message },
    ]);

    try {
      const response = await fetch("/api/macro/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const payload = (await response.json().catch(() => null)) as
        | (ChatPayload & { error?: string })
        | null;
      if (!response.ok) {
        throw new Error(payload?.error ?? `Chat failed (${response.status})`);
      }
      setMessages((current) => [
        ...current,
        { id: Date.now() + 1, role: "assistant", text: payload?.reply ?? "Done." },
      ]);
      if (payload?.logged.length) await onLogged();
    } catch (caught) {
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          text: caught instanceof Error ? caught.message : "I couldn’t log that right now.",
        },
      ]);
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      {rendered && (
        <div
          className={`fixed inset-0 z-40 transition-colors duration-300 ${
            open ? "bg-black/45" : "bg-black/0"
          }`}
          onClick={hideSheet}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-label="Chat and log food"
            className={`sheet-safe-area absolute inset-x-0 bottom-0 mx-auto flex max-h-[min(60dvh,calc(100dvh-env(safe-area-inset-top)-1rem))] max-w-md flex-col rounded-t-3xl border border-line bg-surface pt-2 shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] ${
              open ? "translate-y-0" : "translate-y-full"
            }`}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mx-auto mb-2 h-[5px] w-9 shrink-0 rounded-full bg-muted/40" aria-hidden="true" />
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="font-semibold">Chat &amp; Log</h2>
                <p className="text-xs text-muted">Describe what you ate naturally.</p>
              </div>
              <button
                type="button"
                aria-label="Close chat"
                onClick={hideSheet}
                className="min-h-10 min-w-10 rounded-full text-xl text-muted"
              >
                ×
              </button>
            </div>

            <div className="momentum-scroll min-h-0 flex-1 space-y-2 overflow-y-auto pb-3 overscroll-contain">
              {messages.map((message) => (
                <div
                  key={message.id}
                  className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <p
                    className={`max-w-[84%] rounded-2xl px-3 py-2 text-sm leading-relaxed ${
                      message.role === "user"
                        ? "rounded-br-md bg-surface-2"
                        : "rounded-bl-md bg-bg"
                    }`}
                  >
                    {message.text}
                  </p>
                </div>
              ))}
              {sending && <p className="text-sm text-muted">typing…</p>}
              <div ref={endRef} />
            </div>

            <form onSubmit={send} className="flex gap-2 border-t border-line pt-3">
              <input
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="had a Barebells and coffee"
                aria-label="Food message"
                disabled={sending}
                className="min-w-0 flex-1 rounded-xl bg-surface-2 px-3 text-base outline-none placeholder:text-muted focus:ring-1 focus:ring-protein/70"
              />
              <button
                type="submit"
                disabled={!input.trim() || sending}
                className="min-h-11 rounded-xl bg-protein px-4 text-sm font-semibold text-black disabled:opacity-40"
              >
                Send
              </button>
            </form>
          </section>
        </div>
      )}

      <button
        type="button"
        aria-label="Open Chat and Log"
        onClick={showSheet}
        className="fixed bottom-[calc(env(safe-area-inset-bottom)+1.25rem)] right-[max(1.25rem,env(safe-area-inset-right))] z-30 flex h-14 w-14 items-center justify-center rounded-full bg-protein text-2xl text-black shadow-xl active:scale-[0.97]"
      >
        💬
      </button>
    </>
  );
}
