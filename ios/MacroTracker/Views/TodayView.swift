import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var store: AppStore
    @Binding var selectedTab: Int
    @State private var note = ""
    @State private var noteDay = ""
    @State private var lastPersistedNote = ""
    @State private var isChangingDay = false
    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday, isEnabled: !store.isLoadingDay && !isChangingDay) { delta in Task { await changeDay(by: delta) } }
                    CoachStrip { selectedTab = 1 }
                    if store.isLoadingDay && store.day == nil { loading }
                    else if let day = store.day {
                        MacroRingsView(totals: day.totals, targets: day.targets, remaining: day.remaining)
                        MealListView(meals: day.meals, onDelete: { id in Task { await store.deleteMeal(id) } }, onAdd: { selectedTab = 1 })
                        if let warning = day.warning { Label(warning, systemImage: "exclamationmark.triangle.fill").font(.footnote).foregroundStyle(Theme.danger).appCard() }
                        BriefCard(text: $note) { Task { await persistCurrentNote() } }
                        PresetGridView(presets: store.presets) { preset in Task { await store.logPreset(preset) } }
                    } else { EmptyState(icon: "wifi.exclamationmark", title: "No daily data", message: "Pull to refresh after checking your API settings.") }
                }.padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 96)
            }
            .scrollDismissesKeyboard(.interactively)
            .refreshable { await store.loadDay() }
        }
        .navigationBarHidden(true)
        .onChange(of: store.brief?.date) { _, _ in adoptBriefIfMatching() }
        .onChange(of: store.brief?.text) { _, _ in adoptBriefIfMatching() }
        .onChange(of: store.isLoadingDay) { _, loading in if !loading { adoptBriefIfMatching() } }
        .onAppear { adoptBriefIfMatching() }
    }
    private var loading: some View { VStack(spacing: 12) { RoundedRectangle(cornerRadius: 22).fill(Theme.surface).frame(height: 176); HStack { ForEach(0..<4) { _ in Circle().fill(Theme.surface).frame(height: 72) } } }.redacted(reason: .placeholder).shimmering() }

    private func adoptBriefIfMatching() {
        guard let brief = store.brief, brief.date == store.dateString else { return }
        if noteDay == store.dateString, note != lastPersistedNote { return }
        let text = brief.text ?? ""
        note = text
        lastPersistedNote = text
        noteDay = brief.date
    }

    private func persistCurrentNote() async {
        guard noteDay == store.dateString, note != lastPersistedNote else { return }
        await store.saveBrief(note)
        if store.brief?.date == noteDay { lastPersistedNote = store.brief?.text ?? note }
    }

    private func changeDay(by delta: Int) async {
        guard !store.isLoadingDay, !isChangingDay else { return }
        isChangingDay = true
        defer { isChangingDay = false }
        await persistCurrentNote()
        await store.moveDay(by: delta)
    }
}

private struct CoachStrip: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: "figure.mind.and.body")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.16), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text("Coach").font(.subheadline.weight(.bold)).foregroundStyle(.white)
                    Text("Ready when you are").font(.caption).foregroundStyle(.white.opacity(0.75)).lineLimit(1)
                }
                Spacer(minLength: 4)
                Text("Ask →").font(.caption.weight(.bold)).foregroundStyle(Theme.accent).padding(.horizontal, 13).padding(.vertical, 7).background(.white, in: Capsule())
            }
            .padding(16)
            .background(LinearGradient(colors: [Theme.accentDark, Theme.accent], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        }.buttonStyle(.plain)
    }
}

private struct BriefCard: View {
    @Binding var text: String; let save: () -> Void
    @FocusState private var focused: Bool
    @State private var isEditing = false
    private var snapshot: ReadinessSnapshot { ReadinessSnapshot(text: text) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel(text: snapshot.hasWhoopMetrics ? "Whoop readiness" : "Daily note")
                    if snapshot.hasWhoopMetrics {
                        Label("Synced from today’s brief", systemImage: "checkmark.icloud.fill")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if snapshot.hasWhoopMetrics && !isEditing {
                    Text(snapshot.verdict.label)
                        .font(.caption2.weight(.heavy))
                        .tracking(0.8)
                        .foregroundStyle(snapshot.verdict.color)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(snapshot.verdict.color.opacity(0.12), in: Capsule())
                }
                Button(isEditing ? "Done" : "Edit", systemImage: isEditing ? "checkmark" : "pencil") {
                    if isEditing { finishEditing() }
                    else {
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) { isEditing = true }
                        focused = true
                    }
                }
                .labelStyle(.iconOnly)
                .accessibilityLabel(isEditing ? "Save daily brief" : "Edit daily brief")
            }

