"use client";

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export interface VendorProfile {
  vendor_code: string;
  vendor_name: string;
  role: "ADMIN" | "VIEWER" | string;
}

export interface AuthState {
  /** JWT returned by the native vendor auth endpoints (120-min TTL). */
  token: string | null;
  profile: VendorProfile | null;
  signIn: (token: string, profile: VendorProfile) => void;
  signOut: () => void;
}

/**
 * Global auth state persisted to sessionStorage (survives reloads within the
 * tab; cleared when the tab closes). The axios request interceptor reads the
 * token from here, so every outbound call carries Authorization: Bearer.
 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      profile: null,
      signIn: (token, profile) => set({ token, profile }),
      signOut: () => set({ token: null, profile: null }),
    }),
    {
      name: "finrecon-auth",
      storage: createJSONStorage(() => sessionStorage),
    }
  )
);
