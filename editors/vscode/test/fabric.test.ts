import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the Azure CLI spawn so we can count calls and control when they resolve.
const { execFileMock } = vi.hoisted(() => ({ execFileMock: vi.fn() }));
vi.mock("node:child_process", () => ({ execFile: execFileMock }));

type ExecCb = (err: unknown, stdout: string, stderr: string) => void;

function tokenJson(token: string, secondsFromNow = 3600): string {
  return JSON.stringify({
    accessToken: token,
    expires_on: Math.floor(Date.now() / 1000) + secondsFromNow,
  });
}

describe("getDatabaseToken", () => {
  beforeEach(() => {
    execFileMock.mockReset();
    vi.resetModules(); // reset module-level dbTokenCache / dbTokenInFlight
  });
  afterEach(() => vi.resetModules());

  it("shares one az call among concurrent callers (dedup in-flight)", async () => {
    let cb: ExecCb | undefined;
    execFileMock.mockImplementation((_az, _args, _opts, callback: ExecCb) => {
      cb = callback; // defer resolution so both callers overlap
    });
    const { getDatabaseToken } = await import("../src/fabric");

    const p1 = getDatabaseToken();
    const p2 = getDatabaseToken();
    expect(execFileMock).toHaveBeenCalledTimes(1); // only ONE spawn

    cb!(null, tokenJson("TKN"), "");
    const [a, b] = await Promise.all([p1, p2]);
    expect(a).toBe("TKN");
    expect(b).toBe("TKN");
    expect(execFileMock).toHaveBeenCalledTimes(1);
  });

  it("serves a cached token without spawning az again", async () => {
    execFileMock.mockImplementation((_az, _args, _opts, callback: ExecCb) => {
      callback(null, tokenJson("CACHED"), "");
    });
    const { getDatabaseToken } = await import("../src/fabric");

    expect(await getDatabaseToken()).toBe("CACHED");
    expect(await getDatabaseToken()).toBe("CACHED");
    expect(execFileMock).toHaveBeenCalledTimes(1); // second call hit the cache
  });

  it("clears the in-flight slot on failure so a later call retries", async () => {
    execFileMock.mockImplementationOnce((_az, _a, _o, cb: ExecCb) =>
      cb(new Error("no login"), "", "az: not logged in"),
    );
    const { getDatabaseToken } = await import("../src/fabric");
    await expect(getDatabaseToken()).rejects.toThrow(/not logged in/);

    // a fresh attempt must spawn az again (in-flight was cleared, no cache set)
    execFileMock.mockImplementationOnce((_az, _a, _o, cb: ExecCb) =>
      cb(null, tokenJson("RETRY"), ""),
    );
    expect(await getDatabaseToken()).toBe("RETRY");
    expect(execFileMock).toHaveBeenCalledTimes(2);
  });
});
