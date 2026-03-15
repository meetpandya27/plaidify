/**
 * Plaidify Interactive Demo — GreenGrid Energy
 *
 * Drives the connect-widget flow:
 *   Step 1: Load blueprints → select provider
 *   Step 2: Enter credentials
 *   Step 3: POST /connect → animate progress
 *   Step 3b: MFA challenge (if needed)
 *   Step 4: Display results
 */

"use strict";

// ── State ────────────────────────────────────────────────────────────────────

const state = {
  selectedSite: null,
  selectedName: null,
  sessionId: null,
  startTime: null,
  apiResponse: null,
};

// ── DOM Helpers ──────────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function showStep(stepId) {
  $$(".widget-step").forEach((s) => (s.dataset.active = "false"));
  const step = $(`#${stepId}`);
  if (step) step.dataset.active = "true";
}

function timestamp() {
  return new Date().toLocaleTimeString("en-US", { hour12: false });
}

function log(text, type = "info") {
  const body = $("#console-log");
  const entry = document.createElement("div");
  entry.className = `console-entry ${type}`;
  entry.innerHTML = `<span class="console-time">${timestamp()}</span><span class="console-text">${text}</span>`;
  body.appendChild(entry);
  body.scrollTop = body.scrollHeight;
}

// ── Step 1: Load Blueprints ──────────────────────────────────────────────────

async function loadBlueprints() {
  const list = $("#institution-list");

  try {
    log("GET /blueprints", "request");
    const res = await fetch("/blueprints");
    const data = await res.json();
    log(`Found ${data.count} blueprint(s)`, "response");

    list.innerHTML = "";

    if (data.blueprints.length === 0) {
      list.innerHTML = `<div class="institution-item"><span class="inst-icon">⚠️</span><div class="inst-info"><div class="inst-name">No blueprints found</div><div class="inst-domain">Add a .json file to /connectors</div></div></div>`;
      return;
    }

    // Map icons to tags
    const iconMap = {
      utility: "⚡", energy: "⚡", banking: "🏦", test: "🧪",
      finance: "💰", healthcare: "🏥", insurance: "🛡️",
    };

    data.blueprints.forEach((bp) => {
      const icon = bp.tags.reduce((acc, t) => acc || iconMap[t], null) || "🌐";
      const tags = bp.tags.map((t) => `<span class="inst-tag">${t}</span>`).join("");

      const item = document.createElement("div");
      item.className = "institution-item";
      item.innerHTML = `
        <span class="inst-icon">${icon}</span>
        <div class="inst-info">
          <div class="inst-name">${bp.name}</div>
          <div class="inst-domain">${bp.domain}</div>
          <div class="inst-tags">${tags}${bp.has_mfa ? '<span class="inst-tag" style="background:#f59e0b22;color:#f59e0b;">MFA</span>' : ""}</div>
        </div>
        <span class="inst-arrow">›</span>
      `;
      item.addEventListener("click", () => selectProvider(bp.site, bp.name, icon));
      list.appendChild(item);
    });
  } catch (err) {
    log(`Failed to load blueprints: ${err.message}`, "error");
    list.innerHTML = `<div class="institution-item"><span class="inst-icon">❌</span><div class="inst-info"><div class="inst-name">Error loading providers</div><div class="inst-domain">${err.message}</div></div></div>`;
  }
}

function selectProvider(site, name, icon) {
  state.selectedSite = site;
  state.selectedName = name;
  log(`Selected: ${name} (${site})`, "info");

  // Update credential step with provider info
  $("#selected-bank-name").textContent = name;
  $(".bank-icon").textContent = icon;
  $("#success-site").textContent = name;

  showStep("step-credentials");
  $("#demo-username").focus();
}

// ── Step 2: Credentials & Connect ────────────────────────────────────────────

function setupCredentialsForm() {
  // Pre-fill buttons
  $$(".hint-btn[data-user]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#demo-username").value = btn.dataset.user;
      $("#demo-password").value = btn.dataset.pass;
    });
  });

  // Change provider button
  $("#change-bank-btn").addEventListener("click", () => {
    showStep("step-select");
  });

  // Connect form submit
  $("#credentials-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = $("#demo-username").value.trim();
    const password = $("#demo-password").value.trim();

    if (!username || !password) {
      log("Please enter both username and password", "error");
      return;
    }

    await doConnect(username, password);
  });
}

