"""The child's environment, and the API key that reaches it.

The key cannot be exported in whichever shell starts the server -- that puts it
in shell history -- and cannot be exported in some other terminal, because the
child inherits the server process's environment and nothing else. So it is read
from a file at spawn time, which is also what makes it survive a child restart.
"""

import os
import stat

import pytest

from dsterm.serve import load_api_key, load_env_file


class TestEnvFile:
    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_env_file(tmp_path / "absent") == {}

    def test_pairs_comments_and_blanks(self, tmp_path):
        p = tmp_path / "env"
        p.write_text(
            "# a gateway\n"
            "\n"
            "ANTHROPIC_BASE_URL=https://gateway.example.com\n"
            "ANTHROPIC_MODEL = example/model-name\n"
        )
        p.chmod(0o600)
        assert load_env_file(p) == {
            "ANTHROPIC_BASE_URL": "https://gateway.example.com",
            "ANTHROPIC_MODEL": "example/model-name",
        }

    def test_surrounding_quotes_are_stripped(self, tmp_path):
        # Typing them is the obvious mistake, and a quoted URL that reached the
        # child would fail as a malformed endpoint rather than as a typo.
        p = tmp_path / "env"
        p.write_text('ANTHROPIC_BASE_URL="https://gateway.example.com"\n')
        p.chmod(0o600)
        assert load_env_file(p)["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"

    def test_a_value_containing_equals_is_kept_whole(self, tmp_path):
        p = tmp_path / "env"
        p.write_text("TOKEN=abc=def=ghi\n")
        p.chmod(0o600)
        assert load_env_file(p)["TOKEN"] == "abc=def=ghi"

    def test_a_world_readable_file_is_refused(self, tmp_path):
        p = tmp_path / "env"
        p.write_text("ANTHROPIC_MODEL=x\n")
        p.chmod(0o644)
        with pytest.raises(PermissionError):
            load_env_file(p)

    def test_a_malformed_line_names_its_number(self, tmp_path):
        p = tmp_path / "env"
        p.write_text("GOOD=1\nthis is not a pair\n")
        p.chmod(0o600)
        with pytest.raises(ValueError, match="2"):
            load_env_file(p)


class TestApiKeyFile:
    def test_missing_file_is_not_an_error(self, tmp_path):
        # No key file means fall back to whatever the child already uses, which
        # is how this ran for its whole life before API keys were an option.
        assert load_api_key(tmp_path / "absent") is None

    def test_key_is_read_and_stripped(self, tmp_path):
        p = tmp_path / "anthropic-key"
        p.write_text("sk-ant-example\n")
        p.chmod(0o600)
        assert load_api_key(p) == "sk-ant-example"

    def test_a_world_readable_key_is_refused(self, tmp_path):
        # Same rule the pairing key follows. A secret readable by other accounts
        # on the box is not a secret.
        p = tmp_path / "anthropic-key"
        p.write_text("sk-ant-example\n")
        p.chmod(0o644)
        with pytest.raises(PermissionError):
            load_api_key(p)

    def test_an_empty_key_is_refused(self, tmp_path):
        # An empty file would otherwise set ANTHROPIC_API_KEY to "" and produce
        # an authentication failure that looks nothing like its cause.
        p = tmp_path / "anthropic-key"
        p.write_text("\n")
        p.chmod(0o600)
        with pytest.raises(ValueError):
            load_api_key(p)


class TestChildEnvironment:
    def test_the_child_receives_the_env_it_was_given(self):
        from dsterm.auth import generate_key
        from dsterm.server import Server

        marker = "ds-terminal-env-check-8b3"
        env = dict(os.environ)
        env["DSTERM_TEST_MARKER"] = marker

        server = Server(
            ["bash", "--noprofile", "--norc", "-i"],
            generate_key(),
            host="127.0.0.1",
            port=0,
            env=env,
            on_event=lambda _: None,
        )
        with server:
            server._session.write(b"echo $DSTERM_TEST_MARKER\r")
            import time

            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                server._session.pump()
                grid, _, _ = server._session.snapshot()
                if marker in grid.to_text():
                    break
                time.sleep(0.05)
            assert marker in server._session.grid.to_text()

    def test_a_restarted_child_receives_it_too(self):
        # The restart path builds a fresh Session; an env dropped there would
        # mean the key works until the first `exit` and silently stops after.
        from dsterm.auth import generate_key
        from dsterm.server import Server

        marker = "ds-terminal-restart-env-2f7"
        env = dict(os.environ)
        env["DSTERM_TEST_MARKER"] = marker

        server = Server(
            ["bash", "--noprofile", "--norc", "-i"],
            generate_key(),
            host="127.0.0.1",
            port=0,
            env=env,
            on_event=lambda _: None,
        )
        with server:
            first = server._session
            server._restart_session()
            assert server._session is not first
            assert server._session.env is env
