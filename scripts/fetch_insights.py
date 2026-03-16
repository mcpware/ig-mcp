#!/usr/bin/env python3
"""
Hourly cron job to fetch Instagram post insights and store in SQLite.

Fetches the latest 10 posts' insights every hour:
- Views / impressions / reach
- Follower vs non-follower breakdown
- Traffic sources (profile, explore, hashtag, etc.)

Usage:
    python scripts/fetch_insights.py          # Run once
    python scripts/fetch_insights.py --posts 5  # Fetch top 5 posts only

Cron (every hour):
    0 * * * * cd /home/nicole/MyGithub/ig-mcp && /usr/bin/python3 scripts/fetch_insights.py >> logs/cron_insights.log 2>&1
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so we can import src modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.config import get_settings
from src.instagram_client import InstagramClient

# ── Config ───────────────────────────────────────────────────────

DB_PATH = PROJECT_ROOT / "data" / "insights.db"
LOG_DIR = PROJECT_ROOT / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "cron_insights.log"),
    ],
)
logger = logging.getLogger(__name__)

# Instagram Graph API media insight metrics.
# For IMAGE and CAROUSEL_ALBUM posts, we use the standard set.
# For VIDEO/REEL posts, we swap in video-specific metrics.
#
# Docs: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media/insights
MEDIA_INSIGHTS_IMAGE = [
    "impressions",         # Total times the post was displayed
    "reach",               # Unique accounts that saw the post
    "saved",               # Times saved
    "likes",               # Like count
    "comments",            # Comment count
    "shares",              # Share count
]

MEDIA_INSIGHTS_VIDEO = [
    "impressions",
    "reach",
    "saved",
    "likes",
    "comments",
    "shares",
    "video_views",         # Total video views
    "plays",               # Total plays (includes replays)
]

# These breakdown metrics require metric_type=total_value
MEDIA_BREAKDOWN_METRICS = [
    "reach",               # With breakdown=follow_type → follower vs non_follower
]


# ── Database ─────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    # Main insights table — one row per (post, metric, fetch_time)
    c.execute("""
        CREATE TABLE IF NOT EXISTS post_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            media_id TEXT NOT NULL,
            media_type TEXT,
            caption TEXT,
            permalink TEXT,
            posted_at TEXT,
            metric_name TEXT NOT NULL,
            metric_value INTEGER,
            UNIQUE(fetched_at, media_id, metric_name)
        )
    """)

    # Breakdown table — follower vs non-follower, traffic source, etc.
    c.execute("""
        CREATE TABLE IF NOT EXISTS post_insights_breakdown (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            media_id TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            breakdown_dimension TEXT NOT NULL,
            breakdown_key TEXT NOT NULL,
            breakdown_value INTEGER,
            UNIQUE(fetched_at, media_id, metric_name, breakdown_dimension, breakdown_key)
        )
    """)

    # Index for fast queries by media_id and time
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_insights_media_time
        ON post_insights(media_id, fetched_at)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_breakdown_media_time
        ON post_insights_breakdown(media_id, fetched_at)
    """)

    conn.commit()
    return conn


def store_insights(conn, fetched_at: str, media_id: str, media_type: str,
                   caption: str, permalink: str, posted_at: str,
                   insights: list):
    """Store basic metric values."""
    c = conn.cursor()
    for insight in insights:
        name = insight.get("name", "")
        values = insight.get("values", [])
        if values:
            value = values[0].get("value", 0)
            # value could be a dict (breakdown) — skip those here
            if isinstance(value, int):
                c.execute("""
                    INSERT OR REPLACE INTO post_insights
                    (fetched_at, media_id, media_type, caption, permalink, posted_at,
                     metric_name, metric_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (fetched_at, media_id, media_type, caption, permalink,
                      posted_at, name, value))
    conn.commit()


def store_breakdowns(conn, fetched_at: str, media_id: str, breakdowns: list):
    """Store breakdown data (follower/non-follower, traffic source, etc.)."""
    c = conn.cursor()
    for item in breakdowns:
        name = item.get("name", "")
        total_value = item.get("total_value", {})
        if not total_value:
            continue

        # total_value looks like:
        # {"breakdowns": [{"dimension_keys": ["follow_type"],
        #   "results": [{"dimension_values": ["FOLLOWER"], "value": 123}, ...]}]}
        for bd in total_value.get("breakdowns", []):
            dimension = "_".join(bd.get("dimension_keys", ["unknown"]))
            for result in bd.get("results", []):
                key = "_".join(result.get("dimension_values", ["unknown"]))
                value = result.get("value", 0)
                c.execute("""
                    INSERT OR REPLACE INTO post_insights_breakdown
                    (fetched_at, media_id, metric_name, breakdown_dimension,
                     breakdown_key, breakdown_value)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (fetched_at, media_id, name, dimension, key, value))
    conn.commit()


