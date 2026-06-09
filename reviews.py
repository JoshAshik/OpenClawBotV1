"""
Google Reviews monitoring — polls Google Places API for new reviews.

Setup:
  1. Go to console.cloud.google.com → enable Places API (New)
  2. Create an API key → restrict to Places API
  3. Add GOOGLE_PLACES_API_KEY to .env

How it works:
  - Each property is stored by its Google Place ID
  - A background job polls every N minutes (default 15)
  - New reviews (not in seen_reviews table) trigger a Telegram alert
  - /addproperty searches by name and lets you pick the right one
"""

import hashlib
import logging

import httpx

import config
import db

logger = logging.getLogger(__name__)

_BASE_URL = "https://places.googleapis.com/v1"


def _get_api_key() -> str:
    if not config.GOOGLE_PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set in .env")
    return config.GOOGLE_PLACES_API_KEY


async def search_places(query: str) -> list[dict]:
    """Search for a place by text query. Returns up to 5 matches with name, address, place_id."""
    api_key = _get_api_key()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_BASE_URL}/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount",
            },
            json={"textQuery": query, "maxResultCount": 5},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for place in data.get("places", []):
        results.append({
            "place_id": place.get("id", ""),
            "name": place.get("displayName", {}).get("text", "Unknown"),
            "address": place.get("formattedAddress", ""),
            "rating": place.get("rating", 0),
            "review_count": place.get("userRatingCount", 0),
        })
    return results


async def fetch_reviews(place_id: str) -> list[dict]:
    """Fetch current reviews for a place. Returns list of review dicts."""
    api_key = _get_api_key()

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_BASE_URL}/places/{place_id}",
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "reviews",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    reviews = []
    for r in data.get("reviews", []):
        # Build a stable ID from author + time since Google doesn't expose review IDs
        author = r.get("authorAttribution", {}).get("displayName", "Anonymous")
        pub_time = r.get("publishTime", "")
        review_id = hashlib.sha256(f"{author}:{pub_time}".encode()).hexdigest()[:16]

        # Parse relative time to a sortable int (epoch seconds from publishTime)
        time_val = 0
        if pub_time:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                time_val = int(dt.timestamp())
            except (ValueError, TypeError):
                pass

        reviews.append({
            "review_id": review_id,
            "author": author,
            "rating": r.get("rating", 0),
            "text": r.get("text", {}).get("text", ""),
            "relative_time": r.get("relativePublishTimeDescription", ""),
            "time": time_val,
            "publish_time": pub_time,
        })

    return reviews


async def check_new_reviews(place_id: str) -> list[dict]:
    """
    Fetch reviews for a place, compare against seen_reviews, return only new ones.
    New reviews are automatically marked as seen.
    """
    reviews = await fetch_reviews(place_id)
    new_reviews = []

    for r in reviews:
        if not await db.is_review_seen(place_id, r["review_id"]):
            await db.mark_review_seen(
                place_id=place_id,
                review_id=r["review_id"],
                author=r["author"],
                rating=r["rating"],
                text=r["text"],
                time_val=r["time"],
            )
            new_reviews.append(r)

    return new_reviews


async def poll_all_properties() -> list[dict]:
    """
    Check all monitored properties for new reviews.
    Returns list of {property, review} dicts for each new review found.
    """
    properties = await db.list_properties()
    alerts = []

    for prop in properties:
        try:
            new_reviews = await check_new_reviews(prop["place_id"])
            for review in new_reviews:
                alerts.append({
                    "property_name": prop["name"],
                    "property_address": prop["address"],
                    "place_id": prop["place_id"],
                    **review,
                })
        except Exception as e:
            logger.error(f"Failed to check reviews for {prop['name']}: {e}")

    return alerts


def rating_stars(rating: int) -> str:
    return "⭐" * rating + "☆" * (5 - rating)


async def seed_existing_reviews(place_id: str) -> int:
    """
    On first add, mark all current reviews as 'seen' so we only alert on truly new ones.
    Returns count of reviews seeded.
    """
    reviews = await fetch_reviews(place_id)
    for r in reviews:
        await db.mark_review_seen(
            place_id=place_id,
            review_id=r["review_id"],
            author=r["author"],
            rating=r["rating"],
            text=r["text"],
            time_val=r["time"],
        )
    return len(reviews)
