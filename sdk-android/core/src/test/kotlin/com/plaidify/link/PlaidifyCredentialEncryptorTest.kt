package com.plaidify.link

import org.junit.jupiter.api.Test
import java.security.KeyPairGenerator
import java.security.spec.MGF1ParameterSpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.OAEPParameterSpec
import javax.crypto.spec.PSource
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class PlaidifyCredentialEncryptorTest {
    private val keyPair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()

    private val pem: String = "-----BEGIN PUBLIC KEY-----\n" +
        Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(keyPair.public.encoded) +
        "\n-----END PUBLIC KEY-----\n"

    private fun decrypt(ciphertext: String, mgf1: MGF1ParameterSpec): String {
        val cipher = Cipher.getInstance("RSA/ECB/OAEPPadding")
        cipher.init(
            Cipher.DECRYPT_MODE,
            keyPair.private,
            OAEPParameterSpec("SHA-256", "MGF1", mgf1, PSource.PSpecified.DEFAULT),
        )
        return cipher.doFinal(Base64.getDecoder().decode(ciphertext)).toString(Charsets.UTF_8)
    }

    @Test
    fun roundTripsWithOaepSha256AndMgf1Sha256LikeTheServer() {
        val ciphertext = RsaOaepEncryptor.encrypt("hunter22 ✓", pem)
        assertEquals("hunter22 ✓", decrypt(ciphertext, MGF1ParameterSpec.SHA256))
    }

    @Test
    fun doesNotUseTheJavaDefaultMgf1Sha1() {
        // "OAEPWithSHA-256AndMGF1Padding" defaults MGF1 to SHA-1, which the
        // server (cryptography's OAEP(MGF1(SHA256), SHA256)) cannot decrypt.
        val ciphertext = RsaOaepEncryptor.encrypt("hunter22", pem)
        assertFailsWith<Exception> { decrypt(ciphertext, MGF1ParameterSpec.SHA1) }
    }

    @Test
    fun rejectsGarbageKeys() {
        assertFailsWith<PlaidifyLinkClientException.Encryption> { RsaOaepEncryptor.encrypt("x", "not a key") }
    }
}
