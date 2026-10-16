"""Discovered actions: what becomes a button, and what deliberately does not.

The interesting tests here are the refusals. A discovered action composes text
that can reach a shell, and the defence is not escaping -- it is declining to
build a command out of anything that is not plainly a name. These tests are
that claim, written down.
"""

from __future__ import annotations

import json

import pytest

from dsterm import actions
from dsterm.protocol import PROJECT_LABEL_MAX, PROJECT_VALUE_MAX


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return tmp_path


class TestNpmScripts:
    def test_scripts_become_buttons(self, tmp_path):
        write(tmp_path, "package.json",
              json.dumps({"scripts": {"test": "jest", "build": "tsc"}}))
        found = dict(actions.discover(str(tmp_path)))
        assert found["test"] == "!npm run test"
        assert found["build"] == "!npm run build"

    @pytest.mark.parametrize("name", [
        "test; rm -rf ~",      # the obvious one
        "build && curl x|sh",
        "a`whoami`",
        "a$(id)",
        "a b",                 # a space is enough to change what it means
        "'quoted'",
        '"quoted"',
        "-leading-dash",       # would read as an option, not a script
        "",
    ])
    def test_a_name_that_is_not_a_name_is_skipped(self, tmp_path, name):
        # Not escaped, not quoted, not sanitised into something else: skipped.
        # A button whose text needs escaping to be safe is a button nobody can
        # check by reading, and reading it is the entire gate.
        write(tmp_path, "package.json",
              json.dumps({"scripts": {name: "x", "ok": "y"}}))
        found = dict(actions.discover(str(tmp_path)))
        assert list(found) == ["ok"]

    def test_malformed_package_json_yields_nothing(self, tmp_path):
        write(tmp_path, "package.json", "{not json at all")
        assert actions.discover(str(tmp_path)) == ()

    def test_package_json_that_is_not_an_object(self, tmp_path):
        write(tmp_path, "package.json", "[1, 2, 3]")
        assert actions.discover(str(tmp_path)) == ()

    def test_scripts_that_is_not_an_object(self, tmp_path):
        write(tmp_path, "package.json", json.dumps({"scripts": "nope"}))
        assert actions.discover(str(tmp_path)) == ()

    def test_an_enormous_file_is_not_parsed(self, tmp_path):
        write(tmp_path, "package.json",
              json.dumps({"scripts": {"test": "jest"}, "pad": "x" * 400_000}))
        assert actions.discover(str(tmp_path)) == ()


class TestMakefile:
    def test_targets_become_buttons(self, tmp_path):
        write(tmp_path, "Makefile", "all: build\n\tgcc x\nbuild:\n\tgcc y\n")
        found = dict(actions.discover(str(tmp_path)))
        assert found["all"] == "!make all"
        assert found["build"] == "!make build"

    def test_recipe_lines_are_not_targets(self, tmp_path):
        # A recipe line begins with a tab. Without that check, a shell line
        # inside a recipe that happens to contain a colon becomes a button.
        write(tmp_path, "Makefile", "all:\n\tssh host:/path something\n")
        assert dict(actions.discover(str(tmp_path))) == {"all": "!make all"}

    def test_special_targets_are_skipped(self, tmp_path):
        write(tmp_path, "Makefile", ".PHONY: all\n.SILENT:\nall:\n\tgcc x\n")
        assert list(dict(actions.discover(str(tmp_path)))) == ["all"]

    def test_variable_assignment_is_not_a_target(self, tmp_path):
        write(tmp_path, "Makefile", "CC := gcc\nFLAGS ?= -O2\nall:\n\tgcc x\n")
        assert list(dict(actions.discover(str(tmp_path)))) == ["all"]

    def test_duplicate_targets_appear_once(self, tmp_path):
        write(tmp_path, "Makefile", "all:\n\tx\nall:\n\ty\n")
        assert list(dict(actions.discover(str(tmp_path)))) == ["all"]


class TestPython:
    def test_a_marked_project_with_tests_gets_one_button(self, tmp_path):
        (tmp_path / "tests").mkdir()
        write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
        assert ("pytest", "!pytest -q") in actions.discover(str(tmp_path))

    def test_tests_alone_is_not_enough(self, tmp_path):
        # A directory called tests/ in something that is not a Python project
        # should not offer to run pytest in it.
        (tmp_path / "tests").mkdir()
        assert actions.discover(str(tmp_path)) == ()


class TestBudgets:
    def test_nothing_here_is_a_project(self, tmp_path):
        assert actions.discover(str(tmp_path)) == ()

    def test_a_missing_directory_is_silence(self, tmp_path):
        assert actions.discover(str(tmp_path / "nope")) == ()

    def test_the_list_is_capped(self, tmp_path):
        write(tmp_path, "package.json",
              json.dumps({"scripts": {f"s{i}": "x" for i in range(50)}}))
        assert len(actions.discover(str(tmp_path))) == actions.MAX_ACTIONS

    def test_everything_fits_the_wire(self, tmp_path):
        long_name = "a" * 60
        write(tmp_path, "package.json",
              json.dumps({"scripts": {long_name: "x", "ok": "y"}}))
        for label, body in actions.discover(str(tmp_path)):
            assert 0 < len(label) <= PROJECT_LABEL_MAX
            assert 0 < len(body) <= PROJECT_VALUE_MAX

    def test_a_name_too_long_to_compose_is_dropped_not_truncated(self, tmp_path):
        # Truncating the *body* would produce a command that runs something
        # other than the button says. Dropping it is the only safe answer.
        long_name = "a" * (PROJECT_VALUE_MAX + 10)
        write(tmp_path, "package.json",
              json.dumps({"scripts": {long_name: "x", "ok": "y"}}))
        bodies = [body for _, body in actions.discover(str(tmp_path))]
        assert bodies == ["!npm run ok"]

    def test_the_same_command_is_offered_once(self, tmp_path):
        write(tmp_path, "package.json", json.dumps({"scripts": {"all": "x"}}))
        write(tmp_path, "Makefile", "all:\n\tx\n")
        bodies = [body for _, body in actions.discover(str(tmp_path))]
        assert bodies == ["!npm run all", "!make all"]
        assert len(bodies) == len(set(bodies))


def test_your_own_buttons_come_first(tmp_path):
    """A `.dsterm-buttons` file in the project and one in the config directory
    are read, in that order, before anything discovered; lines that a button
    could not show honestly are skipped, not clipped."""
    from dsterm import actions, protocol

    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (tmp_path / ".dsterm-buttons").write_text(
        "# mine\n"
        "deploy = ./deploy.sh staging\n"
        "push=git push\n"
        "no equals sign here\n"
        "tab\there = nope\n"
        f"{'L' * (protocol.PROJECT_LABEL_MAX + 1)} = too long a label\n"
    )
    user = tmp_path / "buttons"
    user.write_text("status = git status -sb\npush = git push\n")

    got = actions.discover(str(tmp_path), user_file=user)
    assert got[:3] == (("deploy", "./deploy.sh staging"), ("push", "git push"),
                       ("status", "git status -sb"))
    # The duplicate `git push` from the second file is dropped, and the
    # project's discovered script still follows.
    assert ("test", "!npm run test") in got
    assert sum(1 for _, body in got if body == "git push") == 1


def test_missing_button_files_cost_nothing(tmp_path):
    from dsterm import actions

    assert actions.discover(str(tmp_path), user_file=tmp_path / "absent") == ()
