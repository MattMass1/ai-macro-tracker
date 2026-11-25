import SwiftUI
import UIKit

private struct PixelMotionPausedKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var pixelMotionPaused: Bool {
        get { self[PixelMotionPausedKey.self] }
        set { self[PixelMotionPausedKey.self] = newValue }
    }
}

enum PixelOperation: Equatable {
    case ready
    case disabled
    case busy(String)
    case error(String)
    case complete(String)

    var allowsAction: Bool {
        switch self {
        case .ready, .error: return true
        case .disabled, .busy, .complete: return false
        }
    }

    var message: String? {
        switch self {
        case .busy(let message), .error(let message), .complete(let message): return message
        case .ready, .disabled: return nil
        }
    }

    func animates(reduceMotion: Bool, active: Bool) -> Bool {
        if case .busy = self { return !reduceMotion && active }
        return false
    }

    static func profile(saving: Bool, error: String?, complete: Bool) -> Self {
        if saving { return .busy("Saving…") }
        if let error { return .error(error) }
        return complete ? .complete("Saved") : .ready
    }
    func actionTitle(_ title: String, busyTitle: String, completeTitle: String) -> String {
        switch self {
        case .busy: return busyTitle
        case .complete: return completeTitle
        default: return title
        }
    }
}

enum PixelFigureSelection {
    static func resolve(raw: String, visible: Bool) -> CompanionCharacter? {
        visible ? CompanionCharacter(rawValue: raw) ?? .male : nil
    }
}

enum PixelGlyph: String, CaseIterable {
    case leaf = "PixelLeaf"
    case check = "PixelCheck"
    case chevron = "PixelChevron"
    case error = "PixelError"
}

struct PixelSceneBackground: View {
    var body: some View {
        GeometryReader { proxy in
            Image("studio-background").resizable().interpolation(.none)
                .scaledToFill().frame(width: proxy.size.width, height: proxy.size.height)
                .clipped().colorMultiply(Color(red: 0.28, green: 0.51, blue: 0.40))
                .overlay(Theme.profileCanvas.opacity(0.18))
        }.ignoresSafeArea().accessibilityHidden(true)
    }
}

struct PixelBevel: View {
    var face: Color = Theme.profileSurface
    var inset = false
    var focused = false
    var body: some View {
        PixelPanelShape().fill(Theme.profileWellInk)
            .overlay {
                PixelPanelShape().fill(inset ? Theme.profileOutline : Theme.profileWellInk)
                    .offset(y: inset ? 0 : 5)
            }
            .overlay {
                PixelPanelShape().fill(face).padding(3)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(Theme.profileWellInk.opacity(inset ? 0 : 0.16)).frame(height: 12)
                            .padding(.horizontal, 3).padding(.bottom, 3)
                    }
                    .clipShape(PixelPanelShape())
                    .overlay(alignment: .top) {
                        Rectangle().fill(inset ? Theme.profileWellInk : Theme.profileAccent.opacity(0.7))
                            .frame(height: inset ? 3 : 2).padding(.horizontal, 10).padding(.top, 3)
                    }
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(inset ? Theme.profileAccent.opacity(0.65) : Theme.profileOutline)
                            .frame(height: 3).padding(.horizontal, 10).padding(.bottom, 3)
                    }
            }
            .overlay(PixelPanelShape().stroke(focused ? Theme.profileAccent : Theme.profileWellInk, lineWidth: focused ? 3 : 2))
    }
}

struct PixelChoice<Value: Hashable>: View {
    let title: String
    let value: Value
    @Binding var selection: Value
    let id: String
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        Button { selection = value } label: {
            let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(spacing: 8)) : AnyLayout(HStackLayout(spacing: 8))
            layout {
                if selection == value { PixelGlyphView(glyph: .check) }
                if typeSize.isAccessibilitySize {
                    VStack(spacing: 0) {
                        ForEach(Array(title.split(separator: " ").enumerated()), id: \.offset) { _, word in
                            Text(String(word)).fixedSize()
                        }
                    }.font(Theme.pixelFont(23))
                } else {
                    Text(title).font(Theme.pixelFont(23)).fixedSize(horizontal: false, vertical: true)
                }
            }.frame(maxWidth: .infinity, minHeight: 44).padding(6).contentShape(PixelPanelShape())
        }.buttonStyle(PixelActionStyle(secondary: selection != value))
            .accessibilityLabel(title).accessibilityIdentifier(id).accessibilityAddTraits(selection == value ? [.isSelected] : [])
            .accessibilityValue(selection == value ? "Selected" : "Not selected")
    }
}

