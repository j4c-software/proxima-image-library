"""AI-powered alt text generation using Claude."""

import base64
import io
from pathlib import Path
from typing import Optional, Union
from anthropic import Anthropic
from PIL import Image as PILImage

# Maps file extensions to MIME types for the Claude API
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class AltTextGenerator:
    """Generate alt text for images using Claude's vision capabilities.

    Accepts either a local file path (str) or raw image bytes.
    When passing bytes, also pass the filename so the media type can be derived.
    """

    def __init__(self):
        self.client = Anthropic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    _API_MAX_BYTES = 5 * 1024 * 1024  # 5 MB Claude API limit
    _API_MAX_SIDE = 1568              # Claude's recommended max dimension

    def _shrink_for_api(self, data: bytes) -> bytes:
        """Resize image bytes so they fit within the Claude API size limit."""
        if len(data) <= self._API_MAX_BYTES:
            return data
        with PILImage.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            w, h = img.size
            scale = min(self._API_MAX_SIDE / max(w, h), 1.0)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            result = buf.getvalue()
            # If still too large, reduce quality further
            if len(result) > self._API_MAX_BYTES:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=60)
                result = buf.getvalue()
        return result

    def _encode(self, source: Union[str, bytes], filename: str = "") -> tuple[str, str]:
        """Return (base64_data, media_type) from a path or bytes."""
        if isinstance(source, bytes):
            data = self._shrink_for_api(source)
            ext = Path(filename).suffix.lower() if filename else ".jpg"
            media_type = "image/jpeg" if len(data) != len(source) else _MEDIA_TYPES.get(ext, "image/jpeg")
            return base64.standard_b64encode(data).decode("utf-8"), media_type

        # Local file path
        path = Path(source)
        with open(path, "rb") as f:
            raw = f.read()
        data = self._shrink_for_api(raw)
        media_type = "image/jpeg" if len(data) != len(raw) else _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
        return base64.standard_b64encode(data).decode("utf-8"), media_type

    def _vision_message(self, source: Union[str, bytes], filename: str, prompt: str) -> str:
        """Send a vision request to Claude and return the response text."""
        image_data, media_type = self._encode(source, filename)
        message = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return message.content[0].text.strip()

    # ------------------------------------------------------------------
    # Legacy helpers (kept for backwards compatibility)
    # ------------------------------------------------------------------

    def encode_image(self, image_path: str) -> str:
        data, _ = self._encode(image_path)
        return data

    def get_image_media_type(self, image_path: str) -> str:
        return _MEDIA_TYPES.get(Path(image_path).suffix.lower(), "image/jpeg")

    def generate_alt_text(
        self,
        source: Union[str, bytes],
        context: Optional[str] = None,
        max_length: int = 125,
        filename: str = "",
    ) -> Optional[str]:
        """Generate alt text for an image using Claude.

        Args:
            source:    Local file path (str) OR raw image bytes
            context:   Optional context about the image
            max_length: Max characters for alt text (default 125)
            filename:  Original filename — required when source is bytes
                       so the media type can be derived from the extension
        """
        try:
            if isinstance(source, str):
                if not Path(source).exists():
                    print(f"Image file not found: {source}")
                    return None
                filename = filename or source

            prompt = (
                f"Analyze this image and generate a concise, descriptive alt text for web accessibility.\n"
                f"The alt text should:\n"
                f"- Be descriptive but concise (max {max_length} characters)\n"
                f"- Describe what's in the image without being overly detailed\n"
                f"- Be suitable for screen readers\n"
                f"- Not start with 'Image of' or 'Picture of'\n"
            )
            if context:
                prompt += f"\nContext: {context}\n"
            prompt += "\nGenerate ONLY the alt text, nothing else."

            return self._vision_message(source, filename, prompt)

        except Exception as e:
            print(f"Error generating alt text for {filename or source}: {e}")
            return None

    def generate_tags(
        self,
        source: Union[str, bytes],
        context: Optional[str] = None,
        filename: str = "",
    ) -> Optional[str]:
        """Generate comma-separated tags for an image using Claude.

        Args:
            source:   Local file path (str) OR raw image bytes
            context:  Optional context about the image
            filename: Original filename — required when source is bytes
        """
        try:
            if isinstance(source, str):
                if not Path(source).exists():
                    return None
                filename = filename or source

            # Load current vocabulary from TagLibrary
            from src.tag_library import TagLibrary
            allowed = ", ".join(TagLibrary.instance().get_flat())

            prompt = f"""Analyze this image and select 2-5 tags from the following approved list.

Approved tags:
{allowed}

Rules:
- Prefer tags from the approved list above
- If an important aspect of the image has no good match in the list, you may suggest \
up to 2 new tags by prefixing them with '?' (e.g. ?rooftop-garden)
- Suggested tags must be lowercase and hyphenated (no spaces or special characters)
- No punctuation other than commas and the '?' suggestion prefix"""

            if context:
                prompt += f"\nContext: {context}"
            prompt += "\n\nReturn exactly one line: TAGS: <comma-separated list>. Do not include any other text, reasoning, or lines."

            raw = self._vision_message(source, filename, prompt) or ""
            for line in raw.splitlines():
                line = line.strip()
                if line.upper().startswith("TAGS:"):
                    return line[len("TAGS:"):].strip() or None
            # Fallback for legacy/malformed responses: use the last non-empty line only.
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            return lines[-1] if lines else None

        except Exception as e:
            print(f"Error generating tags for {filename or source}: {e}")
            return None

    # Keep old signature working for any callers that pass image_path positionally
    # (batch_generate_alt_text etc.)
    def _generate_tags_legacy(
        self,
        image_path: str,
        context: Optional[str] = None,
    ) -> Optional[str]:
        """Legacy wrapper — remove once all callers updated."""
        try:
            if not Path(image_path).exists():
                return None

            return self.generate_tags(image_path, context)

        except Exception as e:
            print(f"Error generating tags for {image_path}: {e}")
            return None

    def generate_category(
        self,
        source: Union[str, bytes],
        categories: list,
        filename: str = "",
    ) -> Optional[str]:
        """Determine the best category for an image from an approved list.

        Args:
            source:     Local file path (str) OR raw image bytes
            categories: List of valid category names to choose from
            filename:   Original filename — required when source is bytes
        """
        try:
            if isinstance(source, str):
                if not Path(source).exists():
                    return None
                filename = filename or source

            category_list = "\n".join(f"- {c}" for c in categories)
            prompt = f"""Look at this image and select the single most appropriate category from this list:

{category_list}

Rules:
- Return ONLY the category name, exactly as written above
- No explanation, no punctuation, nothing else"""

            result = self._vision_message(source, filename, prompt)
            # Validate response is one of the allowed categories
            for cat in categories:
                if cat.lower() == result.strip().lower():
                    return cat
            # Fallback: return first word match
            for cat in categories:
                if cat.lower() in result.lower():
                    return cat
            return None

        except Exception as e:
            print(f"Error determining category for {filename or source}: {e}")
            return None

    def generate_all(
        self,
        source: Union[str, bytes],
        categories: list,
        filename: str = "",
        context: Optional[str] = None,
        max_alt_length: int = 125,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Generate category, alt text, and tags in a single Claude API call.

        Returns (category, alt_text, tags). Any field may be None on failure.
        Encodes the image once and sends it once — ~3× faster than calling the
        three methods individually.
        """
        try:
            if isinstance(source, str):
                if not Path(source).exists():
                    return None, None, None
                filename = filename or source

            from src.tag_library import TagLibrary
            allowed_tags = ", ".join(TagLibrary.instance().get_flat())
            category_list = "\n".join(f"- {c}" for c in categories)

            prompt = f"""Analyze this image and return exactly three labeled lines:

CATEGORY: <choose the single best option from this list>
{category_list}

ALT: <concise descriptive alt text, max {max_alt_length} characters, suitable for screen readers, do not start with "Image of" or "Picture of">

TAGS: <2-5 comma-separated tags chosen from the approved list below; if an important aspect has no good match you may suggest up to 2 new tags prefixed with '?' (e.g. ?rooftop-garden); suggested tags must be lowercase and hyphenated>
Approved tags: {allowed_tags}
"""
            if context:
                prompt += f"\nContext: {context}"
            prompt += "\n\nReturn ONLY the three labeled lines (CATEGORY:, ALT:, TAGS:), nothing else."

            image_data, media_type = self._encode(source, filename)
            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = message.content[0].text.strip()

            category_out: Optional[str] = None
            alt_out: Optional[str] = None
            tags_out: Optional[str] = None

            for line in raw.splitlines():
                line = line.strip()
                if line.upper().startswith("CATEGORY:"):
                    val = line[len("CATEGORY:"):].strip()
                    for cat in categories:
                        if cat.lower() == val.lower():
                            category_out = cat
                            break
                    if category_out is None:
                        for cat in categories:
                            if cat.lower() in val.lower():
                                category_out = cat
                                break
                elif line.upper().startswith("ALT:"):
                    alt_out = line[len("ALT:"):].strip()[:max_alt_length] or None
                elif line.upper().startswith("TAGS:"):
                    tags_out = line[len("TAGS:"):].strip() or None

            return category_out, alt_out, tags_out

        except Exception as e:
            print(f"Error in generate_all for {filename or source}: {e}")
            return None, None, None

    def generate_alt_and_tags(
        self,
        source: Union[str, bytes],
        filename: str = "",
        context: Optional[str] = None,
        max_alt_length: int = 125,
    ) -> tuple[Optional[str], Optional[str]]:
        """Generate alt text and tags in a single Claude API call (no category).

        Returns (alt_text, tags). Used by the retag path where category is not needed.
        """
        try:
            if isinstance(source, str):
                if not Path(source).exists():
                    return None, None
                filename = filename or source

            from src.tag_library import TagLibrary
            allowed_tags = ", ".join(TagLibrary.instance().get_flat())

            prompt = f"""Analyze this image and return exactly two labeled lines:

ALT: <concise descriptive alt text, max {max_alt_length} characters, suitable for screen readers, do not start with "Image of" or "Picture of">

TAGS: <2-5 comma-separated tags chosen from the approved list below; if an important aspect has no good match you may suggest up to 2 new tags prefixed with '?' (e.g. ?rooftop-garden); suggested tags must be lowercase and hyphenated>
Approved tags: {allowed_tags}
"""
            if context:
                prompt += f"\nContext: {context}"
            prompt += "\n\nReturn ONLY the two labeled lines (ALT:, TAGS:), nothing else."

            image_data, media_type = self._encode(source, filename)
            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = message.content[0].text.strip()

            alt_out: Optional[str] = None
            tags_out: Optional[str] = None

            for line in raw.splitlines():
                line = line.strip()
                if line.upper().startswith("ALT:"):
                    alt_out = line[len("ALT:"):].strip()[:max_alt_length] or None
                elif line.upper().startswith("TAGS:"):
                    tags_out = line[len("TAGS:"):].strip() or None

            return alt_out, tags_out

        except Exception as e:
            print(f"Error in generate_alt_and_tags for {filename or source}: {e}")
            return None, None

    def batch_generate_alt_text(
        self,
        image_paths: list,
        context: Optional[str] = None,
    ) -> dict:
        """Generate alt text for multiple images.

        Args:
            image_paths: List of image file paths
            context: Optional context about the images

        Returns:
            Dictionary mapping image paths to generated alt text
        """
        results = {}
        for i, image_path in enumerate(image_paths, 1):
            print(f"Processing image {i}/{len(image_paths)}: {Path(image_path).name}")
            alt_text = self.generate_alt_text(image_path, context)
            results[image_path] = alt_text
        return results
