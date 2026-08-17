# Set up YouTube uploads

Momento does not ship a Google OAuth client identity. To upload from Momento,
create a Desktop OAuth client in a Google Cloud project that you control, then
import its JSON file in **Settings > YouTube**.

Momento uses these permissions:

- `https://www.googleapis.com/auth/youtube.upload` to upload the video and
  metadata you confirm in the upload dialog.
- `https://www.googleapis.com/auth/youtube.readonly` to read the connected
  channel's name, ID, and public avatar.

## Before you start

You need a Google account with a YouTube channel. Keep the downloaded OAuth
JSON private. Do not commit it, attach it to an issue, or send it with logs.

## 1. Create a Google Cloud project

1. Open the [Google Cloud console](https://console.cloud.google.com/).
2. Use the project selector to create a project or select one you control.
3. Give it a name that you will recognise during Google sign-in.

## 2. Enable YouTube Data API v3

1. Open **APIs & Services > Library** in your project.
2. Find **YouTube Data API v3**.
3. Select **Enable**.

Google's [YouTube API getting-started guide](https://developers.google.com/youtube/v3/getting-started)
describes projects, API enablement, credentials, and quota.

## 3. Configure Google Auth Platform

Open **Google Auth Platform** for the same project:

1. Under **Branding**, enter the app name and the support details Google
   requests. These details appear on Google's consent screen.
2. Under **Audience**, choose **External**. If the publishing status is
   **Testing**, add the Google account that will connect to Momento as a test
   user.
3. Under **Data Access**, add both Momento scopes shown at the top of this
   guide.

Google expires refresh tokens for an External app in Testing after seven days
when it requests scopes beyond basic profile information. You will need to
connect again after the token expires. Moving the app to Production removes
that Testing-mode limit, but Google may require OAuth verification.

See Google's current [OAuth consent configuration](https://support.google.com/cloud/answer/15549945?hl=en)
and [audience and test-user guidance](https://support.google.com/cloud/answer/15549257?hl=en).

## 4. Create a Desktop OAuth client

1. Open **Google Auth Platform > Clients**.
2. Select **Create client**.
3. Choose **Desktop app** as the application type.
4. Create the client and download its JSON file.

Momento rejects Web application credentials and OAuth files with non-Google
endpoints or non-local redirect addresses. Google's
[installed-app OAuth guide](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
explains the Desktop flow.

## 5. Import the JSON in Momento

1. Open **Settings > YouTube**.
2. Select **Import OAuth JSON...** and choose the file from Google.
3. Wait for Momento to report that the Google project is ready.
4. Delete the downloaded JSON if you no longer need it for another local
   installation.

Momento validates and normalises the file, encrypts the stored copy with
Windows Data Protection API (DPAPI), and writes it to
`%APPDATA%\Momento\youtube_oauth_client.dat`. Windows binds that encrypted
copy to your Windows account. Momento does not add the file to recordings,
logs, source archives, installers, or release assets.

## 6. Connect your YouTube account

1. Select **Connect YouTube account...**.
2. Review the project name and requested permissions on Google's page.
3. Continue only if they match the project and scopes you configured.
4. Return to Momento after the browser confirms the connection.

Momento stores the resulting refresh token in
`%APPDATA%\Momento\youtube_token.dat`, encrypted with DPAPI. It caches the
channel name and ID in `config.json` and the public channel avatar in
`youtube_avatar.png`.

## Upload visibility and quota

Google restricts videos uploaded through unverified API projects created after
28 July 2020 to private visibility. Google keeps that restriction until the
project passes its API audit. Selecting Public or Unlisted in Momento cannot
override it. See the restriction in Google's
[`videos.insert` documentation](https://developers.google.com/youtube/v3/docs/videos/insert).

Google currently gives each project a separate daily allowance for video
uploads. Check **APIs & Services > Quotas** in your own project for the current
limit if Google reports that the upload quota has run out.

## Disconnect, revoke, or remove setup

- **Disconnect the account:** Open **Settings > YouTube > Disconnect**. Momento
  deletes the local token, channel avatar, and cached channel details.
- **Remove the imported client:** Select **Remove OAuth setup** on the same
  page. Momento deletes `youtube_oauth_client.dat` and clears the local YouTube
  connection.
- **Revoke Google access:** Open your
  [Google Account permissions](https://myaccount.google.com/permissions), find
  the app name you chose, and remove access. This invalidates tokens that may
  still exist on a device.

Disconnecting or removing setup affects files on the current Windows account.
Google-side revocation applies to the connected Google account.

## Troubleshooting

- **Access blocked or user not authorised:** Add the connecting Google account
  under **Audience > Test users** while the app remains in Testing.
- **Redirect URI mismatch:** Download a Desktop app client. Momento does not
  support Web application OAuth clients.
- **Connection stops working after seven days:** Reconnect, or review the
  project's publishing status and verification requirements.
- **The upload is private:** This is Google's restriction for an unaudited API
  project. Momento cannot change the video to Public or Unlisted until Google
  lifts the restriction.
- **Quota exceeded:** Review the YouTube Data API v3 quota for your project.
