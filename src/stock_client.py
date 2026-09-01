"""Stock photo search clients for Pexels, Shutterstock, and Unsplash."""

import base64
import concurrent.futures
import os
import re

import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text or "").lower().strip()
    s = re.sub(r"[\s-]+", "-", s)
    return s[:max_len].rstrip("-")


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
    "for", "from", "had", "has", "have", "he", "her", "here", "hers", "him",
    "his", "i", "if", "in", "into", "is", "it", "its", "just", "me", "my",
    "of", "on", "or", "our", "she", "so", "some", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those", "to",
    "too", "us", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "you", "your",
}

# Pixabay category map: if a cleaned keyword matches, pass the category param.
_PIXABAY_CATEGORIES = {
    "fashion": "fashion", "nature": "nature", "background": "backgrounds",
    "backgrounds": "backgrounds", "science": "science", "education": "education",
    "feeling": "feelings", "feelings": "feelings", "health": "health",
    "people": "people", "person": "people", "religion": "religion",
    "place": "places", "places": "places", "animal": "animals", "animals": "animals",
    "industry": "industry", "computer": "computer", "food": "food", "sport": "sports",
    "sports": "sports", "transportation": "transportation", "travel": "travel",
    "building": "buildings", "buildings": "buildings", "business": "business",
    "music": "music",
}


def _prepare_query(phrase: str) -> dict:
    """Return per-API query strings derived from the user phrase."""
    # Strip stop words and punctuation, cap at 5 keywords.
    words = re.sub(r"[^\w\s]", "", phrase.lower()).split()
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 1][:5]
    cleaned = " ".join(keywords) if keywords else phrase.strip()

    # Pixabay: space-separated keywords, 100-char limit, optional category.
    pixabay_q = cleaned[:100]
    pixabay_category = next(
        (_PIXABAY_CATEGORIES[w] for w in keywords if w in _PIXABAY_CATEGORIES),
        None,
    )

    return {
        "pexels": cleaned,
        "unsplash": cleaned,
        "shutterstock": cleaned,
        "pixabay": pixabay_q,
        "pixabay_category": pixabay_category,
    }


# ---------------------------------------------------------------------------
# Per-library search functions
# ---------------------------------------------------------------------------

