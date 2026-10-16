"""The guard in conftest.py has to fire, or it is decoration.

A protection nobody has watched trigger is indistinguishable from one that was
silently disabled by a refactor -- and this one only ever runs when something is
already going wrong, so it would rot unnoticed.
"""

import subprocess

import pytest

from dsterm import serve
from conftest import _names_production


class TestTheProductionSocketIsUnreachableFromTests:
    def test_a_hand_built_argv_is_refused(self):
        # The shape that caused the incident: a command line assembled in a
        # test rather than obtained from tmux_command.
        with pytest.raises(RuntimeError, match="shipped service"):
            subprocess.run(
                ["tmux", "-L", serve.TMUX_SOCKET, "kill-server"],
                capture_output=True,
            )

    def test_starting_a_child_on_it_is_refused(self):
        # Popen is covered separately from run because this is how a Session
        # starts its child, and it is the route that did the damage.
        with pytest.raises(RuntimeError, match="shipped service"):
            subprocess.Popen(["tmux", "-L", serve.TMUX_SOCKET, "new-session"])

    def test_the_default_socket_from_tmux_command_is_refused(self):
        # Omitting socket= is the mistake the guard exists to make impossible,
        # so the argv that omission produces must not be runnable.
        argv = serve.tmux_command("guard", ["true"], 42, 24)
        with pytest.raises(RuntimeError, match="shipped service"):
            subprocess.run(argv, capture_output=True)

    def test_tmux_with_no_socket_flag_is_refused(self):
        # No -L is not "no socket", it is tmux's own default server, which is
        # equally not ours to disturb.
        assert _names_production(["tmux", "ls"])

    def test_a_test_socket_is_allowed(self):
        # The guard must not be so broad that the suite cannot use tmux at all.
        assert not _names_production(["tmux", "-L", "dstermtest", "ls"])
        assert not _names_production(
            serve.tmux_command("t", ["true"], 42, 24, socket="dstermtest")
        )

    def test_a_session_named_like_the_socket_does_not_trip_it(self):
        # Matched on the -L pair, not by searching for the string anywhere, so
        # a window or session that happens to be called dsterm is fine.
        assert not _names_production(
            ["tmux", "-L", "dstermtest", "new-session", "-s", serve.TMUX_SOCKET]
        )

    def test_unrelated_commands_pass_through(self):
        assert not _names_production(["bash", "-c", "echo hi"])
        assert not _names_production([])
