import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateAccountModal } from "./CreateAccountModal";

const apiMock = vi.hoisted(() => ({
  createAccount: vi.fn(),
}));

vi.mock("../../shared/api", () => ({ api: apiMock }));
vi.mock("../../shared/toast", () => ({
  useToast: () => ({ push: vi.fn() }),
}));
vi.mock("../../shared/account-context", () => ({
  useAccount: () => ({
    setAccountId: vi.fn(),
    refresh: async () => undefined,
  }),
}));

function renderModal() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CreateAccountModal open onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

describe("CreateAccountModal", () => {
  it("defaults to a paper account and hides live risks", () => {
    renderModal();
    expect((screen.getByLabelText(/^账户类型/) as HTMLSelectElement).value).toBe("paper");
    expect(screen.queryByText(/实盘账户会产生真实资金变动/)).toBeNull();
    expect(document.querySelector("button.danger")).toBeNull();
  });

  it("shows the live risks and a destructive-styled button when switching to live", async () => {
    renderModal();
    fireEvent.change(screen.getByLabelText(/^账户类型/), { target: { value: "live" } });

    await waitFor(() => expect(screen.getByText(/实盘账户会产生真实资金变动/)).toBeTruthy());
    expect(screen.getByText(/X-Live-Confirm/)).toBeTruthy();
    expect(screen.getByText(/LQ_LIVE_AUTO_TRADING_ENABLED/)).toBeTruthy();
    expect(screen.getByText(/不构成投资建议/)).toBeTruthy();
    // The primary action must not look like the ordinary paper button.
    expect(document.querySelector("button.danger")).toBeTruthy();
  });

  it("sends kind=live only when the user selected it", async () => {
    apiMock.createAccount.mockResolvedValue({ id: "a", name: "实盘账户" });
    renderModal();

    fireEvent.click(screen.getByRole("button", { name: "创建账户" }));
    await waitFor(() => expect(apiMock.createAccount).toHaveBeenCalled());
    expect(apiMock.createAccount.mock.calls[0][0].kind).toBe("paper");

    apiMock.createAccount.mockClear();
    fireEvent.change(screen.getByLabelText(/^账户类型/), { target: { value: "live" } });
    fireEvent.click(screen.getByRole("button", { name: "创建实盘账户" }));
    await waitFor(() => expect(apiMock.createAccount).toHaveBeenCalled());
    expect(apiMock.createAccount.mock.calls[0][0].kind).toBe("live");
  });

  it("hides leverage for live accounts", async () => {
    renderModal();
    expect(screen.getByLabelText(/^杠杆/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^账户类型/), { target: { value: "live" } });
    await waitFor(() => expect(screen.queryByLabelText(/^杠杆/)).toBeNull());
  });
});
