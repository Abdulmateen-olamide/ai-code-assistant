/**
 * Unit tests for consistent currency/cost formatting (issue #441).
 *
 * Costs are estimates and must always show both the configured currency code
 * and the estimate label.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

const MODULE_PATH = resolve(process.cwd(), "app/static/js/currency.js");

let currency;

beforeAll(() => {
  window.eval(readFileSync(MODULE_PATH, "utf8"));
  currency = window.AICACurrency;
  if (!currency || typeof currency.formatCost !== "function") {
    throw new Error("currency.js did not expose window.AICACurrency.formatCost");
  }
});

describe("currency formatting", () => {
  it("defaults to an explicit USD label", () => {
    expect(currency.formatCost(0.0025)).toBe("$0.0025 USD");
  });

  it("uses the configured symbol, code and precision", () => {
    expect(
      currency.formatCost(12.345, { code: "eur", symbol: "€", decimal_places: 2 })
    ).toBe("€12.35 EUR");
  });

  it("groups thousands", () => {
    expect(
      currency.formatCost(1234567.891, { code: "USD", symbol: "$", decimal_places: 2 })
    ).toBe("$1,234,567.89 USD");
  });

  it("rejects negative and non-finite amounts", () => {
    expect(() => currency.formatCost(-0.01)).toThrow(/non-negative/);
    expect(() => currency.formatCost(Number.NaN)).toThrow(/finite/);
  });

  it("labels estimates and includes the token count", () => {
    const text = currency.formatEstimate({
      prompt_tokens: 1234,
      estimated_cost_usd: 0.0024,
      currency: { code: "USD", symbol: "$", decimal_places: 4 },
    });
    expect(text).toBe("Estimated: ~1,234 tokens ($0.0024 USD; estimate)");
  });
});