# ── Fetch Logic ──────────────────────────────────────────────────

async def fetch_and_store(num_posts: int = 10):
    """Main function: fetch latest posts' insights and store them."""
    settings = get_settings()
    client = InstagramClient(settings)
    conn = init_db()

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        await client.initialize()

        # Step 1: Get latest posts
        logger.info(f"Fetching latest {num_posts} posts...")
        posts = await client.get_media_posts(limit=num_posts)
        logger.info(f"Got {len(posts)} posts")

        for post in posts:
            media_id = post.id
            media_type = post.media_type.value if post.media_type else "IMAGE"
            caption = (post.caption or "")[:200]  # Truncate for storage
            permalink = post.permalink or ""
            posted_at = post.timestamp.isoformat() if post.timestamp else ""

            logger.info(f"  Fetching insights for {media_id} ({media_type})...")

            # Step 2a: Get basic metrics
            try:
                metrics = MEDIA_INSIGHTS_VIDEO if media_type in ("VIDEO", "REELS") else MEDIA_INSIGHTS_IMAGE
                params = {
                    "metric": ",".join(metrics),
                }
                data = await client._make_request(
                    "GET", f"{media_id}/insights", params=params
                )
                store_insights(conn, fetched_at, media_id, media_type,
                               caption, permalink, posted_at,
                               data.get("data", []))
            except Exception as e:
                logger.warning(f"  Basic insights failed for {media_id}: {e}")

            # Step 2b: Get breakdown metrics (follower vs non-follower)
            try:
                breakdown_params = {
                    "metric": ",".join(MEDIA_BREAKDOWN_METRICS),
                    "metric_type": "total_value",
                    "breakdown": "follow_type",
                }
                bd_data = await client._make_request(
                    "GET", f"{media_id}/insights", params=breakdown_params
                )
                store_breakdowns(conn, fetched_at, media_id,
                                 bd_data.get("data", []))
            except Exception as e:
                logger.warning(f"  Follower breakdown failed for {media_id}: {e}")

            # Step 2c: Get reach by traffic source
            try:
                source_params = {
                    "metric": "reach",
                    "metric_type": "total_value",
                    "breakdown": "media_product_type",
                }
                src_data = await client._make_request(
                    "GET", f"{media_id}/insights", params=source_params
                )
                store_breakdowns(conn, fetched_at, media_id,
                                 src_data.get("data", []))
            except Exception as e:
                logger.warning(f"  Traffic source breakdown failed for {media_id}: {e}")

        logger.info(f"Done! Stored insights at {fetched_at}")

        # Print quick summary
        c = conn.cursor()
        c.execute("""
            SELECT metric_name, SUM(metric_value)
            FROM post_insights WHERE fetched_at = ?
            GROUP BY metric_name ORDER BY metric_name
        """, (fetched_at,))
        logger.info("── Summary ──")
        for name, total in c.fetchall():
            logger.info(f"  {name}: {total}")

        c.execute("""
            SELECT breakdown_dimension, breakdown_key, SUM(breakdown_value)
            FROM post_insights_breakdown WHERE fetched_at = ?
            GROUP BY breakdown_dimension, breakdown_key
        """, (fetched_at,))
        for dim, key, total in c.fetchall():
            logger.info(f"  {dim}/{key}: {total}")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
    finally:
        conn.close()
        await client.close()


# ── Entry Point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Instagram post insights")
    parser.add_argument("--posts", type=int, default=10,
                        help="Number of latest posts to fetch (default: 10)")
    args = parser.parse_args()

    asyncio.run(fetch_and_store(args.posts))


if __name__ == "__main__":
    main()
