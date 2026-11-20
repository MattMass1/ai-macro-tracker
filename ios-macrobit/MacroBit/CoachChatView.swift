import SwiftUI
import UIKit

// Studio composition from ios/ at 4db7c1245f979e9550dc165434a2206867cec0b2.
// Food-only actions replace unavailable workout/metrics/history routes.
struct CoachChatView: View {
    @EnvironmentObject private var store: AppStore
    var openWorkouts: () -> Void = {}
    @State private var showFoodDay = false
    @State private var showManual = false
    @State private var showSignOutConfirm = false
    @State private var showScanner = false
    @State private var scanPaused = false
    @State private var scanEvent = UUID()
    @State private var draft = ""
    @FocusState private var inputFocused: Bool
    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true
    private var character: CompanionCharacter { CompanionCharacter(rawValue: characterRaw) ?? .male }
    private var trimmedInput: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { geometry in
                let layout = StudioLayout(width: geometry.size.width, height: geometry.size.height)
                ZStack(alignment: .bottom) {
                    StudioBackground(alignment: .bottom)
                    VStack(spacing: 12) {
                        coachHeader
                        VStack(spacing: 0) {
                            HStack {
                                Text("Coach / meals")
                                    .font(Theme.pixelFont(StudioLayout.panelTitleSize)).textCase(.uppercase)
                                Spacer()
                                Button { showFoodDay = true } label: {
                                    Image(systemName: "fork.knife").frame(width: 44, height: 44)
                                }
                                .accessibilityLabel("Meals and macros")
                                .accessibilityIdentifier("studio.meals")
                            }
                            .foregroundStyle(Theme.ink)
                            .padding(.horizontal, 14)
                            .background(Theme.accentTint)
                            ScrollViewReader { proxy in
                                ScrollView {
                                    VStack(spacing: 10) {
                                        if store.messages.isEmpty { studioSummary }
                                        ForEach(store.messages) { message in
                                            VStack(alignment: .leading, spacing: 10) {
                                                if message.role == .coach, !message.surfaces.isEmpty {
                                                    ForEach(Array(message.surfaces.enumerated()), id: \.offset) { index, surface in
                                                        ChatSurfaceCard(surface: surface,
                                                            canAct: message.id == store.messages.last?.id && !store.isSending && store.retryTurn == nil && !store.conflictBlocked,
                                                            canConfirm: message.quickReplies.contains("Log it") && index == message.surfaces.firstIndex(where: { if case .mealPreview = $0 { return true }; return false }),
                                                            confirm: { draft = ""; dismissKeyboard(); Task { await store.sendQuickReply("Log it") } },
                                                            edit: { draft = "make it 2 servings"; inputFocused = true },
                                                            review: { draft = "Show my daily totals"; inputFocused = true })
                                                    }
                                                } else {
                                                    CoachBubble(message: message)
                                                }
                                                if message.role == .coach, !message.quickReplies.isEmpty {
                                                    QuickReplyRow(labels: message.quickReplies.filter { label in
                                                        label != "Log it" || !message.surfaces.contains(where: { if case .mealPreview = $0 { return true }; return false })
                                                    }, isBusy: store.isSending || store.retryTurn != nil || store.conflictBlocked) { label in
                                                        Task { await store.sendQuickReply(label) }
                                                    }
                                                }
                                            }.id(message.id)
                                        }
                                        if store.isSending { ProgressView("Writing. First connection can take about a minute.") }
                                        if store.retryTurn != nil {
                                            Button("Retry same message safely") { Task { await store.retryLastTurn() } }
                                                .buttonStyle(PixelSecondaryButtonStyle())
                                            Text("The original turn ID is reused to avoid duplicate saves.").font(.caption)
                                            Button("Send again as a new message") { Task { await store.resendAsNewTurn() } }
                                                .buttonStyle(PixelSecondaryButtonStyle())
                                        }
                                        if store.conflictBlocked {
                                            Text("Wait for the previous message to finish before continuing. It will not be resent.").font(.caption)
                                            Button("Continue without resending") { store.continueAfterConflict() }
                                                .buttonStyle(PixelSecondaryButtonStyle())
                                        }
                                        if !store.scanQueue.isEmpty { Text("Barcode queued. Waiting for the current message.").font(.caption) }
                                        quickActionChips
                                        Color.clear.frame(height: 1).id("end")
                                    }.padding(16)
                                }
                                .scrollDismissesKeyboard(.interactively)
                                .onChange(of: store.messages.count) { _, _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
                            }
                        }
                        .background(Theme.surface, in: PixelPanelShape())
                        .overlay(PixelPanelShape().stroke(Theme.accentEdge, lineWidth: 2))
                        .padding(.horizontal, 20)
                        .accessibilityIdentifier("studio.conversation")
                        if showsCompanion {
                            CompanionStage(chat: store, character: character, size: layout.companionSize)
                                .frame(maxWidth: .infinity)
                                .padding(.bottom, 8)
                                .frame(height: layout.floorHeight, alignment: .bottom)
                                .accessibilityIdentifier("studio.companion")
                        }
                    }.padding(.top, 8)
                }
            }
            composer.fixedSize(horizontal: false, vertical: true)
        }
        .background(Theme.dock)
        .navigationBarHidden(true)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("Done") { dismissKeyboard() }.fontWeight(.semibold) }
        }
        .sheet(isPresented: $showManual) { NavigationStack { ManualEntryView() } }
        .sheet(isPresented: $showFoodDay) { NavigationStack { TodayView() } }
        .sheet(isPresented: $showScanner, onDismiss: { Task { await store.drainScans() } }) {
            BarcodeScannerView(paused: $scanPaused, onCode: { code in
                store.acceptScan(code, eventID: scanEvent); showScanner = false
            }, onClose: { showScanner = false }, injectedCode: injectedScanCode)
        }
        .confirmationDialog("Sign out?", isPresented: $showSignOutConfirm, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { Task { await store.signOutEverywhere() } }
        } message: { Text("You'll need your device token to sign back in.") }
    }

    private var coachHeader: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Macro-Bit").font(Theme.pixelFont(StudioLayout.titleSize, relativeTo: .title)).foregroundStyle(Theme.sectionInk)
                Text(store.effectiveDate ?? "Your studio").font(.caption).foregroundStyle(Theme.muted)
            }
            Spacer()
            Menu {
                Picker("Coach artwork", selection: $characterRaw) { ForEach(CompanionCharacter.allCases) { Text($0.displayName).tag($0.rawValue) } }
                Toggle("Show coach artwork", isOn: $showsCompanion)
                Button("Manual food entry", systemImage: "plus") { showManual = true }
                Button("Meals and macros", systemImage: "fork.knife") { showFoodDay = true }
                Button("Sign out", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) { showSignOutConfirm = true }
            } label: {
                Image(systemName: "slider.horizontal.3").frame(width: 44, height: 44)
                    .background(Theme.dock, in: PixelPanelShape())
                    .overlay(PixelPanelShape().stroke(Theme.edgeStrong))
            }.accessibilityLabel("Coach menu")
        }.padding(.leading, 64).padding(.trailing, 24)
    }

    private var studioSummary: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What are we working on?").font(Theme.pixelFont(StudioLayout.summaryTitleSize, relativeTo: .title2))
            Text("Tell me what you ate or review your day.").font(.footnote).foregroundStyle(Theme.muted)
            if let day = store.day {
                Button { showFoodDay = true } label: {
                    HStack {
                        VStack(alignment: .leading) {
                            Text("Food today").font(Theme.pixelFont(20))
                            Text("\(day.meals.count) meals logged").font(.caption)
                        }
                        Spacer()
                        Text("\(day.totals.calories.formatted()) kcal").font(Theme.pixelFont(20))
                        Image(systemName: "chevron.right")
                    }.padding(12).background(Theme.accentTint, in: PixelPanelShape())
                }.buttonStyle(.plain)
            }
            Button(action: openWorkouts) { Label("View workout", systemImage: "dumbbell.fill") }
                .buttonStyle(PixelSecondaryButtonStyle(fullWidth: true)).accessibilityIdentifier("studio.workout")
        }.foregroundStyle(Theme.ink).frame(maxWidth: .infinity, alignment: .leading)
    }

    private var quickActionChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(["What's left today?", "How much protein today?"], id: \.self) { action in
                    Button(action) { Task { await store.send(action) } }
                        .font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
                        .padding(.horizontal, 10).frame(minHeight: 44)
                        .background(Theme.accentTint, in: PixelPanelShape())
                        .disabled(store.isSending || store.retryTurn != nil || store.conflictBlocked)
                }
            }
        }.padding(.top, 4)
    }

    private var composer: some View {
        VStack(spacing: 6) {
            HStack {
                Text("Your companion").textCase(.uppercase).font(Theme.pixelFont(StudioLayout.dockLabelSize))
                Spacer()
                Text(store.isSending ? "Writing" : "Meals").font(.caption2)
            }.foregroundStyle(Theme.sectionInk)
            HStack(spacing: 4) {
                Menu {
                    Button("Scan Barcode", systemImage: "barcode.viewfinder", action: scan)
                    Button("Manual food entry", systemImage: "plus") { dismissKeyboard(); showManual = true }
                } label: {
                    Text("Ask Coach").font(Theme.pixelFont(19)).foregroundStyle(Theme.accent).frame(minHeight: 44)
                }.accessibilityLabel("Meal entry options").accessibilityIdentifier("meal-entry-options")
                TextField("Ask, log or adjust", text: $draft, axis: .vertical)
                    .font(.footnote).accessibilityIdentifier("studio.composer").lineLimit(1...4)
                    .focused($inputFocused).padding(.vertical, 8).submitLabel(.send).onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up").font(.subheadline.bold()).frame(width: 44, height: 44)
                        .foregroundStyle(trimmedInput.isEmpty ? Theme.inactive : Theme.accent)
                }.disabled(trimmedInput.isEmpty || store.isSending || store.retryTurn != nil || store.conflictBlocked)
                    .accessibilityLabel("Send to Coach").accessibilityIdentifier("send-message")
            }.padding(.leading, 10).background(Theme.paper, in: PixelPanelShape())
                .overlay(PixelPanelShape().stroke(Theme.edgeStrong, lineWidth: 1)).studioFrame("composer.row")
        }.padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 4).background(Theme.dock)
            .overlay(alignment: .top) { Rectangle().fill(Theme.accentEdge).frame(height: 2) }.studioFrame("composer")
    }

    private func send() {
        guard !trimmedInput.isEmpty, !store.isSending, store.retryTurn == nil, !store.conflictBlocked else { return }
        let message = trimmedInput; draft = ""; dismissKeyboard()
        Task { await store.send(message) }
    }
    private func dismissKeyboard() {
        inputFocused = false
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
    private func scan() {
        dismissKeyboard(); scanPaused = false; scanEvent = UUID()
        showScanner = true
    }

    private var injectedScanCode: String? {
        #if DEBUG
        if store.isFixture || ProcessInfo.processInfo.arguments.contains("-qa-injected-scan") {
            return "00016000275287"
        }
        #endif
        return nil
    }
}

