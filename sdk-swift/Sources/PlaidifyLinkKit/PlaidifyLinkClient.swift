import Foundation

/// Errors emitted by ``PlaidifyLinkClient``.
public enum PlaidifyLinkClientError: Error, Equatable {
    case invalidURL
    case transport(String)
    case http(status: Int, errorCode: String?, message: String)
    case decoding(String)
}

/// Status payload returned by `GET /link/sessions/{token}/status`.
public struct PlaidifyLinkSessionStatus: Codable, Equatable {
    public let status: String
    public let site: String?
    public let mfaType: String?
    public let sessionID: String?
    public let publicToken: String?
    public let errorMessage: String?

    public enum CodingKeys: String, CodingKey {
        case status
        case site
        case mfaType = "mfa_type"
        case sessionID = "session_id"
        case publicToken = "public_token"
        case errorMessage = "error_message"
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

public struct PlaidifyOrganizationSearchResponse: Codable, Equatable {
    public let organizations: [PlaidifyOrganization]
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

/// Response payload from `/connect` and `/mfa/submit`.
public struct PlaidifyConnectResponse: Codable, Equatable {
    public let status: String
    public let sessionID: String?
    public let mfaType: String?
    public let publicToken: String?
    public let jobID: String?
    public let message: String?
    public let errorMessage: String?

    public enum CodingKeys: String, CodingKey {
        case status
        case sessionID = "session_id"
        case mfaType = "mfa_type"
        case publicToken = "public_token"
        case jobID = "job_id"
        case message
        case errorMessage = "error_message"
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
/// performing network I/O.
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

    public static func mfaSubmit(serverURL: URL, sessionID: String, code: String) -> URL? {
        var components = URLComponents(string: base(serverURL) + "/mfa/submit")
        components?.queryItems = [
            URLQueryItem(name: "session_id", value: sessionID),
            URLQueryItem(name: "code", value: code),
        ]
        return components?.url
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

    public func getEncryptionPublicKey() async throws -> [String: String] {
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
        guard let url = PlaidifyLinkURLBuilder.mfaSubmit(
            serverURL: serverURL,
            sessionID: sessionID,
            code: code
        ) else {
            throw PlaidifyLinkClientError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
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
        if T.self == EmptyResponse.self {
            // Should never be requested via this typed path, but keep for clarity.
            return EmptyResponse() as! T
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw PlaidifyLinkClientError.decoding(error.localizedDescription)
        }
    }
}

private struct PlaidifyLinkErrorBody: Decodable {
    let detail: String?
    let error: String?
    let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case detail
        case error
        case errorCode = "error_code"
    }
}

/// Marker type so the generic `send` path can be used for the rare
/// no-body endpoint without making the function non-generic.
public struct EmptyResponse: Decodable {}
