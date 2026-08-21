import AVKit
import SwiftUI

/// In-chat exercise clip: 16:9 playable thumb, name, muscle tag, Log Set + Swap.
/// Matches `ios/ui-concepts/chat-exercise-video-design.html`.
struct ExerciseVideoCard: View {
    let exercise: ExerciseCardExercise
    let onLogSet: (WorkoutLoggerSelection) -> Void
    let onSwap: () -> Void

    private let tagBackground = Color(red: 232 / 255, green: 240 / 255, blue: 236 / 255) // #e8f0ec

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let url = exercise.videoURL {
                InlineExerciseVideo(url: url)
            } else {
                placeholderHeader
            }

            VStack(alignment: .leading, spacing: 0) {
                if exercise.videoURL != nil {
                    Text(exercise.displayName)
                        .font(.system(size: 14, weight: .bold))
                        .foregroundStyle(Theme.ink)
                }
                metaRow
                    .padding(.top, exercise.videoURL == nil && exercise.muscleGroup == nil && prescription == nil ? 0 : 2)
                HStack(spacing: 8) {
                    actionButton("Log Set", kind: .primary) {
                        onLogSet(WorkoutLoggerSelection(
                            type: exercise.loggerType,
                            exercise: exercise.loggerExerciseName,
                            muscleGroup: exercise.muscleGroup,
                            equipment: exercise.equipment
                        ))
                    }
                    actionButton("Swap", kind: .secondary, action: onSwap)
                }
                .padding(.top, 10)
            }
            .padding(.horizontal, 12)
            .padding(.top, exercise.videoURL == nil ? 0 : 10)
            .padding(.bottom, 12)
        }
        .frame(maxWidth: 300, alignment: .leading)
        .background(Theme.surface)
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Theme.divider, lineWidth: 1)
        )
        .shadow(color: Theme.ink.opacity(0.06), radius: 6, y: 2)
    }

    private var placeholderHeader: some View {
        HStack(spacing: 10) {
            Image(systemName: "figure.strengthtraining.traditional")
                .font(.body.weight(.semibold))
                .foregroundStyle(Theme.accent)
                .frame(width: 40, height: 40)
                .background(tagBackground, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            Text(exercise.displayName)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(Theme.ink)
                .lineLimit(2)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.top, 12)
        .padding(.bottom, 2)
    }

    private var metaRow: some View {
        HStack(spacing: 8) {
            if let muscle = exercise.muscleGroup {
                Text(muscle)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 2)
                    .background(tagBackground, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
            if let prescription {
                Text(prescription)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.muted)
                    .lineLimit(1)
            }
        }
    }

    private var prescription: String? {
        let scheme: String?
        if let sets = exercise.sets, let reps = exercise.reps {
            scheme = "\(sets)×\(reps)"
        } else if let sets = exercise.sets {
            scheme = "\(sets) sets"
        } else if let reps = exercise.reps {
            scheme = "\(reps) reps"
        } else {
            scheme = nil
        }
        let parts = [exercise.equipment, scheme].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private enum ActionKind { case primary, secondary }

    private func actionButton(_ title: String, kind: ActionKind, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 13, weight: .semibold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 9)
                .background(
                    kind == .primary ? Theme.accent : Theme.canvas,
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous)
                )
                .overlay {
                    if kind == .secondary {
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .stroke(Theme.divider, lineWidth: 1)
                    }
                }
                .foregroundStyle(kind == .primary ? Color.white : Theme.ink)
        }
        .buttonStyle(.plain)
    }
}

private struct InlineExerciseVideo: View {
    let url: URL
    @State private var player: AVPlayer?
    @State private var isPlaying = false

    var body: some View {
        ZStack {
            Theme.ink
            if isPlaying, let player {
                VideoPlayer(player: player)
            } else {
                Button(action: startPlayback) {
                    ZStack {
                        Circle()
                            .fill(Color.white.opacity(0.92))
                            .frame(width: 48, height: 48)
                            .shadow(color: .black.opacity(0.3), radius: 8, y: 4)
                        Image(systemName: "play.fill")
                            .font(.system(size: 18, weight: .bold))
                            .foregroundStyle(Theme.accent)
                            .offset(x: 2)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Play video")
            }
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .clipped()
        .onDisappear {
            player?.pause()
            isPlaying = false
        }
    }

    private func startPlayback() {
        let avPlayer = player ?? AVPlayer(url: url)
        player = avPlayer
        isPlaying = true
        avPlayer.play()
    }
}
