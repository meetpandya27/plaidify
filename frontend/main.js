"use strict";

window.addEventListener("load", () => {
  // Handle Create Link form submission
  const createLinkForm = document.getElementById("create-link-form");
  createLinkForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const site = document.getElementById("create-link-site").value.trim();
    const resultElement = document.getElementById("create-link-result");

    try {
      const response = await fetch(`/create_link?site=${encodeURIComponent(site)}`, {
        method: "POST"
      });
      if (!response.ok) {
        throw new Error(`Error creating link token: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = `Link Token: ${data.link_token}`;
    } catch (error) {
      resultElement.textContent = `Failed to create link token: ${error.message}`;
    }
  });

  // Handle Submit Credentials form
  const submitCredentialsForm = document.getElementById("submit-credentials-form");
  submitCredentialsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const linkToken = document.getElementById("submit-link-token").value.trim();
    const username = document.getElementById("submit-username").value.trim();
    const password = document.getElementById("submit-password").value.trim();
    const resultElement = document.getElementById("submit-credentials-result");

    try {
      // Try to get the public key for the link_token and encrypt
      let url;
      try {
        const keyRes = await fetch(`/encryption/public_key/${encodeURIComponent(linkToken)}`);
        if (keyRes.ok) {
          const keyData = await keyRes.json();
          const encUser = await encryptWithPublicKey(keyData.public_key, username);
          const encPass = await encryptWithPublicKey(keyData.public_key, password);
          url = `/submit_credentials?link_token=${encodeURIComponent(linkToken)}&encrypted_username=${encodeURIComponent(encUser)}&encrypted_password=${encodeURIComponent(encPass)}`;
        } else {
          throw new Error("no key");
        }
      } catch {
        // Fallback to plaintext
        url = `/submit_credentials?link_token=${encodeURIComponent(linkToken)}&username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}`;
      }

      const response = await fetch(url, { method: "POST" });
      if (!response.ok) {
        throw new Error(`Error submitting credentials: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = `Access Token: ${data.access_token}`;
    } catch (error) {
      resultElement.textContent = `Failed to submit credentials: ${error.message}`;
    }
  });

  // Handle Fetch Data form
  const fetchDataForm = document.getElementById("fetch-data-form");
  fetchDataForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const accessToken = document.getElementById("fetch-access-token").value.trim();
    const resultElement = document.getElementById("fetch-data-result");

    try {
      const response = await fetch(`/fetch_data?access_token=${encodeURIComponent(accessToken)}`, {
        method: "GET"
      });
      if (!response.ok) {
        throw new Error(`Error fetching data: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = JSON.stringify(data, null, 2);
    } catch (error) {
      resultElement.textContent = `Failed to fetch data: ${error.message}`;
    }
  });
});

// ── Client-side Encryption (WebCrypto) ───────────────────────────────────────

async function encryptWithPublicKey(pemPublicKey, plaintext) {
  const pemBody = pemPublicKey
    .replace(/-----BEGIN PUBLIC KEY-----/, "")
    .replace(/-----END PUBLIC KEY-----/, "")
    .replace(/\s/g, "");
  const binaryDer = Uint8Array.from(atob(pemBody), (c) => c.charCodeAt(0));

  const cryptoKey = await crypto.subtle.importKey(
    "spki",
    binaryDer.buffer,
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"]
  );

  const encoded = new TextEncoder().encode(plaintext);
  const cipherBuffer = await crypto.subtle.encrypt(
    { name: "RSA-OAEP" },
    cryptoKey,
    encoded
  );

  const cipherArray = new Uint8Array(cipherBuffer);
  let binary = "";
  for (let i = 0; i < cipherArray.length; i++) {
    binary += String.fromCharCode(cipherArray[i]);
  }
  return btoa(binary);
}