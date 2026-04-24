import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  LinkApi,
  encryptCredentials,
  pollLinkSession,
} from "./api";

function okResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("LinkApi", () => {
  it("encodes the link token in the status URL", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(okResponse({ status: "pending" }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test/",
      linkToken: "tok with space",
      fetchImpl,
    });
    const status = await api.getStatus();

    expect(status.status).toBe("pending");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "https://api.plaidify.test/link/sessions/tok%20with%20space/status",
    );
  });

  it("builds the organization search URL", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(okResponse({ results: [], count: 0 }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    await api.searchOrganizations({ query: "hydro", limit: 5 });

    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "https://api.plaidify.test/organizations/search?q=hydro&limit=5",
    );
  });

  it("serializes the /connect body", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(okResponse({ status: "connected", public_token: "public-1" }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    const response = await api.connect({
      site: "hydro_one",
      encrypted: { username: "u", password: "p" },
    });

    expect(response.status).toBe("connected");
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.plaidify.test/connect");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      link_token: "tok",
      site: "hydro_one",
      encrypted_username: "u",
      encrypted_password: "p",
    });
  });

  it("raises ApiError with the server detail on non-2xx", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () =>
      new Response(JSON.stringify({ detail: "bad token" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    );

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });

    let caught: unknown;
    try {
      await api.getStatus();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(403);
    expect((caught as ApiError).message).toBe("bad token");
  });

  it("builds the /mfa/submit URL with query params", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(okResponse({ status: "connected" }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    await api.submitMfa({ sessionId: "sess-1", code: "123 456" });

    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "https://api.plaidify.test/mfa/submit?session_id=sess-1&code=123+456",
    );
    expect(init.method).toBe("POST");
  });
});

describe("encryptCredentials", () => {
  it("encrypts username and password separately and base64-encodes them", async () => {
    const importKey = vi.fn().mockResolvedValue({ fake: true });
    const encrypt = vi
      .fn()
      .mockImplementationOnce(async () => new Uint8Array([1, 2, 3]).buffer)
      .mockImplementationOnce(async () => new Uint8Array([4, 5, 6]).buffer);

    const subtle = { importKey, encrypt } as unknown as SubtleCrypto;
    const pem = `-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----`;

    const result = await encryptCredentials(pem, "user", "pass", subtle);

    expect(importKey).toHaveBeenCalledTimes(1);
    const [format, _buffer, algorithm] = importKey.mock.calls[0];
    expect(format).toBe("spki");
    expect(algorithm).toEqual({ name: "RSA-OAEP", hash: "SHA-256" });
    expect(encrypt).toHaveBeenCalledTimes(2);
    expect(result.username).toBe(btoa(String.fromCharCode(1, 2, 3)));
    expect(result.password).toBe(btoa(String.fromCharCode(4, 5, 6)));
  });
});

describe("pollLinkSession", () => {
  it("returns immediately on a terminal status", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockImplementation(async () =>
        okResponse({ status: "completed", public_token: "public-1" }),
      );

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    const result = await pollLinkSession({ api, sleep: async () => undefined });
    expect(result.status).toBe("completed");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("polls until the status leaves the pending state", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(okResponse({ status: "pending" }))
      .mockResolvedValueOnce(okResponse({ status: "pending" }))
      .mockResolvedValueOnce(okResponse({ status: "completed", public_token: "public-x" }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    const ticks: string[] = [];
    const result = await pollLinkSession({
      api,
      sleep: async () => undefined,
      onTick: (status) => ticks.push(status.status),
    });
    expect(result.status).toBe("completed");
    expect(ticks).toEqual(["pending", "pending", "completed"]);
  });

  it("returns a timeout status after the max attempts", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockImplementation(async () => okResponse({ status: "pending" }));

    const api = new LinkApi({
      serverUrl: "https://api.plaidify.test",
      linkToken: "tok",
      fetchImpl,
    });
    const result = await pollLinkSession({
      api,
      maxAttempts: 3,
      sleep: async () => undefined,
    });
    expect(result.status).toBe("timeout");
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });
});