def search_pexels(phrase: str, limit: int = 8, page: int = 1) -> dict:
    """Search Pexels. Returns {results: [...], error: str|None}."""
    api_key = os.getenv("PEXELS_API_KEY", "")
    if not api_key:
        return {"results": [], "error": "PEXELS_API_KEY not configured"}
    try:
        headers = {"Authorization": api_key}
        params = {"query": _prepare_query(phrase)["pexels"], "per_page": limit, "page": max(1, int(page))}
        r = requests.get(
            "https://api.pexels.com/v1/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("photos", []):
            title = item.get("alt", "") or phrase
            src = item.get("src", {})
            width = item.get("width")
            height = item.get("height")
            sizes = []
            if src.get("original"):
                sizes.append({"label": "Original", "url": src["original"], "width": width, "height": height})
            if src.get("large2x"):
                sizes.append({"label": "Large (2x)", "url": src["large2x"]})
            if src.get("large"):
                sizes.append({"label": "Large", "url": src["large"]})
            if src.get("medium"):
                sizes.append({"label": "Medium", "url": src["medium"]})
            if src.get("small"):
                sizes.append({"label": "Small", "url": src["small"]})
            results.append({
                "thumb": src.get("medium", ""),
                "preview_url": (
                    src.get("large2x")
                    or src.get("large")
                    or src.get("original", "")
                ),
                "download_url": src.get("original", ""),
                "sizes": sizes,
                "title": title,
                "link": item.get("url", ""),
                "photographer": item.get("photographer", ""),
                "photographer_url": item.get("photographer_url", ""),
                "width": width,
                "height": height,
                "color": item.get("avg_color", ""),
            })
        return {"results": results, "error": None}
    except requests.HTTPError as e:
        return {"results": [], "error": f"Pexels API error {e.response.status_code}"}
    except Exception as e:
        return {"results": [], "error": str(e)}


def search_shutterstock(phrase: str, limit: int = 8, page: int = 1) -> dict:
    """Search Shutterstock. Returns {results: [...], error: str|None}."""
    client_id = os.getenv("SHUTTERSTOCK_CLIENT_ID", "")
    client_secret = os.getenv("SHUTTERSTOCK_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return {"results": [], "error": "SHUTTERSTOCK_CLIENT_ID / SHUTTERSTOCK_CLIENT_SECRET not configured"}
    try:
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {credentials}"}
        params = {
            "query": _prepare_query(phrase)["shutterstock"],
            "per_page": limit,
            "page": max(1, int(page)),
            "image_type": "photo",
            "safe": "true",
        }
        r = requests.get(
            "https://api.shutterstock.com/v2/images/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("data", []):
            item_id = item.get("id", "")
            desc = item.get("description", "")
            assets = item.get("assets", {})
            thumb = (
                assets.get("large_thumb", {}).get("url")
                or assets.get("preview", {}).get("url", "")
            )
            preview_url = (
                assets.get("preview_1000", {}).get("url")
                or assets.get("huge_thumb", {}).get("url")
                or thumb
            )
            results.append({
                "thumb": thumb,
                "preview_url": preview_url,
                "title": desc,
                "link": f"https://www.shutterstock.com/image-photo/{_slug(desc) or 'photo'}-{item_id}",
                "categories": [c.get("name", "") for c in item.get("categories", [])],
                "keywords": item.get("keywords", [])[:15],
                "aspect": item.get("aspect"),
                "is_editorial": item.get("is_editorial", False),
            })
        return {"results": results, "error": None}
    except requests.HTTPError as e:
        return {"results": [], "error": f"Shutterstock API error {e.response.status_code}"}
    except Exception as e:
        return {"results": [], "error": str(e)}


def search_pixabay(phrase: str, limit: int = 8, page: int = 1) -> dict:
    """Search Pixabay. Returns {results: [...], error: str|None}."""
    api_key = os.getenv("PIXABAY_API_KEY", "")
    if not api_key:
        return {"results": [], "error": "PIXABAY_API_KEY not configured"}
    try:
        pq = _prepare_query(phrase)
        params = {
            "key": api_key,
            "q": pq["pixabay"],
            "image_type": "photo",
            "per_page": limit,
            "page": max(1, int(page)),
            "safesearch": "true",
        }
        if pq["pixabay_category"]:
            params["category"] = pq["pixabay_category"]
        r = requests.get(
            "https://pixabay.com/api/",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("hits", []):
            tags = item.get("tags", "")
            sizes = []
            if item.get("largeImageURL"):
                sizes.append({
                    "label": "Large", "url": item["largeImageURL"],
                    "width": item.get("imageWidth"), "height": item.get("imageHeight"),
                })
            if item.get("webformatURL"):
                sizes.append({
                    "label": "Medium", "url": item["webformatURL"],
                    "width": item.get("webformatWidth"), "height": item.get("webformatHeight"),
                })
            results.append({
                "thumb": item.get("webformatURL", ""),
                "preview_url": item.get("largeImageURL", "") or item.get("webformatURL", ""),
                "download_url": item.get("largeImageURL", ""),
                "sizes": sizes,
                "title": tags,
                "link": item.get("pageURL", ""),
                "width": item.get("imageWidth"),
                "height": item.get("imageHeight"),
                "image_size": item.get("imageSize"),
                "views": item.get("views"),
                "downloads": item.get("downloads"),
                "likes": item.get("likes"),
                "user": item.get("user", ""),
                "type": item.get("type", ""),
            })
        return {"results": results, "error": None}
    except requests.HTTPError as e:
        return {"results": [], "error": f"Pixabay API error {e.response.status_code}"}
    except Exception as e:
        return {"results": [], "error": str(e)}


def search_unsplash(phrase: str, limit: int = 8, page: int = 1) -> dict:
    """Search Unsplash. Returns {results: [...], error: str|None}."""
    access_key = os.getenv("UNSPLASH_ACCESS_KEY", "")
    if not access_key:
        return {"results": [], "error": "UNSPLASH_ACCESS_KEY not configured"}
    try:
        headers = {"Authorization": f"Client-ID {access_key}"}
        params = {
            "query": _prepare_query(phrase)["unsplash"],
            "per_page": limit,
            "page": max(1, int(page)),
            "content_filter": "high",
        }
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("results", []):
            user = item.get("user", {})
            photographer = user.get("name", "")
            profile_url = (
                user.get("links", {}).get("html", "")
                + "?utm_source=proxima_image_library&utm_medium=referral"
            )
            photo_link = (
                item.get("links", {}).get("html", "")
                + "?utm_source=proxima_image_library&utm_medium=referral"
            )
            urls = item.get("urls", {})
            width = item.get("width")
            height = item.get("height")
            sizes = []
            if urls.get("full"):
                sizes.append({"label": "Full", "url": urls["full"], "width": width, "height": height})
            if urls.get("regular"):
                sizes.append({"label": "Regular", "url": urls["regular"]})
            if urls.get("small"):
                sizes.append({"label": "Small", "url": urls["small"]})
            results.append({
                "thumb": urls.get("small", ""),
                "preview_url": (
                    urls.get("regular")
                    or urls.get("full")
                    or urls.get("small", "")
                ),
                "download_url": urls.get("full", ""),
                "download_location": item.get("links", {}).get("download_location", ""),
                "sizes": sizes,
                "title": item.get("alt_description") or item.get("description") or "",
                "link": photo_link,
                "photographer": photographer,
                "photographer_url": profile_url,
                "width": width,
                "height": height,
                "color": item.get("color", ""),
                "likes": item.get("likes"),
                "tags": [t.get("title", "") for t in item.get("tags", []) if t.get("title")],
            })
        return {"results": results, "error": None}
    except requests.HTTPError as e:
        return {"results": [], "error": f"Unsplash API error {e.response.status_code}"}
    except Exception as e:
        return {"results": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Concurrent multi-library search
# ---------------------------------------------------------------------------

def search_all_libraries(phrases: list, limit: int = 8, page: int = 1, libraries: list | None = None) -> list:
    """Search all three libraries for all phrases concurrently.

    `libraries` restricts the search to a subset of library keys (e.g. ["pexels"]);
    omit to search all of them.
    """
    all_searchers = {
        "pexels": search_pexels,
        "shutterstock": search_shutterstock,
        "unsplash": search_unsplash,
        "pixabay": search_pixabay,
    }
    searchers = (
        {k: v for k, v in all_searchers.items() if k in libraries}
        if libraries else all_searchers
    )

    results_map = {p: {} for p in phrases}
    max_workers = min(len(phrases) * 4, 16)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {}
        for phrase in phrases:
            for lib_name, fn in searchers.items():
                f = executor.submit(fn, phrase, limit, page)
                future_to_key[f] = (phrase, lib_name)

        for future in concurrent.futures.as_completed(future_to_key, timeout=25):
            phrase, lib_name = future_to_key[future]
            try:
                results_map[phrase][lib_name] = future.result()
            except Exception as e:
                results_map[phrase][lib_name] = {"results": [], "error": str(e)}

    return [{"phrase": p, **results_map[p]} for p in phrases]


# ---------------------------------------------------------------------------
# Phrase extractor
# ---------------------------------------------------------------------------

def parse_photo_suggestions(content: str) -> list:
    """
    Extract photo suggestion phrases from skill output content.

    Handles:
    - Full markdown skill output with a "Photo Suggestion" section
    - Plain text / one phrase per line
    """
    lines = content.splitlines()

    # ── Phase 1: find Photo Suggestion section ──────────────────────────
    in_section = False
    section_lines = []
    blank_run = 0

    for line in lines:
        if re.search(r"photo\s+suggestion", line, re.IGNORECASE):
            in_section = True
            continue

        if in_section:
            stripped = line.strip()
            # Stop at next numbered output field or markdown heading
            if stripped and re.match(r"^(#+\s|\d+\.\s+\*\*)", stripped):
                break
            if not stripped:
                blank_run += 1
                if blank_run >= 2:
                    break
            else:
                blank_run = 0
                section_lines.append(stripped)

    # ── Phase 2: clean and validate ──────────────────────────────────────
    def _clean(line: str) -> str:
        line = re.sub(r"^```.*$", "", line)                        # code fences
        line = re.sub(r"^[\-\*\+\s>`]+", "", line)                 # bullets
        line = re.sub(r"^\d+[.\)]\s*", "", line)                   # numbered list
        line = re.sub(r"\*{1,2}([^*]*)\*{1,2}", r"\1", line)       # bold/italic
        line = re.sub(r"`([^`]*)`", r"\1", line)                    # inline code
        return line.strip().strip("\"'")

    def _valid(phrase: str) -> bool:
        words = phrase.split()
        return (
            1 <= len(words) <= 7
            and not re.search(r"—{1,}", phrase)
            and not re.search(
                r"(suggestion|adobe|shutterstock|unsplash|search phrase|per the)",
                phrase,
                re.IGNORECASE,
            )
        )

    if section_lines:
        phrases = [_clean(ln) for ln in section_lines]
        phrases = [p for p in phrases if _valid(p)]
        if phrases:
            return phrases[:20]

    # ── Fallback A: treat each non-empty line as a phrase ───────────────
    phrases = [_clean(ln) for ln in lines if ln.strip()]
    phrases = [p for p in phrases if _valid(p)][:20]
    if phrases:
        return phrases

    # ── Fallback B: derive short phrases from prose input ───────────────
    text = _clean(content)
    tokens = []
    for raw in re.findall(r"[A-Za-z][A-Za-z'-]*", text):
        tok = raw.lower().strip("'")
        if tok:
            tokens.append(tok)

    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
        "for", "from", "had", "has", "have", "he", "her", "here", "hers", "him",
        "his", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
        "more", "most", "my", "of", "on", "or", "our", "ours", "she", "so", "some",
        "than", "that", "the", "their", "theirs", "them", "then", "there", "these",
        "they", "this", "those", "to", "too", "us", "was", "we", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "you", "your", "yours",
        "we're", "you're", "they're", "it's", "that's", "i'm", "can't", "don't", "won't",
        "else", "here", "somewhere", "sometimes", "worked", "working", "stops", "stop",
        "happens", "happen", "approach", "willing",
    }
    concrete_words = {
        "city", "street", "teacher", "student", "students", "school", "classroom", "people",
        "person", "man", "woman", "child", "building", "downtown", "sidewalk", "park", "office",
    }
    location_words = {"city", "street", "downtown", "sidewalk", "park", "office", "building", "school", "classroom"}
    subject_words = {"teacher", "student", "students", "people", "person", "man", "woman", "child"}

    core = [t for t in tokens if t not in stop_words and len(t) > 2]
    if len(core) < 2:
        return []

    candidates = []

    # Hand-crafted combinations generate more natural phrase chips from prose.
    core_set = set(core)
    boosted_phrases = []
    has_city = "city" in core_set
    has_teacher = "teacher" in core_set
    has_students = bool({"student", "students"} & core_set)

    if has_teacher and has_students:
        boosted_phrases.append("teacher with students")
    if has_city and has_students:
        boosted_phrases.append("students in city")
    if has_city and has_teacher:
        boosted_phrases.append("teacher in city")
    if has_city and (has_teacher or has_students):
        boosted_phrases.append("urban classroom scene")

    local_subjects = [w for w in core if w in subject_words]
    local_locations = [w for w in core if w in location_words]
    if local_subjects and local_locations:
        boosted_phrases.append(f"{local_locations[0]} {local_subjects[0]}")
        if len(local_subjects) > 1:
            boosted_phrases.append(f"{local_subjects[0]} and {local_subjects[1]}")

    for phrase in boosted_phrases:
        if _valid(phrase):
            candidates.append((12, phrase))

    max_n = min(3, len(core))
    for n in range(2, max_n + 1):
        for i in range(0, len(core) - n + 1):
            gram = core[i:i + n]
            phrase = " ".join(gram)
            if not _valid(phrase):
                continue
            score = sum(2 for w in gram if w in concrete_words) + sum(1 for w in gram if len(w) >= 6)
            if any(w in concrete_words for w in gram):
                score += 2
            if any("'" in w for w in gram):
                score -= 2
            candidates.append((score, phrase))

    # Prefer concrete, image-friendly phrases and keep output unique.
    candidates.sort(key=lambda x: (-x[0], x[1]))
    selected = []
    seen = set()
    for _, phrase in candidates:
        if phrase in seen:
            continue
        seen.add(phrase)
        if len(phrase.split()) > 3:
            continue
        selected.append(phrase)
        if len(selected) >= 6:
            break

    return selected[:20]
