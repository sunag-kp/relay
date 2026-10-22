import { formatCount, formatMs, formatPercent, formatUsd, shortDay } from "./format";

test("formats numbers for tiles and tables", () => {
  expect(formatCount(1234)).toBe("1,234");
  expect(formatCount(25_300)).toBe("25.3K");
  expect(formatUsd(0)).toBe("$0");
  expect(formatUsd(0.000077)).toBe("$0.000077");
  expect(formatUsd(12.5)).toBe("$12.50");
  expect(formatPercent(0.1818)).toBe("18%");
  expect(formatPercent(0.042)).toBe("4.2%");
  expect(formatMs(31.5)).toBe("32 ms");
  expect(formatMs(1500)).toBe("1.50 s");
  expect(shortDay("2026-10-02")).toBe("10/2");
});
