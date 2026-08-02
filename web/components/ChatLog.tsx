"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import type { ChatPayload } from "@/lib/types";

type ChatMessage = { id: number; role: "user" | "assistant"; text: string };

export default function ChatLog({ onLogged }: { onLogged: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: 0, role: "assistant", text: "Tell me what you ate and I’ll log it." },
  ]);
  const endRef = useRef<HTMLDivElement>(null);

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
      {open && (
        <div className="fixed inset-0 z-40 bg-black/45" onClick={() => setOpen(false)}>
          <section
            role="dialog"
            aria-label="Chat and log food"
            className="safe-x safe-bottom absolute inset-x-0 bottom-0 mx-auto flex max-h-[60vh] max-w-md flex-col rounded-t-2xl border border-line bg-surface pt-4 shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="font-semibold">Chat &amp; Log</h2>
                <p className="text-xs text-muted">Describe what you ate naturally.</p>
              </div>
              <button
                type="button"
                aria-label="Close chat"
                onClick={() => setOpen(false)}
                className="min-h-10 min-w-10 rounded-full text-xl text-muted"
              >
                ×
              </button>
            </div>

            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pb-3">
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
                className="min-w-0 flex-1 rounded-xl bg-surface-2 px-3 text-sm outline-none placeholder:text-muted focus:ring-1 focus:ring-protein"
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
        onClick={() => setOpen(true)}
        className="fixed bottom-[max(1.25rem,env(safe-area-inset-bottom))] right-[max(1.25rem,env(safe-area-inset-right))] z-30 flex h-14 w-14 items-center justify-center rounded-full bg-protein text-2xl text-black shadow-xl active:scale-95"
      >
        💬
      </button>
    </>
  );
}
