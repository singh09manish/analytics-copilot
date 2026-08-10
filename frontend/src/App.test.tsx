import { render, screen } from "@testing-library/react";
import App from "./App";
import { setAuth } from "./auth";

test("renders title and input", () => {
  setAuth({ token: "t", role: "analyst", email: "analyst@demo" });
  render(<App />);
  expect(screen.getByText("Analytics Copilot")).toBeDefined();
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});

test("unauthenticated users see the login form", () => {
  localStorage.clear();
  render(<App />);
  expect(screen.getByPlaceholderText("password")).toBeDefined();
});
