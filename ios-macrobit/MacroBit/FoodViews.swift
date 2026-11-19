import SwiftUI
import Charts

struct TodayView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var manual = false
    @State private var deleting: FoodEntry?
    @State private var preset: Preset?
    @State private var servings = "1"
    @State private var meal = "Snack"
    @State private var logged: String?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if store.isLoading { ProgressView("Refreshing. The server may take about a minute.") }
                if let error = store.errorMessage {
                    VStack(alignment: .leading) { Text(error).foregroundStyle(Theme.danger); Button("Try again") { Task { await store.refreshToday() } } }.appCard()
                }
                if let day = store.day {
                    Text(store.effectiveDate ?? day.date).font(.headline)
                    PixelMacroSummary(day: day)
                    PixelPresetGrid(presets: store.presets, busy: store.isMutating, logged: logged,
                        quickLog: { item in
                            // One tap logs one serving in the preset's own meal slot, like the old Quick add grid.
                            Task { if await store.logPreset(LogPresetBody(presetName: item.name, servings: 1, meal: item.meal, day: store.effectiveDate)) { logged = item.name } }
                        },
                        customize: { item in preset = item; servings = "1"; meal = item.meal })
                    HStack { SectionLabel(text: "Meals"); Spacer(); Button("Manual entry") { manual = true }.buttonStyle(PixelSecondaryButtonStyle()) }
                    if day.meals.isEmpty { EmptyState(icon: "fork.knife", title: "A fresh log", message: "Start in the Studio chat, tap a saved meal or enter one.") }
                    ForEach(day.meals) { entry in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(entry.name).font(.headline)
                                    Text("\(entry.meal) · \(entry.calories.formatted()) kcal").font(.subheadline)
                                    Text("P \(nutrientText(entry.protein)) · C \(nutrientText(entry.carbs)) · F \(nutrientText(entry.fat)) · Fiber \(nutrientText(entry.fiber))").font(.caption)
                                    if let source = entry.macroSource { Text(source).font(.caption2).foregroundStyle(Theme.muted) }
                                }
                                Spacer()
                                Button { deleting = entry } label: { Image(systemName: "trash").frame(width: 44, height: 44) }.accessibilityLabel("Delete \(entry.name)").disabled(store.isMutating)
                            }
                            if entry.nutrientsComplete {
                                // The server copies this entry's stored numbers for both actions; the app sends none.
                                HStack(spacing: 8) {
                                    Button("+1 Again") { Task { if await store.repeatMeal(entry) { logged = entry.name } } }
                                        .buttonStyle(PixelSecondaryButtonStyle())
                                        .accessibilityIdentifier("repeat-\(entry.name)")
                                        .accessibilityLabel("Log \(entry.name) again")
                                    if store.isSavedMeal(entry) {
                                        Text("★ SAVED").font(Theme.pixelFont(16)).foregroundStyle(Theme.muted).accessibilityIdentifier("saved-\(entry.name)")
                                    } else {
                                        Button("★ Make button") { Task { _ = await store.saveMealAsPreset(entry) } }
                                            .buttonStyle(PixelSecondaryButtonStyle())
                                            .accessibilityIdentifier("save-\(entry.name)")
                                            .accessibilityLabel("Save \(entry.name) as a saved meal")
                                    }
                                    Spacer(minLength: 0)
                                }
                                .disabled(store.isMutating)
                            } else {
                                Text("Unknown macros: log it again in chat.").font(.caption2).foregroundStyle(Theme.muted)
                            }
                        }.appCard()
                    }
                } else { Button("Load today") { Task { await store.refreshToday() } }.buttonStyle(PixelPrimaryButtonStyle()) }
            }.padding(16)
        }.background(Theme.canvas).navigationTitle("Today").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .refreshable { await store.refreshToday(); await store.loadPresets() }
            .sheet(isPresented: $manual) { NavigationStack { ManualEntryView() } }
            .sheet(item: $preset) { item in
                NavigationStack {
                    Form {
                        Text(item.name).font(.headline)
                        TextField("Servings", text: $servings).keyboardType(.decimalPad)
                        Picker("Meal", selection: $meal) { ForEach(["Breakfast", "Lunch", "Dinner", "Snack"], id: \.self) { Text($0) } }
                        if let error = store.errorMessage { Text(error).foregroundStyle(Theme.danger) }
                        Button("Log preset") {
                            guard let count = Double(servings), count.isFinite, count > 0 else { return }
                            Task { if await store.logPreset(LogPresetBody(presetName: item.name, servings: count, meal: meal, day: store.effectiveDate)) { preset = nil } }
                        }.disabled(store.isMutating || !(Double(servings).map { $0.isFinite && $0 > 0 } ?? false))
                    }.navigationTitle("Preset").toolbar { Button("Cancel") { preset = nil } }
                }
            }
            .confirmationDialog("Delete this entry?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
                if let entry = deleting { Button("Delete \(entry.name)", role: .destructive) { Task { _ = await store.deleteMeal(entry.id); deleting = nil } } }
                Button("Cancel", role: .cancel) { deleting = nil }
            }
    }
}

