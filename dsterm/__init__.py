"""DS Claude Terminal -- the server half: Claude Code on a Nintendo DS.

The DS is display and input only. Everything here runs on the computer: the
PTY, the VT emulation, and the frame diffing. The handheld receives cells and
sends keys, and is given no other channel.

This file deliberately imports nothing. The grid's size is read from the
environment when dsterm.grid is first imported, and `dsterm serve --grid`
sets that environment first -- which only works if importing the package
does not import the grid on its way in. (It also lets `bin/dsterm setup` run
on a fresh clone before the dependencies the grid needs are installed.)
Import what you need from the submodules: dsterm.grid, dsterm.session, ...
"""