struct PixelToggle: View {
    let title: String
    @Binding var enabled: Bool
    let id: String
    var body: some View {
        Button { enabled.toggle() } label: {
            HStack {
                Text(title).font(Theme.pixelFont(23)).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(enabled ? "On" : "Off").font(Theme.pixelFont(23))
            }.padding(12).frame(minHeight: 44).contentShape(PixelPanelShape())
        }.buttonStyle(PixelActionStyle(secondary: !enabled)).accessibilityValue(enabled ? "On" : "Off")
            .accessibilityIdentifier(id)
    }
}

struct PixelDisclosure<Content: View>: View {
    let title: String
    @Binding var expanded: Bool
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Button { expanded.toggle() } label: {
                HStack {
                    Text(title).font(Theme.pixelFont(25)).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Image("PixelDisclosure").resizable().interpolation(.none).scaledToFit().frame(width: 24, height: 24)
                        .rotationEffect(.degrees(expanded ? 90 : 0)).accessibilityHidden(true)
                }.frame(minHeight: 44).padding(8).contentShape(PixelPanelShape())
            }.buttonStyle(PixelActionStyle(secondary: true))
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                .accessibilityIdentifier("pixel.disclosure")
            if expanded { content() }
        }
    }
}

struct PixelNativeNavigation: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> Controller { Controller() }
    func updateUIViewController(_ controller: Controller, context: Context) {
        controller.applyAppearance()
    }
    final class Controller: UIViewController {
        override func viewDidAppear(_ animated: Bool) {
            super.viewDidAppear(animated)
            applyAppearance()
        }
        func applyAppearance() {
            guard let navigation = navigationController, view.window != nil else { return }
            let renderer = ImageRenderer(content:
                ZStack {
                    PixelBevel()
                    PixelGlyphView(glyph: .chevron).rotationEffect(.degrees(180)).foregroundStyle(Theme.profileInk)
                }.frame(width: 48, height: 44)
            )
            renderer.scale = 3
            guard let image = renderer.uiImage?.withRenderingMode(.alwaysOriginal) else { return }
            let appearance = UINavigationBarAppearance()
            appearance.configureWithTransparentBackground()
            appearance.titleTextAttributes = [.foregroundColor: UIColor(Theme.profileInk)]
            appearance.setBackIndicatorImage(image, transitionMaskImage: image)
            let surround = ImageRenderer(content: PixelBevel().frame(width: 48, height: 44))
            surround.scale = 3
            appearance.backButtonAppearance.normal.backgroundImage = surround.uiImage?.resizableImage(withCapInsets: UIEdgeInsets(top: 12, left: 12, bottom: 12, right: 12))
            for controller in navigation.viewControllers {
                let item = controller.navigationItem
                let back = item.backBarButtonItem ?? UIBarButtonItem(title: "", style: .plain, target: nil, action: nil)
                back.title = ""
                if #available(iOS 26.0, *) { back.hidesSharedBackground = true }
                item.backBarButtonItem = back
                item.backButtonDisplayMode = .minimal
                item.standardAppearance = appearance
                item.scrollEdgeAppearance = appearance
                item.compactAppearance = appearance
            }
        }
    }
}

struct PixelGlyphView: View {
    let glyph: PixelGlyph
    var label: String? = nil
    var body: some View {
        Image(glyph.rawValue).renderingMode(.template).resizable().scaledToFit()
            .frame(width: 20, height: 20).accessibilityLabel(label ?? "")
            .accessibilityHidden(label == nil)
            .accessibilityIdentifier("pixel.glyph.\(glyph.rawValue)")
    }
}

struct PixelPanel<Content: View>: View {
    var title: String? = nil
    var glyph: PixelGlyph? = nil
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let title {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let glyph { PixelGlyphView(glyph: glyph).foregroundStyle(Theme.profileAccent) }
                    Text(title).font(Theme.pixelFont(25, relativeTo: .title2))
                        .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
                }
            }
            content()
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(PixelBevel()).foregroundStyle(Theme.profileInk)
    }
}

enum PixelFieldValidation: Equatable {
    case neutral
    case invalid(String)
}