struct ManualEntryView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var calories = ""
    @State private var protein = ""
    @State private var carbs = ""
    @State private var fat = ""
    @State private var fiber = ""
    @State private var source = ""
    @State private var meal = "Snack"
    // The unchanged LogMealBody requires all nutrients. Blank fields are never zero.
    private var numbers: [Double]? {
        let values = [calories, protein, carbs, fat, fiber].compactMap(Double.init)
        return values.count == 5 && values.allSatisfy { $0.isFinite && $0 >= 0 } ? values : nil
    }
    var body: some View {
        Form {
            Section("Meal") {
                TextField("Food name", text: $name)
                Picker("Category", selection: $meal) { ForEach(["Breakfast", "Lunch", "Dinner", "Snack"], id: \.self) { Text($0) } }
                TextField("Nutrition source, e.g. label", text: $source)
            }
            Section("Enter known values from the source") {
                field("Calories", $calories); field("Protein (g)", $protein); field("Carbs (g)", $carbs); field("Fat (g)", $fat); field("Fiber (g)", $fiber)
                Text("All values are required for manual entry. If a nutrient is unknown, use chat instead. Blank does not mean zero.").font(.caption)
            }
            if let error = store.errorMessage { Text(error).foregroundStyle(Theme.danger) }
            if store.isMutating { ProgressView("Saving. Please wait.") }
            Button("Save meal") {
                guard let values = numbers else { return }
                Task {
                    let body = LogMealBody(name: name.trimmingCharacters(in: .whitespacesAndNewlines), calories: values[0], protein: values[1], carbs: values[2], fat: values[3], fiber: values[4], macroSource: source.trimmingCharacters(in: .whitespacesAndNewlines), meal: meal, day: store.effectiveDate)
                    if await store.logMeal(body) { dismiss() }
                }
            }.disabled(numbers == nil || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || store.isMutating)
        }.navigationTitle("Manual entry").toolbar { Button("Cancel") { dismiss() } }
    }
    private func field(_ label: String, _ binding: Binding<String>) -> some View {
        HStack { Text(label); TextField("Required", text: binding).keyboardType(.decimalPad).multilineTextAlignment(.trailing).accessibilityLabel(label) }
    }
}