async function doConnect(username, password) {
  state.startTime = performance.now();
  showStep("step-connecting");

  // Reset progress indicators
  ["prog-browser", "prog-navigate", "prog-auth", "prog-extract"].forEach((id) => {
    $(`#${id} .prog-icon`).textContent = "○";
    $(`#${id} .prog-icon`).className = "prog-icon";
    $(`#${id} .prog-text`).className = "prog-text";
  });

  // Animate progress steps with delays
  await animateProgress("prog-browser", "Launching browser...", 400);
  await animateProgress("prog-navigate", "Navigating to portal...", 300);

  log(`POST /connect { site: "${state.selectedSite}", username: "${username}" }`, "request");

  try {
    await markActive("prog-auth", "Authenticating...");

    const res = await fetch("/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site: state.selectedSite,
        username,
        password,
      }),
    });

    const data = await res.json();
    log(`Response: ${res.status} ${res.statusText}`, "response");

    if (data.status === "mfa_required") {
      await completeProg("prog-auth");
      log("MFA required — waiting for verification code", "info");
      state.sessionId = data.session_id;
      showStep("step-mfa");
      $("#mfa-code").focus();
      return;
    }

    if (data.status === "connected") {
      await completeProg("prog-auth");
      await animateProgress("prog-extract", "Extracting data...", 200);

      state.apiResponse = data;
      showResults(data);
      showSuccess(data);
      return;
    }

    // Error
    throw new Error(data.error || data.detail || `Status: ${data.status}`);
  } catch (err) {
    log(`Connection failed: ${err.message}`, "error");
    $("#error-message").textContent = err.message;
    showStep("step-error");
  }
}

// ── Progress Animation ───────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function animateProgress(id, logMsg, delay) {
  await markActive(id, logMsg);
  await sleep(delay);
  await completeProg(id);
}

async function markActive(id, logMsg) {
  const icon = $(`#${id} .prog-icon`);
  const text = $(`#${id} .prog-text`);
  icon.textContent = "◌";
  icon.className = "prog-icon active";
  text.className = "prog-text";
  if (logMsg) log(logMsg, "info");
}

async function completeProg(id) {
  const icon = $(`#${id} .prog-icon`);
  const text = $(`#${id} .prog-text`);
  icon.textContent = "●";
  icon.className = "prog-icon done";
  text.className = "prog-text done";
}

// ── MFA ──────────────────────────────────────────────────────────────────────

function setupMFA() {
  $("#mfa-hint-btn").addEventListener("click", () => {
    $("#mfa-code").value = "123456";
  });

  $("#mfa-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("#mfa-code").value.trim();
    if (!code) return;

    log(`POST /mfa/submit { session_id: "...", code: "${code}" }`, "request");

    try {
      const res = await fetch(`/mfa/submit?session_id=${state.sessionId}&code=${code}`);
      const data = await res.json();
      log(`MFA response: ${data.status}`, "response");

      if (data.status === "mfa_submitted") {
        log("MFA verified — connection should resume", "success");
        // In a real flow, the original /connect call would resume.
        // For demo purposes, show success with mock data.
        showStep("step-success");
      } else {
        log(`MFA error: ${data.error}`, "error");
      }
    } catch (err) {
      log(`MFA submit failed: ${err.message}`, "error");
    }
  });
}

// ── Success & Results ────────────────────────────────────────────────────────

function showSuccess(data) {
  const elapsed = ((performance.now() - state.startTime) / 1000).toFixed(1);
  const fieldCount = data.data ? Object.keys(data.data).length : 0;

  $("#field-count").textContent = fieldCount;
  $("#elapsed-time").textContent = elapsed;

  log(`✓ Extracted ${fieldCount} fields in ${elapsed}s`, "success");
  showStep("step-success");
}

