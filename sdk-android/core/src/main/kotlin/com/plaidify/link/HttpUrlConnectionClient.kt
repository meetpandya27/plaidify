package com.plaidify.link

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * Default [PlaidifyLinkHttpClient] on `java.net.HttpURLConnection`, which
 * both the JVM and Android ship — no extra dependency. Blocking I/O runs on
 * [Dispatchers.IO]. Inject your own (OkHttp, Ktor) to share a connection pool.
 */
public class HttpUrlConnectionClient(
    private val connectTimeoutMillis: Int = 15_000,
    private val readTimeoutMillis: Int = 60_000,
) : PlaidifyLinkHttpClient {

    override suspend fun execute(
        request: PlaidifyLinkHttpClient.HttpRequest,
    ): PlaidifyLinkHttpClient.HttpResponse = withContext(Dispatchers.IO) {
        val connection = URL(request.url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = request.method
            connection.connectTimeout = connectTimeoutMillis
            connection.readTimeout = readTimeoutMillis
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            for ((name, value) in request.headers) {
                connection.setRequestProperty(name, value)
            }
            val body = request.body
            if (body != null) {
                connection.doOutput = true
                val bytes = body.toByteArray(Charsets.UTF_8)
                connection.setFixedLengthStreamingMode(bytes.size)
                connection.outputStream.use { it.write(bytes) }
            }
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            val text = stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
            PlaidifyLinkHttpClient.HttpResponse(status, text)
        } finally {
            connection.disconnect()
        }
    }
}
