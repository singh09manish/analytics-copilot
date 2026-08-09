import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders title and input", () => {
  render(<App />);
  expect(screen.getByText("Analytics Copilot")).toBeDefined();
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});
