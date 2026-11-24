# ToonFlix File-to-Link Bot

Telegram bot that receives a video, uploads it to Firebase Storage, writes metadata to Firebase Realtime Database, and returns an MP4-compatible streaming URL.

## Important notes

- This project uses Telethon's MTProto connection with a bot token so it can download large Telegram media beyond the standard Bot API `getFile` 20 MB download limit. Set `API_ID` and `API_HASH` from https://my.telegram.org.
- The Firebase Storage download URL supports HTTP range requests in normal browser/video-player use. Test it in your target Android player and website.
- Firebase Storage download-token URLs are bearer links: anyone who has the URL can access that video. Do not publish private videos unintentionally.
- Configure Firebase Storage billing/plan and security rules appropriately. The service account is highly privileged; never commit it to GitHub or include it in the ZIP.
- Railway ephemeral disk is used only temporarily during upload. Permanent video files are stored in Firebase Storage.
- Upload only content you own or are authorized to distribute.

## 1. Firebase setup

1. Enable Realtime Database and Firebase Storage in your Firebase project.
2. In Firebase Console > Project settings > Service accounts, generate a private service-account JSON key. The Firebase Web SDK config you supplied is not this service-account key; the bot needs the private service-account JSON to upload files securely.
3. This project is preconfigured with your supplied Firebase Database URL (`https://toonflix-4911e-default-rtdb.firebaseio.com/`) and Storage bucket (`toonflix-4911e.firebasestorage.app`). Verify that the Storage bucket exists and is enabled in your Firebase project.
4. For a production project, restrict who can access the database and Storage. The bot uses Firebase Admin credentials on the server.

## 2. Telegram setup

1. Create a bot using `@BotFather` and copy its token.
2. Sign in at https://my.telegram.org and create an API application.
3. Copy `api_id` and `api_hash`.
4. Find your numeric Telegram user ID and set it in `OWNER_IDS`.

## 3. Deploy to Railway

1. Upload this folder to a private GitHub repository.
2. Create a Railway project from that repository.
3. Add all variables from `.env.example` in Railway Variables.
4. Set `PUBLIC_BASE_URL` to the Railway public domain if you want branded `/stream/<id>` links.
5. Deploy. Confirm `/health` returns `{"ok":true}`.

Paste service-account JSON into `FIREBASE_SERVICE_ACCOUNT_JSON` as one line. Alternatively, base64-encode the JSON locally and paste the result.

## 4. Firebase database structure

Records are stored at:

`toonflixVideos/{fileId}`

Fields include `fileName`, `storagePath`, `sizeBytes`, `mimeType`, `durationSeconds`, `width`, `height`, `streamUrl`, `createdAt`, and `status`.

## 5. Use

Open the bot in Telegram and send an MP4 video or video document. The bot uploads it and replies with the streaming URL.

## 6. App / website integration

Use the returned URL as the video source in a player that supports MP4 and HTTP byte-range requests. The `/stream/{fileId}` endpoint redirects to the Firebase Storage download URL.

## Troubleshooting

- `API_ID` / `API_HASH` errors: verify values from my.telegram.org.
- Firebase permission errors: verify service account JSON, database URL, and bucket name.
- Storage bucket errors: copy the exact bucket identifier shown in Firebase Console; do not guess between `.appspot.com` and `.firebasestorage.app`.
- Large upload failures: check Railway request/runtime limits, available temporary disk, and Firebase Storage quotas.

## Firebase Web config

The supplied Firebase Web SDK values are available as environment variables in `.env.example`. The optional `GET /firebase-config` endpoint returns those public client-side values for a website. It does not expose the Firebase service-account key. Firebase Web config is not a substitute for database/storage security rules.
