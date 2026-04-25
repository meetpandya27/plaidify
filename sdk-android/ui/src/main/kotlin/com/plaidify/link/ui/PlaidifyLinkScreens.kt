package com.plaidify.link.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import com.plaidify.link.PlaidifyOrganization

/**
 * Jetpack Compose screens that mirror the SwiftUI flow on iOS. These
 * are kept in a separate `ui/` source set so the JVM-only `core/`
 * module remains testable without Android dependencies. Host apps
 * integrating the SDK depend on `core` for logic and pull these
 * Compose sources via their own Android library module.
 */
@Composable
public fun PlaidifyLinkPicker(
    organizations: List<PlaidifyOrganization>,
    onSelect: (PlaidifyOrganization) -> Unit,
) {
    var query by remember { mutableStateOf("") }
    val filtered = remember(query, organizations) {
        val trimmed = query.trim()
        if (trimmed.isEmpty()) organizations
        else organizations.filter {
            it.name.contains(trimmed, ignoreCase = true) || it.site.contains(trimmed, ignoreCase = true)
        }
    }

    Column(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = "Select your bank",
            modifier = Modifier.semantics { heading() },
        )
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            label = { Text("Search institutions") },
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Search institutions" },
        )
        LazyColumn {
            items(filtered, key = { it.organizationId }) { org ->
                Button(
                    onClick = { onSelect(org) },
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .semantics { contentDescription = "Select ${org.name}" },
                ) {
                    Text("${org.name} (${org.site})")
                }
            }
        }
    }
}

@Composable
public fun PlaidifyLinkCredentials(
    organization: PlaidifyOrganization,
    onSubmit: (String, String) -> Unit,
) {
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    Column(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = "Sign in to ${organization.name}",
            modifier = Modifier.semantics { heading() },
        )
        organization.hintCopy?.let { Text(it) }
        OutlinedTextField(
            value = username,
            onValueChange = { username = it },
            label = { Text("Username") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Username" },
        )
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Password" },
        )
        Button(
            onClick = { onSubmit(username, password) },
            enabled = username.isNotEmpty() && password.isNotEmpty(),
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Continue to verify credentials" },
        ) {
            Text("Continue")
        }
    }
}

@Composable
public fun PlaidifyLinkMfa(
    prompt: String,
    onSubmit: (String) -> Unit,
) {
    var code by remember { mutableStateOf("") }
    Column(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = "Verify your identity",
            modifier = Modifier.semantics { heading() },
        )
        Text(prompt)
        OutlinedTextField(
            value = code,
            onValueChange = { code = it },
            label = { Text("Verification code") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Verification code" },
        )
        Button(
            onClick = { onSubmit(code) },
            enabled = code.isNotEmpty(),
            modifier = Modifier
                .fillMaxWidth()
                .semantics { contentDescription = "Submit verification code" },
        ) {
            Text("Submit")
        }
    }
}

@Composable
public fun PlaidifyLinkProgress(title: String) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        CircularProgressIndicator()
        Spacer(modifier = Modifier.height(8.dp))
        Text(title)
    }
}

@Composable
public fun PlaidifyLinkErrorScreen(message: String, onRetry: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = "Something went wrong",
            modifier = Modifier.semantics { heading() },
        )
        Text(message)
        Button(
            onClick = onRetry,
            modifier = Modifier.semantics { contentDescription = "Retry connection" },
        ) {
            Text("Try again")
        }
    }
}
