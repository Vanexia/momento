# docs/ — Momento landing page and privacy policy

These two HTML files are the materials Google requires for OAuth verification.

| File | Purpose | Required URL field in Cloud Console |
|---|---|---|
| `index.html` | Application homepage describing what Momento is | "Application home page" |
| `privacy.html` | Privacy policy describing data handling | "Application privacy policy link" |

## Hosting on GitHub Pages

The fastest hosting path (free, no signup beyond GitHub, ~5 min):

1. Push the `momento` repo to GitHub (public). If it's already public, skip to step 2.
2. On GitHub: **Settings → Pages**.
3. Under "Build and deployment" → **Source**: "Deploy from a branch."
4. **Branch**: `main` (or whichever is your default), **Folder**: `/docs`.
5. Click Save.

Pages will build within ~30 seconds. URLs will be:

- Landing: `https://<your-username>.github.io/<repo-name>/`
- Privacy: `https://<your-username>.github.io/<repo-name>/privacy.html`

Plug those URLs into the OAuth consent screen in Google Cloud Console
(Google Auth Platform → Branding):

- "Application home page" ← landing URL
- "Application privacy policy link" ← privacy URL
- "Application terms of service link" ← landing URL is fine; we're not
  a commercial service so a separate ToS isn't required.

## Editing

Both pages are single-file static HTML — no Jekyll, no build step, no
dependencies. Edit and push; GH Pages rebuilds in under a minute.

The colour palette matches Momento's violet brand (`--accent: #8b5cf6`).
If you swap the in-app brand colour later (see `momento/ui/theme.py`),
keep these in sync.

## Custom domain (optional, later)

If you want a custom domain like `momento-app.com`:
1. Buy domain.
2. Add a `CNAME` file in `docs/` containing just your domain.
3. Point your DNS at GitHub Pages per their docs.
4. Update the privacy policy's "Contact" link if you switch the support
   email.
