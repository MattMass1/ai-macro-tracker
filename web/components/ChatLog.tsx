"use client";

import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";

import type { ChatPayload, VisionPayload } from "@/lib/types";

type ChatMessage = { id: number; role: "user" | "assistant"; text: string };

const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const MAX_IMAGE_PIXELS = 4_000_000;

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("I couldn’t read that image. Please try another one."));
    };
    image.src = url;
  });
}

async function prepareImage(file: File): Promise<string> {
  const image = await loadImage(file);
  const pixelScale = Math.min(1, Math.sqrt(MAX_IMAGE_PIXELS / (image.width * image.height)));
  let width = Math.max(1, Math.round(image.width * pixelScale));
  let height = Math.max(1, Math.round(image.height * pixelScale));
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Your browser couldn’t prepare that photo.");

  let dataUrl = "";
  for (let pass = 0; pass < 5; pass += 1) {
    canvas.width = width;
    canvas.height = height;
    context.drawImage(image, 0, 0, width, height);
    for (const quality of [0.86, 0.72, 0.58, 0.44]) {
      dataUrl = canvas.toDataURL("image/jpeg", quality);
      if (dataUrl.length <= MAX_IMAGE_BYTES) return dataUrl;
    }
    width = Math.max(1, Math.round(width * 0.78));
    height = Math.max(1, Math.round(height * 0.78));
  }
  if (dataUrl.length > MAX_IMAGE_BYTES) {
    throw new Error("That photo is too large. Please choose a smaller image.");
  }
  return dataUrl;
}

export default function ChatLog({ onLogged }: { onLogged: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [rendered, setRendered] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [photo, setPhoto] = useState<string | null>(null);
  const [vision, setVision] = useState<VisionPayload | null>(null);
  const [visionStatus, setVisionStatus] = useState<"idle" | "analyzing" | "logging">("idle");
  const [visionError, setVisionError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: 0, role: "assistant", text: "Tell me what you ate and I’ll log it." },
  ]);
  const endRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cameraRef = useRef<HTMLInputElement>(null);

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

  function clearPhoto() {
    setPhoto(null);
    setVision(null);
    setVisionError(null);
    setVisionStatus("idle");
    if (cameraRef.current) cameraRef.current.value = "";
  }

  async function capturePhoto(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setVision(null);
    setVisionError(null);
    setVisionStatus("analyzing");
    try {
      const image = await prepareImage(file);
      setPhoto(image);
      const response = await fetch("/api/macro/vision-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image }),
      });
      const payload = (await response.json().catch(() => null)) as
        | (VisionPayload & { error?: string })
        | null;
      if (!response.ok) throw new Error(payload?.error ?? "I couldn’t analyze that photo.");
      setVision(payload);
    } catch (caught) {
      setVisionError(caught instanceof Error ? caught.message : "I couldn’t analyze that photo.");
    } finally {
      setVisionStatus("idle");
    }
  }

  async function logVisionResult() {
    if (!vision || visionStatus === "logging") return;
    setVisionStatus("logging");
    setVisionError(null);
    try {
      const response = await fetch("/api/macro/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: vision.name,
          calories: vision.calories,
          protein: vision.protein,
          carbs: vision.carbs,
          fat: vision.fat,
          fiber: vision.fiber,
          macro_source: "Vision model (estimated from food photo)",
          ...(vision.meal ? { meal: vision.meal } : {}),
        }),
      });
      const payload = (await response.json().catch(() => null)) as { error?: string } | null;
      if (!response.ok) throw new Error(payload?.error ?? "I couldn’t log that meal.");
      await onLogged();
      setMessages((current) => [
        ...current,
        { id: Date.now(), role: "assistant", text: `Logged ${vision.name} — ${vision.calories} kcal ✓` },
      ]);
      clearPhoto();
    } catch (caught) {
      setVisionError(caught instanceof Error ? caught.message : "I couldn’t log that meal.");
      setVisionStatus("idle");
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
              {photo && (
                <div className="rounded-2xl border border-line bg-bg p-3">
                  <img src={photo} alt="Food ready for analysis" className="mb-3 max-h-48 w-full rounded-xl object-cover" />
                  {visionStatus === "analyzing" && <p className="text-sm text-muted">Analyzing…</p>}
                  {vision && (
                    <div className="space-y-3">
                      <div>
                        <p className="font-semibold">Here’s what I see — log this?</p>
                        <p className="text-sm text-muted">{vision.name}</p>
                        <p className="mt-1 text-sm">
                          {vision.calories} kcal · {vision.protein}g protein · {vision.carbs}g carbs · {vision.fat}g fat · {vision.fiber}g fiber
                        </p>
                        <p className="mt-1 text-[11px] text-muted">Vision estimate; portions may vary.</p>
                      </div>
                      <div className="flex gap-2">
                        <button type="button" onClick={logVisionResult} disabled={visionStatus === "logging"} className="min-h-11 flex-1 rounded-xl bg-protein px-4 text-sm font-semibold text-black disabled:opacity-40">
                          {visionStatus === "logging" ? "Logging…" : "Log this"}
                        </button>
                        <button type="button" onClick={clearPhoto} disabled={visionStatus === "logging"} className="min-h-11 rounded-xl bg-surface-2 px-4 text-sm font-semibold disabled:opacity-40">Cancel</button>
                      </div>
                    </div>
                  )}
                  {visionError && (
                    <div className="space-y-2">
                      <p className="text-sm text-over">{visionError}</p>
                      <button type="button" onClick={clearPhoto} className="rounded-xl bg-surface-2 px-3 py-2 text-sm">Choose another photo</button>
                    </div>
                  )}
                </div>
              )}
              <div ref={endRef} />
            </div>

            <form onSubmit={send} className="flex gap-2 border-t border-line pt-3">
              <input ref={cameraRef} type="file" accept="image/*" capture="environment" onChange={capturePhoto} className="sr-only" aria-label="Take a food photo" />
              <button type="button" onClick={() => cameraRef.current?.click()} disabled={sending || visionStatus !== "idle"} aria-label="Take a food photo" className="flex min-h-11 min-w-11 items-center justify-center rounded-xl bg-surface-2 text-xl disabled:opacity-40">📷</button>
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
