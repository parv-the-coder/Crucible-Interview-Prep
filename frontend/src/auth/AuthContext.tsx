import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, onSignedOut, tokens } from "../api/client";
import type { User } from "../api/types";

interface AuthState {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, name: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Restore the session on load. The stored access token may well be expired;
  // /auth/me triggers the client's refresh-and-retry, so a returning user is
  // signed in silently rather than being bounced to the login page.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!tokens.access()) {
        setLoading(false);
        return;
      }
      try {
        const me = await api.get<User>("/auth/me");
        if (!cancelled) setUser(me);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The client signals when a refresh has failed for good.
  useEffect(() => onSignedOut(() => setUser(null)), []);

  const signIn = useCallback(async (email: string, password: string) => {
    const result = await api.signIn(email, password);
    tokens.set(result.tokens);
    setUser(result.user);
  }, []);

  const signUp = useCallback(async (email: string, name: string, password: string) => {
    const result = await api.signUp(email, name, password);
    tokens.set(result.tokens);
    setUser(result.user);
  }, []);

  const signOut = useCallback(async () => {
    await api.signOut();
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, signIn, signUp, signOut }),
    [user, loading, signIn, signUp, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
