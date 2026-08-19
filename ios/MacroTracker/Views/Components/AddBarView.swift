import SwiftUI

struct AddBarView: View {
    let selectedTab: Int
    let onLogMeal: () -> Void
    let onLogWorkout: () -> Void
    let onScanFood: () -> Void
    let onAskCoach: () -> Void

    @Namespace private var morphNamespace
    @Environment(\.accessibilityReduceMotion) private var accessibilityReduceMotion
    @State private var isExpanded = false
    @State private var isPulsing = false

    private let spring = Animation.spring(response: 0.42, dampingFraction: 0.82)

    var body: some View {
        ZStack(alignment: .bottom) {
            if isExpanded {
                Color.clear
                    .contentShape(Rectangle())
                    .ignoresSafeArea()
                    .onTapGesture { isExpanded = false }

                expandedBar
                    .matchedGeometryEffect(id: "addBarShape", in: morphNamespace)
            } else {
                collapsedButton
                    .matchedGeometryEffect(id: "addBarShape", in: morphNamespace)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        .animation(spring, value: isExpanded)
        .onChange(of: isExpanded) { _, expanded in
            if expanded {
                isPulsing = false
            } else {
                restartPulse()
            }
        }
        .onChange(of: selectedTab) { _, _ in isExpanded = false }
        .onDisappear { isPulsing = false }
    }

    private var collapsedButton: some View {
        Button {
            isExpanded = true
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(Theme.accent)
                .matchedGeometryEffect(id: "addBarSymbol", in: morphNamespace)
                .frame(width: 60, height: 60)
                .background(.ultraThinMaterial, in: Circle())
                .overlay { Circle().stroke(Theme.divider.opacity(0.9), lineWidth: 1) }
                .background {
                    Circle()
                        .stroke(Theme.accent.opacity(0.12), lineWidth: 7)
                        .scaleEffect(accessibilityReduceMotion ? 1 : (isPulsing ? 1.06 : 1))
                        .opacity(accessibilityReduceMotion ? 0.75 : (isPulsing ? 0.45 : 0.75))
                        .animation(
                            accessibilityReduceMotion || !isPulsing
                                ? nil
                                : .easeInOut(duration: 2).repeatForever(autoreverses: true),
                            value: isPulsing
                        )
                }
                .shadow(color: Theme.ink.opacity(0.12), radius: 14, y: 6)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Add")
        .accessibilityHint("Shows quick actions for meals, workouts, food scanning, and Coach")
        .onAppear { restartPulse() }
        .onDisappear { isPulsing = false }
        .onChange(of: accessibilityReduceMotion) { _, _ in restartPulse() }
    }

    private var expandedBar: some View {
        HStack(spacing: 0) {
            Button {
                isExpanded = false
            } label: {
                Image(systemName: "plus")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .rotationEffect(.degrees(45))
                    .matchedGeometryEffect(id: "addBarSymbol", in: morphNamespace)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Close quick actions")

            quickAction("Log Meal", systemImage: "fork.knife", action: onLogMeal)
            quickAction("Log Workout", systemImage: "dumbbell", action: onLogWorkout)
            quickAction("Scan Food", systemImage: "camera", action: onScanFood)
            quickAction("Ask Coach", systemImage: "message", action: onAskCoach)
        }
        .frame(width: 320, height: 76)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .stroke(Theme.divider.opacity(0.9), lineWidth: 1)
                .allowsHitTesting(false)
        }
        .contentShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .shadow(color: Theme.ink.opacity(0.14), radius: 18, y: 7)
        .accessibilityElement(children: .contain)
        .accessibilityHint("Double tap an action. The add bar closes after selection.")
    }

    private func quickAction(_ title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button {
            action()
            isExpanded = false
        } label: {
            VStack(spacing: 5) {
                Image(systemName: systemImage)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                Text(title)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }

    private func restartPulse() {
        isPulsing = false
        guard !accessibilityReduceMotion, !isExpanded else { return }
        DispatchQueue.main.async {
            guard !accessibilityReduceMotion, !isExpanded else { return }
            isPulsing = true
        }
    }
}