struct PixelValueField: View {
    let label: String
    @Binding var text: String
    let id: String
    var unit = ""
    var numeric = true
    var required = true
    let focus: FocusState<String?>.Binding
    var validation: PixelFieldValidation = .neutral
    @Environment(\.dynamicTypeSize) private var typeSize
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 6) {
                Text(label).font(.subheadline.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout(alignment: .firstTextBaseline, spacing: 8))
                layout {
                    TextField("", text: $text, prompt: Text(required ? "Required" : "Optional").foregroundStyle(Theme.profileWellMuted))
                        .font(.body.monospacedDigit()).keyboardType(numeric ? .decimalPad : .default)
                        .multilineTextAlignment(.leading)
                        .textInputAutocapitalization(numeric ? .never : .words).autocorrectionDisabled()
                        .focused(focus, equals: id).frame(minWidth: 80, minHeight: 44)
                        .accessibilityLabel(unit.isEmpty ? label : "\(label), \(unit)")
                        .accessibilityHint(validationHint).accessibilityIdentifier(id)
                    if !unit.isEmpty {
                        Text(unit).font(Theme.pixelFont(23)).foregroundStyle(Theme.profileWellMuted)
                            .fixedSize(horizontal: typeSize.isAccessibilitySize, vertical: true)
                    }
                }.padding(.horizontal, 14).padding(.vertical, 4)
                    .foregroundStyle(Theme.profileWellInk).tint(Theme.profileWellInk)
                    .background(PixelBevel(face: Theme.profileMint, inset: true, focused: focus.wrappedValue == id || !validationHint.isEmpty))
            }
            if case .invalid(let message) = validation {
                Text(message).font(.footnote).foregroundStyle(Theme.profileDanger)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
    private var validationHint: String {
        if case .invalid(let message) = validation { return message }
        return ""
    }
}

struct PixelActionStyle: ButtonStyle {
    var secondary = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(secondary ? Theme.profileInk : Theme.profileWellInk)
            .background(PixelBevel(face: secondary ? Theme.profileSurface : Theme.profileAccent, inset: configuration.isPressed))
            .offset(y: configuration.isPressed ? 3 : 0).opacity(enabled ? 1 : 0.55)
    }
}

struct PixelAction: View {
    let title: String
    var state: PixelOperation = .ready
    var busyTitle = "Working…"
    var completeTitle = "Complete"
    var secondary = false
    var glyph: PixelGlyph? = .chevron
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                if case .busy = state { PixelActivity(state: state) }
                Text(actionTitle).font(Theme.pixelFont(25)).fixedSize(horizontal: false, vertical: true)
                if state.message == nil, let glyph { PixelGlyphView(glyph: glyph).accessibilityHidden(true) }
            }.padding(.vertical, 14).frame(maxWidth: .infinity, minHeight: 44).contentShape(PixelPanelShape())
        }.buttonStyle(PixelActionStyle(secondary: secondary)).disabled(!state.allowsAction)
            .onChange(of: state) { _, state in
                if case .busy(let message) = state { UIAccessibility.post(notification: .announcement, argument: message) }
            }
    }
    private var actionTitle: String {
        state.actionTitle(title, busyTitle: busyTitle, completeTitle: completeTitle)
    }
}

enum PixelActivityPolicy {
    static func segment(elapsed: TimeInterval) -> Int {
        guard elapsed.isFinite, elapsed > 0 else { return 0 }
        return Int((elapsed.truncatingRemainder(dividingBy: 1.2) / 0.3).rounded(.down))
    }
}

struct PixelActivity: View {
    let state: PixelOperation
    @Environment(\.pixelMotionPaused) private var motionPaused
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var started = Date()
    var body: some View {
        Group {
            if state.animates(reduceMotion: reduceMotion || motionPaused, active: scenePhase == .active) {
                TimelineView(.periodic(from: started, by: 0.3)) { context in
                    segments(PixelActivityPolicy.segment(elapsed: context.date.timeIntervalSince(started)))
                }
            } else { segments(nil) }
        }.onChange(of: state) { _, _ in started = Date() }.accessibilityHidden(true)
    }
    private func segments(_ selected: Int?) -> some View {
        HStack(spacing: 3) {
            ForEach(0..<4) { index in
                Rectangle().fill(ForegroundStyle()).opacity(selected == nil || selected == index ? 1 : 0.25)
                    .frame(width: 4, height: 12)
            }
        }
    }
}

