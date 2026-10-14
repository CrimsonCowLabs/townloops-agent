# Running your town on a VPS — plain-English quickstart

This guide walks you through running a TownLoops agent on a rented server
(a "VPS" — Virtual Private Server). Once it's set up, the agent finds your
town's events on the web, works out their venues, hosts and performers, and
keeps them live on TownLoops — plus a weekly newsletter draft if you want one.

You do not need to be a developer to follow this. You do need to be
comfortable copy-pasting commands into a terminal.

---

## Table of contents

1. [What this is, in plain terms](#what-this-is-in-plain-terms)
2. [What you'll need before you start](#what-youll-need-before-you-start)
3. [What it costs to run](#what-it-costs-to-run)
4. [Step 1 — Get a VPS](#step-1--get-a-vps)
5. [Step 2 — Get your keys and tokens](#step-2--get-your-keys-and-tokens)
6. [Step 3 — Install the software on the VPS](#step-3--install-the-software-on-the-vps)
7. [Step 4 — Start the shared background services](#step-4--start-the-shared-background-services)
8. [Step 5 — Add your first town](#step-5--add-your-first-town)
9. [Step 6 — Run preflight, then start the agent](#step-6--run-preflight-then-start-the-agent)
10. [Step 7 — Watch it work](#step-7--watch-it-work)
11. [Adding more towns](#adding-more-towns)
12. [Everyday operations](#everyday-operations)
13. [Troubleshooting](#troubleshooting)
14. [Updating to newer versions](#updating-to-newer-versions)
15. [Uninstalling](#uninstalling)

---

## What this is, in plain terms

- The **agent** is a small program that runs 24/7 on your server.
- Every week it looks for the web pages that list your town's events
  ("seeds"). Twice a day it reads those pages and pulls out the events.
- Every 10 minutes it works through a queue, one event at a time: reads the
  event's own page, works out the venue, the host and any performers,
  fills in what it can, and ships the event to your TownLoops town.
- Each town runs in one of two modes, set in `town-profile.yaml`:
  - **supervised** (the default) — you approve events and sources in a
    small web dashboard before anything ships.
  - **autonomous** — the agent makes those calls itself and submits
    everything as *pending*; the TownLoops admin moderation queue is the
    only review step, and there is no dashboard.
- If you set up Telegram, you get a nightly digest and a message when
  something breaks. Otherwise it stays quiet.

**One agent handles one town.** If you run TownLoops for three towns,
you'll run three agents on the same server — they share the scraper and the
firewall but each has its own state.

---

## What you'll need before you start

1. A **credit card or PayPal** (for the VPS).
2. A **TownLoops account** with at least one town created, and an API
   key ("bearer token") for that town. If you don't have one, contact
   whoever set up your TownLoops account. For an **autonomous** town the key
   must be an `api_ingest` key **without** publish permission.
3. A **ChatGPT account**. The agent's AI steps run through the OpenAI
   Codex CLI, signed in as you — there is no separate AI API key to buy.
4. **Optional but recommended**:
   - A Telegram account and a bot. See
     [Setting up Telegram alerts](#telegram-alerts-optional) below.
   - A Listmonk newsletter setup, if you want the agent to draft your
     weekly emails.

You do NOT need:
- Programming experience.
- An Anthropic, OpenAI API or Firecrawl account. The scraper runs on your
  own server, and the AI uses your ChatGPT sign-in.
- To install anything on your own computer beyond an SSH client (Terminal
  on Mac, PowerShell on Windows, or the terminal on Linux).

---

## What it costs to run

Rough monthly estimates (in USD, as of 2026):

| Item | Cost |
|---|---|
| VPS (Hetzner CX22, DigitalOcean $12 droplet, or equivalent) | $5–12 |
| ChatGPT (the plan you already have — the agent uses your sign-in) | $0 extra |
| Firecrawl (self-hosted on your VPS — included) | $0 |
| Domain names (optional) | $0–15 |
| **Per town, running total** | **$5–27/month** |

Costs scale with **how many towns you run** on one VPS (add a bit more RAM
per town). Heavy use can hit your ChatGPT plan's usage limits; when it does,
the AI steps pause and retry later — nothing breaks.

---

## Step 1 — Get a VPS

**Recommended minimum**:
- 4 GB RAM
- 2 CPU cores
- 40 GB SSD
- Ubuntu 22.04 or 24.04 LTS

**Providers that work well**:
- **Hetzner** (cheapest, EU-based): pick "CX22" (~€4.50/mo)
- **DigitalOcean**: "Basic Regular" 2 CPU / 4 GB (~$24/mo)
- **Vultr / Linode / OVH / Contabo**: any 4GB / 2 CPU image
- **Any provider that gives you root SSH access to Ubuntu**

**Not recommended**:
- Shared hosting (cPanel, Bluehost, etc.) — no Docker support.
- The tiniest 1–2 GB tiers — the bundled scraper uses ~2 GB by itself.

After buying, you'll be given:
- An **IP address** (like `95.216.40.153`)
- **SSH credentials** (usually a root password or an SSH key you uploaded)

**Log in from your own computer's terminal**:

```
ssh root@<your-vps-ip>
```

The rest of this guide assumes you're logged in as `root` (or a user with
`sudo`).

---

## Step 2 — Get your keys and tokens

Gather all of these into a text file on your own computer — you'll paste
them into the server in a later step. **Do not share this file.**

### TownLoops API key + town slug

- **Town slug** — the short name for your town on TownLoops, e.g. `slt`
  for South Lake Tahoe. Should be lowercase, letters + digits + hyphens.
- **API key** — the "bearer token" issued for that town. Contact your
  TownLoops administrator if you don't have one. For an autonomous town, ask
  for an `api_ingest` key that **cannot publish** — with a publishing key
  the agent stops all submissions after its first one (preflight reports
  this as `circuit-open`).

### ChatGPT sign-in (no key to copy)

Nothing to collect here — you sign in on the server in
[Step 3c](#3c-sign-in-to-chatgpt-for-the-codex-cli). Just make sure you
know your ChatGPT login.

### Telegram alerts (optional)

If you want a nightly digest and a ping when something breaks:

1. In Telegram, message `@BotFather`. Send `/newbot`. Follow the prompts.
   BotFather gives you a **bot token** (looks like `123456:ABC-DEF...`).
2. In Telegram, start a chat with your new bot and send it any message
   (this is required before it can message you back).
3. Get your **chat ID**: message `@userinfobot` and it'll reply with a
   number. That's your chat ID.

For an **autonomous** town Telegram is your only push alert — without it,
you learn that submissions stopped only by running preflight.

### Listmonk credentials (optional — newsletter only)

Only if you want the agent to draft your weekly newsletter. You'll need
Listmonk running somewhere reachable from the VPS, and its admin user +
API token. Skip this section otherwise.

---

## Step 3 — Install the software on the VPS

**One-time setup** — copy-paste each of these blocks into your SSH
session on the VPS.

### 3a. Install Docker

```
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker --version
docker compose version
```

Both `docker --version` and `docker compose version` should print
numbers. If they do, Docker is installed.

### 3b. Download the townloops-agent code

```
mkdir -p ~/workspace
cd ~/workspace
git clone https://github.com/YOUR-ORG/townloops-agent.git
cd townloops-agent
docker compose build agent
```

(Replace `YOUR-ORG` with the actual GitHub organization name your
TownLoops administrator gave you. If you're using a private repo, you'll
need a deploy key or personal access token.)

### 3c. Sign in to ChatGPT for the Codex CLI

The agent's AI steps (finding events on a page, judging them) call the
OpenAI Codex CLI, signed in with **your ChatGPT account**. You sign in once
on the server; every town's containers mount that login (`~/.codex`) so it
can refresh itself.

```
apt-get install -y nodejs npm
npm install -g @openai/codex
codex login
```

Follow the prompts. When it asks you to open a link, open it on your own
computer and sign in to ChatGPT. Afterwards `~/.codex/auth.json` exists —
that file is what preflight checks for.

---

## Step 4 — Start the shared background services

**This guide uses one setup: one folder per town, sharing one scraper and
one firewall.** The agent talks to two supporting services that every town
on the server shares:

1. **Firecrawl** — a self-hosted web scraper the agent uses to read event
   pages. It runs on your server; cloud Firecrawl is never used.
2. **Squid egress proxy** — a firewall-like guard that only lets the
   agent connect to an allowlist of websites (TownLoops, ChatGPT,
   Telegram, Firecrawl, and the sites each town lists in its
   `town-profile.yaml` → `allowed_hosts`).

Start them once, and every town you add later reuses them.

```
cd ~/workspace/townloops-agent

# Firecrawl (5 containers — takes about a minute the first time to download)
docker compose -f templates/_shared/firecrawl/docker-compose.yml up -d

# Squid egress
docker compose -f templates/_shared/egress/docker-compose.yml up -d
```

**Check they're all healthy**:

```
docker ps
```

You should see about 6 containers running: `firecrawl-api`,
`firecrawl-playwright`, `firecrawl-redis`, `firecrawl-rabbitmq`,
`firecrawl-postgres`, and `squid-egress`.

If any say "Restarting" or "Exited," see [Troubleshooting](#troubleshooting).

> The repo's top-level `docker-compose.yml` (README → "Deploy") is a
> different, single-town developer setup with Firecrawl bundled into the
> same project. Don't mix the two — on a VPS, follow this guide.

---

## Step 5 — Add your first town

The `bin/townloops-agent-add` command creates a folder for your town
with all the config files pre-filled.

```
cd ~/workspace/townloops-agent
./bin/townloops-agent-add YOUR-TOWN-SLUG                # supervised (dashboard)
./bin/townloops-agent-add YOUR-TOWN-SLUG --autonomous   # or: autonomous (no dashboard)
```

Replace `YOUR-TOWN-SLUG` with your actual town's slug (like `slt` or
`bozeman`). The command will:
- Create a folder at `~/townloops-agents/YOUR-TOWN-SLUG/`
- Copy in the docker-compose file, `.env`, `town-profile.yaml`, and the
  repo's cron schedule
- For `--autonomous`, also write a `docker-compose.override.yml` that keeps
  the dashboard from starting

It ends by printing the remaining steps, including the one-time ChatGPT
sign-in for this town's own Hermes runtime (`hermes auth add openai-codex`)
— do that one too.

Later, to pick up a newer compose template for an existing town, run
`./bin/townloops-agent-add YOUR-TOWN-SLUG --rerender`. Don't re-render by hand.
The command first checks that the town's `hermes-home` folder and its logins are
in place, and it refuses to re-render if they aren't. It keeps the old file as a
backup and shows you the diff. Put host-specific edits in the town's
`docker-compose.override.yml`.

### Fill in the `.env` file

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
nano .env
```

Fill in these fields (leave the rest alone unless you know what they do):

```
TOWNLOOPS_API_KEY=<paste the bearer token you got from TownLoops>
AGENT_TZ=<your town's time zone, e.g. America/Denver — see the list below>
TELEGRAM_BOT_TOKEN=<paste the token BotFather gave you, or leave empty>
TELEGRAM_CHAT_ID=<paste the number from @userinfobot, or leave empty>
LISTMONK_USER=<leave empty if not using Listmonk>
LISTMONK_TOKEN=<leave empty if not using Listmonk>
DASHBOARD_PASSWORD=<supervised towns only — see below>
API_SERVER_KEY=<run: openssl rand -hex 32>
```

- **`AGENT_TZ`** must be a real zone name from
  https://en.wikipedia.org/wiki/List_of_tz_database_time_zones (the "TZ
  identifier" column). Every event date is computed in it.
- **`DASHBOARD_PASSWORD`** (supervised towns) is a bcrypt hash, not the
  password itself: run `~/workspace/townloops-agent/bin/dashboard-hash-password`,
  type a password, and paste the `$2b$...` line it prints — writing each `$`
  as `$$`. Autonomous towns leave it empty.

There is no AI key to paste — the ChatGPT sign-in from Step 3c covers it.

Save (Ctrl+O, Enter, Ctrl+X in nano).

### Edit the `town-profile.yaml` file

This tells the agent what your town looks like, where to search, and
which websites it's allowed to visit.

```
nano town-profile.yaml
```

You need to change these:

- **`name`** — the town's real display name (e.g. `"South Lake Tahoe"`).
- **`region`** — the state or province (e.g. `"California"`).
- **`allowed_hosts`** — websites the agent may reach through the egress
  firewall. Start with your local chamber of commerce and any visitor
  bureau site; add each new event source's website as you go. Every entry
  is a website the agent trusts. After changing it, regenerate the shared
  firewall config (see
  [Squid keeps blocking legitimate websites](#squid-keeps-blocking-legitimate-websites)).
- **`discovery.business.category_query_terms`** — the kinds of businesses
  in your town (`"restaurants"`, `"coffee shops"`, `"boutiques"`, etc.).
- **`discovery.business.seed_directories`** — pages the agent scrapes to
  find businesses (the chamber's directory, a "things to do" page).
- **`discovery.community.query_groups`** — how to find nonprofits, city
  government, and community groups. Keep the defaults if unsure; each
  `category_slug` must be one of your town's TownLoops categories, and
  preflight tells you if one isn't.

Save and exit.

---

## Step 6 — Run preflight, then start the agent

**Preflight** is one read-only command that says whether your town will
work. It changes nothing, so you can run it as often as you like — and it
is the first thing to run whenever something seems wrong.

The first time, run `setup` — it saves your town's category list,
prepares the agent's database, then runs preflight:

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose run --rm agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py setup
```

After that, any time:

```
docker compose run --rm agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py preflight
```

(Once the agent is running, `docker compose exec agent …` instead of
`docker compose run --rm agent …` does the same without starting a second
container. Add `--json` for machine-readable output.)

You get a list of findings and a verdict:

- **ERROR** — the agent cannot work until you fix it. Each one names the
  file and the setting (for example `.env: TOWNLOOPS_API_KEY` or
  `town-profile.yaml: discovery.community.query_groups[0].category_slug`)
  and says what goes wrong if you leave it.
- **WARN** — the agent works with a piece missing (no newsletter, no
  Telegram). Fine while you're getting started.
- **INFO** — something preflight can't check for you. `name` and `region`
  are on your honour: TownLoops has no way to confirm them.

The command exits `0` when there are no errors and non-zero when there are
any, so you can use it in a script. A few you may see:

| Code | What to do |
|---|---|
| `credential-rejected`, `town-scope` | Wrong key for this town, or a mistyped `TOWNLOOPS_TOWN_SLUG` — get the right key |
| `category-unknown` | Fix that `category_slug` in `town-profile.yaml` — the finding lists the valid ones |
| `category-snapshot-stale` | Your profile is right; re-run `setup` to refresh the category list |
| `dashboard-password-missing` | Supervised town without `DASHBOARD_PASSWORD` — see above |
| `dashboard-enabled-autonomous` | Autonomous town whose `docker-compose.override.yml` doesn't park the dashboard |
| `firecrawl-unreachable` | Start the shared Firecrawl stack (Step 4) |
| `codex-login-missing` | Run `codex login` on the server (Step 3c) |
| `tz-invalid` / `tz-fallback` | Set `AGENT_TZ` to your town's zone |
| `circuit-open` | Submissions are stopped — the finding says why; fix it, then run `autonomy resume` |

Preflight runs inside the agent container, which can't see the town
folder's `docker-compose.override.yml`; for an autonomous town it says so
(`dashboard-unverified`) unless you give it the folder — add
`-v "$PWD:/town:ro" -e TOWNLOOPS_TOWN_DIR=/town` after `run --rm`.

When preflight says **OK**, start the agent:

```
docker compose up -d
```

---

## Step 7 — Watch it work

### See what's happening right now

```
docker compose logs -f agent
```

Press Ctrl+C to stop watching (the agent keeps running).

### See how far your events have got

```
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/operator-status/status.py
```

You'll see counts of events by stage (pending / approved / shipped), your
event sources ("seeds") by status, and how many items are waiting in the
work queue.

**Typical timeline for a new town:**

| When | What's happening |
|---|---|
| First Sunday (or run it by hand — see below) | Seed discovery proposes the web pages that list your town's events |
| After seeds are approved (by you in supervised mode, by the agent in autonomous) | Event finding runs at 6am and 6pm and pulls in events |
| Every 10 minutes | The work queue reads each event's page, works out venue/host/people, and ships it |
| Nightly 9pm | A digest of the day (dashboard report, plus Telegram if configured) |
| Sundays | A newsletter draft — never sent until you push it |

Supervised towns: open the dashboard (`http://127.0.0.1:8090`, through an
SSH tunnel or your TLS proxy) to approve seeds and events.

### Telegram messages you'll get

Only sent if you configured Telegram in Step 2:
- The nightly digest.
- An alert when an event source suddenly goes quiet.
- An alert when an autonomous town stops submitting (the circuit opened).
- An alert when the TownLoops API changes in a way that freezes writes.
- Newsletter push reminders on Sundays.

Silence = things are working. If in doubt, run preflight.

---

## Adding more towns

Repeat Step 5 and Step 6 for each additional town, using a different slug
each time. The shared Firecrawl and Squid stacks from Step 4 stay as-is.

```
cd ~/workspace/townloops-agent
./bin/townloops-agent-add second-town-slug
cd ~/townloops-agents/second-town-slug
nano .env               # that town's TOWNLOOPS_API_KEY, AGENT_TZ, etc.
nano town-profile.yaml  # that town's name, region, allowed_hosts, seeds
docker compose run --rm agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py setup
docker compose up -d
```

Each town runs in its own container with its own state. They don't share
data. You can add or remove towns without affecting others.

---

## Everyday operations

All commands below run from the town's folder
(`cd ~/townloops-agents/YOUR-TOWN-SLUG`).

### Is everything OK?

```
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py preflight
```

### See all running towns

```
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

You'll see one `townloops-agent-<slug>` line per town, plus the Firecrawl
+ Squid containers.

### Pause a town

```
docker compose stop
```

Resume with `docker compose start`.

### Restart a town after changing `.env` or `town-profile.yaml`

```
docker compose restart agent
```

### Manually run a step right now (instead of waiting for cron)

```
# Look for event sources (seeds)
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/scripts/event_seed_discovery.py

# Read the approved seeds and pull in events
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/scripts/event_find.py

# Work through the queue once, in the foreground
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/scripts/workqueue_heartbeat.py --drain
```

### Autonomous towns: why did the agent do that?

```
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/autonomy/autonomy.py status
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/autonomy/autonomy.py why "Jazz Night"
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/autonomy/autonomy.py resume   # after fixing an open circuit
```

### Check when the TownLoops API last changed

```
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/townloops-api/api.py check
```

If a breaking change is detected, the agent will freeze the affected
resource for write operations and Telegram-alert you. You clear a freeze
by removing the file:

```
docker compose exec agent rm /home/agent/.hermes/state/api_freeze.json
```

Do this only after you've confirmed your version of the agent has been
updated to match the new API shape (usually `git pull` — see below).

---

## Troubleshooting

### Start here: run preflight

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py preflight
```

(If the agent isn't running, use `docker compose run --rm agent` in place of
`docker compose exec agent`.) Fix every ERROR it lists — each names the file
and setting — then restart the agent. Most problems below are things
preflight already reports.

### The agent says "authentication failed"

Preflight shows `credential-rejected` or `town-scope`. The
`TOWNLOOPS_API_KEY` in `.env` is wrong for this town's slug, or the key was
rotated. Get a fresh key from your TownLoops administrator, edit `.env`,
then `docker compose restart agent`.

### The agent starts but never finds anything

Preflight shows `firecrawl-unreachable` or `firecrawl-not-configured` when
the scraper is the cause. Check that Firecrawl is running:

```
docker ps | grep firecrawl
```

You should see 5 containers. If not, restart the Firecrawl stack:

```
cd ~/workspace/townloops-agent
docker compose -f templates/_shared/firecrawl/docker-compose.yml up -d
```

If preflight is clean, check that the town has approved event sources —
`operator-status` (Step 7) shows seed counts. A supervised town needs you
to approve seeds in the dashboard.

### Submissions stopped (autonomous town)

Preflight shows `circuit-open` with the reason. The most common is
`published-without-review`: the API key can publish, so the agent stopped
itself. Get an `api_ingest` key without publish permission, put it in
`.env`, restart the agent, then run `autonomy resume` (see Everyday
operations). The other reason, `rejection-rate`, means TownLoops moderators
rejected too much of what it sent — look at what was rejected before
resuming.

### Squid keeps blocking legitimate websites

The allowlist comes from each town's `town-profile.yaml` →
`allowed_hosts`. Add the website there (just the host, like
`visitexample.com` — no `https://` and no path), regenerate the shared
`squid.conf` from every town's profile with `bin/townloops-agent-add` (see
its `--help`), then restart Squid:

```
docker compose -f ~/workspace/townloops-agent/templates/_shared/egress/docker-compose.yml restart squid
```

Squid restarts don't affect running agents; egress just becomes allowed
on the next connection.

### The Telegram messages stopped

Bot tokens can be revoked, and the chat between you and the bot can go
stale. Test manually:

```
curl -s "https://api.telegram.org/bot<YOUR-BOT-TOKEN>/sendMessage" \
  -d "chat_id=<YOUR-CHAT-ID>" -d "text=test"
```

If that fails, generate a new bot with `@BotFather`, update `.env`, and
restart the agent.

### The AI steps don't seem to do anything

Preflight shows `codex-login-missing` if the ChatGPT sign-in is gone — run
`codex login` on the server again. If the login is there, you may simply
have hit your ChatGPT plan's usage limit; the agent skips the AI step and
retries on the next run, so give it a few hours.

### The VPS is running out of disk

Check with `df -h`. Common cleanups:

```
# Docker cleanup (safe — removes stopped containers + unused images)
docker system prune -a --volumes

# Rotate agent logs (they're in the container, not the host)
docker compose -f ~/townloops-agents/*/docker-compose.yml logs --tail 0
```

If you're still tight, upgrade the VPS to more disk (usually a paid
option in your provider's dashboard).

### "Permission denied" errors

Docker commands require either running as root or being in the `docker`
group. If you're not root:

```
sudo usermod -aG docker $USER
# Log out and back in
```

---

## Updating to newer versions

New features and bug fixes land in the `townloops-agent` repo over time.
To pull them:

```
cd ~/workspace/townloops-agent
git pull
docker compose build agent

# Restart every town so it picks up the new engine/skills code, then check it.
for town in ~/townloops-agents/*/; do
  cd "$town"
  docker compose up -d
  docker compose exec agent /usr/bin/python3 /home/agent/.hermes/skills/business/town-init/init.py preflight
done
```

The shared Firecrawl and Squid stacks rarely change — you don't need to
touch them unless the release notes call it out.

### Watching for TownLoops API updates

The agent already does this automatically via the daily `api-check`
cron. If TownLoops adds new endpoints or fields, the snapshot at
`state/openapi.json` updates silently. If TownLoops removes something
your version of the agent depends on, you get a Telegram alert with the
affected resources — pull the latest agent code before clearing the
freeze.

---

## Uninstalling

To completely remove one town but keep others:

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose down
rm -rf ~/townloops-agents/YOUR-TOWN-SLUG
```

**Note**: this does NOT delete anything on TownLoops itself — only the
local state on your VPS.

To tear down the entire installation:

```
# Stop every town
for town in ~/townloops-agents/*/; do
  cd "$town" && docker compose down
done

# Stop shared services
docker compose -f ~/workspace/townloops-agent/templates/_shared/firecrawl/docker-compose.yml down --volumes
docker compose -f ~/workspace/townloops-agent/templates/_shared/egress/docker-compose.yml down

# Remove all files
rm -rf ~/townloops-agents ~/workspace/townloops-agent

# Remove Docker if you're done with it entirely
apt-get remove -y docker-ce docker-ce-cli containerd.io
```

---

## Getting help

If you're stuck:

1. **Run preflight** — `town-init/init.py preflight` (see Troubleshooting).
   It names the file and setting behind most failures.
2. **Read the agent's logs** — `docker compose logs -f agent`.
3. **Check the pipeline** — `operator-status/status.py` shows how many
   events are at each stage and how deep the work queue is.
4. **Ask your TownLoops administrator** — they can check whether the
   agent is actually posting to TownLoops, and whether your bearer token
   is valid.
5. **File an issue** on the townloops-agent repo with:
   - What you were trying to do
   - The exact command you ran
   - The output of `preflight --json`
   - The last ~20 lines of `docker compose logs agent`
   - Your VPS provider + Ubuntu version

The agent is designed to be quiet when things work and loud when they
don't. If preflight is clean and Telegram is silent, you're fine — the work
queue drains every 10 minutes even when it looks like nothing is happening.
