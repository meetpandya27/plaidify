package com.plaidify.link

import java.security.KeyFactory
import java.security.PublicKey
import java.security.spec.MGF1ParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.OAEPParameterSpec
import javax.crypto.spec.PSource

/** Encrypts one credential to the link session's public key before it leaves the device. */
public fun interface PlaidifyCredentialEncryptor {
    /** @return the base64 ciphertext `/connect` expects. */
    public fun encrypt(plaintext: String, publicKeyPem: String): String
}

/**
 * RSA-OAEP with SHA-256 *and MGF1-SHA-256*, the scheme the server and the
 * hosted page's WebCrypto use (`src/crypto.py`). Java's
 * "OAEPWithSHA-256AndMGF1Padding" alone would use MGF1-SHA-1 and the server
 * could not decrypt it, so the parameters are spelled out.
 */
public object RsaOaepEncryptor : PlaidifyCredentialEncryptor {
    private val OAEP_SHA256: OAEPParameterSpec = OAEPParameterSpec(
        "SHA-256",
        "MGF1",
        MGF1ParameterSpec.SHA256,
        PSource.PSpecified.DEFAULT,
    )

    override fun encrypt(plaintext: String, publicKeyPem: String): String {
        val key = publicKey(publicKeyPem)
        val cipher = try {
            Cipher.getInstance("RSA/ECB/OAEPPadding").apply { init(Cipher.ENCRYPT_MODE, key, OAEP_SHA256) }
        } catch (e: Exception) {
            throw PlaidifyLinkClientException.Encryption("RSA-OAEP is unavailable: ${e.message}")
        }
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        return Base64.getEncoder().encodeToString(ciphertext)
    }

    /** A key from a PEM `PUBLIC KEY` (X.509 SubjectPublicKeyInfo). */
    internal fun publicKey(pem: String): PublicKey {
        val body = pem
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", "")
            .filterNot { it.isWhitespace() }
        return try {
            val der = Base64.getDecoder().decode(body)
            KeyFactory.getInstance("RSA").generatePublic(X509EncodedKeySpec(der))
        } catch (e: Exception) {
            throw PlaidifyLinkClientException.Encryption("The public key is not a valid RSA PEM key.")
        }
    }
}