private struct ChatSurfaceCard: View {
    let surface: CoachSurface
    let canAct: Bool
    let canConfirm: Bool
    let confirm: () -> Void
    let edit: () -> Void
    let review: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch surface {
            case .mealPreview(let meal):
                heading("Review meal", date: meal.date, meal: meal.meal)
                Text("Not saved yet").font(.caption).foregroundStyle(Theme.muted)
                if let notice = meal.notice { Text(notice).font(.subheadline.weight(.semibold)).fixedSize(horizontal: false, vertical: true) }
                ForEach(Array(meal.foods.enumerated()), id: \.offset) { _, food in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(food.name).font(.subheadline.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                        Text("\(food.portion)\(food.assumed ? " · assumed" : "") · \(number(food.nutrition.calories)) kcal").font(.caption)
                        if let reason = food.not_counted_reason { Text("Not counted: \(reason)").font(.caption) }
                        if let changes = food.modifications { Text(changes).font(.caption) }
                        if let note = food.recipe_note { Text(note).font(.caption).fixedSize(horizontal: false, vertical: true) }
                        Text(food.source).font(.caption2).foregroundStyle(Theme.muted).fixedSize(horizontal: false, vertical: true)
                    }
                }
                nutrition(meal.totals)
                if let day = meal.day_after { ChatDayBars(title: "After this meal", totals: day.totals, targets: day.targets, targetSource: day.target_source) }
                if canConfirm {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) { confirmationButtons }
                        VStack(spacing: 8) { confirmationButtons }
                    }.disabled(!canAct)
                }
            case .mealReceipt(let meal):
                heading("Saved", date: meal.date, meal: meal.meal)
                ForEach(Array(meal.entries.enumerated()), id: \.offset) { _, entry in
                    Text(entry.name).font(.subheadline.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                }
                nutrition(meal.totals)
                if let source = meal.source { Text(source).font(.caption2).foregroundStyle(Theme.muted) }
                if let day = meal.day_after { ChatDayBars(title: "Updated day", totals: day.totals, targets: day.targets, targetSource: day.target_source) }
            case .saveFailed(let failure):
                Text("Could not save").font(Theme.pixelFont(24))
                Text(failure.reason).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                if failure.retry_safe {
                    Button("Review & retry", action: review).buttonStyle(PixelSecondaryButtonStyle(fullWidth: true)).disabled(!canAct)
                    Text("Review your day first. This does not save or resend a confirmation.").font(.caption).foregroundStyle(Theme.muted)
                }
            case .dayTotals(let day):
                heading("Day totals", date: day.date, meal: nil)
                ChatDayBars(title: "Logged today", totals: day.totals, targets: day.targets, targetSource: day.target_source)
                Text("\(number(day.remaining.calories)) kcal remaining").font(.subheadline)
                if !day.macros_complete { Text("Some macros are unknown, not zero.").font(.caption).foregroundStyle(Theme.muted) }
            }
        }
        .foregroundStyle(Theme.ink)
        .frame(maxWidth: .infinity, alignment: .leading)
        .appCard(padding: 12)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifier)
    }

    @ViewBuilder private var confirmationButtons: some View {
        Button("Confirm meal", action: confirm).buttonStyle(PixelPrimaryButtonStyle())
            .accessibilityHint("Sends Log it once for the current preview")
        Button("Edit portion", action: edit).buttonStyle(PixelSecondaryButtonStyle(fullWidth: true))
            .accessibilityHint("Prepares a message without sending it")
    }

    private func heading(_ title: String, date: String, meal: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(Theme.pixelFont(24))
            Text([date, meal].compactMap { $0 }.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.muted)
        }
    }

    private func nutrition(_ value: SurfaceNutrition) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("\(number(value.calories)) kcal").font(Theme.pixelFont(26))
            Text("Protein \(surfaceNutrientText(value.protein)) · Carbs \(surfaceNutrientText(value.carbs)) · Fat \(surfaceNutrientText(value.fat)) · Fiber \(surfaceNutrientText(value.fiber))")
                .font(.caption).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func number(_ value: Double?) -> String { surfaceNumber(value) }
    private var identifier: String {
        switch surface {
        case .mealPreview: return "chat.surface.meal_preview"
        case .mealReceipt: return "chat.surface.meal_receipt"
        case .saveFailed: return "chat.surface.save_failed"
        case .dayTotals: return "chat.surface.day_totals"
        }
    }
}

