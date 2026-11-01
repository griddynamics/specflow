// Drive the Auth.js HTTP flow as a browser would, without a browser.

/** Minimal cookie jar: name -> value, from Set-Cookie headers. */
export class Jar {
  #cookies = new Map();
  /** Every Set-Cookie line absorbed so far (for tests that check cookie attributes). */
  setCookies = [];
  absorb(res) {
    for (const line of res.headers.getSetCookie()) {
      this.setCookies.push(line);
      const [pair, ...attrs] = line.split(";");
      const eq = pair.indexOf("=");
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1).trim();
      const expired = attrs.some((a) => /^\s*(max-age=0|expires=thu, 01 jan 1970)/i.test(a));
      if (expired || value === "") this.#cookies.delete(name);
      else this.#cookies.set(name, value);
    }
  }
  header() {
    return [...this.#cookies].map(([k, v]) => `${k}=${v}`).join("; ");
  }
  names() {
    return [...this.#cookies.keys()];
  }
}

export async function csrf(base, jar) {
  const res = await fetch(`${base}/api/auth/csrf`, { headers: { cookie: jar.header() } });
  jar.absorb(res);
  return (await res.json()).csrfToken;
}

/**
 * Sign in through the real Auth.js OIDC code flow, as a browser does, against an issuer the test runs (the stub issuer
 * signs in the `login_hint` user). The issuer's public URL must be reachable from here. The session cookie ends up in `jar`.
 */
export async function oidcLogin(base, email, jar = new Jar()) {
  const csrfToken = await csrf(base, jar);
  const start = await fetch(`${base}/api/auth/signin/oidc`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken, callbackUrl: `${base}/` }),
  });
  jar.absorb(start);
  if (start.status !== 302) throw new Error(`oidc sign-in start answered ${start.status}`);
  const authorize = new URL(start.headers.get("location"));
  authorize.searchParams.set("login_hint", email);
  const approved = await fetch(authorize, { redirect: "manual" });
  if (approved.status !== 302) throw new Error(`issuer authorize answered ${approved.status}`);
  const callback = await fetch(approved.headers.get("location"), { redirect: "manual", headers: { cookie: jar.header() } });
  jar.absorb(callback);
  if (callback.status !== 302) throw new Error(`oidc callback answered ${callback.status}`);
  return jar;
}

export const sessionOf = async (base, jar) => {
  const res = await fetch(`${base}/api/auth/session`, { headers: { cookie: jar.header() } });
  jar.absorb(res);
  return res.json();
};
