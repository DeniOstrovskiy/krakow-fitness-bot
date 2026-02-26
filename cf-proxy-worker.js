// Cloudflare Worker: proxy for fetching schedule pages from EU edge nodes.
// Deploy this to Cloudflare Workers (free tier: 100K requests/day).
//
// Usage: GET https://your-worker.workers.dev/?url=https://example.com/schedule
//
// Set SECRET_TOKEN env var in Cloudflare dashboard to protect the endpoint.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const target = url.searchParams.get("url");
    const token = url.searchParams.get("token");

    if (!target) {
      return new Response("Missing ?url= parameter", { status: 400 });
    }

    if (env.SECRET_TOKEN && token !== env.SECRET_TOKEN) {
      return new Response("Forbidden", { status: 403 });
    }

    try {
      const resp = await fetch(target, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
          "Accept":
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        },
      });

      const body = await resp.text();
      return new Response(body, {
        status: resp.status,
        headers: { "Content-Type": resp.headers.get("Content-Type") || "text/html" },
      });
    } catch (err) {
      return new Response(`Proxy error: ${err.message}`, { status: 502 });
    }
  },
};
