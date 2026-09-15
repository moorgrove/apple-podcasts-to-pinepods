# Apple Podcasts → PinePods: import listening history

Script + commands for moving listening history and saved/bookmarked
episodes from Apple Podcasts on Mac into a self-hosted
[PinePods](https://github.com/madeofpendletonwool/PinePods) instance.

> Vibe-coded with [Claude](https://claude.ai). Reviewed and tested against a
> real PinePods instance, but written mostly by prompting rather than
> hand-typed — read the code before trusting it blindly, especially if you
> extend it.

Your podcasts need to already be subscribed in PinePods before running the
import — the script matches history against existing podcasts/episodes, it
doesn't create new subscriptions.

## Quick start

```bash
# 1. Export data from Apple Podcasts (see database_export_commands.md)
#    -> apple_podcasts_history.csv
#    -> apple_podcasts_saved.csv (optional, bookmarked episodes)

# 2. Create a venv and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install requests

# 3. Dry-run first — shows what WOULD happen without writing anything
python3 import_to_pinepods.py \
  --csv apple_podcasts_history.csv \
  --saved-csv apple_podcasts_saved.csv \
  --url https://your-pinepods-instance.example.com \
  --api-key YOUR_API_KEY \
  --user-id YOUR_USER_ID \
  --dry-run

# 4. Run for real (same command without --dry-run)
```

Safe to re-run — history just overwrites the same position/status, no
risk of duplicates.

## How matching works

1. Podcast title → `podcast_id` via PinePods `/api/data/return_pods/{user_id}`
2. Episode → `episode_id` via `/api/data/podcast_episodes`, tried in order:
   exact URL → exact title → fuzzy title
3. Known podcast-title differences (see `PROXY_TITLE_TOKENS` in the script)
   are stripped from both sides before comparing — add more there if you
   find new variants in `unmatched.csv`

Rows that don't match end up in `unmatched.csv` / `unmatched_saved.csv`
instead of guessing wrong.

## Pitfalls we ran into

- **`ZPLAYSTATE` is NOT reliable** for "fully listened". It gets
  incorrectly set to "played" for a podcast's entire back catalog when you
  subscribe, and doesn't seem to be reliably kept up to date by cloud sync
  either. Use **`ZPLAYCOUNT > 0`** instead — verified against actual
  listening history.
- **`ZSAVED` is NOT the bookmark icon.** That field was always 0 in our
  database. Bookmarked/saved episodes live in **`ZISBOOKMARKED`**.
- **Cloud sync is lazy, per podcast.** New `ZPLAYCOUNT`/`ZPLAYHEAD` values
  may seem to get pulled down into the local Mac database once you
  actually open that podcast's page in the app. Go through your podcasts
  in the app first if the numbers look incomplete, re-export, and re-run
  the import (safe, see above).
- **Apple's Data & Privacy export (`Podcasts Playstate.csv`)** uses its
  own, undocumented `Episode ID` format that matches neither `ZGUID` nor
  `ZUUID` in the local database — we found no way to bridge them. That
  file turned out not to be usable as a data source for this import.
- **Local ad-free proxies** (e.g. for Swedish Radio feeds) can append a
  suffix to the podcast title on either the Apple or the PinePods side
  (`" — SR-restored"`, `"(Ad-Free)"`) — handled by the
  `PROXY_TITLE_TOKENS` list in the script.

## Files

- `import_to_pinepods.py` — the import script itself
- `database_export_commands.md` — every sqlite3 command used to read the
  Apple Podcasts database (schema inspection + ready-made export queries)
