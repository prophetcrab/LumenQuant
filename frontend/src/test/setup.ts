import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// React Testing Library does not auto-clean when globals are enabled via
// vitest.config; leaving mounted trees behind makes queries ambiguous.
afterEach(() => {
  cleanup();
});
