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
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if store.isLoading { ProgressView("Refreshing. The server may take about a minute.") }
                if let error = store.errorMessage {
                    VStack(alignment: .leading) { Text(error).foregroundStyle(Theme.danger); Button("Try again") { Task { await store.refreshToday() } } }.appCard()
                }
                if let day = store.day {
                    Text(store.effectiveDate ?? day.date).font(.headline)
                    VStack(alignment: .leading, spacing: 12) {
                        SectionLabel(text: "Calories today")
                        Text("\(day.totals.calories.formatted()) kcal").font(.largeTitle.bold())
                        Text(day.hasTargets == true ? "Target \(day.targets.calories.formatted()) kcal" : "Reference target \(day.targets.calories.formatted()) kcal, not a configured goal").font(.caption).foregroundStyle(Theme.muted)
                        if day.nutrientsComplete {
                            Text("Protein \(nutrientText(day.totals.protein)) · Carbs \(nutrientText(day.totals.carbs))")
                            Text("Fat \(nutrientText(day.totals.fat)) · Fiber \(nutrientText(day.totals.fiber))")
                            Text("Targets: P \(nutrientText(day.targets.protein)), C \(nutrientText(day.targets.carbs)), F \(nutrientText(day.targets.fat)), fiber \(nutrientText(day.targets.fiber))").font(.caption)
                        } else {
                            Text("Nutrients incomplete. Totals are known-only subtotals, not exact totals.").font(.subheadline.bold())
                            if let note = day.macroNote { Text(note) }
                        }
                        if let warning = day.warning { Text(warning).font(.caption) }
                    }.frame(maxWidth: .infinity, alignment: .leading).appCard()
                    HStack { SectionLabel(text: "Meals"); Spacer(); Button("Manual entry") { manual = true }.buttonStyle(PixelSecondaryButtonStyle()) }
                    if day.meals.isEmpty { EmptyState(icon: "fork.knife", title: "A fresh log", message: "Start in the Studio chat, choose a preset or enter a meal.") }
                    ForEach(day.meals) { entry in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(entry.name).font(.headline)
                                Text("\(entry.meal) · \(entry.calories.formatted()) kcal").font(.subheadline)
                                Text("P \(nutrientText(entry.protein)) · C \(nutrientText(entry.carbs)) · F \(nutrientText(entry.fat)) · Fiber \(nutrientText(entry.fiber))").font(.caption)
                                if let source = entry.macroSource { Text(source).font(.caption2).foregroundStyle(Theme.muted) }
                            }
                            Spacer()
                            Button { deleting = entry } label: { Image(systemName: "trash").frame(width: 44, height: 44) }.accessibilityLabel("Delete \(entry.name)").disabled(store.isMutating)
                        }.appCard()
                    }
                    SectionLabel(text: "Presets")
                    if store.presets.isEmpty { Text("No presets for this account.").foregroundStyle(Theme.muted) }
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())]) {
                        ForEach(store.presets) { item in
                            Button { preset = item; servings = "1"; meal = item.meal } label: {
                                VStack(alignment: .leading) { Text(item.name).font(.headline); Text("\(item.calories.formatted()) kcal").font(.caption) }.frame(maxWidth: .infinity, alignment: .leading).appCard()
                            }.buttonStyle(.plain)
                        }
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
