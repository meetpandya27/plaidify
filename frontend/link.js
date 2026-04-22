/**
 * PlaidifyLink - Embeddable JavaScript widget for Plaidify.
 */
(function (root) {
  "use strict";

  var instances = [];

  var DEFAULT_THEME = {
    accentColor: "#087f6b",
    bgColor: "#eef5ff",
    borderRadius: "28px",
    logo: null,
    fullscreenOnMobile: true,
    mobileBreakpoint: 768,
  };

  function PlaidifyLink(config) {
    var serverOrigin = new URL((config.serverUrl || "").replace(/\/$/, ""), window.location.href).origin;

    this._config = {
      serverUrl: (config.serverUrl || "").replace(/\/$/, ""),
      token: config.token || "",
      onSuccess: config.onSuccess || function () {},
      onExit: config.onExit || function () {},
      onEvent: config.onEvent || function () {},
      onMFA: config.onMFA || function () {},
      theme: merge(DEFAULT_THEME, config.theme || {}),
    };

    this._serverOrigin = serverOrigin;

    this._iframe = null;
    this._isOpen = false;
    this._overlay = null;
    this._destroyed = false;
    this._messageHandler = this._onMessage.bind(this);
    this._resizeHandler = this._applyResponsiveLayout.bind(this);

    instances.push(this);
  }

  PlaidifyLink.create = function (config) {
    if (!config || !config.token) {
      throw new Error("PlaidifyLink.create() requires a 'token' in config.");
    }
    return new PlaidifyLink(config);
  };

  PlaidifyLink.prototype.open = function () {
    if (this._destroyed) {
      throw new Error("This PlaidifyLink instance has been destroyed.");
    }
    if (this._isOpen) {
      return;
    }

    this._isOpen = true;
    this._createOverlay();
    this._createIframe();
    window.addEventListener("message", this._messageHandler, false);
    window.addEventListener("resize", this._resizeHandler, false);
    this._config.onEvent("OPEN", {});
  };

  PlaidifyLink.prototype.close = function () {
    if (!this._isOpen) {
      return;
    }
    this._teardownUI();
    this._isOpen = false;
    this._config.onEvent("CLOSE", {});
  };

  PlaidifyLink.prototype.destroy = function () {
    this.close();
    window.removeEventListener("message", this._messageHandler, false);
    window.removeEventListener("resize", this._resizeHandler, false);
    this._destroyed = true;

    var index = instances.indexOf(this);
    if (index !== -1) {
      instances.splice(index, 1);
    }
  };

  PlaidifyLink.prototype._createOverlay = function () {
    var overlay = document.createElement("div");
    overlay.setAttribute("data-plaidify-overlay", "true");

    var style = overlay.style;
    style.position = "fixed";
    style.top = "0";
    style.left = "0";
    style.width = "100%";
    style.height = "100%";
    style.display = "flex";
    style.alignItems = "center";
    style.justifyContent = "center";
    style.padding = "20px";
    style.background = "radial-gradient(circle at top, rgba(255,255,255,0.18), transparent 28%), rgba(15, 23, 42, 0.44)";
    style.backdropFilter = "blur(18px)";
    style.opacity = "0";
    style.transition = "opacity 220ms ease";
    style.zIndex = "2147483646";

    document.body.appendChild(overlay);
    overlay.offsetHeight;
    style.opacity = "1";

    this._overlay = overlay;
    this._applyResponsiveLayout();

    var self = this;
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) {
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
      encodeURIComponent(this._config.token) +
      "&origin=" +
      encodeURIComponent(window.location.origin);

    if (this._config.theme.accentColor) {
      url += "&accent=" + encodeURIComponent(this._config.theme.accentColor);
    }
    if (this._config.theme.bgColor) {
      url += "&bg=" + encodeURIComponent(this._config.theme.bgColor);
    }
    if (this._config.theme.borderRadius) {
      url += "&radius=" + encodeURIComponent(this._config.theme.borderRadius);
    }
    if (this._config.theme.logo) {
      url += "&logo=" + encodeURIComponent(this._config.theme.logo);
    }

    iframe.setAttribute("src", url);
    iframe.setAttribute("title", "Plaidify Link");
    iframe.setAttribute("allowtransparency", "true");
    iframe.setAttribute("frameborder", "0");
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups");

    var style = iframe.style;
    style.width = "min(100%, 680px)";
    style.maxWidth = "680px";
    style.height = "min(820px, 92vh)";
    style.border = "none";
    style.borderRadius = this._config.theme.borderRadius;
    style.boxShadow = "0 30px 90px rgba(15, 23, 42, 0.28)";
    style.background = "transparent";
    style.opacity = "0";
    style.transform = "translateY(10px) scale(0.98)";
    style.transition = "opacity 240ms ease, transform 240ms ease";

    this._overlay.appendChild(iframe);
    this._applyResponsiveLayout();
    requestAnimationFrame(function () {
      style.opacity = "1";
      style.transform = "translateY(0) scale(1)";
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
      }, 220);
      this._overlay = null;
    }
  };

  PlaidifyLink.prototype._applyResponsiveLayout = function () {
    if (!this._overlay || !this._iframe) {
      return;
    }

    var overlayStyle = this._overlay.style;
    var frameStyle = this._iframe.style;
    var shouldFullscreen =
      this._config.theme.fullscreenOnMobile !== false &&
      window.innerWidth <= (this._config.theme.mobileBreakpoint || 768);

    if (shouldFullscreen) {
      overlayStyle.padding = "0";
      overlayStyle.alignItems = "stretch";
      overlayStyle.justifyContent = "stretch";
      frameStyle.width = "100vw";
      frameStyle.maxWidth = "100vw";
      frameStyle.height = "100vh";
      frameStyle.borderRadius = "0";
      frameStyle.boxShadow = "none";
      return;
    }

    overlayStyle.padding = "20px";
    overlayStyle.alignItems = "center";
    overlayStyle.justifyContent = "center";
    frameStyle.width = "min(100%, 680px)";
    frameStyle.maxWidth = "680px";
    frameStyle.height = "min(820px, 92vh)";
    frameStyle.borderRadius = this._config.theme.borderRadius;
    frameStyle.boxShadow = "0 30px 90px rgba(15, 23, 42, 0.28)";
  };

  PlaidifyLink.prototype._onMessage = function (event) {
    if (!event.data || event.data.source !== "plaidify-link") {
      return;
    }

    if (event.origin !== this._serverOrigin) {
      return;
    }

    var message = event.data;
    var eventName = message.event;
    this._config.onEvent(eventName, message);

    switch (eventName) {
      case "CONNECTED":
        this._config.onSuccess(message.public_token || "", {
          job_id: message.job_id || "",
          organization_id: message.organization_id || "",
          organization_name: message.organization_name || "",
          public_token: message.public_token || "",
          site: message.site,
        });
        var self = this;
        setTimeout(function () {
          self.close();
        }, 1400);
        break;

      case "EXIT":
      case "DONE":
        this._config.onExit({ reason: message.reason || "user_closed" });
        this.close();
        break;

      case "MFA_REQUIRED":
        this._config.onMFA({
          mfa_type: message.mfa_type,
          session_id: message.session_id,
        });
        break;

      case "ERROR":
        this._config.onExit({ reason: "error", error: message.error });
        break;
    }
  };

  function merge(defaults, overrides) {
    var result = {};
    Object.keys(defaults).forEach(function (key) {
      result[key] = Object.prototype.hasOwnProperty.call(overrides, key)
        ? overrides[key]
        : defaults[key];
    });
    return result;
  }

  root.PlaidifyLink = PlaidifyLink;
})(typeof window !== "undefined" ? window : this);