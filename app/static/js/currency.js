// AI Code Assistant — consistent currency and cost formatting (issue #441).
//
// Cost figures are estimates, never invoice amounts. This module is
// dependency-free and DOM-free so the browser and the Vitest suite use the
// exact same formatting rules. The configured currency is passed in from the
// estimate API (or window.AICA_CURRENCY when rendered by the server) and
// defaults to USD.

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AICACurrency = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_CURRENCY = { code: "USD", symbol: "$", decimal_places: 4 };

  function normalize(config) {
    var source = config || {};
    var code = String(source.code || DEFAULT_CURRENCY.code).trim().toUpperCase();
    var symbol = source.symbol == null ? DEFAULT_CURRENCY.symbol : String(source.symbol).trim();
    var places = Number(source.decimal_places);
    if (!Number.isFinite(places)) {
      places = DEFAULT_CURRENCY.decimal_places;
    }
    places = Math.min(Math.max(Math.trunc(places), 0), 8);
    return {
      code: code.slice(0, 8) || DEFAULT_CURRENCY.code,
      symbol: symbol.slice(0, 8),
      decimal_places: places,
    };
  }

  function roundHalfUp(value, places) {
    var factor = Math.pow(10, places);
    return Math.round((value + Number.EPSILON) * factor) / factor;
  }

  function groupThousands(value) {
    var parts = value.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts.join(".");
  }

  function formatCost(amount, config) {
    var currency = normalize(config);
    var value = Number(amount == null ? 0 : amount);
    if (!Number.isFinite(value)) {
      throw new Error("Cost amount must be finite.");
    }
    if (value < 0) {
      throw new Error("Cost amount must be non-negative.");
    }
    var rounded = roundHalfUp(value, currency.decimal_places).toFixed(currency.decimal_places);
    return (currency.symbol || "") + groupThousands(rounded) + " " + currency.code;
  }

  function formatEstimate(estimate, config) {
    var data = estimate || {};
    var currency = normalize(config || data.currency);
    var tokens = Number(data.prompt_tokens) || 0;
    var amount = data.estimated_cost_usd;
    if (amount == null) {
      amount = data.estimated_cost;
    }
    return (
      "Estimated: ~" +
      Math.max(0, Math.trunc(tokens)).toLocaleString("en-US") +
      " tokens (" +
      formatCost(amount, currency) +
      "; estimate)"
    );
  }

  return {
    DEFAULT_CURRENCY: DEFAULT_CURRENCY,
    formatCost: formatCost,
    formatEstimate: formatEstimate,
    normalize: normalize,
  };
});
