import Foundation

/// Errors emitted by ``PlaidifyLinkClient``.
public enum PlaidifyLinkClientError: Error, Equatable {
    case invalidURL
    case transport(String)
    case http(status: Int, errorCode: String?, message: String)
    case decoding(String)
    case encryption(String)
}

/// Status payload returned by `GET /link/sessions/{token}/status`.
public struct PlaidifyLinkSessionStatus: Codable, Equatable {
    public let status: String
    public let site: String?
    public let mfaType: String?
    public let sessionID: String?
    public let publicToken: String?
    public let jobID: String?
    public let message: String?
    public let errorMessage: String?

    public enum CodingKeys: String, CodingKey {
        case status
        case site
        case mfaType = "mfa_type"
        case sessionID = "session_id"
        case publicToken = "public_token"
        case jobID = "job_id"
        case message
        case errorMessage = "error_message"
    }

    public init(
        status: String,
        site: String? = nil,
        mfaType: String? = nil,
        sessionID: String? = nil,
        publicToken: String? = nil,
        jobID: String? = nil,
        message: String? = nil,
        errorMessage: String? = nil
    ) {
        self.status = status
        self.site = site
        self.mfaType = mfaType
        self.sessionID = sessionID
        self.publicToken = publicToken
        self.jobID = jobID
        self.message = message
        self.errorMessage = errorMessage
    }
}

/// Organization record returned by `/organizations/search`.
public struct PlaidifyOrganization: Codable, Equatable, Identifiable {
    public let organizationID: String
    public let name: String
    public let site: String
    public let logoURL: String?
    public let primaryColor: String?
    public let accentColor: String?
    public let secondaryColor: String?
    public let hintCopy: String?
    public let authStyle: String?

    public var id: String { organizationID }

    public enum CodingKeys: String, CodingKey {
        case organizationID = "organization_id"
        case name
        case site
        case logoURL = "logo_url"
        case primaryColor = "primary_color"
        case accentColor = "accent_color"
        case secondaryColor = "secondary_color"
        case hintCopy = "hint_copy"
        case authStyle = "auth_style"
    }
}

/// `GET /organizations/search` — the matches are under `results`.
public struct PlaidifyOrganizationSearchResponse: Codable, Equatable {
    public let results: [PlaidifyOrganization]
    public let count: Int?

    public init(results: [PlaidifyOrganization], count: Int? = nil) {
        self.results = results
        self.count = count
    }
}

/// `GET /encryption/public_key/{link_token}`.
public struct PlaidifyEncryptionKey: Codable, Equatable {
    public let publicKey: String

    public enum CodingKeys: String, CodingKey {
        case publicKey = "public_key"
    }
}

/// Encrypted credential pair posted to `/connect`.
public struct PlaidifyEncryptedCredentials: Equatable {
    public let username: String
    public let password: String

    public init(username: String, password: String) {
        self.username = username
        self.password = password
    }
}

/// Extra detail on a connect / MFA reply; `message` is the prompt to show.
public struct PlaidifyConnectMetadata: Codable, Equatable {
    public let message: String?

    public init(message: String? = nil) {
        self.message = message
    }
}

/// Response payload from `/connect` and `/mfa/submit`.
///
/// `status` is `connected`, `mfa_required`, `pending` (still running —
/// poll the session), `mfa_submitted` (code accepted; poll the session) or
/// `error` (with `error`).
public struct PlaidifyConnectResponse: Codable, Equatable {
    public let status: String
    public let sessionID: String?
    public let mfaType: String?
    public let publicToken: String?
    public let jobID: String?
    public let message: String?
    public let error: String?
    public let metadata: PlaidifyConnectMetadata?

    public enum CodingKeys: String, CodingKey {
        case status
        case sessionID = "session_id"
        case mfaType = "mfa_type"
        case publicToken = "public_token"
        case jobID = "job_id"
        case message
        case error
        case metadata
    }

    public init(
        status: String,
        sessionID: String? = nil,
        mfaType: String? = nil,
        publicToken: String? = nil,
        jobID: String? = nil,
        message: String? = nil,
        error: String? = nil,
        metadata: PlaidifyConnectMetadata? = nil
    ) {
        self.status = status
        self.sessionID = sessionID
        self.mfaType = mfaType
        self.publicToken = publicToken
        self.jobID = jobID
        self.message = message
        self.error = error
        self.metadata = metadata
    }
}

/// Minimal abstraction over `URLSession` so the client is testable.
public protocol PlaidifyLinkHTTPClient {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}

extension URLSession: PlaidifyLinkHTTPClient {}

/// Builds canonical URLs for Plaidify Link REST endpoints.
///
/// Extracted from the client so it can be unit-tested without
/// performing network I/O. Secrets (codes, credentials) never go in these
/// URLs; they travel in request bodies.
public enum PlaidifyLinkURLBuilder {
    public static func base(_ serverURL: URL) -> String {
        let absolute = serverURL.absoluteString
        return absolute.hasSuffix("/") ? String(absolute.dropLast()) : absolute
    }

