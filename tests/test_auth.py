"""Authentication tests.

The threat model is a LAN peer who can read every byte -- WEP and open Wi-Fi are
both in scope for a DS. So the tests care about what an observer learns, not just
whether the happy path works.
"""

import stat

import pytest

from dsterm.auth import (
    KEY_BYTES,
    compute_mac,
    format_for_entry,
    generate_key,
    generate_nonce,
    load_key,
    save_key,
    verify_mac,
)
from dsterm.protocol import MAC_SIZE, NONCE_SIZE, ProtocolError


class TestNonce:
    def test_correct_length(self):
        assert len(generate_nonce()) == NONCE_SIZE

    def test_nonces_are_unique(self):
        # A repeated nonce makes a captured MAC replayable by anyone who
        # watched the earlier handshake.
        seen = {generate_nonce() for _ in range(500)}
        assert len(seen) == 500


class TestMac:
    def test_correct_mac_verifies(self):
        key, nonce = generate_key(), generate_nonce()
        assert verify_mac(key, nonce, compute_mac(key, nonce))

    def test_mac_length(self):
        assert len(compute_mac(generate_key(), generate_nonce())) == MAC_SIZE

    def test_wrong_key_fails(self):
        nonce = generate_nonce()
        assert not verify_mac(generate_key(), nonce, compute_mac(generate_key(), nonce))

    def test_replay_against_new_nonce_fails(self):
        # The core property: a MAC captured off the air is worthless next time.
        key = generate_key()
        captured = compute_mac(key, generate_nonce())
        assert not verify_mac(key, generate_nonce(), captured)

    def test_truncated_mac_rejected(self):
        key, nonce = generate_key(), generate_nonce()
        good = compute_mac(key, nonce)
        assert not verify_mac(key, nonce, good[:-1])

    def test_empty_mac_rejected(self):
        assert not verify_mac(generate_key(), generate_nonce(), b"")

    def test_wrong_size_nonce_rejected(self):
        with pytest.raises(ProtocolError):
            compute_mac(generate_key(), b"short")

    def test_key_is_not_recoverable_from_the_exchange(self):
        # Everything an eavesdropper sees is nonce + mac. Neither contains the key.
        key = generate_key()
        nonce = generate_nonce()
        mac = compute_mac(key, nonce)
        assert key not in nonce + mac


class TestKeyStorage:
    def test_generated_key_length(self):
        assert len(generate_key()) == KEY_BYTES

    def test_keys_differ(self):
        assert generate_key() != generate_key()

    def test_save_and_load(self, tmp_path):
        key = generate_key()
        path = save_key(key, tmp_path / "psk")
        assert load_key(path) == key

    def test_saved_with_owner_only_permissions(self, tmp_path):
        path = save_key(generate_key(), tmp_path / "psk")
        mode = path.stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_world_readable_key_refused(self, tmp_path):
        path = save_key(generate_key(), tmp_path / "psk")
        path.chmod(0o644)
        with pytest.raises(PermissionError):
            load_key(path)

    def test_missing_key_gives_actionable_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="dsterm setup"):
            load_key(tmp_path / "absent")

    def test_garbage_file_rejected(self, tmp_path):
        p = tmp_path / "psk"
        p.write_text("not hex at all")
        p.chmod(0o600)
        with pytest.raises(ValueError):
            load_key(p)

    def test_short_key_rejected(self, tmp_path):
        p = tmp_path / "psk"
        p.write_text("aabb")
        p.chmod(0o600)
        with pytest.raises(ValueError, match="bytes"):
            load_key(p)


class TestEntryFormat:
    def test_grouped_for_hand_entry(self):
        formatted = format_for_entry(bytes(range(KEY_BYTES)))
        assert all(len(group) == 4 for group in formatted.split(" "))

    def test_round_trips_when_spaces_removed(self):
        key = generate_key()
        assert bytes.fromhex(format_for_entry(key).replace(" ", "")) == key
