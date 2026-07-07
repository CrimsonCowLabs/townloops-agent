# townloops-agent

A standalone Hermes agent that runs the ongoing operations of a single TownLoops town: event ingest, entity enrichment, weekly Listmonk newsletter, and (once endpoints ship) sponsor / ad-spot management.

## What one deployment does

One clone of this repo runs **one town**. `TOWNLOOPS_TOWN_SLUG` is single-valued. For a second town, clone the repo again with a different `.env`.

Every deployment cascades: research sources → ingest events → mine + enrich venues/businesses/performers → publish weekly newsletter.

## Deploy

```bash
git clone <repo> ~/townloops-agent-<town>
cd ~/townloops-agent-<town>

cp .env.example .env
# Fill in: TOWNLOOPS_API_KEY, TOWNLOOPS_TOWN_SLUG, LISTMONK_*, FIRECRAWL_API_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY

docker-compose up -d
docker-compose logs -f agent
```

The first successful boot runs `town-init setup` automatically (verifies credentials, snapshots category taxonomy, migrates the state DB).

### Without Docker

Copy `systemd/hermes-gateway.service.template` to `~/.config/systemd/user/hermes-gateway-townloops.service`, edit the `WorkingDirectory` and `EnvironmentFile` paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway-townloops
```

You must also `pip install jinja2 requests pyyaml firecrawl-py` into the interpreter that runs Hermes.

## Skills

| Skill | Purpose |
|---|---|
| `town-init` | Bootstrap: verify keys, snapshot categories, migrate SQLite. |
| `event-source-discovery` | Build the town's `sources.yaml` — search → probe → review → compile. |
| `event-ingest` | Fetch feeds, mine venues/businesses/performers, POST/PATCH to TownLoops. |
| `venue-enrichment` | Web-research thin venues to fill address/phone/website/image. |
| `business-enrichment` | Same, for businesses (seeded from venue research + manual adds). |
| `performer-enrichment` | Same, for performers (bio, spotify, socials). |
| `newsletter-weekly` | Compose weekly HTML campaign, push to Listmonk. |
| `sponsor-management` | CRUD sponsors + ad_spots. Disabled until TownLoops endpoints ship. |

See `docs/skill-inventory.md` for the full skill catalog and CLI invocations.

## Sponsors

The v1 TownLoops Ingest API has no sponsor/ad endpoints. See `docs/api-sponsor-schema-proposal.md` for the proposed schema. The `sponsor-management` skill is written against that proposal and gated by `SPONSORS_ENABLED=false` — flip to `true` once the endpoints land.

## State

Runtime data lives in `./state/` (docker volume). SQLite databases:
- `state/townloops.db` — entities, enrichment queue, sponsor mirrors, newsletter log.
- `state/discovery.db` — source discovery candidates and probes.
- `state/sources.yaml` — compiled feed list, input to `event-ingest`.
- `state/categories.json` — snapshot of the town's platform category taxonomy.
- `state/newsletters/<iso-week>.html` — composed campaign bodies.

Back this up. It represents the agent's memory of what's already shipped to TownLoops.
