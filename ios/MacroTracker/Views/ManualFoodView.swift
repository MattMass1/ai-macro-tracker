import SwiftUI

/// Label-value manual entry. Reached from the Canvas menu; it writes through the
/// existing `AppStore.logMeal` path and never through a second coach surface.
struct ManualFoodView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""; @State private var meal = "Snack"; @State private var calories = ""; @State private var protein = ""; @State private var carbs = ""; @State private var fat = ""; @State private var fiber = ""
    @FocusState private var isInputFocused: Bool
    let meals = ["Breakfast", "Lunch", "Dinner", "Snack"]
    var body: some View {
        NavigationStack {
            Form {
                Section("Food") { TextField("Name", text: $name).focused($isInputFocused); Picker("Meal", selection: $meal) { ForEach(meals, id: \.self) { Text($0) } } }
                Section("Macros") { numberField("Calories", $calories); numberField("Protein (g)", $protein); numberField("Carbs (g)", $carbs); numberField("Fat (g)", $fat); numberField("Fiber (g)", $fiber) }
                Section { Text("Enter the label values as written. You can delete the entry from Meals if anything needs correcting.").font(.caption).foregroundStyle(.secondary) }
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("Manual entry")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Log") { submit() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty) }
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { isInputFocused = false }
                }
            }
        }.presentationDetents([.medium, .large])
    }
    private func numberField(_ title: String, _ value: Binding<String>) -> some View { TextField(title, text: value).keyboardType(.decimalPad).focused($isInputFocused) }
    private func submit() { isInputFocused = false; Task { let body = LogMealBody(name: name, calories: Double(calories) ?? 0, protein: Double(protein) ?? 0, carbs: Double(carbs) ?? 0, fat: Double(fat) ?? 0, fiber: Double(fiber) ?? 0, macroSource: "Manual iOS entry", meal: meal, day: store.isToday ? nil : store.dateString); if await store.logMeal(body) { dismiss() } } }
}
