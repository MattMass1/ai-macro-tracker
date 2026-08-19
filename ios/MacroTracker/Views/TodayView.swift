import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var store: AppStore
    @State private var note = ""
    @State private var noteDay = ""
    @State private var lastPersistedNote = ""
    @State private var isChangingDay = false
    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday, isEnabled: !store.isLoadingDay && !isChangingDay) { delta in Task { await changeDay(by: delta) } }
                    if store.isLoadingDay && store.day == nil { loading }
                    else if let day = store.day {
                        MacroRingsView(totals: day.totals, targets: day.targets, remaining: day.remaining)
                        if let warning = day.warning { Label(warning, systemImage: "exclamationmark.triangle.fill").font(.footnote).foregroundStyle(Theme.calories).appCard() }
                        BriefCard(text: $note) { Task { await persistCurrentNote() } }
                        PresetGridView(presets: store.presets) { preset in Task { await store.logPreset(preset) } }
                        MealListView(meals: day.meals) { id in Task { await store.deleteMeal(id) } }
                    } else { EmptyState(icon: "wifi.exclamationmark", title: "No daily data", message: "Pull to refresh after checking your API settings.") }
                }.padding(.horizontal, 16).padding(.bottom, 30)
            }.refreshable { await store.loadDay() }
        }
        .navigationTitle("Macro Tracker").navigationBarTitleDisplayMode(.large)
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

private struct BriefCard: View {
    @Binding var text: String; let save: () -> Void
    @FocusState private var focused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack { SectionLabel(text: "Daily note"); Spacer(); if focused { Button("Save", action: save).font(.caption.weight(.bold)) } }
            TextField("Training time, meal prep, anything useful…", text: $text, axis: .vertical).lineLimit(2...5).font(.subheadline).focused($focused).onSubmit(save)
        }.appCard()
    }
}

private struct Shimmer: ViewModifier {
    @State private var phase = -1.0
    func body(content: Content) -> some View { content.overlay(LinearGradient(colors: [.clear, .white.opacity(0.3), .clear], startPoint: .leading, endPoint: .trailing).offset(x: phase * 300).mask(content)).onAppear { withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: false)) { phase = 1 } } }
}
extension View { func shimmering() -> some View { modifier(Shimmer()) } }
