import Foundation

struct SavedChat: Codable, Equatable {
    struct Line: Codable, Equatable {
        let fromUser: Bool
        let text: String
    }
    let day: String
    let sessionID: UUID
    var lines: [Line]
    static let maxLines = 200
}

protocol ChatJournal {
    func load() -> SavedChat?
    func save(_ chat: SavedChat)
    func clear()
}

struct FileChatJournal: ChatJournal {
    let url: URL
    init(url: URL? = nil) {
        if let url { self.url = url; return }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        self.url = base.appendingPathComponent("macrobit-chat-today.json")
    }
    func load() -> SavedChat? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(SavedChat.self, from: data)
    }
    func save(_ chat: SavedChat) {
        guard let data = try? JSONEncoder().encode(chat) else { return }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
    func clear() { try? FileManager.default.removeItem(at: url) }
}

struct MealSuggestion: Codable, Identifiable, Equatable {
    var id: String { entryId }
    let entryId: String
    let name: String
    let meal: String
    let times: Int
    let calories: Double
    let protein: Double?
    let carbs: Double?
    let fat: Double?
    let fiber: Double?
}

struct MealSuggestionsPayload: Codable, Equatable {
    let days: Int
    let suggestions: [MealSuggestion]
}
