// AI Code Assistant - trust, signature and compatibility badges.
//
// Marketplace and plugin-management surfaces must make a plugin's trust and
// compatibility state understandable without relying on color alone. Every
// badge therefore renders an icon (hidden from assistive technology), explicit
// visible text, and an `aria-label` that spells out the full state.
//
// The module is dependency-free and DOM-free so it can be unit tested under
// Node and reused by any marketplace view:
//
//     var html = window.AICAPluginBadges.render(plugin);
//
// Issue #419: trust and compatibility badges UI.

(function (root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AICAPluginBadges = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var TRUST_LABELS = {
    trusted: "Trusted publisher",
    unverified: "Unverified publisher",
    invalid: "Invalid signature",
  };

  var TRUST_ICONS = {
    trusted: "\u2713",
    unverified: "?",
    invalid: "\u2715",
  };

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function trustState(plugin) {
    var value = plugin && plugin.trust_state != null ? String(plugin.trust_state).trim().toLowerCase() : "";
    if (value === "trusted" || value === "verified") return "trusted";
    if (value === "invalid") return "invalid";
    return "unverified";
  }

  function compatibilityState(plugin) {
    if (plugin && plugin.compatible_with_app === true) return "compatible";
    if (plugin && plugin.compatible_with_app === false) return "incompatible";
    return "unknown";
  }

  function badge(kind, tone, icon, label, detail) {
    var text = label + (detail ? " " + detail : "");
    var aria = kind + ": " + text;
    return (
      '<span class="plugin-badge plugin-badge-' + escapeHtml(tone) + '" role="note" ' +
      'aria-label="' + escapeHtml(aria) + '" title="' + escapeHtml(text) + '">' +
      '<span class="plugin-badge-icon" aria-hidden="true">' + escapeHtml(icon) + "</span>" +
      '<span class="plugin-badge-text">' + escapeHtml(text) + "</span>" +
      "</span>"
    );
  }

  function publisherDetail(plugin) {
    var publisher = plugin && plugin.trust_publisher != null ? String(plugin.trust_publisher).trim() : "";
    return publisher ? "(" + publisher + ")" : "";
  }

  function signatureLabel(state) {
    if (state === "trusted") return "Signature verified";
    if (state === "invalid") return "Signature invalid";
    return "No publisher signature";
  }

  function trustBadge(plugin) {
    var state = trustState(plugin);
    return badge("Trust", state, TRUST_ICONS[state], TRUST_LABELS[state], publisherDetail(plugin));
  }

  function signatureBadge(plugin) {
    var state = trustState(plugin);
    var tone = state === "trusted" ? "trusted" : state === "invalid" ? "invalid" : "unverified";
    return badge("Signature", tone, TRUST_ICONS[state], signatureLabel(state), "");
  }

  function compatibilityBadge(plugin) {
    var state = compatibilityState(plugin);
    var appVersion = plugin && plugin.app_version != null ? String(plugin.app_version).trim() : "";
    var required = plugin && plugin.compatibility != null ? String(plugin.compatibility).trim() : "";

    if (state === "compatible") {
      return badge(
        "Compatibility",
        "compatible",
        "\u2713",
        "Compatible with app" + (appVersion ? " " + appVersion : ""),
        ""
      );
    }
    if (state === "incompatible") {
      var reason = required && required !== "any" ? "(requires " + required + ")" : "";
      return badge(
        "Compatibility",
        "incompatible",
        "\u2715",
        "Incompatible with app" + (appVersion ? " " + appVersion : ""),
        reason
      );
    }
    return badge("Compatibility", "unknown", "?", "Compatibility unknown", "");
  }

  function render(plugin) {
    return (
      '<span class="plugin-badges">' +
      trustBadge(plugin) +
      signatureBadge(plugin) +
      compatibilityBadge(plugin) +
      "</span>"
    );
  }

  return {
    escapeHtml: escapeHtml,
    trustState: trustState,
    compatibilityState: compatibilityState,
    trustBadge: trustBadge,
    signatureBadge: signatureBadge,
    compatibilityBadge: compatibilityBadge,
    render: render,
  };
});
