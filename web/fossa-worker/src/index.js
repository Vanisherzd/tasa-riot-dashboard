const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}

function tokenMatches(candidate, expected) {
  if (typeof candidate !== "string" || typeof expected !== "string") {
    return false;
  }

  const candidateBytes = new TextEncoder().encode(candidate);
  const expectedBytes = new TextEncoder().encode(expected);
  if (candidateBytes.length !== expectedBytes.length) {
    return false;
  }

  let difference = 0;
  for (let index = 0; index < candidateBytes.length; index += 1) {
    difference |= candidateBytes[index] ^ expectedBytes[index];
  }
  return difference === 0;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

function keyPart(value, fallback) {
  if (typeof value !== "string" || value.length === 0) {
    return fallback;
  }
  return value.replace(/[^0-9A-Za-z_-]/g, "_").slice(0, 80) || fallback;
}

async function storeMessage(request, env) {
  const receivedUnix = Math.floor(Date.now() / 1000);
  const contentType = (request.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
  const requestBytes = new Uint8Array(await request.arrayBuffer());
  let body;

  if (contentType === "application/json") {
    try {
      body = JSON.parse(new TextDecoder().decode(requestBytes));
    } catch (error) {
      return jsonResponse({ ok: false, error: "invalid JSON" }, 400);
    }
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      return jsonResponse({ ok: false, error: "JSON body must be an object" }, 400);
    }
  } else if (contentType === "application/octet-stream") {
    body = { message: bytesToBase64(requestBytes), raw: true };
  } else {
    return jsonResponse({ ok: false, error: "unsupported content type" }, 415);
  }

  const digestBytes = new Uint8Array(await crypto.subtle.digest("SHA-256", requestBytes));
  const digest = [...digestBytes].map((byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, 8);
  const satTimestamp = Number(body.sat_timestamp);
  const timestamp = Number.isFinite(satTimestamp) && satTimestamp > 0 ? Math.floor(satTimestamp) : receivedUnix;
  const mac = keyPart(body.mac, "unknown");
  const key = `${timestamp}-${mac}-${digest}`;
  const value = {
    received_at: new Date(receivedUnix * 1000).toISOString(),
    content_type: contentType,
    body,
  };

  await env.FOSSA_KV.put(key, JSON.stringify(value));
  return jsonResponse({ ok: true });
}

async function exportMessages(url, env) {
  const rawSince = url.searchParams.get("since") || "0";
  const since = Number.parseInt(rawSince, 10);
  if (!Number.isFinite(since) || since < 0) {
    return jsonResponse({ error: "since must be a non-negative Unix timestamp" }, 400);
  }

  const cursor = url.searchParams.get("cursor") || undefined;
  const page = await env.FOSSA_KV.list({ limit: 1000, cursor });
  const selected = page.keys.filter((entry) => {
    const timestamp = Number.parseInt(entry.name.split("-", 1)[0], 10);
    return Number.isFinite(timestamp) && timestamp >= since;
  });
  const values = await Promise.all(
    selected.map(async (entry) => {
      const stored = await env.FOSSA_KV.get(entry.name, "json");
      return stored === null ? null : { key: entry.name, ...stored };
    }),
  );

  return jsonResponse({
    items: values.filter((item) => item !== null),
    cursor: page.list_complete ? "" : page.cursor,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }

    // Two separate secrets: INGEST_TOKEN (given to FOSSA for POST) and EXPORT_TOKEN
    // (used by the dashboard pipeline for GET). A leaked ingest URL cannot read data.
    // Both must be set and at least 16 characters, otherwise every route is 404.
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts.length !== 2) {
      return new Response("Not Found", { status: 404 });
    }
    const expected = parts[0] === "fossa" ? env.INGEST_TOKEN : parts[0] === "export" ? env.EXPORT_TOKEN : undefined;
    if (typeof expected !== "string" || expected.length < 16 || !tokenMatches(parts[1], expected)) {
      return new Response("Not Found", { status: 404 });
    }

    if (request.method === "POST" && parts[0] === "fossa") {
      return storeMessage(request, env);
    }
    if (request.method === "GET" && parts[0] === "export") {
      return exportMessages(url, env);
    }
    return new Response("Method Not Allowed", { status: 405, headers: { allow: parts[0] === "fossa" ? "POST" : "GET" } });
  },
};
