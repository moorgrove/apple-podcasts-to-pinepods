#!/usr/bin/env python3
"""
Import listening history from Apple Podcasts (exported as CSV, see
database_export_commands.md) into a self-hosted PinePods instance.

Vibe-coded with Claude (claude.ai) — see README.md.

Matching happens automatically in two steps:
  1. podcast title -> podcast_id   (against your existing podcast list in PinePods)
  2. episode URL (fallback: title) -> episode_id  (against that podcast's episodes)

No manual matching required, but rows that fail to match are logged to
unmatched.csv instead of guessing wrong.

IMPORTANT to know before running:
  - PinePods' /api/data/record_podcast_history only accepts a position, no
    date. History entries will therefore end up with "now" as their
    timestamp in the PinePods database, not the actual date you listened on
    Mac. This hasn't been verified against a real instance.
  - Run with --dry-run first to see what the script WOULD do without
    actually writing anything.

Usage:
    pip install requests
    python3 import_to_pinepods.py \
        --csv ~/Desktop/apple_podcasts_history.csv \
        --url https://pinepods.yourdomain.com \
        --api-key YOUR_API_KEY \
        --user-id YOUR_USER_ID \
        --dry-run

    # once you're happy with the dry-run output, run again without --dry-run

    # to import "saved" episodes (the bookmark icon in Apple Podcasts,
    # ZISBOOKMARKED=1) instead of/in addition to history:
    python3 import_to_pinepods.py \
        --saved-csv ~/Desktop/apple_podcasts_saved.csv \
        --url https://pinepods.yourdomain.com \
        --api-key YOUR_API_KEY \
        --user-id YOUR_USER_ID \
        --dry-run

    # --csv and --saved-csv can both be given in the same run
"""

import argparse
import csv
import difflib
import re
import sys
import time
from pathlib import Path

import requests

# Known tokens that a local proxy (e.g. for SR/ad-free feeds) may append to
# podcast titles, which don't appear in Apple Podcasts copy of the same podcast.
# Add new tokens here as you find them in unmatched.csv.
PROXY_TITLE_TOKENS = [
    "SR-restored",
    "(Ad-Free)",
    "Ad-Free",
]


