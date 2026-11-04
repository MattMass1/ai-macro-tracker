import SwiftUI

/// The Today home from the design package: today's workout and food cards on
/// the studio wall, the chosen coach standing on the floor, and the Ask Coach
/// dock. Every control here is a real action; nothing is simulated.
struct HomeView: View {
    @EnvironmentObject private var store: AppStore
    /// Switches to the Coach tab, optionally sending a message first.
    let openCoach: (String?) -> Void

    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true
    @State private var sessionSelection: WorkoutPlanPayload.PlannedDay?
    @State private var showFoodDay = false
    @State private var showLogMeal = false

    private var character: CompanionCharacter { CompanionCharacter(rawValue: characterRaw) ?? .male }

    private var todaySession: WorkoutPlanPayload.PlannedDay? {
        guard store.isToday, let plan = store.plan, plan.hasPlan == true else { return nil }
        return plan.upcoming.first
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .bottom) {
                StudioBackground(alignment: .bottom)
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        header
                        workoutCard
                        foodCard
                        Spacer(minLength: 8)
                        floor
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                    .frame(maxWidth: .infinity)
                    .containerRelativeFrame(.vertical, alignment: .top) { height, _ in height }
                }
                .scrollBounceBehavior(.basedOnSize)
            }
            CompanionDock(chat: store.chat) { text in openCoach(text) }
        }
        .background(Theme.canvas)
        .navigationBarHidden(true)
        .sheet(item: $sessionSelection) { session in
            WorkoutSessionView(session: session, date: store.dateString, onAskCoach: { text in
                sessionSelection = nil
                openCoach(text)
            })
        }
        .sheet(isPresented: $showFoodDay) {
            // TodayView hides the navigation bar, so the sheet brings its own header.
            NavigationStack {
                VStack(spacing: 0) {
                    HStack {
                        Text("Food today").font(.headline).foregroundStyle(Theme.ink)
                        Spacer()
                        Button("Done") { showFoodDay = false }.font(.subheadline.weight(.bold)).foregroundStyle(Theme.accent)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                    .background(Theme.dock)
                    .overlay(alignment: .bottom) { Rectangle().fill(Theme.accentEdge).frame(height: 2) }
                    TodayView(onAskCoach: { showFoodDay = false; openCoach(nil) })
                }
            }
        }
        .sheet(isPresented: $showLogMeal) { ManualFoodView() }
    }

    // MARK: - Pieces

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Your day").font(.system(size: 30, weight: .bold)).foregroundStyle(Theme.ink)
                Text("\(store.selectedDate.formatted(.dateTime.weekday(.wide))) · your training studio")
                    .font(.subheadline).foregroundStyle(Theme.muted)
            }
            Spacer()
            Menu {
                Picker("Coach", selection: $characterRaw) {
                    ForEach(CompanionCharacter.allCases) { Text($0.displayName).tag($0.rawValue) }
                }
                Toggle("Show coach artwork", isOn: $showsCompanion)
            } label: {
                Image(systemName: "person.crop.square")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .frame(width: 44, height: 44)
                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.cardRadius))
                    .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).stroke(Theme.edge))
            }
            .accessibilityLabel("Companion settings")
        }
        .padding(.top, 6)
    }

    @ViewBuilder
    private var workoutCard: some View {
        if let session = todaySession {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(session.type).font(.title2.weight(.bold)).foregroundStyle(Theme.ink)
                    Spacer()
                    Button("Change focus") { openCoach("I want to change today's workout.") }
                        .font(.subheadline.weight(.bold)).foregroundStyle(Theme.accent)
                }
                Text(session.exercises.isEmpty
                     ? "No exercises assigned yet."
                     : "\(session.exercises.count) exercises · \(session.exercises.prefix(3).map(\.name).joined(separator: ", "))")
                    .font(.subheadline).foregroundStyle(Theme.muted).lineLimit(2)
                Button {
                    sessionSelection = session
                } label: {
                    Label("View workout", systemImage: "arrow.right").labelStyle(.titleAndIcon)
                }
                .buttonStyle(PixelPrimaryButtonStyle())
                .disabled(session.exercises.isEmpty)
            }
            .appCard(padding: 15)
        } else if store.isLoadingWorkouts && store.plan == nil {
            RoundedRectangle(cornerRadius: Theme.cardRadius).fill(Theme.surface).frame(height: 132)
                .redacted(reason: .placeholder)
        } else {
            VStack(alignment: .leading, spacing: 12) {
                Text(store.isToday ? "No session planned" : "Earlier day").font(.title3.weight(.bold)).foregroundStyle(Theme.ink)
                Text(store.isToday ? "Ask Coach to set up a routine, or log a workout from the Workouts tab."
                                   : "Today's session is only shown for the current day.")
                    .font(.subheadline).foregroundStyle(Theme.muted)
                if store.isToday {
                    Button("Plan with Coach") { openCoach("Help me set up a workout routine.") }
                        .buttonStyle(PixelSecondaryButtonStyle())
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .appCard(padding: 15)
        }
    }

    private var foodCard: some View {
        HStack(spacing: 12) {
            Button { showFoodDay = true } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Food today").font(.headline).foregroundStyle(Theme.ink)
                    Text(foodSummary).font(.subheadline).foregroundStyle(Theme.muted).lineLimit(2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens today's meals and macros")
            Button("Log meal") { showLogMeal = true }
                .buttonStyle(PixelSecondaryButtonStyle())
        }
        .appCard(padding: 15)
    }

    private var foodSummary: String {
        guard let day = store.day else { return "Log your next meal." }
        if day.meals.isEmpty { return "Nothing logged yet. Log your next meal." }
        let left = Int(day.remaining.calories.rounded())
        return left > 0 ? "\(day.meals.count) logged · \(left) kcal left" : "\(day.meals.count) logged · target reached"
    }

    @ViewBuilder
    private var floor: some View {
        if showsCompanion {
            CompanionStage(chat: store.chat, character: character)
                .frame(maxWidth: .infinity)
                .padding(.bottom, 4)
        } else {
            Color.clear.frame(height: 24)
        }
    }
}

/// The coach on the studio floor. Observes the chat store so the pen moves
/// only while a request is actually pending.
struct CompanionStage: View {
    @ObservedObject var chat: CoachChatStore
    let character: CompanionCharacter
    var size: CGFloat = 224

    var body: some View {
        CompanionView(character: character, state: chat.isResponding ? .writing : .rest)
            .frame(width: size, height: size)
    }
}

/// "YOUR COMPANION" dock: the Ask Coach composer on the cream base.
struct CompanionDock: View {
    @ObservedObject var chat: CoachChatStore
    let onSend: (String) -> Void
    @State private var draft = ""
    @FocusState private var focused: Bool

    private var trimmed: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(spacing: 8) {
            HStack {
                SectionLabel(text: "Your companion")
                Spacer()
                SectionLabel(text: chat.isResponding ? "Writing" : "Ready")
            }
            HStack(spacing: 6) {
                Text("Ask Coach").font(.subheadline.weight(.bold)).foregroundStyle(Theme.sectionInk).padding(.leading, 8)
                TextField("Ask, log, or adjust", text: $draft)
                    .font(.subheadline)
                    .focused($focused)
                    .submitLabel(.send)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up").fontWeight(.bold)
                        .frame(width: 42, height: 42)
                        .background(trimmed.isEmpty ? Theme.edge : Theme.accent, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
                        .foregroundStyle(.white)
                }
                .disabled(trimmed.isEmpty)
                .accessibilityLabel("Send to Coach")
            }
            .padding(3)
            .background(Theme.paper, in: RoundedRectangle(cornerRadius: Theme.controlRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).stroke(Theme.edgeStrong, lineWidth: 2))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Theme.dock)
        .overlay(alignment: .top) { Rectangle().fill(Theme.accentEdge).frame(height: 3) }
    }

    private func send() {
        guard !trimmed.isEmpty else { return }
        let text = trimmed
        draft = ""
        focused = false
        onSend(text)
    }
}
