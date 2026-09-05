// Temporary sunset screen. Reverting the sunset commit restores the web front end.
export default function SunsetPage() {
  return (
    <main className="relative flex min-h-dvh items-center justify-center overflow-hidden bg-[#f5f6f8] px-5 py-10 text-[#1a1d23]">
      <div aria-hidden="true" className="absolute -right-24 -top-24 h-72 w-72 rounded-full bg-[#0f5132]/10 blur-3xl" />
      <div aria-hidden="true" className="absolute -bottom-32 -left-24 h-80 w-80 rounded-full bg-[#b45309]/10 blur-3xl" />

      <section className="relative w-full max-w-lg rounded-[1.375rem] border border-black/5 bg-white px-7 py-9 text-center shadow-[0_24px_70px_rgba(26,29,35,0.10)] sm:px-10 sm:py-12">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-[1.25rem] bg-[#0f5132] shadow-[0_10px_30px_rgba(15,81,50,0.25)]">
          <svg aria-hidden="true" viewBox="0 0 24 24" className="h-8 w-8 fill-none stroke-white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 17c4.5 0 8-3.5 8-8-4.5 0-8 3.5-8 8Z" />
            <path d="M7 17c0-2.8 1.8-5.1 5.2-6.8M7 17v2" />
          </svg>
        </div>

        <p className="mt-7 text-xs font-bold uppercase tracking-[0.2em] text-[#0f5132]">Macro Coach</p>
        <h1 className="mt-3 text-3xl font-extrabold tracking-[-0.035em] sm:text-4xl">The web app is taking a break.</h1>
        <p className="mx-auto mt-4 max-w-sm text-base leading-7 text-[#687184]">
          This version is temporarily paused and disconnected. To keep your food log accurate, please use the native Macro Coach app on your iPhone.
        </p>

        <div className="mt-8 rounded-2xl bg-[#f5f6f8] px-5 py-4 text-left">
          <p className="text-sm font-bold text-[#1a1d23]">Keep logging on iPhone</p>
          <p className="mt-1 text-sm leading-6 text-[#687184]">
            Open the Macro Coach app installed on your iPhone. Your web session cannot view or change tracker data while this pause is active.
          </p>
        </div>

        <p className="mt-7 text-xs leading-5 text-[#8a93a6]">The web app will return after this temporary pause.</p>
      </section>
    </main>
  );
}
