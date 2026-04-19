/**
 * Plaidify Link Page — self-contained hosted link flow.
 *
 * Reads ?token= from the URL, validates it against the server,
 * then walks the user through: Select Provider → Credentials → MFA (if needed) → Success.
 *
 * Communicates with the parent window via postMessage when embedded in an iframe.
 */
(function () {
  "use strict";

  // ── State ────────────────────────────────────────────────────────────────
  const params = new URLSearchParams(window.location.search);
  const linkToken = params.get("token");
  const serverUrl = params.get("server") || window.location.origin;
  const inIframe = window.parent !== window;

  let selectedSite = null;
  let sessionId = null;
  let blueprints = [];

  // Determine the parent origin for secure postMessage communication.
  // Accept an explicit ?origin= param, fall back to document.referrer, then "*" for non-iframe.
  const parentOrigin = (function () {
    const explicit = params.get("origin");
    if (explicit) return explicit;
    if (document.referrer) {
      try {
        const url = new URL(document.referrer);
        return url.origin;
      } catch (_) { /* ignore */ }
    }
    return inIframe ? serverUrl : "*";
  })();

  // ── Apply theme overrides from URL params ────────────────────────────────
  (function applyTheme() {
    const root = document.documentElement.style;
    const accent = params.get("accent");
    const bg = params.get("bg");
    const radius = params.get("radius");
    const logo = params.get("logo");
    if (accent) root.setProperty("--accent", accent);
    if (bg) root.setProperty("--bg", bg);
    if (radius) root.setProperty("--border-radius", radius);
    if (logo) {
      const logoEl = document.querySelector(".plaidify-logo");
      if (logoEl) logoEl.src = logo;
    }
  })();

  // ── DOM refs ─────────────────────────────────────────────────────────────
  const steps = {
    select: document.getElementById("step-select"),
    credentials: document.getElementById("step-credentials"),
    mfa: document.getElementById("step-mfa"),
    connecting: document.getElementById("step-connecting"),
    success: document.getElementById("step-success"),
    error: document.getElementById("step-error"),
  };

  const dots = [
    document.getElementById("dot-1"),
    document.getElementById("dot-2"),
    document.getElementById("dot-3"),
  ];

  // ── Helpers ──────────────────────────────────────────────────────────────

  function showStep(name, dotIndex) {
    Object.values(steps).forEach((s) => s.classList.remove("active"));
    steps[name].classList.add("active");
    dots.forEach((d, i) => {
      d.classList.remove("current", "completed");
      if (i < dotIndex) d.classList.add("completed");
      if (i === dotIndex) d.classList.add("current");
    });
    // Update progress indicator ARIA
    const indicator = document.querySelector(".step-indicator");
    if (indicator) {
      indicator.setAttribute("aria-valuenow", String(dotIndex + 1));
      indicator.setAttribute("aria-label", `Step ${dotIndex + 1} of 3`);
    }
  }

  function postEvent(event, data) {
    if (inIframe) {
      window.parent.postMessage({ source: "plaidify-link", event, ...data }, parentOrigin);
    }
  }

  async function apiCall(method, path, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(serverUrl + path, opts);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || err.error || "Request failed");
    }
    return res.json();
  }

  // ── Validate Token ───────────────────────────────────────────────────────

  async function validateToken() {
    if (!linkToken) {
      showError("No link token provided. Please use a valid link URL.");
      return false;
    }
    try {
      const data = await apiCall("GET", `/link/sessions/${encodeURIComponent(linkToken)}/status`);
      if (data.status === "expired" || data.status === "completed") {
        showError(`This link has ${data.status}. Please request a new one.`);
        return false;
      }
      return true;
    } catch {
      showError("Invalid or expired link token.");
      return false;
    }
  }

  // ── Load Blueprints ──────────────────────────────────────────────────────

  async function loadBlueprints() {
    try {
      const data = await apiCall("GET", "/blueprints");
      blueprints = data.blueprints || [];
      renderInstitutions(blueprints);
    } catch {
      document.getElementById("institution-list").innerHTML =
        '<div class="institution-empty">Failed to load providers.</div>';
    }
  }

  function _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function renderInstitutions(list) {
    const container = document.getElementById("institution-list");
    if (!list.length) {
      container.innerHTML = '<div class="institution-empty">No providers found.</div>';
      return;
    }

    const icons = { energy: "\u26A1", utility: "\uD83D\uDCA1", bank: "\uD83C\uDFE6", telecom: "\uD83D\uDCF1" };
    container.innerHTML = list
      .map(
        (bp) => `
      <div class="institution-item" data-site="${_escapeHtml(bp.site)}">
        <span class="inst-icon">${_escapeHtml(icons[bp.tags?.[0]] || "\uD83D\uDD17")}</span>
        <span class="inst-name">${_escapeHtml(bp.name)}</span>
        <span class="inst-domain">${_escapeHtml(bp.domain || "")}</span>
        <span class="inst-arrow">›</span>
      </div>`
      )
      .join("");

    container.querySelectorAll(".institution-item").forEach((item) => {
      item.addEventListener("click", () => selectInstitution(item.dataset.site));
    });
  }

  // ── Institution Search ───────────────────────────────────────────────────

  document.getElementById("institution-search").addEventListener("input", (e) => {
    const query = e.target.value.toLowerCase();
    const filtered = blueprints.filter(
      (bp) =>
        bp.name.toLowerCase().includes(query) ||
        (bp.domain || "").toLowerCase().includes(query) ||
        bp.site.toLowerCase().includes(query)
    );
    renderInstitutions(filtered);
  });

  // ── Select Institution ───────────────────────────────────────────────────

  function selectInstitution(site) {
    selectedSite = blueprints.find((bp) => bp.site === site);
    if (!selectedSite) return;

    const icons = { energy: "⚡", utility: "💡", bank: "🏦", telecom: "📱" };
    document.getElementById("provider-icon").textContent =
      icons[selectedSite.tags?.[0]] || "🔗";
    document.getElementById("provider-name").textContent = selectedSite.name;

    showStep("credentials", 1);
    postEvent("INSTITUTION_SELECTED", { site: selectedSite.site });

    // Update link session status
    apiCall("POST", `/link/sessions/${encodeURIComponent(linkToken)}/event`, {
      event: "INSTITUTION_SELECTED",
      site: selectedSite.site,
    }).catch(() => {});
  }

  // ── Change Provider ──────────────────────────────────────────────────────

  document.getElementById("change-provider-btn").addEventListener("click", () => {
    selectedSite = null;
    showStep("select", 0);
  });

  // ── Submit Credentials ───────────────────────────────────────────────────

  document.getElementById("credentials-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("link-username").value.trim();
    const password = document.getElementById("link-password").value;

    // Clear previous field errors
    document.querySelectorAll(".field-error").forEach((el) => (el.textContent = ""));
    document.querySelectorAll(".input-error").forEach((el) => el.classList.remove("input-error"));

    let hasError = false;
    if (!username) {
      const el = document.getElementById("link-username-error");
      if (el) el.textContent = "Username is required";
      document.getElementById("link-username")?.classList.add("input-error");
      hasError = true;
    }
    if (!password) {
      const el = document.getElementById("link-password-error");
      if (el) el.textContent = "Password is required";
      document.getElementById("link-password")?.classList.add("input-error");
      hasError = true;
    }
    if (hasError) return;

    const btn = document.getElementById("connect-btn");
    btn.disabled = true;
    btn.textContent = "Connecting...";

    showStep("connecting", 1);
    postEvent("CREDENTIALS_SUBMITTED", { site: selectedSite.site });

    try {
      // Fetch encryption key for the link token
      let payload = { site: selectedSite.site };
      const encSession = await apiCall("GET", `/encryption/public_key/${encodeURIComponent(linkToken)}`);
      if (!encSession || !encSession.public_key) {
        throw new Error("Encryption unavailable. Cannot send credentials securely.");
      }
      const encrypted = await encryptCredentials(encSession.public_key, username, password);
      payload.encrypted_username = encrypted.username;
      payload.encrypted_password = encrypted.password;
      payload.link_token = linkToken;

      const result = await apiCall("POST", "/connect", payload);

      if (result.status === "mfa_required") {
        sessionId = result.session_id;
        const message = result.metadata?.message || "Enter the verification code.";
        document.getElementById("mfa-message").textContent = message;
        showStep("mfa", 2);
        postEvent("MFA_REQUIRED", { mfa_type: result.mfa_type, session_id: sessionId });
        document.getElementById("mfa-code").focus();
      } else if (result.status === "connected") {
        showSuccess(result);
      } else {
        showError(result.error || "Unexpected response from server.");
      }
    } catch (err) {
      showError(err.message || "Failed to connect. Please try again.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Connect";
    }
  });

  // ── Submit MFA ───────────────────────────────────────────────────────────

  document.getElementById("mfa-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = document.getElementById("mfa-code").value.trim();
    if (!code || !sessionId) return;

    const btn = document.getElementById("mfa-submit-btn");
    btn.disabled = true;
    btn.textContent = "Verifying...";

    postEvent("MFA_SUBMITTED", { session_id: sessionId });

    try {
      const result = await apiCall(
        "POST",
        `/mfa/submit?session_id=${encodeURIComponent(sessionId)}&code=${encodeURIComponent(code)}`
      );

      if (result.status === "success" || result.status === "connected") {
        showSuccess(result);
      } else if (result.status === "error") {
        document.getElementById("mfa-code").value = "";
        document.getElementById("mfa-code").focus();
        document.getElementById("mfa-message").textContent =
          result.error || "Invalid code. Please try again.";
      } else {
        showSuccess(result);
      }
    } catch (err) {
      showError(err.message || "MFA verification failed.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Verify";
    }
  });

  // ── Success ──────────────────────────────────────────────────────────────

  function showSuccess(result) {
    const accessToken = result.access_token || result.data?.access_token || "";
    if (accessToken) {
      document.getElementById("access-token-display").textContent = accessToken;
      document.getElementById("access-token-display").style.display = "block";
    } else {
      document.getElementById("access-token-display").style.display = "none";
    }

    showStep("success", 2);
    postEvent("CONNECTED", { access_token: accessToken, data: result.data });

    // Update session status
    apiCall("POST", `/link/sessions/${encodeURIComponent(linkToken)}/event`, {
      event: "CONNECTED",
      access_token: accessToken,
    }).catch(() => {});
  }

  // ── Error ────────────────────────────────────────────────────────────────

  function showError(message) {
    document.getElementById("error-message").textContent = message;
    showStep("error", 0);
    postEvent("ERROR", { error: message });
  }

  // ── Close / Done ─────────────────────────────────────────────────────────

  document.getElementById("close-btn").addEventListener("click", () => {
    postEvent("EXIT", { reason: "user_closed" });
    if (!inIframe) window.close();
  });

  document.getElementById("done-btn").addEventListener("click", () => {
    postEvent("DONE", {});
    if (!inIframe) window.close();
  });

  // Keyboard navigation: Escape to close
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      postEvent("EXIT", { reason: "user_pressed_escape" });
      if (!inIframe) window.close();
    }
  });

  document.getElementById("retry-btn").addEventListener("click", () => {
    selectedSite = null;
    sessionId = null;
    document.getElementById("link-username").value = "";
    document.getElementById("link-password").value = "";
    document.getElementById("mfa-code").value = "";
    showStep("select", 0);
    loadBlueprints();
  });

  // ── RSA Encryption (Web Crypto API) ──────────────────────────────────────

  async function encryptCredentials(pemPublicKey, username, password) {
    // Parse PEM to ArrayBuffer
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

    const encUser = await crypto.subtle.encrypt(
      { name: "RSA-OAEP" },
      cryptoKey,
      new TextEncoder().encode(username)
    );
    const encPass = await crypto.subtle.encrypt(
      { name: "RSA-OAEP" },
      cryptoKey,
      new TextEncoder().encode(password)
    );

    return {
      username: btoa(String.fromCharCode(...new Uint8Array(encUser))),
      password: btoa(String.fromCharCode(...new Uint8Array(encPass))),
    };
  }

  // ── Init ─────────────────────────────────────────────────────────────────

  async function init() {
    const valid = await validateToken();
    if (valid) {
      await loadBlueprints();
    }
  }

  init();
})();