function showResults(data) {
  const d = data.data || {};
  const section = $("#results-section");
  section.style.display = "block";

  // Account card
  setText("res-bill", d.current_bill != null ? `$${d.current_bill}` : "—");
  setText("res-usage", d.usage_kwh || "—");
  setText("res-account-number", d.account_number || "—");
  setText("res-status", d.account_status || "—");

  // Service card
  setText("res-address", d.service_address || "—");
  setText("res-plan", d.plan_name || "—");
  setText("res-meter", d.meter_id || "—");
  setText("res-next-read", d.next_read_date || "—");

  // Customer card
  setText("res-name", d.customer_name || "—");
  setText("res-email", d.customer_email || "—");
  setText("res-phone", d.customer_phone || "—");

  // Usage history table
  const usageBody = $("#usage-body");
  if (d.usage_history && d.usage_history.length > 0) {
    $("#usage-count").textContent = `${d.usage_history.length} months`;
    usageBody.innerHTML = d.usage_history
      .map(
        (row) => `
      <tr>
        <td>${row.month || "—"}</td>
        <td>${row.kwh != null ? Number(row.kwh).toLocaleString() : "—"}</td>
        <td class="amount-col">${row.cost != null ? `$${Number(row.cost).toFixed(2)}` : "—"}</td>
        <td>${row.avg_temp || "—"}</td>
      </tr>`
      )
      .join("");
  }

  // Payment history table
  const paymentBody = $("#payment-body");
  if (d.payments && d.payments.length > 0) {
    $("#payment-count").textContent = `${d.payments.length} items`;
    paymentBody.innerHTML = d.payments
      .map(
        (row) => `
      <tr>
        <td>${row.date || "—"}</td>
        <td>${row.description || "—"}</td>
        <td class="amount-col amount-neg">${row.amount != null ? `$${Math.abs(Number(row.amount)).toFixed(2)}` : "—"}</td>
        <td>${row.status || "—"}</td>
      </tr>`
      )
      .join("");
  }

  // Raw JSON
  $("#raw-json").textContent = JSON.stringify(data, null, 2);

  // cURL
  $("#curl-example").textContent = `curl -X POST http://localhost:8000/connect \\
  -H "Content-Type: application/json" \\
  -d '{
    "site": "${state.selectedSite}",
    "username": "demo_user",
    "password": "demo_pass"
  }'`;

  // Scroll to results
  setTimeout(() => section.scrollIntoView({ behavior: "smooth", block: "start" }), 300);
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

// ── Toggles & Reset ─────────────────────────────────────────────────────────

function setupToggles() {
  // JSON toggle
  $("#json-toggle").addEventListener("click", () => {
    const body = $("#json-body");
    const arrow = $("#json-arrow");
    const visible = body.style.display !== "none";
    body.style.display = visible ? "none" : "block";
    arrow.className = visible ? "toggle-arrow" : "toggle-arrow open";
  });

  // cURL toggle
  $("#request-toggle").addEventListener("click", () => {
    const body = $("#request-body");
    const arrow = $("#request-arrow");
    const visible = body.style.display !== "none";
    body.style.display = visible ? "none" : "block";
    arrow.className = visible ? "toggle-arrow" : "toggle-arrow open";
  });

  // Try again / Retry
  $("#try-again-btn").addEventListener("click", resetDemo);
  $("#retry-btn").addEventListener("click", resetDemo);

  // Clear console
  $("#clear-console").addEventListener("click", () => {
    $("#console-log").innerHTML = `<div class="console-entry info"><span class="console-time">${timestamp()}</span><span class="console-text">Console cleared</span></div>`;
  });
}

function resetDemo() {
  state.selectedSite = null;
  state.selectedName = null;
  state.sessionId = null;
  state.apiResponse = null;

  $("#demo-username").value = "";
  $("#demo-password").value = "";
  $("#mfa-code").value = "";
  $("#results-section").style.display = "none";

  showStep("step-select");
  loadBlueprints();
  log("— Reset —", "info");
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  loadBlueprints();
  setupCredentialsForm();
  setupMFA();
  setupToggles();

  // Fetch API version
  fetch("/")
    .then((r) => r.json())
    .then((d) => {
      if (d.version) $("#api-version").textContent = `v${d.version}`;
    })
    .catch(() => {});
});
