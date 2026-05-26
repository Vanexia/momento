# resources/youtube/

Drop the OAuth `client_secrets.json` downloaded from Google Cloud Console
here (Desktop application type). Filename must be exactly
**`client_secrets.json`**.

This file is gitignored — it identifies *your* Momento build to Google's
OAuth servers and shouldn't be shared via the repository. PyInstaller
bundles whatever's in this folder into the frozen build.

To produce one:

1. https://console.cloud.google.com/ → create a project ("Momento")
2. APIs & Services → Library → enable **YouTube Data API v3**
3. APIs & Services → OAuth consent screen → External → fill app name,
   support email, developer email. Add scopes
   `https://www.googleapis.com/auth/youtube.upload` and
   `https://www.googleapis.com/auth/youtube.readonly`.
   Add yourself (and any test users) under **Test users**.
4. APIs & Services → Credentials → Create Credentials → OAuth client ID
   → **Desktop app** → download JSON → save here as `client_secrets.json`.
