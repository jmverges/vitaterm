"""Properties of the checkout itself.

These are here because they have already broken twice. The repo is edited from
both a Mac and a Windows host writing into WSL over UNC, and that second path
silently drops the executable bit -- so a commit can record mode 100644 for a
script that worked perfectly on the machine it was written on. The next `git
pull` then hands the other machine a file it cannot run.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

EXECUTABLES = ["bin/dsterm", "dsclient/run_tests.sh"]


def git_mode(path: str) -> str:
    out = subprocess.run(["git", "ls-files", "-s", path],
                         cwd=REPO, capture_output=True, text=True, check=True)
    if not out.stdout.strip():
        pytest.fail(f"{path} is not tracked by git")
    return out.stdout.split()[0]


# A "Download ZIP" copy has no .git: these properties are git's to report,
# and there is no git to ask.  Skipped there rather than failed, since the
# copy itself is fine -- it is CI, in a real clone, that checks them.
IN_GIT = (REPO / ".git").exists()
needs_git = pytest.mark.skipif(not IN_GIT, reason="not a git checkout")


@needs_git
class TestExecutableBits:
    @pytest.mark.parametrize("path", EXECUTABLES)
    def test_git_records_the_executable_bit(self, path):
        # Not the filesystem bit -- what git will hand the next machine. A local
        # chmod fixes the symptom and pushes the same broken mode.
        assert git_mode(path) == "100755", (
            f"{path} is committed as non-executable; "
            f"fix with: git update-index --chmod=+x {path}"
        )

    @pytest.mark.parametrize("path", EXECUTABLES)
    def test_the_file_starts_with_a_shebang(self, path):
        # An executable bit on a file with no shebang is worse than neither:
        # the shell runs it with itself, which mostly works until it does not.
        first = (REPO / path).read_bytes().split(b"\n", 1)[0]
        assert first.startswith(b"#!"), f"{path} has no shebang"


@needs_git
class TestTrackedSecretsAndArtifacts:
    """The checkout must never publish card credentials or deployed binaries."""

    @staticmethod
    def tracked_paths() -> list[str]:
        out = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True, check=True)
        return out.stdout.splitlines()

    def test_no_card_config_or_pairing_material_is_tracked(self):
        paths = self.tracked_paths()
        assert not any(path.rsplit("/", 1)[-1] == "dsterm.cfg" for path in paths)
        assert not any(path.rsplit("/", 1)[-1].lower() == "psk" for path in paths)
        assert not any(path.lower().endswith(".key") for path in paths)

    def test_no_deployed_rom_or_nand_image_is_tracked(self):
        paths = self.tracked_paths()
        assert not any(path.lower().endswith(".nds") for path in paths)
        assert not any("nand" in part.lower()
                       for path in paths for part in path.split("/"))


class TestSnippetComposerGate:
    """Pushed snippets reach the PTY only through the composer.

    docs/design/SNIPPETS_PUSH.md: the link has no per-message integrity, so an
    injector can plant text that the user's own thumb later fires. The composer
    gate -- the body is displayed in full and needs a second, deliberate
    commit -- is the entire safety case. A future change that routes a
    snippet straight to send deletes the gate and must fail here, loudly,
    pointing at the doc. Same shape as the protocol's no-side-channels test:
    a test whose job is to force the argument again.
    """

    # The entire API snippets.h may declare. Adding a name is a deliberate
    # act -- it means a new way for snippet content to move, and it has to
    # survive this test and the argument in the doc.
    API = {
        "snippets_init",
        "snippets_parse",
        "snippets_load",
        "snippets_apply_pushed",
        "snippet_load_picked",
    }

    def test_main_touches_snippet_bodies_only_through_the_gate(self):
        main = (REPO / "dsclient/source/main.c").read_text()
        for lineno, line in enumerate(main.splitlines(), 1):
            if "snippets.items" not in line:
                continue
            # The label draw path reads only labels; everything else must go
            # through snippet_load_picked, which is the composer gate.
            assert ".label" in line or "snippet_load_picked" in line, (
                f"main.c:{lineno} reaches into a snippet body outside the "
                f"composer gate -- see docs/design/SNIPPETS_PUSH.md"
            )

    def test_snippets_header_declares_only_the_gate_api(self):
        header = (REPO / "dsclient/source/snippets.h").read_text()
        declared = set(re.findall(r"\b((?:snippets|snippet)_\w+)\s*\(", header))
        assert declared == self.API, (
            f"snippets.h declares {sorted(declared - self.API)} outside the "
            f"whitelist in tests/test_repo.py -- every function here is a way "
            f"for snippet content to move, and it must be argued in "
            f"docs/design/SNIPPETS_PUSH.md first"
        )
