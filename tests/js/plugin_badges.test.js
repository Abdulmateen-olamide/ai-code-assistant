/**
 * Unit tests for the plugin trust, signature and compatibility badges (#419).
 *
 * Every badge must state its status in text (never color alone), expose an
 * accessible label, and safely escape plugin-controlled values.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

const MODULE_PATH = resolve(process.cwd(), "app/static/js/plugin_badges.js");

let badges;

beforeAll(() => {
  window.eval(readFileSync(MODULE_PATH, "utf8"));
  badges = window.AICAPluginBadges;
  if (!badges || typeof badges.render !== "function") {
    throw new Error("plugin_badges.js did not expose window.AICAPluginBadges.render");
  }
});

function mount(html) {
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

function basePlugin(overrides) {
  return Object.assign(
    {
      id: "example-plugin",
      trust_state: "Trusted",
      trust_publisher: "Acme",
      compatibility: "any",
      compatible_with_app: true,
      app_version: "0.1.0",
    },
    overrides || {}
  );
}

describe("testing surface", () => {
  it("publishes the badge helpers", () => {
    for (const name of [
      "render",
      "trustBadge",
      "signatureBadge",
      "compatibilityBadge",
      "trustState",
      "compatibilityState",
    ]) {
      expect(typeof badges[name]).toBe("function");
    }
  });
});

describe("trust badges", () => {
  it("renders a verified publisher with text and an accessible label", () => {
    const container = mount(badges.render(basePlugin()));
    const trust = container.querySelector(".plugin-badge-trusted");

    expect(trust).not.toBeNull();
    expect(trust.textContent).toContain("Trusted publisher");
    expect(trust.textContent).toContain("Acme");
    expect(trust.getAttribute("aria-label")).toBe("Trust: Trusted publisher (Acme)");
    expect(trust.getAttribute("role")).toBe("note");
  });

  it("renders an unverified publisher", () => {
    const container = mount(badges.render(basePlugin({ trust_state: "Unverified", trust_publisher: null })));
    const trust = container.querySelector(".plugin-badge-unverified");

    expect(trust).not.toBeNull();
    expect(trust.textContent).toContain("Unverified publisher");
    expect(trust.getAttribute("aria-label")).toContain("Unverified publisher");
  });

  it("renders an invalid signature as distinct from unverified", () => {
    const container = mount(badges.render(basePlugin({ trust_state: "Invalid", trust_publisher: "Acme" })));
    const trust = container.querySelector(".plugin-badge-invalid");

    expect(trust).not.toBeNull();
    expect(trust.textContent).toContain("Invalid signature");
    expect(container.textContent).not.toContain("Unverified publisher");
  });

  it("treats an unknown or missing trust state as unverified", () => {
    expect(badges.trustState({})).toBe("unverified");
    expect(badges.trustState({ trust_state: "trusted" })).toBe("trusted");
    expect(badges.trustState({ trust_state: "VERIFIED" })).toBe("trusted");
  });
});

describe("signature badges", () => {
  it("says the signature is verified for a trusted publisher", () => {
    const container = mount(badges.render(basePlugin()));
    expect(container.textContent).toContain("Signature verified");
  });

  it("says there is no publisher signature for an unverified plugin", () => {
    const container = mount(badges.render(basePlugin({ trust_state: "Unverified" })));
    expect(container.textContent).toContain("No publisher signature");
  });

  it("says the signature is invalid for an invalid plugin", () => {
    const container = mount(badges.render(basePlugin({ trust_state: "Invalid" })));
    expect(container.textContent).toContain("Signature invalid");
  });
});

describe("compatibility badges", () => {
  it("renders a compatible plugin with the app version", () => {
    const container = mount(badges.render(basePlugin()));
    const compat = container.querySelector(".plugin-badge-compatible");

    expect(compat).not.toBeNull();
    expect(compat.textContent).toContain("Compatible with app 0.1.0");
    expect(compat.getAttribute("aria-label")).toContain("Compatibility:");
  });

  it("renders an incompatible plugin with the required range", () => {
    const container = mount(
      badges.render(
        basePlugin({
          trust_state: "Unverified",
          compatible_with_app: false,
          compatibility: ">=99.0.0",
        })
      )
    );
    const compat = container.querySelector(".plugin-badge-incompatible");

    expect(compat).not.toBeNull();
    expect(compat.textContent).toContain("Incompatible with app 0.1.0");
    expect(compat.textContent).toContain("requires >=99.0.0");
  });

  it("renders an explicit unknown state when the backend omits compatibility", () => {
    const container = mount(badges.render({ trust_state: "Unverified" }));
    expect(container.textContent).toContain("Compatibility unknown");
    expect(container.querySelector(".plugin-badge-unknown")).not.toBeNull();
  });

  it("identifies compatibility state from the backend flag", () => {
    expect(badges.compatibilityState({ compatible_with_app: true })).toBe("compatible");
    expect(badges.compatibilityState({ compatible_with_app: false })).toBe("incompatible");
    expect(badges.compatibilityState({})).toBe("unknown");
  });
});

describe("accessibility and safety", () => {
  it("never relies on color alone", () => {
    const container = mount(
      badges.render(
        basePlugin({
          trust_state: "Invalid",
          compatible_with_app: false,
          compatibility: ">=99.0.0",
        })
      )
    );

    const rendered = container.querySelectorAll(".plugin-badge");
    expect(rendered.length).toBe(3);
    rendered.forEach((badge) => {
      const text = badge.querySelector(".plugin-badge-text");
      const icon = badge.querySelector(".plugin-badge-icon");
      expect(text.textContent.trim().length).toBeGreaterThan(0);
      expect(badge.getAttribute("aria-label").trim().length).toBeGreaterThan(0);
      expect(icon.getAttribute("aria-hidden")).toBe("true");
    });
  });

  it("escapes plugin-controlled values", () => {
    const container = mount(
      badges.render(
        basePlugin({
          trust_publisher: '<img src=x onerror="alert(1)">',
          compatibility: '"><script>alert(1)</script>',
          compatible_with_app: false,
        })
      )
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("renders a safe fallback for a missing plugin", () => {
    const container = mount(badges.render(null));
    expect(container.textContent).toContain("Unverified publisher");
    expect(container.textContent).toContain("No publisher signature");
    expect(container.textContent).toContain("Compatibility unknown");
  });
});
