import SwiftUI

struct ProfileAnswers: Codable, Equatable {
    enum Sex: String, Codable, CaseIterable, Identifiable {
        case male, female
        var id: String { rawValue }
        var label: String { rawValue.capitalized }
    }
    enum Activity: String, Codable, CaseIterable, Identifiable {
        case sedentary, light, moderate, active, veryActive = "very_active"
        var id: String { rawValue }
        var label: String {
            switch self {
            case .sedentary: return "Mostly sitting"
            case .light: return "Light, 1-3 workouts a week"
            case .moderate: return "Moderate, 3-5 a week"
            case .active: return "Active, 6-7 a week"
            case .veryActive: return "Very active, hard training or a physical job"
            }
        }
    }
    enum Goal: String, Codable, CaseIterable, Identifiable {
        case lose, maintain, gain
        var id: String { rawValue }
        var label: String { switch self { case .lose: return "Lose weight"; case .maintain: return "Maintain"; case .gain: return "Gain muscle" } }
    }
    var heightCm: Double
    var weightKg: Double
    var age: Int
    var sex: Sex
    var activity: Activity
    var goal: Goal

    static func fromUS(feet: Int, inches: Double, pounds: Double, age: Int, sex: Sex, activity: Activity, goal: Goal) -> ProfileAnswers {
        ProfileAnswers(heightCm: ((Double(feet) * 12 + inches) * 2.54 * 10).rounded() / 10, weightKg: (pounds * 0.45359237 * 10).rounded() / 10,
                       age: age, sex: sex, activity: activity, goal: goal)
    }
    var isInRange: Bool { (100...250).contains(heightCm) && (30...300).contains(weightKg) && (13...100).contains(age) }
}

struct ProfileTargets: Codable, Equatable {
    var calories: Double
    var protein: Double
    var carbs: Double
    var fat: Double
    var fiber: Double
}

struct ProfilePayload: Codable, Equatable {
    var needsSetup: Bool
    var profile: ProfileAnswers?
    var targets: ProfileTargets?
}

struct OnboardingView: View {
    @EnvironmentObject private var store: AppStore
    @FocusState private var focused: String?
    @State private var step = 0
    @State private var feet = ""
    @State private var inches = ""
    @State private var pounds = ""
    @State private var age = ""
    @State private var sex: ProfileAnswers.Sex = .male
    @State private var activity: ProfileAnswers.Activity = .moderate
    @State private var goal: ProfileAnswers.Goal = .maintain
    @State private var stepError: String?

    private var answers: ProfileAnswers? {
        guard let f = Int(feet), let i = Double(inches.isEmpty ? "0" : inches), let lb = Double(pounds), let a = Int(age) else { return nil }
        let value = ProfileAnswers.fromUS(feet: f, inches: i, pounds: lb, age: a, sex: sex, activity: activity, goal: goal)
        return value.isInRange ? value : nil
    }

    var body: some View {
        ScrollViewReader { reader in
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Color.clear.frame(height: 0).id("setup-top")
                if step == 0 { PixelSelectedArtwork() }
                Text(step == 0 ? "Make it yours" : "Your plan")
                    .font(Theme.pixelFont(34, relativeTo: .largeTitle))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8).background(Theme.profileCanvas.opacity(0.9), in: PixelPanelShape())
                    .accessibilityAddTraits(.isHeader)
                Text(step == 0 ? "A few numbers so Macro-Bit can set your daily targets." : "Pick how active you are and what you're going for. We'll work out your calories and macros.")
                    .font(.body).foregroundStyle(Theme.profileMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8).background(Theme.profileCanvas.opacity(0.9), in: PixelPanelShape())
                PixelStepProgress(titles: ["About you", "Your plan"], current: step)
                if step == 0 { aboutYou } else { plan }
                if let message = stepError ?? store.errorMessage {
                    PixelStatus(state: .error(message)).accessibilityIdentifier("setup-error")
                }
            }.padding(20)
        }
        .onChange(of: step) { _, _ in reader.scrollTo("setup-top", anchor: .top) }
        }
        .scrollDismissesKeyboard(.interactively)
        .background { PixelSceneBackground() }
        .safeAreaInset(edge: .bottom, spacing: 0) { if focused == nil { action } }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button { focused = nil } label: { Text("Done").frame(minWidth: 44, minHeight: 44) }
            }
        }
        .foregroundStyle(Theme.profileInk).tint(Theme.profileAccent).preferredColorScheme(.dark)
        .disabled(store.isMutating)
    }

    private var aboutYou: some View {
        PixelPanel(title: "About you", glyph: .leaf) {
            HStack(alignment: .top, spacing: 12) {
                PixelValueField(label: "Height (feet)", text: $feet, id: "setup-feet", unit: "ft", focus: $focused)
                PixelValueField(label: "Inches", text: $inches, id: "setup-inches", unit: "in", required: false, focus: $focused)
            }
            PixelValueField(label: "Weight", text: $pounds, id: "setup-pounds", unit: "lb", focus: $focused)
            PixelValueField(label: "Age", text: $age, id: "setup-age", unit: "years", focus: $focused)
            VStack(alignment: .leading, spacing: 8) {
                Text("Sex").font(.subheadline.weight(.semibold))
                HStack(spacing: 12) {
                    ForEach(ProfileAnswers.Sex.allCases) { PixelChoice(title: $0.label, value: $0, selection: $sex, id: "setup-sex-\($0.rawValue)") }
                }
                Text("Used only for the calorie formula.").font(.footnote).foregroundStyle(Theme.profileMuted)
            }
        }
    }

    private var plan: some View {
        VStack(alignment: .leading, spacing: 24) {
            PixelPanel(title: "How active are you?", glyph: .leaf) {
                ForEach(ProfileAnswers.Activity.allCases) { PixelChoice(title: $0.label, value: $0, selection: $activity, id: "setup-activity-\($0.rawValue)") }
            }
            PixelPanel(title: "Your goal") {
                ForEach(ProfileAnswers.Goal.allCases) { PixelChoice(title: $0.label, value: $0, selection: $goal, id: "setup-goal-\($0.rawValue)") }
            }
            Text("These are starting targets, not medical advice. You can change them later.")
                .font(.footnote).foregroundStyle(Theme.profileMuted).fixedSize(horizontal: false, vertical: true)
                .padding(12).background(Theme.profileCanvas, in: PixelPanelShape())
        }
    }

    private var action: some View {
        VStack(spacing: 8) {
            if step == 0 {
                PixelAction(title: "Continue") {
                    focused = nil
                    if answers == nil { stepError = "Enter your height, weight and age. Age must be 13 to 100." }
                    else { stepError = nil; withAnimation { step = 1 } }
                }.accessibilityIdentifier("setup-next")
            } else {
                PixelAction(title: "Set my targets", state: .profile(saving: store.isMutating, error: nil, complete: false), busyTitle: "Saving…") {
                    if let answers { Task { _ = await store.saveProfile(answers) } }
                }.accessibilityIdentifier("setup-save")
                PixelAction(title: "Back", secondary: true, glyph: nil) { withAnimation { step = 0 } }
                    .accessibilityIdentifier("setup-back")
            }
        }.padding(.horizontal, 20).padding(.vertical, 12).background(Theme.profileCanvas.opacity(0.96))
    }
}
