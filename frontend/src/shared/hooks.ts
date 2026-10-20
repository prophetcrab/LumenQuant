import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useAccount } from "./account-context";

export function useBookOverview() {
  const { accountId } = useAccount();
  return useQuery({
    queryKey: ["overview", accountId],
    queryFn: () => api.portfolioOverview(accountId),
    enabled: Boolean(accountId),
    refetchInterval: 4_000,
  });
}

export function useEquityHistory(limit = 500) {
  const { accountId } = useAccount();
  return useQuery({
    queryKey: ["equity-history", accountId, limit],
    queryFn: () => api.portfolioHistory(accountId, limit),
    enabled: Boolean(accountId),
    refetchInterval: 8_000,
  });
}

export function usePositions() {
  const { accountId } = useAccount();
  return useQuery({
    queryKey: ["positions", accountId],
    queryFn: () => api.positions(accountId as string),
    enabled: Boolean(accountId),
    refetchInterval: 5_000,
  });
}

export function useOrders(status?: string, limit = 200) {
  const { accountId } = useAccount();
  return useQuery({
    queryKey: ["orders", accountId, status, limit],
    queryFn: () => api.orders(accountId as string, status, limit),
    enabled: Boolean(accountId),
    refetchInterval: 5_000,
  });
}

export function useFills(limit = 100) {
  const { accountId } = useAccount();
  return useQuery({
    queryKey: ["fills", accountId, limit],
    queryFn: () => api.fills(accountId as string, limit),
    enabled: Boolean(accountId),
    refetchInterval: 10_000,
  });
}

export function useSnapshots() {
  return useQuery({
    queryKey: ["snapshots"],
    queryFn: api.snapshots,
    refetchInterval: 5_000,
  });
}

export function useSymbols() {
  return useQuery({ queryKey: ["symbols"], queryFn: api.symbols, refetchInterval: 60_000 });
}

export function useMarketClock() {
  return useQuery({ queryKey: ["clock"], queryFn: api.clock, refetchInterval: 5_000 });
}