def strip_proxy_tokens(title: str) -> str:
    cleaned = title or ""
    for token in PROXY_TITLE_TOKENS:
        cleaned = re.sub(re.escape(token), "", cleaned, flags=re.IGNORECASE)
    # collapse leftover double spaces / stray separators from the removal
    # (handles hyphen -, en dash –, em dash —, colons, pipes, parens)
    cleaned = re.sub(r"[\s\-–—:|()]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def get_headers(api_key: str) -> dict:
    return {"Api-Key": api_key, "Content-Type": "application/json"}


def fetch_podcasts(base_url: str, api_key: str, user_id: int) -> dict:
    """Returns {normalized_title: podcast_id} for every podcast in PinePods.
    The key is normalized AFTER stripping known proxy-added tokens (e.g.
    "SR-restored"), since such tokens can appear on either the PinePods
    side or the Apple side depending on which feed was subscribed to."""
    url = f"{base_url}/api/data/return_pods/{user_id}"
    resp = requests.get(url, headers=get_headers(api_key), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    pods = data.get("pods", [])
    mapping = {}
    for p in pods:
        title = p.get("podcastname", "")
        # both the raw and the stripped form point to the same podcast_id,
        # so a query title matches whichever form applies
        mapping[normalize(title)] = p["podcastid"]
        mapping[normalize(strip_proxy_tokens(title))] = p["podcastid"]
    return mapping


def fetch_episodes_for_podcast(base_url: str, api_key: str, user_id: int, podcast_id: int) -> list:
    """Fetches all episodes (with pagination) for a podcast_id."""
    episodes = []
    offset = 0
    limit = 200
    while True:
        url = f"{base_url}/api/data/podcast_episodes"
        params = {"user_id": user_id, "podcast_id": podcast_id, "limit": limit, "offset": offset}
        resp = requests.get(url, headers=get_headers(api_key), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("episodes", [])
        episodes.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return episodes


def normalize(s: str) -> str:
    return (s or "").strip().lower()


def find_podcast_id(podcast_title: str, podcast_map: dict) -> int | None:
    key = normalize(podcast_title)
    if key in podcast_map:
        return podcast_map[key]

    # fallback: strip known proxy-added tokens (e.g. "SR-restored", "(Ad-Free)")
    stripped_key = normalize(strip_proxy_tokens(podcast_title))
    if stripped_key in podcast_map:
        return podcast_map[stripped_key]

    # fuzzy fallback for anything else (punctuation drift, minor renames)
    close = difflib.get_close_matches(stripped_key, podcast_map.keys(), n=1, cutoff=0.85)
    if close:
        return podcast_map[close[0]]
    return None


def find_episode_id(episode_title: str, episode_url: str, episodes: list) -> tuple[int | None, str]:
    """Returns (episode_id, match_method)."""
    norm_url = (episode_url or "").strip()
    for ep in episodes:
        if ep.get("Episodeurl", "").strip() == norm_url and norm_url:
            return ep["Episodeid"], "url"

    # fallback: exact title
    norm_title = normalize(episode_title)
    for ep in episodes:
        if normalize(ep.get("Episodetitle", "")) == norm_title:
            return ep["Episodeid"], "title"

    # last resort: fuzzy title
    titles = {normalize(ep.get("Episodetitle", "")): ep["Episodeid"] for ep in episodes}
    close = difflib.get_close_matches(norm_title, titles.keys(), n=1, cutoff=0.9)
    if close:
        return titles[close[0]], "fuzzy_title"

    return None, "none"


def looks_completed(play_count: str, playhead: float, duration: float) -> bool:
    """play_count > 0 has been confirmed against known listened/unlistened episodes
    and is far more reliable than Apple's ZPLAYSTATE field, which is corrupted by
    the auto-mark-as-played-on-subscribe behavior. A high playhead/duration ratio
    is kept as a secondary signal for episodes where play_count wasn't populated
    but a real position was recorded."""
    try:
        if play_count and int(float(play_count)) > 0:
            return True
    except (ValueError, TypeError):
        pass
    try:
        if duration and playhead and playhead / duration >= 0.95:
            return True
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    return False


def resolve_episode_id(
    row: dict,
    podcast_map: dict,
    episodes_cache: dict,
    base_url: str,
    api_key: str,
    user_id: int,
) -> tuple[int | None, str]:
    """Matches a CSV row (podcast_title/episode_title/episode_url) against an
    episode_id in PinePods. Returns (episode_id or None, failure reason if
    None else match method). Shared by both the history and saved imports."""
    podcast_title = row.get("podcast_title", "")
    episode_title = row.get("episode_title", "")
    episode_url = row.get("episode_url", "")

    podcast_id = find_podcast_id(podcast_title, podcast_map)
    if podcast_id is None:
        return None, "podcast_not_found"

    if podcast_id not in episodes_cache:
        episodes_cache[podcast_id] = fetch_episodes_for_podcast(base_url, api_key, user_id, podcast_id)

    episode_id, method = find_episode_id(episode_title, episode_url, episodes_cache[podcast_id])
    if episode_id is None:
        return None, "episode_not_found"
    return episode_id, method


def import_saved_episodes(
    saved_csv_path: Path,
    base_url: str,
    api_key: str,
    user_id: int,
    podcast_map: dict,
    episodes_cache: dict,
    dry_run: bool,
) -> None:
    """Matches and posts 'saved' episodes (Apple Podcasts ZISBOOKMARKED=1) to
    PinePods via /api/data/bulk_save_episodes (a single call with a list of
    episode_ids, faster than one call per episode)."""
    rows = list(csv.DictReader(saved_csv_path.open(encoding="utf-8")))
    print(f"\nRead {len(rows)} saved episodes from {saved_csv_path}")

    episode_ids = []
    unmatched = []
    for row in rows:
        episode_id, method = resolve_episode_id(row, podcast_map, episodes_cache, base_url, api_key, user_id)
        if episode_id is None:
            unmatched.append({**row, "reason": method})
            continue
        episode_ids.append(episode_id)
        if dry_run:
            print(
                f"  DRY-RUN saved: '{row.get('podcast_title','')}' / "
                f"'{row.get('episode_title','')}' -> episode_id={episode_id} (match={method})"
            )

    print(f"Saved episodes matched: {len(episode_ids)}/{len(rows)}")

    if unmatched:
        out_path = saved_csv_path.parent / "unmatched_saved.csv"
        with out_path.open("w", newline="", encoding="utf-8") as f:
            fieldnames = list(unmatched[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(unmatched)
        print(f"Unmatched saved episodes written to: {out_path}")

    if dry_run or not episode_ids:
        if dry_run:
            print("DRY-RUN: nothing posted to bulk_save_episodes.")
        return

    resp = requests.post(
        f"{base_url}/api/data/bulk_save_episodes",
        headers=get_headers(api_key),
        json={"episode_ids": episode_ids, "user_id": user_id, "is_youtube": False},
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"  [!] bulk_save_episodes failed: {resp.status_code} {resp.text[:300]}")
        return
    data = resp.json()
    print(
        f"bulk_save_episodes: {data.get('message', '')} "
        f"(processed_count={data.get('processed_count')}, failed_count={data.get('failed_count')})"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="Path to the exported history CSV (play_count/playhead)")
    ap.add_argument("--saved-csv", help="Path to the exported CSV of saved episodes (ZISBOOKMARKED=1)")
    ap.add_argument("--url", required=True, help="Base URL of PinePods, e.g. https://pinepods.yourdomain.com")
    ap.add_argument("--api-key", required=True, help="Your PinePods API key")
    ap.add_argument("--user-id", required=True, type=int, help="Your PinePods user_id")
    ap.add_argument("--dry-run", action="store_true", help="Write nothing, just show what would happen")
    ap.add_argument("--sleep", type=float, default=0.05, help="Pause between API calls in seconds (default 0.05)")
    args = ap.parse_args()

    if not args.csv and not args.saved_csv:
        print("Provide at least one of --csv or --saved-csv", file=sys.stderr)
        sys.exit(1)

    base_url = args.url.rstrip("/")

    print("Fetching podcast list from PinePods...")
    podcast_map = fetch_podcasts(base_url, args.api_key, args.user_id)
    print(f"  found {len(podcast_map)} podcasts in PinePods")

    episodes_cache: dict[int, list] = {}

    if args.csv:
        csv_path = Path(args.csv).expanduser()
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        print(f"Read {len(rows)} rows from {csv_path}")
    if args.csv:
        unmatched = []
        matched = 0
        completed_count = 0

        for i, row in enumerate(rows, 1):
            podcast_title = row.get("podcast_title", "")
            episode_title = row.get("episode_title", "")
            episode_url = row.get("episode_url", "")
            play_count = row.get("play_count", "")
            try:
                playhead = float(row.get("playhead_seconds") or 0)
            except ValueError:
                playhead = 0.0
            try:
                duration = float(row.get("duration_seconds") or 0)
            except ValueError:
                duration = 0.0

            podcast_id = find_podcast_id(podcast_title, podcast_map)
            if podcast_id is None:
                unmatched.append({**row, "reason": "podcast_not_found"})
                continue

            if podcast_id not in episodes_cache:
                episodes_cache[podcast_id] = fetch_episodes_for_podcast(
                    base_url, args.api_key, args.user_id, podcast_id
                )

            episode_id, method = find_episode_id(episode_title, episode_url, episodes_cache[podcast_id])
            if episode_id is None:
                unmatched.append({**row, "reason": "episode_not_found"})
                continue

            completed = looks_completed(play_count, playhead, duration)

            if args.dry_run:
                print(
                    f"[{i}/{len(rows)}] DRY-RUN: '{podcast_title}' / '{episode_title}' "
                    f"-> episode_id={episode_id} (match={method}), pos={playhead:.0f}s, "
                    f"completed={completed}"
                )
            else:
                hist_resp = requests.post(
                    f"{base_url}/api/data/record_podcast_history",
                    headers=get_headers(args.api_key),
                    json={
                        "episode_id": episode_id,
                        "episode_pos": playhead,
                        "user_id": args.user_id,
                        "is_youtube": False,
                    },
                    timeout=30,
                )
                if hist_resp.status_code != 200:
                    print(
                        f"  [!] record_podcast_history failed for episode_id={episode_id}: "
                        f"{hist_resp.status_code} {hist_resp.text[:300]}"
                    )
                    unmatched.append({**row, "reason": f"history_post_failed_{hist_resp.status_code}"})
                    continue

                if completed:
                    comp_resp = requests.post(
                        f"{base_url}/api/data/mark_episode_completed",
                        headers=get_headers(args.api_key),
                        json={"episode_id": episode_id, "user_id": args.user_id, "is_youtube": False},
                        timeout=30,
                    )
                    if comp_resp.status_code == 200:
                        completed_count += 1
                    else:
                        print(
                            f"  [!] mark_episode_completed failed for episode_id={episode_id}: "
                            f"{comp_resp.status_code} {comp_resp.text[:300]}"
                        )

                time.sleep(args.sleep)

            matched += 1

        print()
        print(f"Done. Matched: {matched}/{len(rows)}  (of which marked completed: {completed_count})")
        print(f"Unmatched: {len(unmatched)}")

        if unmatched:
            out_path = csv_path.parent / "unmatched.csv"
            with out_path.open("w", newline="", encoding="utf-8") as f:
                fieldnames = list(unmatched[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(unmatched)
            print(f"Unmatched rows written to: {out_path}")

    if args.saved_csv:
        import_saved_episodes(
            Path(args.saved_csv).expanduser(),
            base_url,
            args.api_key,
            args.user_id,
            podcast_map,
            episodes_cache,
            args.dry_run,
        )

    if args.dry_run:
        print("\nThis was a DRY-RUN — nothing was written to PinePods. Run without --dry-run to actually import.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP error against PinePods: {e}", file=sys.stderr)
        sys.exit(1)
