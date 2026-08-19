import SwiftUI

struct DayPicker: View {
    let date: Date; let canGoForward: Bool; var isEnabled: Bool = true; let onMove: (Int) -> Void
    var body: some View {
        HStack {
            Button { onMove(-1) } label: { Image(systemName: "chevron.left").frame(width: 40, height: 40).background(Theme.surface, in: Circle()) }.disabled(!isEnabled).opacity(isEnabled ? 1 : 0.3).accessibilityLabel("Previous day")
            Spacer()
            VStack(spacing: 2) {
                Text(Calendar.current.isDateInToday(date) ? "TODAY" : date.formatted(.dateTime.weekday(.wide).day().month())).font(.caption.weight(.bold)).tracking(1.7)
                Text(date.formatted(date: .long, time: .omitted)).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button { onMove(1) } label: { Image(systemName: "chevron.right").frame(width: 40, height: 40).background(Theme.surface, in: Circle()) }.disabled(!canGoForward || !isEnabled).opacity(canGoForward && isEnabled ? 1 : 0.3).accessibilityLabel("Next day")
        }
    }
}

struct PresetGridView: View {
    let presets: [Preset]; let action: (Preset) -> Void
    private let columns = [GridItem(.flexible()), GridItem(.flexible())]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Quick add")
            if presets.isEmpty { EmptyState(icon: "square.grid.2x2", title: "No quick adds yet", message: "Add presets in your Meal Presets database and they’ll appear here.") }
            else {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(presets) { preset in
                        Button { action(preset) } label: {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack { Text(preset.emoji).font(.title2); Spacer(); Text("\(Int(preset.calories))").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                                VStack(alignment: .leading, spacing: 3) { Text(preset.name).font(.subheadline.weight(.semibold)).lineLimit(2); Text("\(Int(preset.protein))g protein").font(.caption.weight(.medium)).foregroundStyle(Theme.protein) }
                            }.frame(maxWidth: .infinity, minHeight: 76, alignment: .leading).appCard(padding: 14)
                        }.buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

struct MealListView: View {
    let meals: [FoodEntry]; let onDelete: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Logged")
            if meals.isEmpty { EmptyState(icon: "fork.knife", title: "Nothing logged", message: "Tap a preset or describe what you ate in the Log tab.") }
            else {
                ForEach(meals) { meal in
                    HStack(alignment: .top, spacing: 12) {
                        RoundedRectangle(cornerRadius: 3).fill(mealColor(meal.meal)).frame(width: 5)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(meal.name).font(.subheadline.weight(.semibold)).lineLimit(2)
                            Text("\(Int(meal.calories)) kcal  ·  \(Int(meal.protein))p  ·  \(Int(meal.carbs))c  ·  \(Int(meal.fat))f").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            Text(meal.meal.uppercased()).font(.caption2.weight(.bold)).tracking(1.1).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Menu { Button("Delete entry", systemImage: "trash", role: .destructive) { onDelete(meal.id) } } label: { Image(systemName: "ellipsis").frame(width: 36, height: 36).contentShape(Rectangle()) }
                    }.appCard(padding: 13).swipeActions { Button("Delete", systemImage: "trash", role: .destructive) { onDelete(meal.id) } }
                }
            }
        }
    }
    private func mealColor(_ meal: String) -> Color { switch meal.lowercased() { case "breakfast": Theme.calories; case "lunch": Theme.carbs; case "dinner": Theme.protein; default: Theme.fiber } }
}

