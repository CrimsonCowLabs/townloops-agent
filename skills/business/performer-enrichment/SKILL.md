---
name: performer-enrichment
description: Fill in bio, socials (instagram, spotify), and image for performers mined from event `performer[]` fields. Runs on cron 09:00 daily.
---

# performer-enrichment

Same shape as venue/business enrichment. Kind = `performer`, table = `performers`. Fills:

- `description` (bio)
- `website`
- `instagram`
- `spotify_url`
- `image_path`, `image_alt`

Uses `engine.research.research_url()` which knows to prefer JSON-LD MusicGroup / Person and og:image with Facebook profile fallback.

## Commands

```
python skills/business/performer-enrichment/enrich.py sweep [--limit N] [--firecrawl]
python skills/business/performer-enrichment/enrich.py ship  [--batch N]
python skills/business/performer-enrichment/enrich.py depth
```