struct ProgressViewScreen: View {
    @EnvironmentObject private var store: AppStore
    @State private var window = 7
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Progress").font(.largeTitle.bold())
                Picker("Trend window", selection: $window) { Text("7 days").tag(7); Text("30 days").tag(30) }.pickerStyle(.segmented)
                if let error = store.errorMessage { Text(error).foregroundStyle(Theme.danger) }
                if let trends = store.trends {
                    if trends.days.isEmpty { EmptyState(icon: "chart.bar", title: "No trends yet", message: "Log meals to see your trends.") }
                    else {
                        VStack(alignment: .leading, spacing: 12) {
                            SectionLabel(text: "Daily calories")
                            Chart(trends.days) { day in
                                BarMark(x: .value("Date", day.date), y: .value("Calories", day.calories)).foregroundStyle(Theme.accent)
                            }.frame(height: 200)
                        }.appCard()
                        ForEach(trends.days) { day in
                            VStack(alignment: .leading) {
                                Text(day.date).font(.headline)
                                Text("\(day.calories.formatted()) kcal · \(day.macrosComplete == false ? "Protein incomplete" : nutrientText(day.protein) + " protein")")
                            }.frame(maxWidth: .infinity, alignment: .leading).appCard()
                        }
                        ForEach(trends.weekly) { week in
                            VStack(alignment: .leading) {
                                Text("Week of \(week.weekStart)").font(.headline)
                                Text("\(week.avgCalories.formatted()) kcal average · \(week.daysLogged.formatted()) days logged")
                                Text(week.macrosComplete == false ? "Protein incomplete" : "\(nutrientText(week.avgProtein)) protein average")
                            }.appCard()
                        }
                    }
                } else { ProgressView("Loading trends. The server can take about a minute.") }
            }.padding(16)
        }.background(Theme.canvas)
            .task(id: window) { await store.loadTrends(days: window) }
            .refreshable { await store.loadTrends(days: window) }
    }
}

// MARK: - 8-bit macro summary and saved-meal grid

/// Whole blocks filled for value against target. Code-owned, so it is unit tested; a zero or missing target fills nothing.
enum PixelMeter {
    static let blocks = 16
    /// Time between blocks lighting up when a bar climbs (16 blocks ≈ 0.6 s).
    static let stepMilliseconds = 35
    static func filled(value: Double, target: Double) -> Int {
        guard target > 0, value.isFinite, value > 0 else { return 0 }
        return min(blocks, Int((value / target * Double(blocks)).rounded()))
    }
    static func isOver(value: Double, target: Double) -> Bool { target > 0 && value > target }
}

struct PixelBar: View {
    let value: Double
    let target: Double
    let color: Color
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Blocks currently lit. Climbs one block at a time toward the real value, like an 8-bit power meter.
    @State private var shown = 0
    @State private var climb: Task<Void, Never>?
    var body: some View {
        let filled = PixelMeter.filled(value: value, target: target)
        let over = PixelMeter.isOver(value: value, target: target)
        HStack(spacing: 2) {
            ForEach(0..<PixelMeter.blocks, id: \.self) { index in
                Rectangle()
                    .fill(index < shown ? (over && index == PixelMeter.blocks - 1 ? Theme.danger : color) : Theme.divider)
                    .frame(height: 10)
                    .scaleEffect(y: index == shown - 1 && climb != nil ? 1.35 : 1, anchor: .bottom)
            }
        }
        .padding(2)
        .overlay(Rectangle().stroke(Theme.edgeStrong, lineWidth: 1))
        .accessibilityHidden(true)
        .onAppear { animate(to: filled, from: 0) }
        .onChange(of: filled) { old, new in animate(to: new, from: shown) }
        .onDisappear { climb?.cancel(); climb = nil }
    }
    private func animate(to target: Int, from start: Int) {
        climb?.cancel()
        guard !reduceMotion, target != start else { shown = target; climb = nil; return }
        shown = start
        climb = Task { @MainActor in
            let step = target > start ? 1 : -1
            for value in stride(from: start + step, through: target, by: step) {
                try? await Task.sleep(for: .milliseconds(PixelMeter.stepMilliseconds))
                if Task.isCancelled { return }
                withAnimation(.linear(duration: 0.04)) { shown = value }
            }
            climb = nil
        }
    }
}

