"""Cross-language fixture/membership checks, not a substitute for xcodebuild."""
import json
import re
from pathlib import Path
from agent_canvas import COMPONENTS, ACTIONS, validate_surface

ROOT = Path(__file__).resolve().parents[2]


def test_rolling_server_negotiation_and_warm_load_order_are_explicit():
    store = (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    root = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text()
    assert 'guard hasAccountResponse else { return .loading }' in store
    assert 'accountCanvasProtocol == "mmacros.canvas.v1"' in store
    assert 'return .compatibility' in store
    assert 'if hasAdoptedDayPolicy {' in store
    assert 'async let dayLoad: Void = loadDay(' in store
    assert 'async let workoutLoad: Void = loadWorkoutData(' in store
    assert 'if dateString != requestedKey || dayPolicy.timeZone != requestedPolicy.timeZone' in store
    assert 'store.accountRoute == .compatibility' in root
    assert 'ChatLogView' not in root
    fallback = (ROOT / 'ios/MacroTracker/Views/AgentCanvasView.swift').read_text()
    assert '.sheet(isPresented: $canvas.showsLegacyCoach)' in fallback
    assert 'ChatLogView()' in fallback
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testLegacyServerFallsBackWithoutInventingOnboarding' in native
    assert 'testWarmLoadsOverlapAndRuntimePolicyChangeRefetchesWorkoutDay' in native


def test_day_policy_resources_are_actor_cached_by_policy_not_effective_date():
    source = (ROOT / 'ios/MacroTracker/Models/Models.swift').read_text()
    policy = source.split('struct LoggingDayPolicy:', 1)[1].split('struct DayPayload:', 1)[0]
    assert '@MainActor private static var cachedResources:' in policy
    assert 'cached.timeZone == timeZone && cached.rolloverHour == rolloverHour' in policy
    assert 'var calendar: Calendar { resources.calendar }' in policy
    assert 'var formatter: DateFormatter { resources.formatter }' in policy
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testDayPolicyCacheInvalidationAndDST' in native


def test_standard_coach_onboarding_uses_account_not_selected_day():
    chat = (ROOT / 'ios/MacroTracker/Views/ChatLogView.swift').read_text()
    predicate = chat.split('private var needsOnboarding: Bool', 1)[1].split('private func', 1)[0]
    assert 'store.accountRoute == .onboarding' in predicate
    assert 'store.day' not in predicate and 'freshClaim' not in predicate
    assert 'guard onboardingTurn else { return }' in chat
    store = (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    assert 'if requestedIsToday {' in store
    assert 'accountHasTargets = loadedDay.hasTargets' in store
    assert 'accountHasTargets = nil' in store
    root = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text()
    assert 'return store.accountRoute' in root
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testHistoricalDayCannotReopenAccountOnboarding' in native


def test_root_retries_keep_load_errors_inline_and_preserve_action_errors():
    root = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text()
    store = (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    assert 'loadAll(reportErrors: loader.route != .loading)' in root
    assert 'loader.route != .loading && store.errorMessage != nil' in root
    assert 'func loadAll(reportErrors: Bool = true)' in store
    assert 'clearLoadError(source:' in store
    assert 'if reportErrors { present(error, session: session, source:' in store
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'routes, [.loading, .loading, .canvas]' in native
    assert 'retryLoader.route, .canvas' in native


def test_initial_route_has_no_dead_fresh_claim_parameter():
    content = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text()
    signature = content.split('static func initial(', 1)[1].split(') -> Self', 1)[0]
    assert 'freshClaim' not in signature
    assert 'initial(freshClaim:' not in content
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'initial(freshClaim:' not in native


def test_meals_coach_navigation_resets_destination_and_waits_for_dismissal():
    view = (ROOT / 'ios/MacroTracker/Views/AgentCanvasView.swift').read_text()
    assert 'legacyDestination = canvas.handleLegacyDestination(destination)' in view
    assert 'destination == 0' not in view
    assert 'onDismiss: { canvas.completeLegacyNavigation() }' in view
    assert 'testRepeatedMealsCoachNavigation' in (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()


def test_session_identity_persists_only_opaque_account_scoped_id():
    store = (ROOT / 'ios/MacroTracker/Services/AgentSurfaceStore.swift').read_text()
    api = (ROOT / 'ios/MacroTracker/Services/APIClient.swift').read_text()
    assert 'identity.session(for: api.canvasIdentityScope)' in store
    assert 'identity.clear()' in store
    assert store.count('guard !synchronizeIdentity() else { return }') == 2
    assert 'SHA256.hash' in api and 'baseURL.absoluteString' in api
    assert 'defaults.set(["scope": scope, "id": id]' in store
    assert 'testSessionIdentitySurvivesLaunchButCannotCrossAccountOrBackend' in (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()


def test_recreation_is_adopted_only_from_authoritative_get_and_fences_old_events():
    contract = (ROOT / 'ios/MacroTracker/Models/AgentCanvasContract.swift').read_text()
    store = (ROOT / 'ios/MacroTracker/Services/AgentSurfaceStore.swift').read_text()
    renderer = (ROOT / 'ios/MacroTracker/Views/AgentSurfaceRenderer.swift').read_text()
    assert 'instanceId:' in renderer.split('let probe = AgentCanvasEnvelope(', 1)[1].split('try probe.validate()', 1)[0]
    assert 'authoritativeRefresh: Bool = false' in contract
    assert 'value.instanceId != existing.instanceId' in contract
    assert 'guard authoritativeRefresh else' in contract
    assert 'receive(value, authoritativeRefresh: true)' in store
    assert 'old.revision > value.revision { throw' not in store
    assert 'self.generation == voiceGeneration' in store
    assert 'recoveryNotice = "Session refreshed.' in store
    assert 'canvas.recoveryNotice' in (ROOT / 'ios/MacroTracker/Views/AgentCanvasView.swift').read_text()
    assert 'testRefreshAdoptsRecreatedSessionWithoutPostingWrites' in (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()


def test_onboarding_root_uses_server_readiness_and_completion_not_arrival():
    content = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text().split('struct ProgressDashboardView')[0]
    assert 'AgentCanvasView(canvas: store.canvas)' in content
    assert 'ChatLogView' not in content
    assert 'switch loader.route' not in content
    assert 'AgentRootRoute.initial' in (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    assert 'return store.accountRoute' in content
    # Source tripwires ONLY: behavioral execution is in the unrun XCTest suite.
    routing = content.split('static func initial', 1)[1].split('@MainActor', 1)[0]
    assert 'hasTargets == false || hasPlan == false' in routing
    assert 'return .loading' in routing
    assert 'freshClaim ||' not in routing
    assert 'AgentRootLoader' in content
    assert 'retryDelays: [Double] = [5, 15]' in content
    assert 'Button("Retry")' in content
    assert '.task(id: reloadID)' in content
    assert 'Readiness updates the permanent strip' in content
    assert 'loader.errorMessage' in content
    assert 'testUnknownReadinessRetriesThenRecoversWithoutRelaunch' in (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'auth.completeOnboarding()' not in content
    chat = (ROOT / 'ios/MacroTracker/Views/ChatLogView.swift').read_text()
    assert 'Button("Continue to MMacros")' in chat
    assert 'onOnboardingComplete?()' in chat
    completion = chat.split('private var canFinishOnboarding: Bool')[1].split('private var', 1)[0]
    assert '!isSending' in completion and '!isFinalizingOnboardingPlan' in completion
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testFreshClaimAndMissingTargetsStayInEstablishedOnboarding' in native


def test_logger_preserves_safe_assigned_type_through_submission():
    source = (ROOT / 'ios/MacroTracker/Views/WorkoutsView.swift').read_text()
    logger = source.split('struct WorkoutLoggerView: View')[1]
    assert 'WorkoutLoggerType.value(for: initialType)' in logger
    assert 'WorkoutLoggerType.choices(for: initialType)' in logger
    assert 'CanonicalWorkoutType.value(for: initialType)' not in logger
    assert 'type: type, onLogged: onLogged' in logger
    assert 'guard WorkoutLoggerType.isValid(type)' in logger
    assert 'testLoggerPreservesCustomAssignedTypeAndCanonicalChoices' in (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()


def test_custom_workout_labels_use_same_bounded_contract_in_swift():
    swift = (ROOT / 'ios/MacroTracker/Models/AgentCanvasContract.swift').read_text()
    assert 'CanvasValidation.validWorkoutLabel(workout.type)' in swift
    assert '[A-Za-z0-9][A-Za-z0-9 /&()+.\'_\\-]{0,79}' in swift
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasContractTests.swift').read_text()
    assert 'testCustomWorkoutLabelsShareServerFixture' in native



def test_ios_contract_fixture_matches_server_catalog():
    data = json.loads((ROOT / 'docs/agent-canvas/fixtures/snapshot.json').read_text())
    for surface in data['surfaces']:
        validate_surface(surface)
    swift = (ROOT / 'ios/MacroTracker/Models/AgentCanvasContract.swift').read_text()
    for name in COMPONENTS:
        assert f'"{name}"' in swift
    for name in ACTIONS:
        assert f'"{name}"' in swift or re.search(r'\b' + name + r'\b', swift)


def test_foreground_selects_effective_today_before_reload_without_breaking_history():
    view = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text().split('struct ProgressDashboardView')[0]
    active = view.split('if phase == .active {', 1)[1].split('} else if', 1)[0]
    assert 'reloadID = UUID()' in active
    task = view.split('.task(id: reloadID)', 1)[1]
    assert task.index('store.selectCurrentDay()') < task.index('store.loadAll(')
    assert 'store.canvas.refresh()' in active
    canvas = (ROOT / 'ios/MacroTracker/Views/AgentCanvasView.swift').read_text()
    assert 'onChange(of: scenePhase)' not in canvas
    assert 'await app.loadAll()' not in canvas.split('.task {', 1)[1].split('.onDisappear', 1)[0]
    store = (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    assert 'loadedDay.dayTiming' in store
    assert 'dayCalendar.date(byAdding: .day' in store
    assert 'policy.date(from: requestedDateString)' in store
    models = (ROOT / 'ios/MacroTracker/Models/Models.swift').read_text()
    assert 'calendar.component(.hour, from: now) < rolloverHour' in models
    assert 'TimeZone(identifier: timeZone)' in models
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testForegroundDateUsesFourAMEasternBoundary' in native


def test_toast_host_covers_canvas_and_all_fallback_sheets_without_tabs():
    view = (ROOT / 'ios/MacroTracker/Views/AgentCanvasView.swift').read_text()
    assert 'if let toast = app.toast' in view
    assert 'Label(toast, systemImage: "checkmark.circle.fill")' in view
    assert view.count('.modifier(AgentToastModifier())') == 5, 'Canvas sheets only, no second root host'
    root = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text().split('struct ProgressDashboardView')[0]
    assert root.count('.modifier(AgentToastModifier())') == 1
    assert root.count('.alert("Could not complete that"') == 1
    assert 'store.errorMessage = nil' in root
    assert '.alert("Could not complete that"' not in view
    chat = (ROOT / 'ios/MacroTracker/Views/ChatLogView.swift').read_text()
    assert 'ManualFoodView().modifier(AgentToastModifier())' in chat
    assert 'WorkoutLoggerView(initialType: selection.type, initialExercise: selection.exercise)\n                .modifier(AgentToastModifier())' in chat
    assert 'TabView(' not in view
    assert '.allowsHitTesting(false)' in view


def test_swap_preview_never_writes_to_composer_draft():
    renderer = (ROOT / 'ios/MacroTracker/Views/AgentSurfaceRenderer.swift').read_text()
    assert 'canvas.draft =' not in renderer
    assert 'canvas.requestSwap(replacement)' in renderer
    store = (ROOT / 'ios/MacroTracker/Services/AgentSurfaceStore.swift').read_text()
    assert 'func requestSwap(' in store
    assert 'if clearsComposer &&' in store
    assert 'swapTurnIdentity' in store
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testSwapRequestPreservesComposerDraft' in native


def test_receipt_and_rest_use_server_relative_monotonic_deadlines():
    contract = (ROOT / 'ios/MacroTracker/Models/AgentCanvasContract.swift').read_text()
    assert 'expiry - value.serverTime' in contract
    assert 'private(set) var transientDeadlines' in contract
    assert 'ProcessInfo.processInfo.systemUptime' in contract
    assert 'date.timeIntervalSince1970' not in contract
    renderer = (ROOT / 'ios/MacroTracker/Views/AgentSurfaceRenderer.swift').read_text()
    assert 'canvas.state.restRemaining()' in renderer
    assert 'end > context.date.timeIntervalSince1970' not in renderer
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasContractTests.swift').read_text()
    assert 'testReceiptClockSkewAndRefreshDoNotHideOrExtendAcknowledgement' in native


def test_primary_shell_and_live_share_owned_session():
    content = (ROOT / 'ios/MacroTracker/ContentView.swift').read_text()
    assert 'AgentCanvasView' in content.split('struct ProgressDashboardView')[0]
    assert 'TabView(' not in content.split('struct ProgressDashboardView')[0]
    store = (ROOT / 'ios/MacroTracker/Services/AgentSurfaceStore.swift').read_text()
    assert 'canvasSessionId: sessionId' in store
    assert 'api.canvasTurn(sessionId:' in store
    assert 'AgentActionGateway.validate' in store
    assert 'agent.canvas' in (ROOT / 'ios/MacroTracker/Services/LiveCoachTransport.swift').read_text()


def test_brief_request_follows_authoritative_policy_adoption_and_keeps_history():
    source = (ROOT / 'ios/MacroTracker/AppStore.swift').read_text()
    day = source.split('func loadDay(', 1)[1].split('func loadWorkoutData(', 1)[0]
    assert 'api.brief(requestedDateString)' not in day
    assert day.index('dayPolicy = policy') < day.index('loadBrief(date: briefDateString')
    assert 'let briefDateString = dateString' in day
    assert 'selectedDate == completionDate' in day
    assert 'policy.date(from: requestedDateString)' in day
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testBriefUsesAdoptedCurrentDateAndPreservesHistoricalSelection' in native


def test_instance_mismatch_queues_one_read_only_recovery_after_busy_operation():
    store = (ROOT / 'ios/MacroTracker/Services/AgentSurfaceStore.swift').read_text()
    assert 'scheduleInstanceRecovery()' in store
    recovery = store.split('private func scheduleInstanceRecovery()', 1)[1].split('@discardableResult', 1)[0]
    assert 'guard instanceRecoveryTask == nil' in recovery
    assert 'while self.busy' in recovery
    assert 'await self.refresh()' in recovery
    assert 'await self.reloadData()' in recovery
    assert 'canvasTurn' not in recovery and 'canvasAction' not in recovery
    assert 'current == self.generation' in recovery
    assert 'snapshot.sessionId == sessionId' in store
    native = (ROOT / 'ios/MacroTrackerTests/AgentCanvasIntegrationTests.swift').read_text()
    assert 'testFencedTurnRecoversWithOneGETAndNeverReplaysPOST' in native
    assert 'testRepeatedFencedEventsCoalesceAndResetCancelsReadback' in native


def test_coverage_unknown_is_explained_not_rendered_as_no_training():
    models = (ROOT / 'ios/MacroTracker/Models/Models.swift').read_text()
    view = (ROOT / 'ios/MacroTracker/Views/WorkoutsView.swift').read_text()
    assert 'var muscleCoverageComplete: Bool?' in models
    assert 'if let note = stats.coverage.note' in view
    assert 'ForEach(muscles, id: \\.self)' in view
    assert 'muscleCoverageComplete == false ? []' not in view
    assert '"Classified coverage"' in view
    assert 'stats.coverage.muscleCoverageComplete != false' in view


def test_new_swift_files_have_target_membership_and_exact_pin():
    project = (ROOT / 'ios/MacroTracker.xcodeproj/project.pbxproj').read_text()
    for name in ['AgentCanvasContract.swift', 'AgentSurfaceStore.swift', 'AgentSurfaceRenderer.swift',
                 'AgentCanvasView.swift', 'AgentCanvasContractTests.swift', 'AgentCanvasIntegrationTests.swift']:
        assert project.count(f'{name} in Sources') >= 2, name
        assert f'path = {name};' in project
    assert '4af5dda15dd050e091a80026b940cd41f8d5b93c' in project
    assert 'kind = revision;' in project
