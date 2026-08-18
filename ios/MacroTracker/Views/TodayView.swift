import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var store: AppStore
    @State private var note = ""
    var body: some View {
        ZStack { Theme.canvas.ignoresSafeArea()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    DayPicker(date: store.selectedDate, canGoForward: !store.isToday) { delta in Task { await store.moveDay(by: delta) } }
                    if store.isLoadingDay && store.day == nil { loading }
                    else if let day = store.day {
                        MacroRingsView(totals: day.totals, targets: day.targets, remaining: day.remaining)
                        if let warning = day.warning { Label(warning, systemImage: "exclamationmark.triangle.fill").font(.footnote).foregroundStyle(Theme.calories).appCard() }
                        BriefCard(text: $note) { Task { await store.saveBrief(note) } }
                        PresetGridView(presets: store.presets) { preset in Task { await store.logPreset(preset) } }
                        MealListView(meals: day.meals) { id in Task { await store.deleteMeal(id) } }
                    } else { EmptyState(icon: "wifi.exclamationmark", title: "No daily data", message: "Pull to refresh after checking your API settings.") }
                }.padding(.horizontal, 16).padding(.bottom, 30)
            }.refreshable { await store.loadDay() }
        }
        .navigationTitle("Macro Tracker").navigationBarTitleDisplayMode(.large)
        .onChange(of: store.brief?.text) { _, value in note = value ?? "" }
        .onAppear { note = store.brief?.text ?? "" }
    }
    private var loading: some View { VStack(spacing: 12) { RoundedRectangle(cornerRadius: 22).fill(Theme.surface).frame(height: 176); HStack { ForEach(0..<4) { _ in Circle().fill(Theme.surface).frame(height: 72) } } }.redacted(reason: .placeholder).shimmering() }
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

