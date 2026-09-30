import Foundation
import Security

/// Encrypts one credential to the link session's public key before it
/// leaves the device.
public protocol PlaidifyCredentialEncrypting {
    /// - Returns: the base64 ciphertext `/connect` expects.
    func encrypt(_ plaintext: String, publicKeyPEM: String) throws -> String
}

/// RSA-OAEP with SHA-256 (and MGF1-SHA-256), the scheme the server and the
/// hosted page's WebCrypto use (`src/crypto.py`).
public struct PlaidifyRSAOAEPEncryptor: PlaidifyCredentialEncrypting {
    public init() {}

    public func encrypt(_ plaintext: String, publicKeyPEM: String) throws -> String {
        let key = try Self.publicKey(fromPEM: publicKeyPEM)
        var error: Unmanaged<CFError>?
        guard let ciphertext = SecKeyCreateEncryptedData(
            key,
            .rsaEncryptionOAEPSHA256,
            Data(plaintext.utf8) as CFData,
            &error
        ) as Data? else {
            let reason = error?.takeRetainedValue().localizedDescription ?? "unknown error"
            throw PlaidifyLinkClientError.encryption("RSA-OAEP encryption failed: \(reason)")
        }
        return ciphertext.base64EncodedString()
    }

    /// A `SecKey` from a PEM `PUBLIC KEY` (X.509 SubjectPublicKeyInfo).
    static func publicKey(fromPEM pem: String) throws -> SecKey {
        let base64 = pem
            .replacingOccurrences(of: "-----BEGIN PUBLIC KEY-----", with: "")
            .replacingOccurrences(of: "-----END PUBLIC KEY-----", with: "")
            .components(separatedBy: .whitespacesAndNewlines)
            .joined()
        guard let der = Data(base64Encoded: base64) else {
            throw PlaidifyLinkClientError.encryption("The public key is not valid base64 PEM.")
        }
        let rsaPublicKey = try rsaPublicKey(fromSubjectPublicKeyInfo: der)
        let attributes: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeRSA,
            kSecAttrKeyClass: kSecAttrKeyClassPublic,
        ]
        var error: Unmanaged<CFError>?
        guard let key = SecKeyCreateWithData(rsaPublicKey as CFData, attributes as CFDictionary, &error) else {
            let reason = error?.takeRetainedValue().localizedDescription ?? "unknown error"
            throw PlaidifyLinkClientError.encryption("The public key was rejected: \(reason)")
        }
        return key
    }

    /// DER of `rsaEncryption` (1.2.840.113549.1.1.1).
    private static let rsaEncryptionOID: [UInt8] = [0x06, 0x09, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01]

    /// The PKCS#1 `RSAPublicKey` inside a SubjectPublicKeyInfo, which is
    /// what `SecKeyCreateWithData` takes:
    ///
    ///     SubjectPublicKeyInfo ::= SEQUENCE {
    ///         algorithm         AlgorithmIdentifier,   -- rsaEncryption
    ///         subjectPublicKey  BIT STRING }           -- RSAPublicKey
    static func rsaPublicKey(fromSubjectPublicKeyInfo der: Data) throws -> Data {
        var outer = DERReader(der)
        var spki = DERReader(try outer.read(tag: 0x30))
        let algorithm = try spki.read(tag: 0x30)
        guard algorithm.starts(with: rsaEncryptionOID) else {
            throw PlaidifyLinkClientError.encryption("The public key is not an RSA key.")
        }
        let bitString = try spki.read(tag: 0x03)
        guard bitString.first == 0x00 else {
            throw PlaidifyLinkClientError.encryption("The public key has an unexpected bit string.")
        }
        return Data(bitString.dropFirst())
    }
}

/// Just enough DER to walk a SubjectPublicKeyInfo.
private struct DERReader {
    private let bytes: [UInt8]
    private var offset = 0

    init(_ data: Data) {
        bytes = [UInt8](data)
    }

    /// The contents of the next element, which must carry `tag`.
    mutating func read(tag: UInt8) throws -> Data {
        guard offset < bytes.count, bytes[offset] == tag else {
            throw PlaidifyLinkClientError.encryption("Malformed public key (expected DER tag \(tag)).")
        }
        offset += 1
        let length = try readLength()
        guard length <= bytes.count - offset else {
            throw PlaidifyLinkClientError.encryption("Malformed public key (truncated).")
        }
        defer { offset += length }
        return Data(bytes[offset..<offset + length])
    }

    private mutating func readLength() throws -> Int {
        guard offset < bytes.count else {
            throw PlaidifyLinkClientError.encryption("Malformed public key (no length).")
        }
        let first = bytes[offset]
        offset += 1
        if first & 0x80 == 0 {
            return Int(first)
        }
        let count = Int(first & 0x7F)
        guard (1...4).contains(count), offset + count <= bytes.count else {
            throw PlaidifyLinkClientError.encryption("Malformed public key (bad length).")
        }
        var length = 0
        for _ in 0..<count {
            length = (length << 8) | Int(bytes[offset])
            offset += 1
        }
        return length
    }
}
