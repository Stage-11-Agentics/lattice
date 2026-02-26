/**
 * Cloudflare Worker: GitHub OAuth token exchange proxy.
 *
 * Handles the OAuth code→token exchange for the Lattice dashboard
 * deployed on GitHub Pages. GitHub's OAuth endpoint doesn't support
 * CORS and requires the client_secret, so this worker acts as the
 * secure intermediary.
 *
 * Setup:
 *   1. Create a Cloudflare Worker (free tier)
 *   2. Set these secrets:
 *      - GITHUB_CLIENT_ID    (from your GitHub OAuth App)
 *      - GITHUB_CLIENT_SECRET (from your GitHub OAuth App)
 *   3. Deploy this file as the worker script
 *   4. Set the worker URL as OAUTH_PROXY_URL in your dashboard config
 *
 * Usage from frontend:
 *   POST https://your-worker.workers.dev/
 *   Body: {"code": "the_oauth_code_from_github"}
 *   Returns: {"access_token": "...", "token_type": "bearer", "scope": "..."}
 */

export default {
  async fetch(request, env) {
    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "Method not allowed" }), {
        status: 405,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
      });
    }

    try {
      const { code } = await request.json();
      if (!code) {
        return new Response(JSON.stringify({ error: "Missing 'code' in request body" }), {
          status: 400,
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
        });
      }

      // Exchange the authorization code for an access token
      const tokenResponse = await fetch("https://github.com/login/oauth/access_token", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
        body: JSON.stringify({
          client_id: env.GITHUB_CLIENT_ID,
          client_secret: env.GITHUB_CLIENT_SECRET,
          code: code,
        }),
      });

      const tokenData = await tokenResponse.json();

      return new Response(JSON.stringify(tokenData), {
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
        },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
      });
    }
  },
};