struct PixelStatus: View {
    let state: PixelOperation
    var announce = true
    var body: some View {
        Group {
            if let message = state.message {
                HStack(alignment: .top, spacing: 12) {
                    switch state {
                    case .busy: PixelActivity(state: state).padding(.top, 4)
                    case .error: PixelGlyphView(glyph: .error)
                    case .complete: PixelGlyphView(glyph: .check)
                    default: EmptyView()
                    }
                    Text(message).font(.body).fixedSize(horizontal: false, vertical: true)
                }.padding(14).background(PixelBevel())
                    .foregroundStyle(isError ? Theme.profileDanger : Theme.profileAccent)
            }
        }.onChange(of: state) { _, newState in
            if announce, let message = newState.message {
                UIAccessibility.post(notification: .announcement, argument: message)
            }
        }
    }
    private var isError: Bool {
        if case .error = state { return true }
        return false
    }
}

struct PixelStepState: Equatable {
    let titles: [String]
    let current: Int?
    init(titles: [String], current: Int) {
        self.titles = titles
        self.current = titles.isEmpty ? nil : min(max(0, current), titles.count - 1)
    }
    var label: String {
        guard let current else { return "No steps" }
        return "Step \(current + 1) of \(titles.count), \(titles[current])"
    }
}

struct PixelStepProgress: View {
    let titles: [String]
    let current: Int
    @Environment(\.pixelMotionPaused) private var motionPaused
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    private var step: PixelStepState { PixelStepState(titles: titles, current: current) }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ForEach(titles.indices, id: \.self) { index in
                    Rectangle().fill(index <= (step.current ?? -1) ? Theme.profileAccent : Theme.profileOutline).frame(height: 8)
                }
            }
            Text(step.current.map { titles[$0] } ?? "No steps")
                .font(Theme.pixelFont(21)).fixedSize(horizontal: false, vertical: true).id(step.current).transition(.opacity)
        }.padding(12).background(PixelBevel()).foregroundStyle(Theme.profileInk)
            .animation(reduceMotion || motionPaused || scenePhase != .active ? nil : .easeOut(duration: 0.2), value: current)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(step.label)
            .accessibilityIdentifier("pixel.steps")
    }
}

