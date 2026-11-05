import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from humpback.cli import main
from humpback.domain.models import Venue
from humpback.signer import keystore
from humpback.signer.backends import load_private_key

KEY_HEX = "0123456789012345678901234567890123456789012345678901234567890123"
KEY = bytes.fromhex(KEY_HEX)
PASS = "correct horse battery"
FAST = 2**10  # small scrypt cost so the tests run quickly


def make(tmp_path: Path, venue: Venue = Venue.HYPERLIQUID, name: str = "k.json") -> Path:
    path = tmp_path / "keys" / name
    keystore.create(path, KEY, PASS, venue=venue, label="test", scrypt_n=FAST)
    return path


def test_round_trip_and_file_never_contains_the_key(tmp_path: Path) -> None:
    path = make(tmp_path)
    text = path.read_text()
    assert KEY_HEX not in text and KEY_HEX.upper() not in text
    key, info = keystore.load(path, PASS)
    assert key == KEY
    assert info.venue is Venue.HYPERLIQUID and info.fingerprint == keystore.fingerprint(KEY)
    assert load_private_key(path, PASS) == KEY


def test_permissions_are_private(tmp_path: Path) -> None:
    path = make(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    os.chmod(path, 0o644)
    with pytest.raises(keystore.KeystoreError, match="readable by other users"):
        keystore.load(path, PASS)


def test_wrong_passphrase_and_tampering_are_detected(tmp_path: Path) -> None:
    path = make(tmp_path)
    with pytest.raises(keystore.KeystoreError, match="wrong passphrase"):
        keystore.load(path, "wrong passphrase!!")
    doc = json.loads(path.read_text())
    doc["venue"] = "aster"  # swap metadata: authenticated data no longer matches
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    with pytest.raises(keystore.KeystoreError, match="tampered"):
        keystore.load(path, PASS)


def test_never_overwrites_and_rejects_weak_input(tmp_path: Path) -> None:
    path = make(tmp_path)
    with pytest.raises(keystore.KeystoreError, match="never overwritten"):
        keystore.create(path, KEY, PASS, venue=Venue.HYPERLIQUID, label="x", scrypt_n=FAST)
    with pytest.raises(keystore.KeystoreError, match="at least"):
        keystore.create(tmp_path / "b.json", KEY, "short", venue=Venue.ASTER, label="x")
    with pytest.raises(keystore.KeystoreError, match="32 bytes"):
        keystore.parse_private_key("0x1234", Venue.HYPERLIQUID)
    with pytest.raises(keystore.KeystoreError, match="hexadecimal"):
        keystore.parse_private_key("zz" * 32, Venue.ASTER)
    assert keystore.parse_private_key("0x" + KEY_HEX, Venue.ASTER) == KEY
    lighter_key = "ab" * 40
    assert keystore.parse_private_key(lighter_key, Venue.LIGHTER) == bytes.fromhex(lighter_key)


def test_cli_create_info_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(keystore, "DEFAULT_SCRYPT_N", FAST)
    monkeypatch.setattr(keystore.create, "__kwdefaults__", {"scrypt_n": FAST})
    answers = iter([KEY_HEX, PASS, PASS])
    out = tmp_path / "keys" / "hl.keystore.json"
    code = main(
        [
            "keystore",
            "create",
            "--venue",
            "hyperliquid",
            "--label",
            "hl-testnet",
            "--out",
            str(out),
        ],
        prompt=lambda _: next(answers),
    )
    assert code == 0 and out.exists()
    printed = capsys.readouterr().out
    assert KEY_HEX not in printed and keystore.fingerprint(KEY) in printed

    assert main(["keystore", "info", str(out)]) == 0
    assert main(["keystore", "check", str(out)], prompt=lambda _: PASS) == 0
    assert main(["keystore", "check", str(out)], prompt=lambda _: "nope nope nope") == 2


def test_cli_refuses_mismatched_passphrases(tmp_path: Path) -> None:
    answers = iter([KEY_HEX, PASS, PASS + "x"])
    out = tmp_path / "keys" / "x.json"
    code = main(
        ["keystore", "create", "--venue", "aster", "--label", "a", "--out", str(out)],
        prompt=lambda _: next(answers),
    )
    assert code == 2 and not out.exists()
