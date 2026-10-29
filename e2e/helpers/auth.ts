/**
 * Authenticated Playwright fixtures.
 *
 * Every /api/* route except /health, /api/health and /api/auth/* requires a JWT, so API specs
 * use `authRequest` (an APIRequestContext that sends `Authorization: Bearer <admin token>`).
 * The plain `request` fixture stays unauthenticated for tests that assert 401 behaviour.
 *
 * The admin password comes from E2E_ADMIN_PASSWORD (CI generates one and starts the backend with
 * the same value); it falls back to "admin", which only works against a backend started with
 * ENVIRONMENT=development and no ADMIN_PASSWORD.
 */

import { test as base, expect, request as pwRequest, APIRequestContext } from "@playwright/test";

const API = process.env.BASE_URL ?? "http://localhost:8000";
export const ADMIN_USER = process.env.E2E_ADMIN_USER ?? "admin";
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? "admin";

export async function loginAdmin(request: APIRequestContext): Promise<string> {
  const r = await request.post(`${API}/api/auth/login`, {
    data: { username: ADMIN_USER, password: ADMIN_PASSWORD },
  });
  if (r.status() !== 200) {
    throw new Error(`Admin login failed: HTTP ${r.status()} (is E2E_ADMIN_PASSWORD set correctly?)`);
  }
  const body = await r.json();
  if (!body.access_token) throw new Error("Admin login returned no access_token");
  return body.access_token as string;
}

export const test = base.extend<{ authRequest: APIRequestContext }, { adminToken: string }>({
  // One login per worker keeps us well under the login rate limit
  adminToken: [
    async ({}, use) => {
      const anon = await pwRequest.newContext();
      try {
        await use(await loginAdmin(anon));
      } finally {
        await anon.dispose();
      }
    },
    { scope: "worker" },
  ],
  authRequest: async ({ adminToken }, use) => {
    const ctx = await pwRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${adminToken}` },
    });
    await use(ctx);
    await ctx.dispose();
  },
});

export { expect };