    public static func status(serverURL: URL, linkToken: String) -> URL? {
        let escaped = linkToken.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? linkToken
        return URL(string: base(serverURL) + "/link/sessions/\(escaped)/status")
    }

    public static func organizationSearch(
        serverURL: URL,
        query: String?,
        site: String?,
        limit: Int
    ) -> URL? {
        var components = URLComponents(string: base(serverURL) + "/organizations/search")
        var items: [URLQueryItem] = [URLQueryItem(name: "limit", value: String(limit))]
        if let query, !query.isEmpty {
            items.append(URLQueryItem(name: "q", value: query))
        }
        if let site, !site.isEmpty {
            items.append(URLQueryItem(name: "site", value: site))
        }
        components?.queryItems = items
        return components?.url
    }

    public static func encryptionPublicKey(serverURL: URL, linkToken: String) -> URL? {
        let escaped = linkToken.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? linkToken
        return URL(string: base(serverURL) + "/encryption/public_key/\(escaped)")
    }

    public static func connect(serverURL: URL) -> URL? {
        URL(string: base(serverURL) + "/connect")
    }

    public static func mfaSubmit(serverURL: URL) -> URL? {
        URL(string: base(serverURL) + "/mfa/submit")
    }
}

/// REST client that talks to the same hosted-link endpoints as the
/// React frontend. All methods are `async throws`.
public final class PlaidifyLinkClient {
    public let serverURL: URL
    public let linkToken: String

    private let http: PlaidifyLinkHTTPClient
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(
        serverURL: URL,
        linkToken: String,
        http: PlaidifyLinkHTTPClient = URLSession.shared
    ) {
        self.serverURL = serverURL
        self.linkToken = linkToken
        self.http = http
        self.decoder = JSONDecoder()
        self.encoder = JSONEncoder()
    }

    public func getStatus() async throws -> PlaidifyLinkSessionStatus {
        guard let url = PlaidifyLinkURLBuilder.status(serverURL: serverURL, linkToken: linkToken) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        return try await send(URLRequest(url: url))
    }

    public func searchOrganizations(
        query: String? = nil,
        site: String? = nil,
        limit: Int = 40
    ) async throws -> PlaidifyOrganizationSearchResponse {
        guard let url = PlaidifyLinkURLBuilder.organizationSearch(
            serverURL: serverURL,
            query: query,
            site: site,
            limit: limit
        ) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        return try await send(URLRequest(url: url))
    }

    public func getEncryptionPublicKey() async throws -> PlaidifyEncryptionKey {
        guard let url = PlaidifyLinkURLBuilder.encryptionPublicKey(
            serverURL: serverURL,
            linkToken: linkToken
        ) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        return try await send(URLRequest(url: url))
    }

    public func connect(
        site: String,
        encrypted: PlaidifyEncryptedCredentials
    ) async throws -> PlaidifyConnectResponse {
        guard let url = PlaidifyLinkURLBuilder.connect(serverURL: serverURL) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        let body: [String: String] = [
            "link_token": linkToken,
            "site": site,
            "encrypted_username": encrypted.username,
            "encrypted_password": encrypted.password,
        ]
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(body)
        return try await send(request)
    }

    public func submitMFA(sessionID: String, code: String) async throws -> PlaidifyConnectResponse {
        guard let url = PlaidifyLinkURLBuilder.mfaSubmit(serverURL: serverURL) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(["session_id": sessionID, "code": code])
        return try await send(request)
    }

    // MARK: - Internals

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        var req = request
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await http.data(for: req)
        } catch {
            throw PlaidifyLinkClientError.transport(error.localizedDescription)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if !(200..<300).contains(status) {
            let info = try? decoder.decode(PlaidifyLinkErrorBody.self, from: data)
            throw PlaidifyLinkClientError.http(
                status: status,
                errorCode: info?.errorCode,
                message: info?.detail ?? info?.error ?? "HTTP \(status)"
            )
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw PlaidifyLinkClientError.decoding(error.localizedDescription)
        }
    }
}

/// `{"detail": str | [{"msg": str}]}` (FastAPI) or `{"error", "error_code"}`.
private struct PlaidifyLinkErrorBody: Decodable {
    let detail: String?
    let error: String?
    let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case detail
        case error
        case errorCode = "error_code"
    }

    private struct ValidationIssue: Decodable {
        let msg: String?
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let text = try? container.decode(String.self, forKey: .detail) {
            detail = text
        } else if let issues = try? container.decode([ValidationIssue].self, forKey: .detail) {
            let messages = issues.compactMap(\.msg)
            detail = messages.isEmpty ? nil : messages.joined(separator: "; ")
        } else {
            detail = nil
        }
        error = try? container.decode(String.self, forKey: .error)
        errorCode = try? container.decode(String.self, forKey: .errorCode)
    }
}

/// Marker type for endpoints whose reply body carries nothing of interest.
public struct EmptyResponse: Decodable {}
