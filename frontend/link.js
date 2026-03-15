/**
 * PlaidifyLink — Embeddable JavaScript widget for Plaidify.
 *
 * Creates an overlay iframe pointing to the hosted /link page.
 * Credentials stay inside the iframe — the parent page never sees them.
 *
 * Usage:
 *   const link = PlaidifyLink.create({
 *     serverUrl: "http://localhost:8000",
 *     token: "link-token-uuid",
 *     onSuccess: (accessToken, metadata) => { ... },
 *     onExit: (error) => { ... },
 *     onEvent: (event, data) => { ... },
 *     onMFA: (challenge) => { ... },
 *     theme: { accentColor: "#22c55e", borderRadius: "12px" },
 *   });
 *   link.open();
 *   // link.close();
 *   // link.destroy();
 */
(function (root) {
  "use strict";

  var _instances = [];

  // ── Default theme ────────────────────────────────────────────────────────
  var DEFAULT_THEME = {
    accentColor: "#22c55e",
    bgColor: "#0c0f14",
    borderRadius: "12px",
    logo: null,
  };

  // ── PlaidifyLink Constructor ─────────────────────────────────────────────

  function PlaidifyLink(config) {
    this._config = {
      serverUrl: (config.serverUrl || "").replace(/\/$/, ""),
      token: config.token || "",
      onSuccess: config.onSuccess || function () {},
      onExit: config.onExit || function () {},
      onEvent: config.onEvent || function () {},
      onMFA: config.onMFA || function () {},
      theme: _merge(DEFAULT_THEME, config.theme || {}),
    };

    this._iframe = null;
    this._overlay = null;
    this._isOpen = false;
    this._destroyed = false;
    this._messageHandler = this._onMessage.bind(this);

    _instances.push(this);
  }

  // ── Static factory ───────────────────────────────────────────────────────

  PlaidifyLink.create = function (config) {
    if (!config || !config.token) {
      throw new Error("PlaidifyLink.create() requires a 'token' in config.");
    }
    return new PlaidifyLink(config);
  };

  // ── Public methods ───────────────────────────────────────────────────────

  PlaidifyLink.prototype.open = function () {
    if (this._destroyed) throw new Error("This PlaidifyLink instance has been destroyed.");
    if (this._isOpen) return;

    this._isOpen = true;
    this._createOverlay();
    this._createIframe();
    window.addEventListener("message", this._messageHandler, false);
    this._config.onEvent("OPEN", {});
  };

  PlaidifyLink.prototype.close = function () {
    if (!this._isOpen) return;
    this._teardownUI();
    this._isOpen = false;
    this._config.onEvent("CLOSE", {});
  };

  PlaidifyLink.prototype.destroy = function () {
    this.close();
    window.removeEventListener("message", this._messageHandler, false);
    this._destroyed = true;
    var idx = _instances.indexOf(this);
    if (idx !== -1) _instances.splice(idx, 1);
  };

  // ── Private methods ──────────────────────────────────────────────────────

  PlaidifyLink.prototype._createOverlay = function () {
    var overlay = document.createElement("div");
    overlay.setAttribute("data-plaidify-overlay", "true");
    var s = overlay.style;
    s.position = "fixed";
    s.top = "0";
    s.left = "0";
    s.width = "100%";
    s.height = "100%";
    s.backgroundColor = "rgba(0, 0, 0, 0.6)";
    s.zIndex = "2147483646";
    s.display = "flex";
    s.alignItems = "center";
    s.justifyContent = "center";
    s.opacity = "0";
    s.transition = "opacity 0.25s ease";

    document.body.appendChild(overlay);
    // Force reflow then animate
    overlay.offsetHeight; // eslint-disable-line no-unused-expressions
    s.opacity = "1";

    this._overlay = overlay;

    // Close on overlay click (outside iframe)
    var self = this;
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) {
        self._config.onExit({ reason: "overlay_click" });
        self.close();
      }
    });
  };

  PlaidifyLink.prototype._createIframe = function () {
    var iframe = document.createElement("iframe");
    var url =
      this._config.serverUrl +
      "/link?token=" +
      encodeURIComponent(this._config.token);

    iframe.setAttribute("src", url);
    iframe.setAttribute("title", "Plaidify Link");
    iframe.setAttribute("allowtransparency", "true");
    iframe.setAttribute("frameborder", "0");
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups");

    var s = iframe.style;
    s.width = "420px";
    s.maxWidth = "95vw";
    s.height = "580px";
    s.maxHeight = "90vh";
    s.border = "none";
    s.borderRadius = this._config.theme.borderRadius;
    s.boxShadow = "0 24px 64px rgba(0, 0, 0, 0.5)";
    s.backgroundColor = this._config.theme.bgColor;
    s.transition = "transform 0.3s ease, opacity 0.3s ease";
    s.transform = "scale(0.95)";
    s.opacity = "0";

    this._overlay.appendChild(iframe);
    // Animate in
    requestAnimationFrame(function () {
      s.transform = "scale(1)";
      s.opacity = "1";
    });

    this._iframe = iframe;
  };

  PlaidifyLink.prototype._teardownUI = function () {
    if (this._iframe) {
      this._iframe.remove();
      this._iframe = null;
    }
    if (this._overlay) {
      var overlay = this._overlay;
      overlay.style.opacity = "0";
      setTimeout(function () {
        overlay.remove();
      }, 250);
      this._overlay = null;
    }
  };

  PlaidifyLink.prototype._onMessage = function (event) {
    // Only accept messages from our iframe origin
    if (!event.data || event.data.source !== "plaidify-link") return;

    var msg = event.data;
    var evtName = msg.event;

    // Forward all events
    this._config.onEvent(evtName, msg);

    switch (evtName) {
      case "CONNECTED":
        this._config.onSuccess(msg.access_token || "", {
          site: msg.site,
          data: msg.data,
        });
        // Auto-close after a short delay
        var self = this;
        setTimeout(function () { self.close(); }, 800);
        break;

      case "EXIT":
      case "DONE":
        this._config.onExit({ reason: msg.reason || "user_closed" });
        this.close();
        break;

      case "MFA_REQUIRED":
        this._config.onMFA({
          mfa_type: msg.mfa_type,
          session_id: msg.session_id,
        });
        break;

      case "ERROR":
        this._config.onExit({ reason: "error", error: msg.error });
        break;
    }
  };

  // ── Utility ──────────────────────────────────────────────────────────────

  function _merge(defaults, overrides) {
    var result = {};
    for (var k in defaults) {
      if (defaults.hasOwnProperty(k)) {
        result[k] = overrides.hasOwnProperty(k) ? overrides[k] : defaults[k];
      }
    }
    return result;
  }

  // ── Export ───────────────────────────────────────────────────────────────

  root.PlaidifyLink = PlaidifyLink;
})(typeof window !== "undefined" ? window : this);
