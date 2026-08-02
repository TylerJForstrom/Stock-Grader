from __future__ import annotations

import hashlib
from pathlib import Path

_REGISTERED_SPEC_SHA256 = "bd02dccb979fd57cfc059e608be2bfea7bb7e7f3dd25ce34c1364c71371ebe94"
_MEMBERSHIP_SHA256 = {
    "universe_liq250_2026-07-30.txt": (
        "b2005b1ecf890fe7d6c21943f7a4e682872af39d2e22a04210d6d2b94aebdc22"
    ),
    "universe_liq500_2026-07-30.txt": (
        "da00b7d1b937b803f4d5ea3344edc14701e25f62bf15a361b02932147607bc0f"
    ),
    "universe_liq1000_2026-07-30.txt": (
        "9d174b5b73b1f27d708d23951040850c0179b00fa8f6ed08c5dc55d63d67a409"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_registered_m5_universe_artifact_bytes_are_platform_stable() -> None:
    config = Path(__file__).resolve().parent.parent / "config"

    assert _sha256(config / "universe_spec.json") == _REGISTERED_SPEC_SHA256
    for name, expected_sha256 in _MEMBERSHIP_SHA256.items():
        path = config / name
        assert _sha256(path) == expected_sha256
        header = {
            key.strip(): value.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("#") and ":" in line
            for key, value in [line.removeprefix("#").split(":", 1)]
        }
        assert header["spec_sha256"] == _REGISTERED_SPEC_SHA256
