#if canImport(SwiftUI)
import Foundation
import SwiftUI

/// SwiftUI screens that render the Plaidify hosted Link flow natively.
///
/// These views are intentionally lightweight: they own no business
/// logic, deferring all decisions to ``PlaidifyLinkConnectFlow`` and
/// ``PlaidifyLinkClient``. Embedders that want a different look-and-feel
/// can replace the views entirely while keeping the flow + client.
@available(iOS 15.0, macOS 13.0, *)
public struct PlaidifyLinkPickerView: View {
    public let organizations: [PlaidifyOrganization]
    public let onSelect: (PlaidifyOrganization) -> Void
    @State private var query: String = ""

    public init(
        organizations: [PlaidifyOrganization],
        onSelect: @escaping (PlaidifyOrganization) -> Void
    ) {
        self.organizations = organizations
        self.onSelect = onSelect
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Select your bank")
                .font(.title2.bold())
                .accessibilityAddTraits(.isHeader)
            TextField("Search institutions", text: $query)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("Search institutions")
            List(filtered) { organization in
                Button {
                    onSelect(organization)
                } label: {
                    HStack(spacing: 12) {
                        Text(organization.name)
                            .font(.body)
                        Spacer()
                        Text(organization.site)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                .accessibilityLabel("Select \(organization.name)")
            }
        }
        .padding()
    }

    private var filtered: [PlaidifyOrganization] {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return organizations }
        return organizations.filter { org in
            org.name.range(of: trimmed, options: .caseInsensitive) != nil
                || org.site.range(of: trimmed, options: .caseInsensitive) != nil
        }
    }
}

@available(iOS 15.0, macOS 13.0, *)
public struct PlaidifyLinkCredentialsView: View {
    public let organization: PlaidifyOrganization
    public let onSubmit: (String, String) -> Void
    @State private var username: String = ""
    @State private var password: String = ""

    public init(
        organization: PlaidifyOrganization,
        onSubmit: @escaping (String, String) -> Void
    ) {
        self.organization = organization
        self.onSubmit = onSubmit
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Sign in to \(organization.name)")
                .font(.title2.bold())
                .accessibilityAddTraits(.isHeader)
            if let hint = organization.hintCopy {
                Text(hint)
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
            TextField("Username", text: $username)
                .textFieldStyle(.roundedBorder)
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.emailAddress)
                #endif
                .accessibilityLabel("Username")
            SecureField("Password", text: $password)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("Password")
            Button {
                onSubmit(username, password)
            } label: {
                Text("Continue")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .disabled(username.isEmpty || password.isEmpty)
            .accessibilityLabel("Continue to verify credentials")
        }
        .padding()
    }
}

@available(iOS 15.0, macOS 13.0, *)
public struct PlaidifyLinkMFAView: View {
    public let prompt: String
    public let onSubmit: (String) -> Void
    @State private var code: String = ""

    public init(prompt: String, onSubmit: @escaping (String) -> Void) {
        self.prompt = prompt
        self.onSubmit = onSubmit
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Verify your identity")
                .font(.title2.bold())
                .accessibilityAddTraits(.isHeader)
            Text(prompt)
                .font(.callout)
                .foregroundColor(.secondary)
            TextField("Verification code", text: $code)
                .textFieldStyle(.roundedBorder)
                #if os(iOS)
                .keyboardType(.numberPad)
                #endif
                .accessibilityLabel("Verification code")
            Button {
                onSubmit(code)
            } label: {
                Text("Submit")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .disabled(code.isEmpty)
            .accessibilityLabel("Submit verification code")
        }
        .padding()
    }
}

@available(iOS 15.0, macOS 13.0, *)
public struct PlaidifyLinkProgressView: View {
    public let title: String
    public init(title: String) {
        self.title = title
    }
    public var body: some View {
        VStack(spacing: 16) {
            ProgressView()
                .accessibilityLabel(title)
            Text(title)
                .font(.body)
        }
        .padding()
    }
}

@available(iOS 15.0, macOS 13.0, *)
public struct PlaidifyLinkErrorView: View {
    public let message: String
    public let onRetry: () -> Void
    public init(message: String, onRetry: @escaping () -> Void) {
        self.message = message
        self.onRetry = onRetry
    }
    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Something went wrong")
                .font(.title2.bold())
                .accessibilityAddTraits(.isHeader)
            Text(message)
                .font(.callout)
                .foregroundColor(.secondary)
            Button("Try again", action: onRetry)
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("Retry connection")
        }
        .padding()
    }
}
#endif
