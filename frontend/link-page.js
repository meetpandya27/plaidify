(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const linkToken = params.get("token");
  const serverUrl = (params.get("server") || window.location.origin).replace(/\/$/, "");
  const inIframe = window.parent !== window;

  const state = {
    currentJobId: null,
    directoryFilters: {
      category: "",
      country: "",
      query: "",
    },
    featuredCount: 0,
    filtersInitialized: false,
    featuredOrganizations: [],
    lastResult: null,
    organizations: [],
    polling: false,
    preselectedSite: null,
    progressTimers: [],
    searchRequestId: 0,
    searchTimer: null,
    selectedSite: null,
    sessionId: null,
    summary: null,
  };

  const flowCopy = {
    select: {
      eyebrow: "Step 1 of 3",
      title: "Choose your organization",
      subtitle: "Choose from trusted providers and continue through a secure, read-only connection.",
      footer: "Your sign-in details stay inside this encrypted connection window.",
    },
    credentials: {
      eyebrow: "Step 2 of 3",
      title: "Enter credentials securely",
      subtitle: "Plaidify encrypts your credentials in-browser before they are submitted.",
      footer: "Only the selected provider receives your encrypted sign-in details.",
    },
    connecting: {
      eyebrow: "Connecting",
      title: "Creating your secure session",
      subtitle: "We are opening the provider portal and preparing a secure handoff back to your app.",
      footer: "If the provider prompts for additional verification, the flow will pause here.",
    },
    mfa: {
      eyebrow: "Step 3 of 3",
      title: "Finish verification",
      subtitle: "Enter the confirmation code from your provider to complete the connection.",
      footer: "Verification codes are only used to resume this session.",
    },
    success: {
      eyebrow: "Connected",
      title: "Your secure handoff is ready",
      subtitle: "Plaidify completed the provider flow and prepared the final handoff back to your app.",
      footer: "You can safely close this window at any time.",
    },
    error: {
      eyebrow: "Connection stopped",
      title: "The flow needs another try",
      subtitle: "No data was shared. Review the message below and retry when you are ready.",
      footer: "This session can be retried without reopening the modal.",
    },
  };

  const parentOrigin = (function () {
    const explicit = params.get("origin");
    if (explicit) {
      return explicit;
    }

    if (document.referrer) {
      try {
        return new URL(document.referrer).origin;
      } catch (_) {
        return inIframe ? serverUrl : "*";
      }
    }

    return inIframe ? serverUrl : "*";
  })();

  const $ = (selector) => document.querySelector(selector);
  const steps = {
    select: $("#step-select"),
    credentials: $("#step-credentials"),
    connecting: $("#step-connecting"),
    mfa: $("#step-mfa"),
    success: $("#step-success"),
    error: $("#step-error"),
  };
  const progressNodes = [$("#dot-1"), $("#dot-2"), $("#dot-3")];
  const statusItems = ["status-handshake", "status-browser", "status-auth", "status-read"];

  applyThemeOverrides();

  function applyThemeOverrides() {
    const root = document.documentElement.style;
    const accent = params.get("accent");
    const bgTint = params.get("bg");
    const radius = params.get("radius");

    if (accent) {
      root.setProperty("--accent", accent);
    }
    if (bgTint) {
      root.setProperty("--bg-tint", bgTint);
    }
    if (radius) {
      root.setProperty("--radius", radius);
    }
  }

  function showStep(stepName, progressState, overrides) {
    Object.values(steps).forEach((step) => step.classList.remove("active"));
    if (steps[stepName]) {
      steps[stepName].classList.add("active");
    }
    renderProgress(progressState);
    setFlowCopy(stepName, overrides || {});
  }

  function renderProgress(progressState) {
    progressNodes.forEach((node) => {
      node.classList.remove("completed", "current");
    });

    if (progressState === "complete") {
      progressNodes.forEach((node) => node.classList.add("completed"));
      return;
    }

    if (typeof progressState !== "number") {
      return;
    }

    progressNodes.forEach((node, index) => {
      if (index < progressState) {
        node.classList.add("completed");
      } else if (index === progressState) {
        node.classList.add("current");
      }
    });
  }

  function setFlowCopy(stepName, overrides) {
    const copy = Object.assign({}, flowCopy[stepName] || flowCopy.select, overrides || {});

    $("#flow-eyebrow").textContent = copy.eyebrow;
    $("#flow-title").textContent = copy.title;
    $("#flow-subtitle").textContent = copy.subtitle;
    $("#footer-context").textContent = copy.footer;
  }

  function iconForOrganization(organization) {
    if (organization.category === "utility") {
      return "&#9889;";
    }
    if (organization.category === "finance") {
      return "&#127974;";
    }
    if (organization.category === "insurance") {
      return "&#128737;";
    }
    if (organization.category === "healthcare") {
      return "&#9877;";
    }
    if (organization.category === "telecom") {
      return "&#128241;";
    }
    if (organization.category === "government") {
      return "&#127970;";
    }
    return "&#9670;";
  }

  function buildFeaturedOrganizations(results) {
    const seenCategories = new Set();
    return results.filter((organization) => {
      if (seenCategories.has(organization.category)) {
        return false;
      }
      seenCategories.add(organization.category);
      return true;
    }).slice(0, 5);
  }

  function renderFeaturedPicks() {
    const container = $("#featured-picks");
    if (!container) {
      return;
    }

    const hasFilters = Boolean(
      state.directoryFilters.query || state.directoryFilters.country || state.directoryFilters.category
    );
    if (hasFilters || !state.featuredOrganizations.length) {
      container.innerHTML = "";
      container.style.display = "none";
      return;
    }

    container.style.display = "flex";
    container.innerHTML = state.featuredOrganizations
      .map((organization) => `
        <button class="featured-pick" type="button" data-featured-organization-id="${escapeHtml(organization.organization_id)}">
          <span class="featured-pick__icon">${iconForOrganization(organization)}</span>
          <span>${escapeHtml(organization.brand)}</span>
          <span class="featured-pick__meta">${escapeHtml(organization.category_label)}</span>
        </button>`)
      .join("");

    container.querySelectorAll(".featured-pick").forEach((button) => {
      button.addEventListener("click", () => selectInstitution(button.dataset.featuredOrganizationId));
    });
  }

  function renderInstitutionState(kind, title, copy) {
    const container = $("#institution-list");
    if (!container) {
      return;
    }

    if (kind === "loading") {
      container.innerHTML = `
        <div class="institution-state institution-state--loading" aria-live="polite">
          <span class="institution-spinner" aria-hidden="true"></span>
          <div>
            <div class="institution-state__title">${escapeHtml(title)}</div>
            <div class="institution-state__copy">${escapeHtml(copy)}</div>
          </div>
        </div>`;
      return;
    }

    const icon = kind === "error" ? "!" : "?";
    container.innerHTML = `
      <div class="institution-state" aria-live="polite">
        <span class="institution-state__icon" aria-hidden="true">${escapeHtml(icon)}</span>
        <div>
          <div class="institution-state__title">${escapeHtml(title)}</div>
          <div class="institution-state__copy">${escapeHtml(copy)}</div>
        </div>
      </div>`;
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function hasNativeBridge() {
    return Boolean(
      window.ReactNativeWebView?.postMessage ||
      window.webkit?.messageHandlers?.plaidifyLink?.postMessage ||
      window.PlaidifyLinkBridge?.postMessage ||
      window.PlaidifyLinkBridge?.onEvent
    );
  }

  function postToNativeBridge(message) {
    const serialized = JSON.stringify(message);

    try {
      if (window.ReactNativeWebView?.postMessage) {
        window.ReactNativeWebView.postMessage(serialized);
      }
    } catch (_) {
      // Ignore bridge delivery errors and continue to other targets.
    }

    try {
      if (window.webkit?.messageHandlers?.plaidifyLink?.postMessage) {
        window.webkit.messageHandlers.plaidifyLink.postMessage(message);
      }
    } catch (_) {
      // Ignore bridge delivery errors and continue to other targets.
    }

    try {
      if (typeof window.PlaidifyLinkBridge?.postMessage === "function") {
        window.PlaidifyLinkBridge.postMessage(serialized);
      } else if (typeof window.PlaidifyLinkBridge?.onEvent === "function") {
        window.PlaidifyLinkBridge.onEvent(serialized);
      }
    } catch (_) {
      // Ignore bridge delivery failures.
    }
  }

  function postEvent(eventName, payload) {
    const message = Object.assign({ source: "plaidify-link", event: eventName }, payload || {});

    if (inIframe) {
      window.parent.postMessage(message, parentOrigin);
    }

    if (hasNativeBridge()) {
      postToNativeBridge(message);
    }
  }

  async function apiCall(method, path, body) {
    const request = { method, headers: {} };
    if (body !== undefined) {
      request.headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(body);
    }

    const response = await fetch(`${serverUrl}${path}`, request);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || response.statusText || "Request failed.");
    }
    return payload;
  }

  // Durable event delivery: hosted-link lifecycle events must reach the
  // server so session state, SSE, and webhooks stay in sync with what the
  // user actually did in the browser. We queue posts and retry with
  // exponential backoff; persistent failures are surfaced to the UI.
  const eventDelivery = {
    queue: [],
    inFlight: false,
    retryTimer: null,
  };

  function scheduleEventRetry(delayMs) {
    if (eventDelivery.retryTimer) {
      return;
    }
    eventDelivery.retryTimer = window.setTimeout(() => {
      eventDelivery.retryTimer = null;
      drainEventQueue();
    }, delayMs);
  }

  async function drainEventQueue() {
    if (eventDelivery.inFlight || !linkToken) {
      return;
    }
    const next = eventDelivery.queue[0];
    if (!next) {
      return;
    }
    eventDelivery.inFlight = true;
    try {
      await apiCall(
        "POST",
        `/link/sessions/${encodeURIComponent(linkToken)}/event`,
        Object.assign({ event: next.event }, next.payload || {}),
      );
      eventDelivery.queue.shift();
      eventDelivery.inFlight = false;
      if (eventDelivery.queue.length) {
        drainEventQueue();
      }
    } catch (error) {
      eventDelivery.inFlight = false;
      next.attempts = (next.attempts || 0) + 1;
      const maxAttempts = 6;
      if (next.attempts >= maxAttempts) {
        eventDelivery.queue.shift();
        // Surface a visible error so the parent app/operator knows
        // lifecycle state may have diverged from the server.
        try {
          postEvent("EVENT_DELIVERY_FAILED", {
            failed_event: next.event,
            error: error?.message || "event delivery failed",
          });
        } catch (_) {
          // best-effort telemetry only
        }
        if (eventDelivery.queue.length) {
          drainEventQueue();
        }
        return;
      }
      const backoff = Math.min(250 * 2 ** (next.attempts - 1), 4000);
      scheduleEventRetry(backoff);
    }
  }

  function bestEffortLinkEvent(eventName, payload) {
    if (!linkToken) {
      return;
    }
    eventDelivery.queue.push({ event: eventName, payload, attempts: 0 });
    drainEventQueue();
  }

  function buildSearchPath(overrides) {
    const filters = Object.assign({}, state.directoryFilters, overrides || {});
    const params = new URLSearchParams();
    if (filters.query) {
      params.set("q", filters.query);
    }
    if (filters.country) {
      params.set("country", filters.country);
    }
    if (filters.category) {
      params.set("category", filters.category);
    }
    if (filters.site) {
      params.set("site", filters.site);
    }
    params.set("limit", String(filters.limit || 40));
    return `/organizations/search?${params.toString()}`;
  }

  function updateDirectoryMeta(payload) {
    const summary = payload.summary || state.summary;
    if (summary) {
      state.summary = summary;
    }

    const totalCount = summary?.total_count || payload.count || 0;
    const formattedTotal = new Intl.NumberFormat("en-US").format(totalCount);
    const formattedVisible = new Intl.NumberFormat("en-US").format(payload.count || 0);
    $("#directory-count").textContent = `${formattedTotal} organizations available`;

    const parts = [];
    if (state.directoryFilters.country) {
      parts.push(state.directoryFilters.country === "CA" ? "Canada" : "United States");
    }
    if (state.directoryFilters.category) {
      const categoryMatch = summary?.categories?.find((item) => item.key === state.directoryFilters.category);
      parts.push(categoryMatch ? categoryMatch.label : state.directoryFilters.category);
    }

    const scopeCopy = parts.length ? parts.join(" · ") : "Finance, utilities, insurance, telecom, healthcare, and government";
    $("#directory-caption").textContent = `${formattedVisible} matching result${payload.count === 1 ? "" : "s"} in ${scopeCopy} across the USA and Canada.`;
  }

  function populateFilters(summary) {
    if (!summary || state.filtersInitialized) {
      return;
    }

    const countrySelect = $("#country-filter");
    const categorySelect = $("#category-filter");

    summary.countries.forEach((country) => {
      const option = document.createElement("option");
      option.value = country.code;
      option.textContent = `${country.label} (${new Intl.NumberFormat("en-US").format(country.count)})`;
      countrySelect.appendChild(option);
    });

    summary.categories.forEach((category) => {
      const option = document.createElement("option");
      option.value = category.key;
      option.textContent = `${category.label} (${new Intl.NumberFormat("en-US").format(category.count)})`;
      categorySelect.appendChild(option);
    });

    state.filtersInitialized = true;
  }

  async function loadOrganizations(overrides) {
    const requestId = ++state.searchRequestId;
    const path = buildSearchPath(overrides);
    renderInstitutionState(
      "loading",
      overrides?.site ? "Preparing your provider" : "Finding the best matches",
      overrides?.site
        ? "We are loading a secure connection path for your selected provider."
        : "Curating trusted providers for this secure connection."
    );
    const payload = await apiCall("GET", path);

    if (requestId !== state.searchRequestId) {
      return null;
    }

    state.organizations = payload.results || [];
    state.summary = payload.summary || state.summary;
    if (!state.featuredOrganizations.length && !overrides?.site) {
      state.featuredOrganizations = buildFeaturedOrganizations(payload.results || []);
    }
    populateFilters(state.summary);
    updateDirectoryMeta(payload);
    renderFeaturedPicks();
    renderInstitutions(state.organizations);
    return payload;
  }

  function scheduleSearch() {
    if (state.searchTimer) {
      window.clearTimeout(state.searchTimer);
    }

    state.searchTimer = window.setTimeout(async () => {
      try {
        await loadOrganizations();
      } catch (error) {
        renderInstitutionState(
          "error",
          "We couldn't load providers right now",
          error.message || "Try again in a moment."
        );
      }
    }, 180);
  }

  function buildSuccessCards(providerName) {
    const cards = [
      { label: "Status", value: "Connection verified" },
      { label: "Provider", value: providerName },
      { label: "Next step", value: "Finish in your app" },
    ];

    return cards
      .map(
        (card) => `
          <div class="highlight-card">
            <div class="highlight-label">${escapeHtml(card.label)}</div>
            <div class="highlight-value">${escapeHtml(card.value)}</div>
          </div>`
      )
      .join("");
  }

  function updateSelectedProvider(provider) {
    $("#provider-icon").innerHTML = iconForOrganization(provider);
    $("#provider-name").textContent = provider.name;
    $("#provider-domain").textContent = `${provider.service_area} · ${provider.category_label}`;
    $("#provider-badges").innerHTML = [
      provider.country_code,
      provider.region_code,
      provider.has_mfa ? "MFA ready" : null,
      provider.site ? `Connector ${provider.site}` : null,
    ]
      .filter(Boolean)
      .map((badge) => `<span class="provider-badge">${escapeHtml(badge)}</span>`)
      .join("");
    $("#credentials-caption").textContent = `Plaidify encrypts your ${provider.name} credentials before submitting them through the live read-only connection flow.`;

    const consentItems = [
      `Open a secure browser session for ${provider.name}.`,
      "Encrypt your sign-in details before they leave this window.",
      "Return a secure completion back to your app when verification finishes.",
    ];

    if (provider.has_mfa) {
      consentItems.push("Pause for provider verification if an MFA challenge appears.");
    }

    $("#consent-list").innerHTML = consentItems
      .map((item) => `<li>${escapeHtml(item)}</li>`)
      .join("");
  }

  function renderInstitutions(list) {
    const container = $("#institution-list");
    if (!list.length) {
      renderInstitutionState(
        "empty",
        "No providers matched that search",
        "Try a broader search, or change your country or category filters."
      );
      return;
    }

    container.innerHTML = list
      .map((organization) => {
        const tags = [organization.category_label, organization.country_code, organization.region_code]
          .filter(Boolean)
          .map((tag) => `<span class="inst-tag">${escapeHtml(tag)}</span>`)
          .join("");

        const support = organization.has_mfa
          ? "Additional verification is supported if the provider asks for it"
          : "Secure sign-in and read-only access are supported for this connection";

        return `
          <button class="institution-item" type="button" data-organization-id="${escapeHtml(organization.organization_id)}">
            <span class="inst-icon">${iconForOrganization(organization)}</span>
            <span class="inst-content">
              <span class="inst-name">${escapeHtml(organization.name)}</span>
              <span class="inst-domain">${escapeHtml(organization.service_area)} · ${escapeHtml(organization.category_label)}</span>
              <span class="inst-tags">${tags}</span>
              <span class="inst-support">${escapeHtml(support)}</span>
            </span>
            <span class="inst-arrow" aria-hidden="true">&rsaquo;</span>
          </button>`;
      })
      .join("");

    container.querySelectorAll(".institution-item").forEach((button) => {
      button.addEventListener("click", () => selectInstitution(button.dataset.organizationId));
    });
  }

  async function validateToken() {
    if (!linkToken) {
      showError("This link is missing a session token. Request a fresh link and try again.");
      return false;
    }

    try {
      const payload = await getLinkSessionStatus();
      if (payload.status === "expired" || payload.status === "completed") {
        showError(`This link has ${payload.status}. Request a fresh link to continue.`);
        return false;
      }

      state.currentJobId = payload.job_id || state.currentJobId;
      state.preselectedSite = payload.site || null;
      return true;
    } catch (error) {
      showError(error.message || "This link is invalid or has expired.");
      return false;
    }
  }

  async function loadInitialOrganizations() {
    try {
      if (state.preselectedSite) {
        const payload = await loadOrganizations({ site: state.preselectedSite, limit: 8 });
        if (payload && payload.results && payload.results.length) {
          selectInstitution(payload.results[0].organization_id);
          return;
        }
      }

      await loadOrganizations();
    } catch (error) {
      renderInstitutionState(
        "error",
        "We couldn't load providers right now",
        error.message || "Try again in a moment."
      );
    }
  }

  function selectInstitution(organizationId) {
    const provider = state.organizations.find((item) => item.organization_id === organizationId);
    if (!provider) {
      return;
    }

    state.selectedSite = provider;
    updateSelectedProvider(provider);
    showStep("credentials", 1, {
      subtitle: `Continue with ${provider.name} using Plaidify's encrypted, read-only connection flow.`,
    });
    postEvent("INSTITUTION_SELECTED", {
      organization_id: provider.organization_id,
      organization_name: provider.name,
      site: provider.site,
    });
    bestEffortLinkEvent("INSTITUTION_SELECTED", {
      organization_id: provider.organization_id,
      organization_name: provider.name,
      site: provider.site,
    });
    $("#link-username").focus();
  }

  function clearErrors() {
    document.querySelectorAll(".field-error").forEach((element) => {
      element.textContent = "";
    });
    document.querySelectorAll(".input-error").forEach((element) => {
      element.classList.remove("input-error");
    });
  }

  function markFieldError(inputId, errorId, message) {
    const input = document.getElementById(inputId);
    const error = document.getElementById(errorId);
    if (input) {
      input.classList.add("input-error");
    }
    if (error) {
      error.textContent = message;
    }
  }

  function resetStatusRail() {
    statusItems.forEach((itemId, index) => {
      const element = document.getElementById(itemId);
      if (element) {
        element.dataset.state = index === 0 ? "active" : "idle";
      }
    });
  }

  function setConnectingState(activeIndex, title, subtitle, note) {
    statusItems.forEach((itemId, index) => {
      const element = document.getElementById(itemId);
      if (!element) {
        return;
      }

      if (index < activeIndex) {
        element.dataset.state = "complete";
      } else if (index === activeIndex) {
        element.dataset.state = "active";
      } else {
        element.dataset.state = "idle";
      }
    });

    if (title) {
      $("#connecting-text").textContent = title;
    }
    if (subtitle) {
      $("#connecting-sub").textContent = subtitle;
    }
    if (note) {
      $("#connection-note").textContent = note;
    }
  }

  function clearProgressTimers() {
    state.progressTimers.forEach((timer) => window.clearTimeout(timer));
    state.progressTimers = [];
  }

  function startConnectingAnimation(providerName) {
    clearProgressTimers();
    resetStatusRail();
    setConnectingState(
      0,
      "Preparing secure connection",
      `Creating an encrypted session for ${providerName}.`,
      "This usually completes in a few seconds. If the provider asks for verification, we will pause and prompt for it."
    );

    state.progressTimers.push(
      window.setTimeout(() => {
        setConnectingState(1, "Opening provider portal", `Launching ${providerName} in a protected browser context.`);
      }, 500)
    );

    state.progressTimers.push(
      window.setTimeout(() => {
        setConnectingState(2, "Submitting credentials", `Plaidify is progressing through the ${providerName} sign-in steps.`);
      }, 1300)
    );

    state.progressTimers.push(
      window.setTimeout(() => {
        setConnectingState(3, "Preparing secure handoff", "Packaging a completion token for your app.");
      }, 2400)
    );
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async function getLinkSessionStatus() {
    return apiCall("GET", `/link/sessions/${encodeURIComponent(linkToken)}/status`);
  }

  function normalizeResult(payload) {
    return {
      jobId: payload.job_id || state.currentJobId || "",
      metadata: payload.metadata || {},
      publicToken: payload.public_token || "",
      sessionId: payload.session_id || state.sessionId || "",
      status: payload.status || "connected",
    };
  }

  async function resolveHostedSuccess(fallbackResult) {
    if (!linkToken) {
      return fallbackResult;
    }

    try {
      const payload = await getLinkSessionStatus();
      state.currentJobId = payload.job_id || state.currentJobId;
      state.sessionId = payload.session_id || state.sessionId;
      if (payload.status === "completed") {
        return normalizeResult(payload);
      }
    } catch (_) {
      // Fall back to the local response when session reconciliation is unavailable.
    }

    return fallbackResult;
  }

  function showMfa(message, mfaType) {
    setFlowCopy("mfa", {
      subtitle: message || "Enter the verification code from your provider to continue.",
    });
    $("#mfa-message").textContent = message || "Enter the verification code from your provider to continue.";
    $("#mfa-label").textContent = mfaType ? `${mfaType.replace(/_/g, " ")} required` : "Verification required";
    clearProgressTimers();
    showStep("mfa", 2);
    postEvent("MFA_REQUIRED", { mfa_type: mfaType || "otp", session_id: state.sessionId });
    bestEffortLinkEvent("MFA_REQUIRED", { mfa_type: mfaType || "otp", session_id: state.sessionId });
    $("#mfa-code").focus();
  }

  async function pollLinkSession(options) {
    state.polling = true;
    const afterMfaSubmit = Boolean(options && options.afterMfaSubmit);

    for (let attempt = 0; attempt < 90; attempt += 1) {
      const payload = await getLinkSessionStatus();
      state.currentJobId = payload.job_id || state.currentJobId;
      state.sessionId = payload.session_id || state.sessionId;

      if (payload.status === "mfa_required") {
        if (afterMfaSubmit && attempt < 3) {
          await sleep(700);
          continue;
        }
        state.polling = false;
        showMfa(payload.message || payload.metadata?.message, payload.mfa_type);
        return;
      }

      if (payload.status === "completed") {
        state.polling = false;
        showSuccess(normalizeResult(payload));
        return;
      }

      if (payload.status === "error") {
        state.polling = false;
        showError(payload.error_message || payload.message || "The connection could not be completed.");
        return;
      }

      if (attempt === 1) {
        setConnectingState(2, "Verifying credentials", "The provider flow is active and the connector is waiting for the next page state.");
      }
      if (attempt >= 3) {
        setConnectingState(3, "Preparing secure handoff", "The connection is active and preparing the secure handoff back to your app.");
      }

      await sleep(1100);
    }

    state.polling = false;
    showError("The connection timed out before the provider completed the flow.");
  }

  function showSuccess(result) {
    clearProgressTimers();
    state.lastResult = result;

    const providerName = state.selectedSite ? state.selectedSite.name : "Your provider";

    $("#success-provider").textContent = providerName;
    $("#success-message").textContent = result.publicToken
      ? "Your secure connection is complete. Return to your app to finish setup."
      : "The secure connection completed successfully. Return to your app to continue.";
    $("#success-highlights").innerHTML = buildSuccessCards(providerName);

    const references = [];
    if (result.publicToken) {
      references.push({ label: "Public token", value: result.publicToken });
    }
    if (result.jobId || state.currentJobId) {
      references.push({ label: "Connection id", value: result.jobId || state.currentJobId });
    }

    const referenceBlock = $("#access-token-display");
    if (references.length) {
      referenceBlock.innerHTML = references
        .map(
          (reference) => `
            <div class="reference-row">
              <span class="reference-label">${escapeHtml(reference.label)}</span>
              <span class="reference-value">${escapeHtml(reference.value)}</span>
            </div>`
        )
        .join("");
      referenceBlock.style.display = "block";
    } else {
      referenceBlock.style.display = "none";
      referenceBlock.innerHTML = "";
    }

    showStep("success", "complete");
    postEvent("CONNECTED", {
      job_id: result.jobId || state.currentJobId || null,
      organization_id: state.selectedSite ? state.selectedSite.organization_id : null,
      organization_name: state.selectedSite ? state.selectedSite.name : null,
      public_token: result.publicToken,
      site: state.selectedSite ? state.selectedSite.site : null,
    });
  }

  function showError(message) {
    clearProgressTimers();
    $("#error-message").textContent = message;
    showStep("error", state.selectedSite ? 1 : 0);
    postEvent("ERROR", { error: message });
    bestEffortLinkEvent("ERROR", { error: message, site: state.selectedSite ? state.selectedSite.site : null });
  }

  async function connectAccount(username, password) {
    if (!state.selectedSite) {
      throw new Error("Choose a provider before continuing.");
    }

    const encryptionSession = await apiCall("GET", `/encryption/public_key/${encodeURIComponent(linkToken)}`);
    if (!encryptionSession.public_key) {
      throw new Error("Unable to establish an encrypted session.");
    }

    const encrypted = await encryptCredentials(encryptionSession.public_key, username, password);

    const response = await apiCall("POST", "/connect", {
      encrypted_password: encrypted.password,
      encrypted_username: encrypted.username,
      link_token: linkToken,
      site: state.selectedSite.site,
    });

    if (response.job_id) {
      state.currentJobId = response.job_id;
    }

    if (response.status === "connected") {
      showSuccess(await resolveHostedSuccess(normalizeResult(response)));
      return;
    }

    if (response.status === "mfa_required") {
      state.sessionId = response.session_id;
      showMfa(response.metadata?.message, response.mfa_type);
      return;
    }

    if (response.status === "pending") {
      await pollLinkSession();
      return;
    }

    throw new Error(response.error || response.detail || `Unexpected status: ${response.status}`);
  }

  function closeWindow() {
    if (inIframe) {
      return;
    }
    if (hasNativeBridge()) {
      return;
    }
    window.close();
    window.setTimeout(() => {
      if (!window.closed) {
        window.location.href = serverUrl;
      }
    }, 120);
  }

  function resetFlow() {
    clearProgressTimers();
    state.currentJobId = null;
    state.lastResult = null;
    state.polling = false;
    state.sessionId = null;
    $("#link-username").value = "";
    $("#link-password").value = "";
    $("#mfa-code").value = "";
    clearErrors();

    state.selectedSite = null;
    showStep("select", 0);
    loadInitialOrganizations();
  }

  async function encryptCredentials(publicKeyPem, username, password) {
    const publicKeyBody = publicKeyPem
      .replace(/-----BEGIN PUBLIC KEY-----/, "")
      .replace(/-----END PUBLIC KEY-----/, "")
      .replace(/\s/g, "");

    const binaryDer = Uint8Array.from(atob(publicKeyBody), (character) => character.charCodeAt(0));
    const cryptoKey = await crypto.subtle.importKey(
      "spki",
      binaryDer.buffer,
      { name: "RSA-OAEP", hash: "SHA-256" },
      false,
      ["encrypt"]
    );

    const encryptedUsername = await crypto.subtle.encrypt(
      { name: "RSA-OAEP" },
      cryptoKey,
      new TextEncoder().encode(username)
    );
    const encryptedPassword = await crypto.subtle.encrypt(
      { name: "RSA-OAEP" },
      cryptoKey,
      new TextEncoder().encode(password)
    );

    return {
      password: btoa(String.fromCharCode(...new Uint8Array(encryptedPassword))),
      username: btoa(String.fromCharCode(...new Uint8Array(encryptedUsername))),
    };
  }

  document.getElementById("institution-search").addEventListener("input", (event) => {
    state.directoryFilters.query = event.target.value.trim();
    scheduleSearch();
  });

  document.getElementById("country-filter").addEventListener("change", (event) => {
    state.directoryFilters.country = event.target.value;
    scheduleSearch();
  });

  document.getElementById("category-filter").addEventListener("change", (event) => {
    state.directoryFilters.category = event.target.value;
    scheduleSearch();
  });

  document.getElementById("password-toggle").addEventListener("click", () => {
    const passwordInput = document.getElementById("link-password");
    const toggle = document.getElementById("password-toggle");
    const isMasked = passwordInput.getAttribute("type") === "password";
    passwordInput.setAttribute("type", isMasked ? "text" : "password");
    toggle.textContent = isMasked ? "Hide" : "Show";
    toggle.setAttribute("aria-label", isMasked ? "Hide password" : "Show password");
  });

  document.getElementById("change-provider-btn").addEventListener("click", () => {
    showStep("select", 0);
    state.preselectedSite = null;
    loadOrganizations().catch((error) => {
      $("#institution-list").innerHTML = `<div class="institution-empty">${escapeHtml(error.message || "Unable to load organizations.")}</div>`;
    });
    document.getElementById("institution-search").focus();
  });

  document.getElementById("credentials-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearErrors();

    const username = document.getElementById("link-username").value.trim();
    const password = document.getElementById("link-password").value;
    let invalid = false;

    if (!username) {
      markFieldError("link-username", "username-error", "Username or email is required.");
      invalid = true;
    }
    if (!password) {
      markFieldError("link-password", "password-error", "Password is required.");
      invalid = true;
    }
    if (invalid) {
      return;
    }

    const button = document.getElementById("connect-btn");
    button.disabled = true;
    button.textContent = "Connecting...";

    showStep("connecting", 1);
    startConnectingAnimation(state.selectedSite ? state.selectedSite.name : "your provider");
    postEvent("CREDENTIALS_SUBMITTED", {
      organization_id: state.selectedSite ? state.selectedSite.organization_id : null,
      organization_name: state.selectedSite ? state.selectedSite.name : null,
      site: state.selectedSite ? state.selectedSite.site : null,
    });
    bestEffortLinkEvent("CREDENTIALS_SUBMITTED", {
      organization_id: state.selectedSite ? state.selectedSite.organization_id : null,
      organization_name: state.selectedSite ? state.selectedSite.name : null,
      site: state.selectedSite ? state.selectedSite.site : null,
    });

    try {
      await connectAccount(username, password);
    } catch (error) {
      showError(error.message || "We could not complete the connection.");
    } finally {
      button.disabled = false;
      button.textContent = "Continue";
    }
  });

  document.getElementById("mfa-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = document.getElementById("mfa-code").value.trim();
    if (!code || !state.sessionId) {
      return;
    }

    const button = document.getElementById("mfa-submit-btn");
    button.disabled = true;
    button.textContent = "Verifying...";
    postEvent("MFA_SUBMITTED", { session_id: state.sessionId });
    bestEffortLinkEvent("MFA_SUBMITTED", { session_id: state.sessionId });

    try {
      showStep("connecting", 2, {
        title: "Resuming connection",
        subtitle: "Your provider verification code has been submitted.",
      });
      setConnectingState(2, "Resuming after verification", "The provider flow is continuing with the verification code you entered.");

      const result = await apiCall(
        "POST",
        `/mfa/submit?session_id=${encodeURIComponent(state.sessionId)}&code=${encodeURIComponent(code)}`
      );

      if (result.status === "error") {
        showMfa(result.error || "The verification code was not accepted.", "verification");
        return;
      }

      if (result.status === "connected") {
        showSuccess(await resolveHostedSuccess(normalizeResult(result)));
        return;
      }

      if (linkToken) {
        await pollLinkSession({ afterMfaSubmit: true });
        return;
      }

      showError("Verification was accepted, but the connection could not be resumed.");
    } catch (error) {
      showError(error.message || "Verification failed.");
    } finally {
      button.disabled = false;
      button.textContent = "Verify and continue";
    }
  });

  document.getElementById("retry-btn").addEventListener("click", resetFlow);
  document.getElementById("done-btn").addEventListener("click", () => {
    postEvent("DONE", {});
    closeWindow();
  });
  document.getElementById("close-btn").addEventListener("click", () => {
    postEvent("EXIT", { reason: "user_closed" });
    bestEffortLinkEvent("EXIT", { reason: "user_closed" });
    closeWindow();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      postEvent("EXIT", { reason: "user_pressed_escape" });
      bestEffortLinkEvent("EXIT", { reason: "user_pressed_escape" });
      closeWindow();
    }
  });

  async function init() {
    const valid = await validateToken();
    if (!valid) {
      return;
    }
    postEvent("OPEN", {});
    bestEffortLinkEvent("OPEN", {});
    await loadInitialOrganizations();
  }

  init();
})();