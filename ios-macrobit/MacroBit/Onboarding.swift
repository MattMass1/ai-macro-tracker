import SwiftUI

struct ProfileAnswers: Codable, Equatable {
    enum Sex: String, Codable, CaseIterable, Identifiable { case male, female; var id: String { rawValue } }
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
    @State private var feet = ""
    @State private var inches = ""
    @State private var pounds = ""
    @State private var age = ""
    @State private var sex: ProfileAnswers.Sex = .male
    @State private var activity: ProfileAnswers.Activity = .moderate
    @State private var goal: ProfileAnswers.Goal = .maintain

    private var answers: ProfileAnswers? {
        guard let f = Int(feet), let i = Double(inches.isEmpty ? "0" : inches), let lb = Double(pounds), let a = Int(age) else { return nil }
        let value = ProfileAnswers.fromUS(feet: f, inches: i, pounds: lb, age: a, sex: sex, activity: activity, goal: goal)
        return value.isInRange ? value : nil
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Height") {
                    HStack {
                        TextField("Feet", text: $feet).keyboardType(.numberPad).accessibilityIdentifier("setup-feet")
                        TextField("Inches", text: $inches).keyboardType(.decimalPad).accessibilityIdentifier("setup-inches")
                    }
                }
                Section("Weight") { TextField("Pounds", text: $pounds).keyboardType(.decimalPad).accessibilityIdentifier("setup-pounds") }
                Section("Age") { TextField("Age", text: $age).keyboardType(.numberPad).accessibilityIdentifier("setup-age") }
                Section("Sex") {
                    Picker("Sex", selection: $sex) { ForEach(ProfileAnswers.Sex.allCases) { Text($0.rawValue.capitalized).tag($0) } }.pickerStyle(.segmented)
                }
                Section("Activity") {
                    Picker("Activity", selection: $activity) { ForEach(ProfileAnswers.Activity.allCases) { Text($0.label).tag($0) } }
                }
                Section("Goal") {
                    Picker("Goal", selection: $goal) { ForEach(ProfileAnswers.Goal.allCases) { Text($0.label).tag($0) } }.pickerStyle(.segmented)
                }
                if let error = store.errorMessage { Text(error).foregroundStyle(Theme.danger) }
                Button(store.isMutating ? "Saving..." : "Set my targets") {
                    if let answers { Task { _ = await store.saveProfile(answers) } }
                }
                .disabled(answers == nil || store.isMutating)
                .accessibilityIdentifier("setup-save")
            }
            .navigationTitle("Set up your targets")
        }
    }
}
