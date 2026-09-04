"use client";

import axios from "axios";

import { useAuthStore } from "@/store/auth";

/**
 * Central HTTP client.
 *
 * - baseURL is same-origin /api/v1 (Next rewrites -> FastAPI) so no CORS
 *   preflight ever fires.
 * - Request interceptor injects the 120-min JWT from sessionStorage-backed
 *   Zustand into Authorization: Bearer on every outbound call (zero
 *   hardcoding of tokens in components).
 * - Response interceptor handles global 401 (expired token): clears auth and
 *   bounces the user to the onboarding screen.
 */
export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 30_000,
  // NOTE: no global Content-Type default. Axios auto-sets "application/json"
  // for object bodies and leaves FormData untouched so the browser attaches
  // the multipart boundary. A forced application/json default made
  // POST /invoices/batch JSON-serialize the PDF FormData (axios 1.x
  // formDataToJSON) and FastAPI 422'd on the missing file field.
});

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status;
    if (status === 401) {
      const token = useAuthStore.getState().token;
      if (token) {
        useAuthStore.getState().signOut();
        // Hard navigation so every provider/page re-initialises unauthenticated.
        if (typeof window !== "undefined" && window.location.pathname !== "/") {
          window.location.assign("/");
        }
      }
    }
    return Promise.reject(error);
  }
);

/** Structured FastAPI error detail: { error_code, message } (fallbacks included). */
export function extractApiError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") {
      return message;
    }
  }
  return "Something went wrong. Please try again.";
}