struct PixelArtworkSlot<Artwork: View>: View {
    @Environment(\.pixelMotionPaused) private var motionPaused
    var character: CompanionCharacter? = nil
    var companionState: CompanionState = .rest
    @ViewBuilder let artwork: () -> Artwork
    var body: some View {
        HStack(alignment: .bottom, spacing: 16) {
            artwork()
            Spacer(minLength: 0)
            if let character {
                CompanionView(character: character, state: companionState, motionPaused: motionPaused).frame(width: 72, height: 72)
                    .background(alignment: .bottom) {
                        PixelBevel(face: Theme.profileOutline).frame(width: 64, height: 12).offset(y: 6)
                            .accessibilityHidden(true)
                    }
                    .accessibilityIdentifier("pixel.figure.\(character.rawValue)")
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct PixelBrandArtwork: View {
    var body: some View {
        Image("OnboardingWordmark").resizable().interpolation(.none).scaledToFit().frame(maxWidth: 224).frame(height: 128)
            .accessibilityLabel("Macro-Bit").accessibilityIdentifier("pixel.artwork.logo")
    }
}

struct PixelSelectedArtwork: View {
    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true
    var body: some View {
        PixelArtworkSlot(character: PixelFigureSelection.resolve(raw: characterRaw, visible: showsCompanion)) {
            PixelBrandArtwork()
        }
    }
}

#if DEBUG
struct PixelComponentGallery: View {
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var operation: PixelOperation = .ready
    @State private var step = 0
    @State private var value = "80"
    @State private var demoCharacter: CompanionCharacter?
    @State private var reducedMotion = false
    @State private var inactive = false
    @State private var disabled = false
    @State private var details = false
    @FocusState private var focus: String?
    @AppStorage(CompanionPreferences.characterKey) private var characterRaw = CompanionCharacter.male.rawValue
    @AppStorage(CompanionPreferences.visibleKey) private var showsCompanion = true
    init() {
        let arguments = ProcessInfo.processInfo.arguments
        _operation = State(initialValue: arguments.contains("-gallery-working") ? .busy("Demonstration pending") : .ready)
        _reducedMotion = State(initialValue: arguments.contains("-gallery-reduced"))
        _inactive = State(initialValue: arguments.contains("-gallery-inactive"))
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Native component demonstrations. No account, network or saved data.")
                        .font(.footnote).foregroundStyle(Theme.profileMuted)
                        .padding(8).background(Theme.profileCanvas, in: PixelPanelShape())
                    ViewThatFits(in: .horizontal) {
                        HStack { controls }
                        VStack { controls }
                    }
                    PixelArtworkSlot(character: demoCharacter ?? PixelFigureSelection.resolve(raw: characterRaw, visible: showsCompanion), companionState: isWorking ? .writing : .rest) {
                        PixelBrandArtwork()
                    }
                    PixelStatus(state: operation, announce: false)
                    PixelStepProgress(titles: ["Starting point", "Daily targets"], current: step)
                    PixelPanel(title: "Shared native controls", glyph: .leaf) {
                        PixelValueField(label: "Weight demonstration", text: $value, id: "gallery.value", unit: "kg", focus: $focus,
                            validation: isError ? .invalid("Demonstration validation. Keep your draft.") : .neutral)
                        HStack(spacing: 16) {
                            PixelGlyphView(glyph: .leaf, label: "Leaf")
                            PixelGlyphView(glyph: .check, label: "Success check")
                            PixelGlyphView(glyph: .chevron, label: "Next step")
                            PixelGlyphView(glyph: .error, label: "Error notice")
                        }.foregroundStyle(Theme.profileAccent)
                        PixelAction(title: "Demonstration action", state: disabled ? .disabled : actionState) {
                            operation = .busy("Demonstration pending")
                        }.accessibilityIdentifier("gallery.action")
                        PixelAction(title: "Next demonstration step", secondary: true) { step = step == 0 ? 1 : 0 }
                            .accessibilityIdentifier("gallery.step")
                    }
                    PixelPanel(title: "Motion and figure demonstrations") {
                        PixelToggle(title: "Reduce Motion · DEMO", enabled: $reducedMotion, id: "gallery.reduce")
                        PixelToggle(title: "Inactive scene · DEMO", enabled: $inactive, id: "gallery.inactive")
                        PixelToggle(title: "Disable action · DEMO", enabled: $disabled, id: "gallery.disable")
                        let selection = Binding(get: { demoCharacter ?? CompanionCharacter(rawValue: characterRaw) ?? .male }, set: { demoCharacter = $0 })
                        ForEach(CompanionCharacter.allCases) { character in
                            PixelChoice(title: "\(character.displayName) · DEMO", value: character, selection: selection, id: "gallery.choice.\(character.rawValue)")
                        }
                        PixelDisclosure(title: "Figure details · DEMO", expanded: $details) {
                            Text("Two bundled looks only. Device-local selection is not changed by this gallery. Writing here is simulated agent work, not a profile save.")
                                .font(.footnote).foregroundStyle(Theme.profileMuted)
                        }
                    }
                }.padding(20)
                    .environment(\.pixelMotionPaused, reducedMotion || systemReduceMotion)
                    .environment(\.scenePhase, inactive ? .inactive : scenePhase)
            }.background { PixelSceneBackground() }.foregroundStyle(Theme.profileInk)
                .navigationTitle("Components · DEMO").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItemGroup(placement: .keyboard) {
                        Spacer()
                        Button { focus = nil } label: { Text("Done").frame(minWidth: 44, minHeight: 44).contentShape(Rectangle()) }.buttonStyle(.plain)
                    }
                }
        }.tint(Theme.profileAccent).preferredColorScheme(.dark)
    }
    private var controls: some View {
        Group {
            Button("Work") { operation = .busy("Demonstration pending") }.accessibilityIdentifier("gallery.work")
            Button("Error") { operation = .error("Demonstration error. Nothing was saved.") }.accessibilityIdentifier("gallery.fail")
            Button("Complete") { operation = .complete("Demonstration complete. No account writes.") }.accessibilityIdentifier("gallery.complete")
            Button("Reset") { operation = .ready }.accessibilityIdentifier("gallery.reset")
        }.font(Theme.pixelFont(23)).padding(8).frame(minWidth: 44, minHeight: 44).buttonStyle(PixelActionStyle(secondary: true))
    }
    private var isWorking: Bool { if case .busy = operation { return true }; return false }
    private var isError: Bool { if case .error = operation { return true }; return false }
    private var actionState: PixelOperation { operation }
}
#endif
