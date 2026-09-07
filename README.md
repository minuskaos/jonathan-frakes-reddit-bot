# Jonathan Frakes Reddit Bot for Unraid

A Reddit watcher with a local web dashboard.

## Dashboard

Open `http://YOUR-UNRAID-IP:8787`.

From the UI you can edit:

- Bot on/off and dry-run mode
- Multiple search keywords, one per line, and match mode
- Subreddit scope
- Monitor comments, posts, or both
- The single global Context URL
- Context link label
- Reply prefix/suffix
- Reply probability
- Recent-question repeat avoidance
- One-reply-per-thread protection
- Hourly and daily reply limits
- Minimum time between replies
- Subreddit and username blacklists
- The complete question list, one question per line

The dashboard also shows counters, per-keyword match counts, recent matches/replies/skips, connection state, and a live log.

## Persistent data

Everything editable is stored at:

`/mnt/user/appdata/frakes-reddit-bot/config.json`

State, counters and reply history are stored in the same appdata directory. Container rebuilds do not erase them.

## Install

1. Copy `.env.example` to `.env` and enter your Reddit credentials.
2. From this folder run `docker compose up -d --build`, or use Unraid Compose Manager.
3. Open port `8787` on your LAN.
4. Leave **Dry run** enabled until you are happy with matching and replies.

Do not expose the dashboard port directly to the public internet. You can optionally set `UI_USERNAME` and `UI_PASSWORD` in `.env` to require a dashboard login.

## Reply format

A question is selected from the configured pool and the same global URL is appended automatically:

```text
Was the sister's husband really the victim of amnesia?

[Context](https://www.youtube.com/watch?v=GxPSApAHakg)
```

Change the URL once in the dashboard and every reply uses the new value.

## Native Unraid install

The repository includes `unraid-template.xml` for a normal Unraid Docker entry with an icon, WebUI button, appdata mapping, editable port, and Reddit/dashboard environment variables.

The Docker image is built automatically by GitHub Actions and published as:

`ghcr.io/minuskaos/jonathan-frakes-reddit-bot:latest`

After the first successful GitHub Actions build, install the template into Unraid's user templates and add the container from **Docker -> Add Container -> Template**.

The template defaults the host WebUI port to `8788` while keeping the container's internal port at `8787`.
