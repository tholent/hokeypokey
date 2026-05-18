"""HKP search term parsing."""

from __future__ import annotations

import re

from hokeypokey.models import ParsedSearch, SearchType

# Matches a valid hex string (case-insensitive)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

# Valid hex lengths after stripping the 0x prefix
_VALID_HEX_LENGTHS = {8, 16, 40}

_LENGTH_TO_TYPE = {
    8: SearchType.SHORT_KEY_ID,
    16: SearchType.LONG_KEY_ID,
    40: SearchType.FINGERPRINT,
}


def parse_search(raw: str) -> ParsedSearch:
    """Parse and classify an HKP ``search`` query parameter value.

    Classification rules (applied in order):

    1. If *raw* starts with ``0x`` or ``0X``: treat as a hex key identifier.
       - Strip the prefix, validate that the remainder is valid hex.
       - 8 hex chars  → :attr:`~hokeypokey.models.SearchType.SHORT_KEY_ID`
       - 16 hex chars → :attr:`~hokeypokey.models.SearchType.LONG_KEY_ID`
       - 40 hex chars → :attr:`~hokeypokey.models.SearchType.FINGERPRINT`
       - Any other length → :exc:`ValueError`
    2. If *raw* contains ``@``: treat as an email address.
       → :attr:`~hokeypokey.models.SearchType.EMAIL`, normalised to lowercase.
    3. Otherwise: free-text UID search.
       → :attr:`~hokeypokey.models.SearchType.TEXT`, kept as-is.

    Args:
        raw: The raw value of the ``search`` query parameter.

    Returns:
        A :class:`~hokeypokey.models.ParsedSearch` with the classified type
        and a normalised form of the search term.

    Raises:
        ValueError: if *raw* starts with ``0x`` but is not a valid hex key ID.
    """
    if not raw:
        raise ValueError("Search term must not be empty.")

    if raw[:2].lower() == "0x":
        hex_part = raw[2:]

        if not hex_part:
            raise ValueError("Key ID must not be empty after '0x' prefix.")

        if not _HEX_RE.match(hex_part):
            raise ValueError(
                f"Key ID {raw!r} contains non-hexadecimal characters."
            )

        if len(hex_part) not in _VALID_HEX_LENGTHS:
            raise ValueError(
                f"Key ID {raw!r} has {len(hex_part)} hex characters; "
                f"expected 8 (short key ID), 16 (long key ID), or 40 (fingerprint)."
            )

        search_type = _LENGTH_TO_TYPE[len(hex_part)]
        normalized = hex_part.upper()
        return ParsedSearch(search_type=search_type, raw=raw, normalized=normalized)

    if "@" in raw:
        return ParsedSearch(
            search_type=SearchType.EMAIL,
            raw=raw,
            normalized=raw.strip().lower(),
        )

    return ParsedSearch(
        search_type=SearchType.TEXT,
        raw=raw,
        normalized=raw,
    )
