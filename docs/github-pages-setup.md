# GitHub Pages Dashboard Setup

Deploy the Lattice dashboard as a static site on GitHub Pages, backed by a GitHub Project for task data.

## Prerequisites

- A GitHub repository (fork of `Stage-11-Agentics/lattice`)
- A GitHub Project (v2) with issues
- `gh` CLI installed and authenticated

## 1. Configure Repository Variables

Go to your repo's **Settings > Secrets and variables > Actions > Variables** and set:

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `GH_ORG` | Yes | `decentralparknyc` | GitHub org or user owning the project |
| `GH_PROJECT_NUMBER` | Yes | `1` | The project number |
| `GH_REPO` | No | `decentralparknyc/tasks.fun` | Default repo for issue links |
| `OAUTH_CLIENT_ID` | No | `Ov23li...` | GitHub OAuth App client ID (for auth) |
| `OAUTH_PROXY_URL` | No | `https://lattice-oauth.workers.dev` | OAuth proxy URL (for auth) |

## 2. Enable GitHub Pages

1. Go to **Settings > Pages**
2. Set **Source** to **GitHub Actions**
3. Save

## 3. Push to Main

The `deploy-dashboard.yml` workflow triggers on:
- Push to `main`
- Issue events (open, close, edit, label, assign)
- Manual dispatch (`workflow_dispatch`)
- Every 6 hours (schedule)

After push, the dashboard will be live at:
```
https://<username>.github.io/<repo-name>/
```

## 4. (Optional) Enable GitHub OAuth

For two-way integration (editing tasks from the dashboard), you need authentication.

### Option A: Personal Access Token (Quick)

Click "Sign in with GitHub" on the deployed dashboard and paste a GitHub Personal Access Token with `repo` and `project` scopes. The token is stored in your browser's localStorage.

### Option B: OAuth App (Multi-user)

For a proper OAuth flow that works for any GitHub user:

#### Create a GitHub OAuth App

1. Go to **GitHub Settings > Developer settings > OAuth Apps > New OAuth App**
2. Set:
   - **Application name**: `Lattice Dashboard`
   - **Homepage URL**: `https://<username>.github.io/<repo>/`
   - **Authorization callback URL**: `https://<username>.github.io/<repo>/`
3. Note the **Client ID** and **Client Secret**

#### Deploy the OAuth Proxy

The OAuth flow requires a server-side proxy to exchange the authorization code for a token (GitHub doesn't support CORS on the token endpoint).

A Cloudflare Worker template is provided at `scripts/oauth-proxy/worker.js`.

**Deploy to Cloudflare Workers (free tier):**

1. Install Wrangler: `npm install -g wrangler`
2. Login: `wrangler login`
3. Create a new worker:
   ```bash
   cd scripts/oauth-proxy
   wrangler init lattice-oauth
   # Copy worker.js into the project
   ```
4. Set secrets:
   ```bash
   wrangler secret put GITHUB_CLIENT_ID
   wrangler secret put GITHUB_CLIENT_SECRET
   ```
5. Deploy: `wrangler deploy`
6. Note the worker URL (e.g., `https://lattice-oauth.<your-subdomain>.workers.dev`)

#### Configure the Dashboard

Set the repository variables:
- `OAUTH_CLIENT_ID`: The Client ID from step 1
- `OAUTH_PROXY_URL`: The Cloudflare Worker URL from step 5

Trigger a rebuild (push to main or manual dispatch).

## Status Mapping

GitHub Project statuses are mapped to Lattice statuses:

| GitHub Status | Lattice Status |
|--------------|----------------|
| Todo | backlog |
| Backlog | backlog |
| In Progress | in_progress |
| In Review | review |
| Needs review | review |
| Done | done |
| Cancelled | cancelled |
| Blocked | blocked |

## How It Works

### Read Path (unauthenticated)
1. CI workflow fetches GitHub Project items via `gh project item-list`
2. `generate_dashboard_data.py` converts them to dashboard-compatible JSON
3. Static files deployed to GitHub Pages
4. Dashboard loads `data/snapshot.json` and `data/config.json` on boot

### Read Path (authenticated)
1. User signs in with GitHub
2. Dashboard fetches live data from GitHub's GraphQL API
3. Real-time view of current project state

### Write Path (authenticated only)
1. User drags a card to a new column → GraphQL `updateProjectV2ItemFieldValue` mutation
2. User adds a comment → REST `POST /repos/.../issues/.../comments`
3. User assigns someone → REST `PATCH /repos/.../issues/...`
4. After each mutation, dashboard refreshes from live API

## Troubleshooting

**Dashboard shows no tasks:**
- Verify `GH_ORG` and `GH_PROJECT_NUMBER` are set correctly
- Check the Actions tab for workflow failures
- Ensure `GITHUB_TOKEN` has read access to the project

**OAuth login fails:**
- Verify the callback URL matches exactly (including trailing slash)
- Check the Cloudflare Worker logs for errors
- Ensure OAuth App scopes include `repo` and `project`

**Status changes don't work:**
- Ensure your GitHub Project has matching status field options
- Check browser console for GraphQL errors
- Verify your token has `project` scope
