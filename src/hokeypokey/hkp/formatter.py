"""HKP response formatters.

Implements the machine-readable index format (op=index&options=mr) and the
ASCII-armored key response format (op=get&options=mr) as specified in the
HKP draft (draft-shaw-openpgp-hkp-00 / draft-gallagher-openpgp-hkp-09).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import quote

import pgpy

from hokeypokey.models import SourceKey

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RFC 9580 algorithm number mapping
# ---------------------------------------------------------------------------

# pgpy uses string names; map them to the integer values clients expect.
_ALGO_MAP: dict[str, int] = {
    "RSAEncryptOrSign": 1,
    "RSAEncrypt": 2,
    "RSASign": 3,
    "ElGamal": 16,
    "DSA": 17,
    "ECDH": 18,
    "ECDSA": 19,
    "EdDSA": 22,
    "Ed25519": 27,
    "Ed448": 29,
    "X25519": 25,
    "X448": 26,
}


# ---------------------------------------------------------------------------
# Key metadata extraction
# ---------------------------------------------------------------------------


@dataclass
class _UIDInfo:
    uid_string: str
    created: int  # unix timestamp
    expires: int | None  # unix timestamp or None
    flags: str  # "r", "e", "d" or combinations


@dataclass
class _KeyInfo:
    fingerprint: str  # uppercase hex, no 0x
    algo: int  # RFC 9580 algorithm number
    keylen: int | None  # bits, None for ECC
    created: int  # unix timestamp
    expires: int | None  # unix timestamp or None
    flags: str  # "r", "e", "d" or combinations
    uids: list[_UIDInfo] = field(default_factory=list)


def _parse_uid_info(uid: object, key_created_ts: int) -> _UIDInfo:
    """Extract HKP UID metadata from a pgpy PGPUID object."""
    uid_str = getattr(uid, "userid", None) or str(uid)
    uid_created_ts = 0
    uid_expires_ts: int | None = None
    uid_flags = ""

    try:
        selfsig = uid.selfsig  # type: ignore[union-attr]
        if selfsig is not None:
            if selfsig.creation:
                uid_created_ts = int(selfsig.creation.timestamp())
            if selfsig.key_expiration is not None:
                # key_expiration is a timedelta from key creation
                uid_expires_ts = key_created_ts + int(selfsig.key_expiration.total_seconds())
    except Exception:
        pass

    # pgpy 0.6.x may not expose is_revoked
    try:
        if getattr(uid, "is_revoked", False):
            uid_flags += "r"
    except Exception:
        pass

    return _UIDInfo(
        uid_string=uid_str, created=uid_created_ts, expires=uid_expires_ts, flags=uid_flags
    )


def parse_key_metadata(armor: str) -> _KeyInfo | None:
    """Parse an ASCII-armored public key and extract HKP index metadata.

    Args:
        armor: ASCII-armored PGP public key block.

    Returns:
        A :class:`_KeyInfo` with all fields needed for the HKP index response,
        or ``None`` if the key cannot be parsed.
    """
    try:
        key, _ = pgpy.PGPKey.from_blob(armor)
    except Exception as exc:
        logger.warning("Failed to parse PGP key: %s", exc)
        return None

    fp = str(key.fingerprint).replace(" ", "").upper()

    # Algorithm
    algo_name = (
        key.key_algorithm.name if hasattr(key.key_algorithm, "name") else str(key.key_algorithm)
    )
    algo_num = _ALGO_MAP.get(algo_name, 0)

    # Key length (bits) — meaningful for RSA/DSA/ElGamal, not ECC
    try:
        keylen: int | None = key.key_size
    except Exception:
        keylen = None

    # Creation date
    created_dt = key.created
    created_ts = int(created_dt.timestamp()) if created_dt else 0

    # Expiration date
    expires_ts: int | None = None
    if key.expires_at is not None:
        expires_ts = int(key.expires_at.timestamp())

    # Flags
    flags = ""
    if key.is_expired:
        flags += "e"
    # pgpy 0.6.x does not expose is_revoked; check via hasattr for forward compatibility
    if getattr(key, "is_revoked", False):
        flags += "r"

    uid_infos = [_parse_uid_info(uid, created_ts) for uid in key.userids]

    return _KeyInfo(
        fingerprint=fp,
        algo=algo_num,
        keylen=keylen,
        created=created_ts,
        expires=expires_ts,
        flags=flags,
        uids=uid_infos,
    )


# ---------------------------------------------------------------------------
# UID percent-encoding
# ---------------------------------------------------------------------------


def _encode_uid(uid: str) -> str:
    """Percent-encode a UID string for the HKP machine-readable index.

    Per the HKP spec: encode anything not 7-bit safe, the ``:`` character,
    and the ``%`` character.  Other characters may be encoded.

    We use :func:`urllib.parse.quote` with a safe set that excludes ``:``
    and ``%`` but allows common printable ASCII.
    """
    # quote() encodes everything except unreserved chars by default.
    # We want to encode ':' and '%' but keep other printable ASCII safe.
    return quote(uid, safe=" !\"#$&'()*+,-./<=>?@[\\]^_`{|}~")


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------


def format_index_response(keys: list[SourceKey]) -> str:
    """Format a list of keys as an HKP machine-readable index response.

    Args:
        keys: Keys to include in the response.

    Returns:
        A ``text/plain`` string in the HKP machine-readable index format::

            info:1:<count>
            pub:<fingerprint>:<algo>:<keylen>:<created>:<expires>:<flags>
            uid:<encoded_uid>:<created>:<expires>:<flags>
            ...
    """
    lines: list[str] = []
    key_infos: list[_KeyInfo] = []

    for source_key in keys:
        info = parse_key_metadata(source_key.key_armor)
        if info is None:
            # Fall back to fingerprint from SourceKey if parsing fails
            info = _KeyInfo(
                fingerprint=source_key.fingerprint.upper().replace("0X", ""),
                algo=0,
                keylen=None,
                created=0,
                expires=None,
                flags="",
                uids=[],
            )
        key_infos.append(info)

    lines.append(f"info:1:{len(key_infos)}")

    for info in key_infos:
        keylen_str = str(info.keylen) if info.keylen is not None else ""
        expires_str = str(info.expires) if info.expires is not None else ""
        lines.append(
            f"pub:{info.fingerprint}:{info.algo}:{keylen_str}"
            f":{info.created}:{expires_str}:{info.flags}"
        )
        for uid in info.uids:
            encoded = _encode_uid(uid.uid_string)
            uid_expires_str = str(uid.expires) if uid.expires is not None else ""
            lines.append(f"uid:{encoded}:{uid.created}:{uid_expires_str}:{uid.flags}")

    return "\n".join(lines) + "\n"


def format_get_response(keys: list[SourceKey]) -> str:
    """Format a list of keys as an HKP get response (ASCII-armored key blocks).

    Args:
        keys: Keys to include in the response.

    Returns:
        Concatenated ASCII-armored public key blocks, each separated by a
        blank line, suitable for ``Content-Type: application/pgp-keys``.
    """
    blocks = [k.key_armor.strip() for k in keys if k.key_armor.strip()]
    return "\n\n".join(blocks) + "\n"
