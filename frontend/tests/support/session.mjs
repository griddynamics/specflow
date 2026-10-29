// Drive the Auth.js HTTP flow as a browser would, without a browser.

/** Minimal cookie jar: name -> value, from Set-Cookie headers. */
export class Jar {
  #cookies = new Map();
  absorb(res) {
    for (const line of res.headers.getSetCookie()) {
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

/** Dev login (credentials provider). Returns the jar holding the session cookie. */
export async function devLogin(base, email, jar = new Jar()) {
  const csrfToken = await csrf(base, jar);
  const res = await fetch(`${base}/api/auth/callback/credentials`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken, email, callbackUrl: `${base}/` }),
  });
  jar.absorb(res);
  return jar;
}

export const sessionOf = async (base, jar) => {
  const res = await fetch(`${base}/api/auth/session`, { headers: { cookie: jar.header() } });
  jar.absorb(res);
  return res.json();
};
