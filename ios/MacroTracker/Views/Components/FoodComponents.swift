import SwiftUI

struct DayPicker: View {
    let date: Date
    let canGoForward: Bool
    var isEnabled: Bool = true
    let onMove: (Int) -> Void

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(date.formatted(.dateTime.weekday(.wide).month(.wide).day()).uppercased())
                    .font(.caption2.weight(.semibold)).tracking(0.7).foregroundStyle(Theme.muted)
                Text(Calendar.current.isDateInToday(date) ? "Today" : date.formatted(.dateTime.month().day()))
                    .font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
            }
            Spacer()
            HStack(spacing: 6) {
                dayButton("chevron.left", enabled: isEnabled) { onMove(-1) }
                dayButton("chevron.right", enabled: canGoForward && isEnabled) { onMove(1) }
            }
        }
    }

    private func dayButton(_ icon: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.caption.weight(.semibold)).foregroundStyle(Theme.sectionInk)
                .frame(width: 32, height: 32)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.divider))
        }.disabled(!enabled).opacity(enabled ? 1 : 0.35)
    }
}

struct PresetGridView: View {
    let presets: [Preset]
    let action: (Preset) -> Void
    private let columns = [GridItem(.flexible()), GridItem(.flexible())]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Quick add")
            if presets.isEmpty {
                EmptyState(icon: "square.grid.2x2", title: "No quick adds yet", message: "Add presets in your Meal Presets database and they’ll appear here.")
            } else {
                LazyVGrid(columns: columns, spacing: 10) {
                    ForEach(presets) { preset in
                        Button { action(preset) } label: {
                            VStack(alignment: .leading, spacing: 10) {
                                HStack { Text(preset.emoji).font(.title2); Spacer(); Image(systemName: "plus.circle.fill").foregroundStyle(Theme.accent) }
                                Text(preset.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink).lineLimit(2)
                                Text("\(Int(preset.calories)) cal · \(Int(preset.protein))g protein").font(.caption).foregroundStyle(Theme.muted)
                            }.frame(maxWidth: .infinity, minHeight: 76, alignment: .leading).appCard(padding: 14)
                        }.buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

struct MealListView: View {
    let meals: [FoodEntry]
    let onDelete: (String) -> Void
    let onAdd: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Label("Meals", systemImage: "fork.knife").font(.subheadline.weight(.bold)).foregroundStyle(Theme.sectionInk).padding(.bottom, 5)
            if meals.isEmpty {
                Text("Nothing logged yet.").font(.subheadline).foregroundStyle(Theme.muted).padding(.vertical, 16)
            } else {
                ForEach(Array(meals.enumerated()), id: \.element.id) { index, meal in
                    HStack(spacing: 10) {
                        Text(mealEmoji(meal.meal)).font(.body).frame(width: 34, height: 34).background(Theme.input, in: RoundedRectangle(cornerRadius: 10))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(meal.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink).lineLimit(1)
                            Text(mealSubtitle(meal)).font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
                        }
                        Spacer()
                        Text("\(Int(meal.calories))").font(.subheadline.weight(.bold)).monospacedDigit().foregroundStyle(Theme.sectionInk)
                        Menu { Button("Delete entry", systemImage: "trash", role: .destructive) { onDelete(meal.id) } } label: {
                            Image(systemName: "ellipsis").foregroundStyle(Theme.muted).frame(width: 24, height: 34)
                        }
                    }.padding(.vertical, 9)
                    if index < meals.count - 1 { Divider().overlay(Theme.divider) }
                }
            }
            Button(action: onAdd) {
                Label("Add food", systemImage: "plus").font(.caption.weight(.bold)).foregroundStyle(Theme.accent).frame(maxWidth: .infinity).padding(.vertical, 10)
                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.accent.opacity(0.25), style: StrokeStyle(lineWidth: 1.5, dash: [5])))
            }.buttonStyle(.plain).padding(.top, 6)
        }.appCard(padding: 16)
    }

    private func mealSubtitle(_ meal: FoodEntry) -> String {
        let kind = meal.meal.isEmpty ? "Meal" : meal.meal.capitalized
        guard let timestamp = meal.createdTime, !timestamp.isEmpty,
              let date = iso8601Date(from: timestamp) else { return kind }
        let formatter = DateFormatter()
        formatter.locale = .autoupdatingCurrent
        formatter.timeZone = .autoupdatingCurrent
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return "\(kind) · \(formatter.string(from: date))"
    }

    private func iso8601Date(from timestamp: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: timestamp) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: timestamp)
    }

    private func mealEmoji(_ meal: String) -> String {
        switch meal.lowercased() {
        case "breakfast": return "🥣"
        case "lunch": return "🥗"
        case "dinner": return "🍽️"
        default: return "🍎"
        }
    }
}
