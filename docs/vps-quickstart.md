# Running your town on a VPS — plain-English quickstart

This guide walks you through running a TownLoops agent on a rented server
(a "VPS" — Virtual Private Server). Once it's set up, the agent
autonomously discovers businesses, venues, events, nonprofits, and
municipal contacts for your town, then keeps that data live on TownLoops
without you doing anything.

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
9. [Step 6 — Watch it work](#step-6--watch-it-work)
10. [Adding more towns](#adding-more-towns)
11. [Everyday operations](#everyday-operations)
12. [Troubleshooting](#troubleshooting)
13. [Updating to newer versions](#updating-to-newer-versions)
14. [Uninstalling](#uninstalling)

---

## What this is, in plain terms

- The **agent** is a small program that runs 24/7 on your server.
- Every hour it wakes up and does one small unit of work — searching the
  web for local businesses, checking whether they're still open, filling
  in their address and phone, and posting them to your TownLoops town.
- After the first few days of that hourly loop, it settles into a slower
  daily rhythm: pulling in events every morning and evening, doing a
  weekly newsletter draft, and re-sweeping for new listings once a week.
- You get a Telegram message every time it moves through a stage or hits
  something it can't handle. Otherwise it stays quiet.

**One agent handles one town.** If you run TownLoops for three towns,
you'll run three agents on the same server — they share resources but
each has its own state.

---

## What you'll need before you start

1. A **credit card or PayPal** (for the VPS + Anthropic API).
2. A **TownLoops account** with at least one town created, and an API
   key ("bearer token") for that town. If you don't have one, contact
   whoever set up your TownLoops account.
3. An **Anthropic API key** — https://console.anthropic.com/ → sign up →
   "API Keys" → create one starting with `sk-ant-`.
4. **Optional but recommended**:
   - A Telegram account and a bot. See
     [Setting up Telegram alerts](#telegram-alerts-optional) below.
   - A Listmonk newsletter setup, if you want the agent to draft your
     weekly emails.

You do NOT need:
- Programming experience.
- To install anything on your own computer beyond an SSH client (Terminal
  on Mac, PowerShell on Windows, or the terminal on Linux).

---

## What it costs to run

Rough monthly estimates (in USD, as of 2026):

| Item | Cost |
|---|---|
| VPS (Hetzner CX22, DigitalOcean $12 droplet, or equivalent) | $5–12 |
| Anthropic API (light usage — a few hundred classifications a month) | $2–10 |
| Firecrawl (self-hosted on your VPS — included) | $0 |
| Domain names (optional) | $0–15 |
| **Per town, running total** | **$7–25/month** |

Costs scale with:
- **How many towns you run** on one VPS (add ~$3–8/mo per additional town for LLM + a bit more RAM).
- **How aggressively the agent discovers** (the daily POST caps in the
  onboarding ramp cap this — see the town-profile.yaml).

If you use **cloud Firecrawl** instead of the self-hosted stack, add
~$10–50/mo depending on volume.

---

## Step 1 — Get a VPS

**Recommended minimum**:
- 4 GB RAM (2 GB works for one town if you use cloud Firecrawl instead of self-hosted)
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
- The tiniest 1 GB tier — Firecrawl uses ~2 GB by itself.

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
  TownLoops administrator if you don't have one.

### Anthropic API key

- Visit https://console.anthropic.com/settings/keys
- "Create Key" → copy the string starting with `sk-ant-`
- Add ~$5 of credit to start (Settings → Billing → Add credits)

### Telegram alerts (optional)

If you want to get pinged when the agent finishes onboarding stages, or
when something breaks:

1. In Telegram, message `@BotFather`. Send `/newbot`. Follow the prompts.
   BotFather gives you a **bot token** (looks like `123456:ABC-DEF...`).
2. In Telegram, start a chat with your new bot and send it any message
   (this is required before it can message you back).
3. Get your **chat ID**: message `@userinfobot` and it'll reply with a
   number. That's your chat ID.

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
```

(Replace `YOUR-ORG` with the actual GitHub organization name your
TownLoops administrator gave you. If you're using a private repo, you'll
need a deploy key or personal access token.)

---

## Step 4 — Start the shared background services

The agent talks to two supporting services that are shared across all
your towns:

1. **Firecrawl** — an in-house web scraper the agent uses to research
   businesses.
2. **Squid egress proxy** — a firewall-like guard that only lets the
   agent connect to a small allowlist of websites (TownLoops, Anthropic,
   Telegram, Firecrawl, and whatever local sites you configure).

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

---

## Step 5 — Add your first town

The `bin/townloops-agent-add` command creates a folder for your town
with all the config files pre-filled.

```
cd ~/workspace/townloops-agent
./bin/townloops-agent-add YOUR-TOWN-SLUG
```

Replace `YOUR-TOWN-SLUG` with your actual town's slug (like `slt` or
`bozeman`). The command will:
- Create a folder at `~/townloops-agents/YOUR-TOWN-SLUG/`
- Copy in the docker-compose file, `.env`, `town-profile.yaml`, and cron schedule

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
ANTHROPIC_API_KEY=<paste the sk-ant-... key from console.anthropic.com>
TELEGRAM_BOT_TOKEN=<paste the token BotFather gave you, or leave empty>
TELEGRAM_CHAT_ID=<paste the number from @userinfobot, or leave empty>
LISTMONK_USER=<leave empty if not using Listmonk>
LISTMONK_TOKEN=<leave empty if not using Listmonk>
```

Save (Ctrl+O, Enter, Ctrl+X in nano).

### Edit the `town-profile.yaml` file

This tells the agent what your town looks like, where to search, and
which websites it's allowed to visit for research.

```
nano town-profile.yaml
```

You need to change these:

- **`name`** — the town's real display name (e.g. `"South Lake Tahoe"`).
- **`region`** — the state or province (e.g. `"California"`).
- **`allowed_hosts`** — websites the agent is allowed to visit. Start with
  your local chamber of commerce and any visitor bureau site. Add more
  later. Every entry is a website the agent trusts.
- **`discovery.business.category_query_terms`** — the kinds of businesses
  in your town (`"restaurants"`, `"coffee shops"`, `"boutiques"`, etc.).
- **`discovery.business.seed_directories`** — pages the agent scrapes to
  find businesses (the chamber's directory, a "things to do" page).
- **`discovery.community.query_groups`** — how to find nonprofits, city
  government, and community groups. Keep the defaults if unsure.

Save and exit.

### Start the agent

```
docker compose up -d
```

That's it. The agent is now running.

---

## Step 6 — Watch it work

### See what's happening right now

```
docker compose logs -f agent
```

Press Ctrl+C to stop watching (the agent keeps running).

### Check the agent's current phase

```
docker compose exec agent python /home/agent/.hermes/skills/business/onboarding-orchestrator/orchestrate.py status
```

You'll see JSON with:
- `phase` — one of `bootstrapping`, `discovering`, `verifying`,
  `researching`, `enriching`, `shipping`, `steady_state`.
- `tick_count` — how many hourly cycles the agent has run.
- `phase_ticks` — how many cycles in the current phase.

**Typical timeline for a new town** (rough — depends on town size + how
many candidates the web sweeps find):

| Time since start | Phase | What's happening |
|---|---|---|
| 0–1 hour | `bootstrapping` → `discovering` | Verifies credentials, snapshots the town's categories |
| Hours 1–24 | `discovering` | Searches Firecrawl for businesses + communities |
| Days 1–2 | `verifying` | Adversarial re-check that each candidate is real, in-town, still open |
| Days 2–3 | `researching` | Fills in categories for anything the keyword map couldn't classify |
| Days 3–5 | `enriching` | Fetches address, phone, website, socials, photos |
| Days 5–7 | `shipping` | Sends everything to TownLoops |
| Day 7+ | `steady_state` | Daily event ingest, weekly discovery re-sweep |

### Telegram messages you'll get

Only sent if you configured Telegram in Step 2. You'll get one line per:
- Phase transition (e.g. `[slt] phase discovering → verifying`)
- Every breaking-change API alert (`freezing writes on: businesses`)
- Newsletter compose reminders on Sundays

Silence = things are working.

---

## Adding more towns

Repeat Step 5 for each additional town, using a different slug each time.
The shared Firecrawl and Squid stacks from Step 4 stay as-is.

```
cd ~/workspace/townloops-agent
./bin/townloops-agent-add second-town-slug
cd ~/townloops-agents/second-town-slug
nano .env               # fill in that town's TOWNLOOPS_API_KEY, etc.
nano town-profile.yaml  # that town's name, region, seeds
docker compose up -d
```

Each town runs in its own container with its own state. They don't share
data. You can add or remove towns without affecting others.

---

## Everyday operations

### See all running towns

```
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

You'll see one `townloops-agent-<slug>` line per town, plus the Firecrawl
+ Squid containers.

### Pause a town

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose stop
```

Resume with `docker compose start`.

### Restart a town after changing `.env` or `town-profile.yaml`

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose restart agent
```

### Stop a town from running discovery, keep it in "steady state" only

Useful if you want to freeze onboarding at a certain point and just have
the daily event ingest + weekly newsletter continue.

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose exec agent python /home/agent/.hermes/skills/business/onboarding-orchestrator/orchestrate.py finalize
```

### See what's on the town's discovery queue

```
docker compose exec agent python /home/agent/.hermes/skills/business/business-discovery/discover.py list --limit 50
```

### Manually run a stage right now (instead of waiting for cron)

```
# Discovery sweep
docker compose exec agent python /home/agent/.hermes/skills/business/business-discovery/discover.py sweep
docker compose exec agent python /home/agent/.hermes/skills/business/community-discovery/discover.py sweep

# One orchestrator tick
docker compose exec agent python /home/agent/.hermes/skills/business/onboarding-orchestrator/orchestrate.py tick
```

### Check when the TownLoops API last changed

```
docker compose exec agent python /home/agent/.hermes/skills/business/townloops-api/api.py check
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

### The agent says "authentication failed" on startup

The `TOWNLOOPS_API_KEY` in `.env` is wrong for this town's slug, or the
key was rotated. Get a fresh key from your TownLoops administrator, edit
`.env`, then `docker compose restart agent`.

### The agent starts but never discovers anything

Check that Firecrawl is running:

```
docker ps | grep firecrawl
```

You should see 5 containers. If not, restart the Firecrawl stack:

```
cd ~/workspace/townloops-agent
docker compose -f templates/_shared/firecrawl/docker-compose.yml up -d
```

Then check that the agent can reach it:

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose exec agent curl -sf http://firecrawl-api:3002/health
```

Should print a "healthy" message. If not, both stacks need to be on the
same Docker network — check `docker network inspect firecrawl-shared-net`.

### Squid keeps blocking legitimate websites

Edit `~/workspace/townloops-agent/templates/_shared/egress/squid.conf`,
add your host to an existing `acl` line, then:

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

### The agent is stuck in one phase for days

By design, the orchestrator will **force-advance** after 7 days in the
same phase (168 hourly ticks). You'll get a Telegram warning when this
happens: `[slug] phase X stuck — force-advancing to Y`. Nothing to do —
it advances itself.

If you want to advance sooner, use `finalize` above (which jumps to
`steady_state`), or delete the state DB and re-run bootstrap:

```
cd ~/townloops-agents/YOUR-TOWN-SLUG
docker compose stop
rm state/townloops.db
docker compose start
```

**Warning**: this wipes local discovery + enrichment state; the agent
will re-derive from TownLoops and re-run all onboarding phases.

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

# Restart every town so it picks up the new engine/skills code.
for town in ~/townloops-agents/*/; do
  cd "$town"
  docker compose up -d --build
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

1. **Read the agent's logs** — `docker compose logs -f agent`. Errors
   almost always print a clear message about what's wrong.
2. **Check the orchestrator status** — the JSON from `orchestrate.py
   status` shows what the agent thinks it's doing.
3. **Ask your TownLoops administrator** — they can check whether the
   agent is actually posting to TownLoops, and whether your bearer token
   is valid.
4. **File an issue** on the townloops-agent repo with:
   - What you were trying to do
   - The exact command you ran
   - The last ~20 lines of `docker compose logs agent`
   - Your VPS provider + Ubuntu version

The agent is designed to be quiet when things work and loud when they
don't. If your Telegram is silent and `orchestrate.py status` shows a
recent `last_tick_at`, you're fine — even if it looks like nothing is
happening, hourly ticks are running and making steady progress.