            if snapshot.hasWhoopMetrics && !isEditing {
                HStack(spacing: 0) {
                    ForEach(Array(snapshot.metrics.enumerated()), id: \.element.id) { index, metric in
                        if index > 0 { Divider().frame(height: 42).padding(.horizontal, 12) }
                        VStack(alignment: .leading, spacing: 3) {
                            Label(metric.label, systemImage: metric.symbol)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.secondary)
                                .labelStyle(.titleAndIcon)
                            HStack(alignment: .firstTextBaseline, spacing: 3) {
                                Text(metric.value)
                                    .font(.system(size: 24, weight: .bold, design: .rounded))
                                    .monospacedDigit()
                                Text(metric.unit).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .transition(.opacity.combined(with: .scale(scale: 0.98)))

                if !snapshot.summary.isEmpty {
                    Text(snapshot.summary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                TextField("Training time, meal prep, anything useful…", text: $text, axis: .vertical)
                    .lineLimit(2...6)
                    .font(.subheadline)
                    .focused($focused)
                    .toolbar {
                        ToolbarItemGroup(placement: .keyboard) {
                            Spacer()
                            Button("Done") { finishEditing() }.fontWeight(.semibold)
                        }
                    }
            }
        }
        .appCard()
    }

    private func finishEditing() {
        focused = false
        save()
        withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) { isEditing = false }
    }
}

private struct ReadinessSnapshot {
    struct Metric: Identifiable {
        let id: String
        let label: String
        let value: String
        let unit: String
        let symbol: String
    }

    enum Verdict {
        case fullSend, moderate, easy, ready

        var label: String {
            switch self {
            case .fullSend: "FULL SEND"
            case .moderate: "MODERATE"
            case .easy: "EASY"
            case .ready: "READY"
            }
        }

        var color: Color {
            switch self {
            case .fullSend: Theme.accent
            case .moderate: Theme.fat
            case .easy: Theme.danger
            case .ready: .secondary
            }
        }
    }

    private static let patterns: [(id: String, label: String, unit: String, symbol: String, pattern: String)] = [
        ("rhr", "RHR", "bpm", "heart.fill", #"\bRHR\s*[:=]?\s*(\d+)\s*(?:bpm)?\b"#),
        ("hrv", "HRV", "ms", "waveform.path.ecg", #"\bHRV\s*[:=]?\s*(\d+)\s*(?:ms)?\b"#),
        ("sleep", "Sleep", "h", "moon.zzz.fill", #"\bsleep(?:\s+duration)?\s*[:=]?\s*([\d.]+)\s*(?:h|hrs?|hours?)\b"#)
    ]

    let verdict: Verdict
    let metrics: [Metric]
    let summary: String
    var hasWhoopMetrics: Bool { !metrics.isEmpty }

    init(text: String) {
        let lowered = text.lowercased()
        if lowered.contains("full send") || lowered.contains("green") { verdict = .fullSend }
        else if lowered.contains("moderate") || lowered.contains("yellow") { verdict = .moderate }
        else if lowered.contains("easy") || lowered.contains("red") { verdict = .easy }
        else { verdict = .ready }

        metrics = Self.patterns.compactMap { item in
            guard let value = Self.capture(in: text, pattern: item.pattern) else { return nil }
            return Metric(id: item.id, label: item.label, value: value, unit: item.unit, symbol: item.symbol)
        }

        var cleaned = text
        cleaned = Self.replacing(in: cleaned, pattern: #"^\s*(?:green|yellow|red|full[\s-]?send|moderate|easy)\s*(?:—|-|:)?\s*"#, with: "")
        for item in Self.patterns { cleaned = Self.replacing(in: cleaned, pattern: item.pattern, with: "") }
        cleaned = Self.replacing(in: cleaned, pattern: #"(?:\s*[|·]\s*)+"#, with: " · ")
        cleaned = Self.replacing(in: cleaned, pattern: #"(?:^|\s)[|·]\s*"#, with: " ")
        cleaned = Self.replacing(in: cleaned, pattern: #"\s{2,}"#, with: " ")
        summary = cleaned.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
    }

    private static func capture(in text: String, pattern: String) -> String? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              match.numberOfRanges > 1,
              let range = Range(match.range(at: 1), in: text) else { return nil }
        return String(text[range])
    }

    private static func replacing(in text: String, pattern: String, with replacement: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return text }
        return regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: replacement)
    }
}

private struct Shimmer: ViewModifier {
    @State private var phase = -1.0
    func body(content: Content) -> some View { content.overlay(LinearGradient(colors: [.clear, .white.opacity(0.3), .clear], startPoint: .leading, endPoint: .trailing).offset(x: phase * 300).mask(content)).onAppear { withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: false)) { phase = 1 } } }
}
extension View { func shimmering() -> some View { modifier(Shimmer()) } }
