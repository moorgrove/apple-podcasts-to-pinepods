# Reading the Apple Podcasts database (Mac)

## Prerequisites

Terminal (or whatever app you run this from) may need **Full Disk Access**:
System Settings → Privacy & Security → Full Disk Access → add the app,
restart it.

## 1. Copy the database

Copy it so you're not reading a file the Podcasts app is writing to at
the same time:

```bash
cp ~/Library/Group\ Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite /tmp/mt.sqlite
```

## 2. Inspect the schema (do this first, every time Apple changes something)

List all tables:

```bash
sqlite3 /tmp/mt.sqlite ".tables"
```

List all columns in any table mentioning "play" (useful for finding the
right field if Apple renamed something):

```bash
sqlite3 /tmp/mt.sqlite "
SELECT m.name AS table_name, p.name AS column_name
FROM sqlite_master m
JOIN pragma_table_info(m.name) p
WHERE m.type='table' AND p.name LIKE '%PLAY%';
"
```

Full schema for the two tables this uses:

```bash
sqlite3 /tmp/mt.sqlite ".schema ZMTEPISODE"
sqlite3 /tmp/mt.sqlite ".schema ZMTPODCAST"
```

Quick check of how many rows actually have data in a given field (good for
confirming a field is actually used before building an export on it):

```bash
sqlite3 /tmp/mt.sqlite "SELECT ZPLAYCOUNT, COUNT(*) FROM ZMTEPISODE GROUP BY ZPLAYCOUNT;"
sqlite3 /tmp/mt.sqlite "SELECT ZISBOOKMARKED, COUNT(*) FROM ZMTEPISODE GROUP BY ZISBOOKMARKED;"
```

## 3. Export listening history

**Note:** use `ZPLAYCOUNT`, not `ZPLAYSTATE` — see the README for why.

```bash
sqlite3 -header -csv /tmp/mt.sqlite "
SELECT
  p.ZTITLE AS podcast_title,
  e.ZTITLE AS episode_title,
  e.ZASSETURL AS episode_url,
  e.ZPLAYCOUNT AS play_count,
  e.ZPLAYHEAD AS playhead_seconds,
  e.ZDURATION AS duration_seconds
FROM ZMTEPISODE e
JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
WHERE e.ZPLAYCOUNT > 0 OR e.ZPLAYHEAD > 0;
" > ~/Desktop/apple_podcasts_history.csv
```

## 4. Export saved/bookmarked episodes

**Note:** use `ZISBOOKMARKED`, not `ZSAVED` — see the README for why.

```bash
sqlite3 -header -csv /tmp/mt.sqlite "
SELECT
  p.ZTITLE AS podcast_title,
  e.ZTITLE AS episode_title,
  e.ZASSETURL AS episode_url,
  e.ZDURATION AS duration_seconds
FROM ZMTEPISODE e
JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
WHERE e.ZISBOOKMARKED = 1;
" > ~/Desktop/apple_podcasts_saved.csv
```

## 5. If the numbers look incomplete

Cloud sync to the local database seems to be lazy/per-podcast — open each
podcast's page in the Podcasts app on the Mac (this seems to trigger a
download of its full history from the cloud), wait a bit, re-run steps 1,
3 and 4, and re-import (safe to re-run, see README).

## Looking up a single podcast's ZGUID (RSS guid) for troubleshooting

Useful if you suspect episode matching is failing for a specific podcast:

```bash
sqlite3 -header -csv /tmp/mt.sqlite "
SELECT e.ZGUID, e.ZTITLE
FROM ZMTEPISODE e
JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
WHERE p.ZTITLE LIKE '%SEARCH_TERM%';
"
```

`ZGUID` corresponds to the `<guid>` tag in the podcast's RSS feed (verified
by comparing against a fetched feed). `ZUUID` is a separate, Apple-internal
ID and does NOT correspond to the RSS guid.