private struct ChatDayBars: View {
    let title: String
    let totals: SurfaceNutrition
    let targets: SurfaceNutrition
    let targetSource: String
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(Theme.pixelFont(19))
            bar("Calories", value: totals.calories, target: targets.calories, unit: "kcal", color: Theme.calories)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                if typeSize.isAccessibilitySize {
                    bar("Protein", value: totals.protein, target: targets.protein, unit: "g", color: Theme.protein)
                    bar("Carbs", value: totals.carbs, target: targets.carbs, unit: "g", color: Theme.carbs)
                    bar("Fat", value: totals.fat, target: targets.fat, unit: "g", color: Theme.fat)
                    bar("Fiber", value: totals.fiber, target: targets.fiber, unit: "g", color: Theme.fiber)
                } else {
                    GridRow {
                        bar("Protein", value: totals.protein, target: targets.protein, unit: "g", color: Theme.protein)
                        bar("Carbs", value: totals.carbs, target: targets.carbs, unit: "g", color: Theme.carbs)
                    }
                    GridRow {
                        bar("Fat", value: totals.fat, target: targets.fat, unit: "g", color: Theme.fat)
                        bar("Fiber", value: totals.fiber, target: targets.fiber, unit: "g", color: Theme.fiber)
                    }
                }
            }
            Text(targetSource == "fallback" ? "Default display targets, not configured goals" : "Configured goals").font(.caption2).foregroundStyle(Theme.muted)
        }
    }

    private func bar(_ label: String, value: Double?, target: Double?, unit: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(Theme.pixelFont(16))
            if let value {
                Text("\(surfaceNumber(value)) / \(surfaceNumber(target)) \(unit)")
                    .font(.caption).fixedSize(horizontal: false, vertical: true)
                if let target, target > 0 { PixelBar(value: value, target: target, color: color) }
                else { Text("No target").font(.caption2).foregroundStyle(Theme.muted) }
            } else {
                Text("Unknown").font(.caption).foregroundStyle(Theme.muted)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct CoachBubble: View {
    let message: ChatMessage
    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 52) }
            Text(message.text).font(.subheadline).textSelection(.enabled)
                .padding(.horizontal, 14).padding(.vertical, 11)
                .background(message.role == .user ? Theme.accentTint : Theme.surface,
                            in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
                .foregroundStyle(Theme.ink)
                .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).stroke(message.role == .user ? Theme.edgeStrong : Theme.edge))
            if message.role == .coach { Spacer(minLength: 52) }
        }
    }
}

private struct QuickReplyRow: View {
    let labels: [String]
    let isBusy: Bool
    let onTap: (String) -> Void
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(labels.enumerated()), id: \.element) { index, label in
                    Button(label) { onTap(label) }.font(.subheadline.weight(.semibold))
                        .foregroundStyle(index == 0 ? Color.white : Theme.accent)
                        .padding(.horizontal, 16).padding(.vertical, 9)
                        .background(index == 0 ? Theme.accent : Theme.accentTint, in: Capsule())
                        .accessibilityHint("Sends \(label)")
                }
            }
        }.disabled(isBusy)
    }
}