struct PixelMacroSummary: View {
    let day: DayPayload
    private var known: Bool { day.nutrientsComplete }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                SectionLabel(text: "Calories today")
                Spacer()
                Text(day.hasTargets == true ? "GOAL \(whole(day.targets.calories))" : "REF \(whole(day.targets.calories))")
                    .font(Theme.pixelFont(16)).foregroundStyle(Theme.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(whole(day.totals.calories)).font(Theme.pixelFont(44, relativeTo: .largeTitle))
                Text("KCAL").font(Theme.pixelFont(18)).foregroundStyle(Theme.muted)
                Spacer()
                VStack(alignment: .trailing, spacing: 0) {
                    Text(whole(max(0, day.remaining.calories))).font(Theme.pixelFont(24))
                    Text(day.totals.calories > day.targets.calories && day.targets.calories > 0 ? "OVER" : "LEFT")
                        .font(Theme.pixelFont(14)).foregroundStyle(Theme.muted)
                }
            }
            PixelBar(value: day.totals.calories, target: day.targets.calories, color: Theme.calories)
            .accessibilityElement(children: .ignore)
            VStack(spacing: 10) {
                row("PROTEIN", day.totals.protein, day.targets.protein, Theme.protein)
                row("CARBS", day.totals.carbs, day.targets.carbs, Theme.carbs)
                row("FAT", day.totals.fat, day.targets.fat, Theme.fat)
                row("FIBER", day.totals.fiber, day.targets.fiber, Theme.fiber)
            }
            if !known {
                Text("Some foods have unknown macros. These are known-only subtotals.").font(.caption.bold())
                if let note = day.macroNote { Text(note).font(.caption) }
            }
            if let warning = day.warning { Text(warning).font(.caption) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("pixel-macro-summary")
    }

    private func row(_ label: String, _ value: Double, _ target: Double, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(Theme.pixelFont(16)).foregroundStyle(color)
                Spacer()
                Text(target > 0 ? "\(grams(value))\(known ? "" : "+") / \(grams(target))" : "\(grams(value))\(known ? "" : "+")")
                    .font(Theme.pixelFont(16))
            }
            PixelBar(value: value, target: target, color: color)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(target > 0 ? "\(label.capitalized) \(grams(value)) of \(grams(target))" : "\(label.capitalized) \(grams(value)), no target")
    }

    private func whole(_ value: Double) -> String { Int(value.rounded()).formatted() }
    private func grams(_ value: Double) -> String { "\(value.formatted(.number.precision(.fractionLength(0...1))))G" }
}

struct PixelPresetGrid: View {
    let presets: [Preset]
    let busy: Bool
    let logged: String?
    let quickLog: (Preset) -> Void
    let customize: (Preset) -> Void
    private let columns = [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionLabel(text: "Saved meals")
                Spacer()
                if let logged { Text("+ \(logged)").font(Theme.pixelFont(14)).foregroundStyle(Theme.accent).lineLimit(1).accessibilityIdentifier("preset-logged") }
            }
            if presets.isEmpty {
                EmptyState(icon: "square.grid.2x2", title: "No saved meals yet", message: "Saved meals appear here. One tap logs one serving.")
            } else {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(presets) { item in
                        Button { quickLog(item) } label: { tile(item) }
                            .buttonStyle(PixelTileButtonStyle())
                            .disabled(busy)
                            .contextMenu { Button("Servings or meal…", systemImage: "slider.horizontal.3") { customize(item) } }
                            .accessibilityIdentifier("preset-\(item.name)")
                            .accessibilityLabel("Log \(item.name), \(Int(item.calories.rounded())) calories")
                            .accessibilityHint("Logs one serving. Touch and hold to change servings or meal.")
                    }
                }
                Text("Tap to log 1 serving · hold for servings or meal").font(.caption2).foregroundStyle(Theme.muted)
            }
        }
    }

    private func tile(_ item: Preset) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Text(item.emoji).font(.title2)
                Spacer()
                Text("+").font(Theme.pixelFont(22)).foregroundStyle(.white)
                    .frame(width: 24, height: 24).background(Theme.accent, in: PixelPanelShape())
            }
            Text(item.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                .lineLimit(2).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
            Text("\(Int(item.calories.rounded())) KCAL · \(Int(item.protein.rounded()))G P").font(Theme.pixelFont(15)).foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
        .padding(12)
    }
}

/// A cream pixel tile that presses down one step, like an arcade button.
struct PixelTileButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(PixelPanelShape().fill(configuration.isPressed ? Theme.accentTint : Theme.surface)
                .shadow(color: Theme.accentEdge.opacity(0.35), radius: 0, y: configuration.isPressed ? 1 : 3))
            .overlay(PixelPanelShape().stroke(Theme.edgeStrong, lineWidth: 1))
            .offset(y: configuration.isPressed ? 2 : 0)
    }
}
