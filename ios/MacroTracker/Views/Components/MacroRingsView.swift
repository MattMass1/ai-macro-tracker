import SwiftUI

struct MacroRing: View {
    let title: String
    let consumed: Double
    let target: Double
    let remaining: Double
    let color: Color
    var large = false

    private var progress: Double { target > 0 ? min(max(consumed / target, 0), 1) : 0 }
    private var over: Bool { remaining < 0 }
    var body: some View {
        VStack(spacing: large ? 10 : 7) {
            ZStack {
                Circle().stroke(Color.secondary.opacity(0.12), lineWidth: large ? 13 : 8)
                Circle().trim(from: 0, to: progress).stroke(over ? Theme.danger : color, style: StrokeStyle(lineWidth: large ? 13 : 8, lineCap: .round)).rotationEffect(.degrees(-90)).animation(.spring(response: 0.7, dampingFraction: 0.78), value: progress)
                VStack(spacing: 1) {
                    Text(format(remaining)).font(.system(size: large ? 35 : 18, weight: .bold, design: .rounded)).monospacedDigit().foregroundStyle(over ? Theme.danger : .primary)
                    Text(over ? "OVER" : "LEFT").font(.system(size: 8, weight: .bold)).tracking(1.2).foregroundStyle(.secondary)
                }
            }.frame(width: large ? 136 : 72, height: large ? 136 : 72)
            VStack(spacing: 2) {
                Text(title.uppercased()).font(.system(size: large ? 12 : 10, weight: .bold)).tracking(1.2).foregroundStyle(large ? color : .secondary)
                Text("\(format(consumed)) / \(format(target))\(title == "Calories" ? "" : "g")").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            }
        }.accessibilityElement(children: .ignore).accessibilityLabel("\(title), \(format(consumed)) of \(format(target)), \(format(abs(remaining))) \(over ? "over" : "remaining")")
    }
    private func format(_ number: Double) -> String { number.formatted(.number.precision(.fractionLength(number.rounded() == number ? 0 : 1))) }
}

struct MacroRingsView: View {
    let totals: MacroTotals; let targets: MacroTotals; let remaining: MacroTotals
    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 22) {
                MacroRing(title: "Protein", consumed: totals.protein, target: targets.protein, remaining: remaining.protein, color: Theme.protein, large: true)
                VStack(alignment: .leading, spacing: 7) {
                    Text("PROTEIN").font(.caption.weight(.bold)).tracking(1.8).foregroundStyle(Theme.protein)
                    Text("\(totals.protein.formatted(.number.precision(.fractionLength(0))))").font(.system(size: 38, weight: .bold, design: .rounded)).monospacedDigit() + Text(" / \(targets.protein.formatted(.number.precision(.fractionLength(0))))g").font(.headline).foregroundStyle(.secondary)
                    Text(remaining.protein < 0 ? "Past today’s target" : "Keep the next meal protein-forward.").font(.subheadline).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.appCard(padding: 18)
            HStack(spacing: 8) {
                MacroRing(title: "Calories", consumed: totals.calories, target: targets.calories, remaining: remaining.calories, color: Theme.calories)
                Spacer(minLength: 0)
                MacroRing(title: "Carbs", consumed: totals.carbs, target: targets.carbs, remaining: remaining.carbs, color: Theme.carbs)
                Spacer(minLength: 0)
                MacroRing(title: "Fat", consumed: totals.fat, target: targets.fat, remaining: remaining.fat, color: Theme.fat)
                Spacer(minLength: 0)
                MacroRing(title: "Fiber", consumed: totals.fiber, target: targets.fiber, remaining: remaining.fiber, color: Theme.fiber)
            }.padding(.horizontal, 4)
        }
    }
}

