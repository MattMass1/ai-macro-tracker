import SwiftUI

struct MacroRingsView: View {
    let totals: MacroTotals
    let targets: MacroTotals
    let remaining: MacroTotals

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Daily Macros", systemImage: "chart.bar.fill")
                .font(.subheadline.weight(.bold))
                .foregroundStyle(Theme.sectionInk)
            CalorieRing(consumed: totals.calories, target: targets.calories, remaining: remaining.calories)
                .frame(maxWidth: .infinity)
            HStack(spacing: 8) {
                MacroBar(title: "Protein", consumed: totals.protein, target: targets.protein, color: Theme.protein)
                MacroBar(title: "Carbs", consumed: totals.carbs, target: targets.carbs, color: Theme.carbs)
                MacroBar(title: "Fat", consumed: totals.fat, target: targets.fat, color: Theme.fat)
                MacroBar(title: "Fiber", consumed: totals.fiber, target: targets.fiber, color: Theme.fiber)
            }
        }.appCard(padding: 18)
    }
}

private struct CalorieRing: View {
    let consumed: Double
    let target: Double
    let remaining: Double

    private var progress: Double { target > 0 ? min(max(consumed / target, 0), 1) : 0 }

    var body: some View {
        ZStack {
            Circle().stroke(Theme.divider, lineWidth: 13)
            Circle()
                .trim(from: 0, to: progress)
                .stroke(
                    AngularGradient(colors: [Theme.accentDark, Theme.accent, Theme.accentDark], center: .center),
                    style: StrokeStyle(lineWidth: 13, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .animation(.spring(response: 0.7, dampingFraction: 0.78), value: progress)
            VStack(spacing: 2) {
                Text(format(consumed)).font(.system(size: 24, weight: .bold)).monospacedDigit().foregroundStyle(Theme.ink)
                Text("of \(format(target)) cal").font(.caption2.weight(.semibold)).foregroundStyle(Theme.muted)
            }
        }
        .frame(width: 136, height: 136)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Calories, \(format(consumed)) of \(format(target)), \(format(abs(remaining))) \(remaining < 0 ? "over" : "remaining")")
    }

    private func format(_ number: Double) -> String { Int(number.rounded()).formatted() }
}

private struct MacroBar: View {
    let title: String
    let consumed: Double
    let target: Double
    let color: Color

    private var progress: CGFloat {
        guard target > 0 else { return 0 }
        return CGFloat(min(max(consumed / target, 0), 1))
    }

    var body: some View {
        VStack(spacing: 5) {
            Text("\(Int(consumed.rounded()))g").font(.subheadline.weight(.bold)).monospacedDigit().foregroundStyle(color)
            Text(title.uppercased()).font(.system(size: 9, weight: .semibold)).tracking(0.4).foregroundStyle(Theme.muted).lineLimit(1).minimumScaleFactor(0.8)
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.divider)
                    Capsule().fill(color).frame(width: geometry.size.width * progress)
                }
            }.frame(height: 4)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 5).padding(.vertical, 9)
        .background(Theme.canvas, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), \(Int(consumed.rounded())) of \(Int(target.rounded())) grams")
    }
}
