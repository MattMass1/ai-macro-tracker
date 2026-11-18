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
                                    LazyVStack(spacing: 10) {
                                        if store.messages.isEmpty { studioSummary }
                                        ForEach(store.messages) { message in
                                            VStack(alignment: .leading, spacing: 10) {
                                                CoachBubble(message: message)
                                                if message.role == .coach, !message.quickReplies.isEmpty {
                                                    QuickReplyRow(labels: message.quickReplies, isBusy: store.isSending || store.retryTurn != nil) { label in
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
            Button("Sign out", role: .destructive) { store.signOut() }
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
