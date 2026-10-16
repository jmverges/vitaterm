"""The camera: photos the card sends, the live view it streams, and the feed
socket and popups that show that view on this computer.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import os
import queue
import selectors
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from . import camera as dscamera
from .protocol import (
    Message,
    MsgType,
    ProtocolError,
    ACK_PHOTO,
    ACK_PREVIEW,
    CAMERA_OFF,
    CAMERA_INNER,
    PHOTO_SAVED_BAD_NAME,
    PHOTO_SAVED_BUSY,
    PHOTO_SAVED_DISK,
    PHOTO_SAVED_INTEGRITY,
    PHOTO_SAVED_OK,
    PHOTO_SAVED_TOO_SOON,
    PREVIEW_FLAG_STILL,
    PREVIEW_H,
    PREVIEW_W,
    decode_camera,
    decode_camera_frame,
    decode_photo_chunk,
    decode_photo_discard,
    decode_photo_end,
    encode_camera_ack,
    encode_photo_saved,
)
from .connection import (
    ClientState,
    log,
)


@dataclass(frozen=True)
class CameraView:
    """What the handheld's camera is showing, server-wide. Immutable so the
    dashboard thread and feed viewers can hold a reference mid-swap."""
    on: bool = False
    camera: int = CAMERA_OFF
    still: bool = False
    seq: int = 0
    w: int = PREVIEW_W
    h: int = PREVIEW_H
    rgb: bytes = b""
    at: float = 0.0


@dataclass
class FeedViewer:
    """One process holding the camera feed socket. Server-to-viewer only;
    anything it sends is read and dropped. ``pending`` is the tail of a
    message send() only partly accepted -- while it is non-empty newer
    frames are skipped for this viewer, so the queue can never grow."""
    sock: socket.socket
    pending: bytearray = field(default_factory=bytearray)
    pending_since: float = 0.0


# Preview frames stored per second, at most. The card sends ~4; the cap is
# for a card that decides to send more -- ACKed regardless, so it is not a
# refusal the sender can see, only storage we decline to keep.
CAMERA_STORE_MIN_INTERVAL = 0.1

# A second saved photo inside this window is TOO_SOON, per connection.
PHOTO_MIN_INTERVAL = 3.0

# A feed viewer is closed when a pending tail has been stuck for this long,
# or when the tail exceeds this many bytes (a whole 128x96 RGB frame is 36864).
FEED_STUCK_SECONDS = 5.0
FEED_PENDING_LIMIT = 256 * 1024
FEED_MAX_VIEWERS = 8

# The feed socket lives beside the pairing key, under run/. UNIX_PATH_MAX is
# 104 here; past 100 the bind would fail anyway, so a long config_dir just
# leaves the feed off rather than failing half way.
FEED_SOCK_MAX = 100


class CameraMixin:
    """Owns `camera_view`, the feed socket and its viewers, the popups, and the
    photo worker (_init_camera); per connection, ClientState.camera."""

    # -- camera --------------------------------------------------------------

    def _handle_camera(self, c: ClientState, msg: Message) -> None:
        if msg.type is MsgType.CAMERA:
            try:
                state = decode_camera(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad camera state: {exc}")
                return
            if state == c.camera.state:
                return
            c.camera.state = state
            if state == CAMERA_OFF:
                if c.camera.assembler is not None:
                    c.camera.assembler.reset()
                if c.camera.saving:
                    c.camera.cancelled = True
                self.camera_view = CameraView(at=time.time())
                self._emit("camera off")
            else:
                name = "inner" if state == CAMERA_INNER else "outer"
                self.camera_view = CameraView(on=True, camera=state,
                                              at=time.time())
                self._emit(f"camera on ({name})")
            self._feed_broadcast()
            return

        if msg.type is MsgType.CAMERA_FRAME:
            if c.camera.state == CAMERA_OFF:
                self._drop(c, "camera frame while the camera is off")
                return
            try:
                fmt, flags, seq, pixels = decode_camera_frame(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad camera frame: {exc}")
                return
            # The ACK is immediate, like PONG: it is the pacer's heartbeat,
            # not an acknowledgement of storage.
            self._send_to(c, encode_camera_ack(ACK_PREVIEW, seq))
            now = time.monotonic()
            if now - c.camera.last_preview_stored_at < CAMERA_STORE_MIN_INTERVAL:
                return
            c.camera.last_preview_stored_at = now
            w, h, rgb = dscamera.expand_view(fmt, pixels)
            self.camera_view = CameraView(
                on=True, camera=c.camera.state,
                still=bool(flags & PREVIEW_FLAG_STILL), seq=seq,
                w=w, h=h, rgb=rgb, at=time.time())
            self._feed_broadcast()
            return

        if msg.type is MsgType.PHOTO_CHUNK:
            try:
                offset, data = decode_photo_chunk(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad photo chunk: {exc}")
                return
            if c.camera.assembler is None:
                c.camera.assembler = dscamera.PhotoAssembler()
            try:
                received = c.camera.assembler.feed(offset, data)
            except ProtocolError as exc:
                self._drop(c, f"photo chunk: {exc}")
                return
            self._send_to(c, encode_camera_ack(ACK_PHOTO, received))
            return

        if msg.type is MsgType.PHOTO_END:
            try:
                fmt, which, crc = decode_photo_end(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad photo end: {exc}")
                return
            asm = c.camera.assembler or dscamera.PhotoAssembler()
            try:
                data = asm.finish(fmt, crc)
            except ProtocolError as exc:
                self._drop(c, f"photo end: {exc}")
                return
            asm.reset()
            now = time.monotonic()
            if now - c.camera.last_photo_at < PHOTO_MIN_INTERVAL:
                self._send_to(c, encode_photo_saved(PHOTO_SAVED_TOO_SOON, 0, ""))
                return
            if self._photo_busy:
                self._send_to(c, encode_photo_saved(PHOTO_SAVED_BUSY, 0, ""))
                return
            if data is None:
                self._send_to(c, encode_photo_saved(PHOTO_SAVED_INTEGRITY, 0, ""))
                return
            self._start_photo_save(c, fmt, which, data)
            return

        if msg.type is MsgType.PHOTO_DISCARD:
            try:
                photo_id = decode_photo_discard(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad photo discard: {exc}")
                return
            path = c.camera.photo_ids.pop(photo_id, None)
            if path is None:
                # Not an error worth ending the link over: ids are
                # connection-scoped, so a foreign or stale id simply names
                # nothing here.
                self._emit(f"client {c.addr} discarded unknown photo {photo_id}")
                return
            # Only through a store this run saved into, which was checked.
            store = self._stores.get(path.parent)
            if store is not None and store.delete(path):
                self._emit("picture discarded")
            return

    def _camera_can_save(self) -> bool:
        """READY's can_save: a string check only -- no subprocess, nothing
        created. In project mode the wire prefix '.dsterm/photos/' is ASCII
        by construction, so this is always True; --photo-dir answers with
        the prefix check alone. Anything real is learned by the worker at
        save time and surfaces there as DISK or BAD_NAME."""
        if self.photo_dir is not None:
            return dscamera.PhotoStore(
                self.photo_dir, self.photo_keep).ascii_ok()
        return True

    def _photo_project(self) -> Path:
        """The project the picture belongs to: the tmux pane's cwd, else this
        server's. Called at save time on the worker, never on the loop --
        display-message can take seconds."""
        target = self._tmux_target()
        if target is not None:
            sock, name = target
            try:
                out = subprocess.run(
                    ["tmux", "-L", sock, "display-message", "-p",
                     "-t", f"={name}", "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                out = None
            if out is not None and out.returncode == 0 and out.stdout.strip():
                return Path(out.stdout.strip())
        return Path(self.cwd or os.getcwd())

    def _start_photo_save(self, c: ClientState, fmt: int, which: int,
                        data: bytes) -> None:
        """Hand the picture to the worker: convert, PNG, save, prune, git
        exclude. Nothing here touches the selector thread's data but the
        result queue."""
        self._photo_busy = True
        c.camera.saving = True
        c.camera.cancelled = False
        threading.Thread(target=self._save_photo,
                         args=(c, fmt, which, data), daemon=True).start()

    def _save_photo(self, c: ClientState, fmt: int, which: int,
                    data: bytes) -> None:
        try:
            project = self._photo_project()
            if self.photo_dir is not None:
                store = dscamera.PhotoStore(self.photo_dir, self.photo_keep)
            else:
                store = dscamera.PhotoStore.for_project(project, self.photo_keep)
            store.ensure()
            w, h = {1: (640, 480), 2: (320, 240)}[fmt]
            rgb = dscamera.planar420_to_rgb(
                data, w, h, mirror=dscamera.MIRROR.get(which, False))
            png = dscamera.png_bytes(rgb, w, h)
            saved = store.save(png)
            if self.photo_dir is None:
                dscamera.git_exclude_dsterm(
                    project, subprocess.run, on_log=log.warning)
            wire = dscamera.wire_path(saved, project)
            store.prune()
            self._photo_results.put(
                (c, PHOTO_SAVED_OK if wire is not None
                 else PHOTO_SAVED_BAD_NAME, saved, wire, len(png), store))
        except Exception as exc:  # noqa: BLE001 -- the card hears a status
            # The detail names real paths, so it stays in the log: the event
            # feed reaches the dashboard, which only ever hears a kind.
            log.warning("picture save failed: %r", exc)
            self._photo_results.put(
                (c, PHOTO_SAVED_DISK, None, None, 0, None))

    def _collect_photo_results(self) -> None:
        kinds = {PHOTO_SAVED_BUSY: "busy",
                 PHOTO_SAVED_INTEGRITY: "integrity",
                 PHOTO_SAVED_DISK: "disk",
                 PHOTO_SAVED_TOO_SOON: "too soon",
                 PHOTO_SAVED_BAD_NAME: "name"}
        while True:
            try:
                c, status, path, wire, size, store = \
                    self._photo_results.get_nowait()
            except queue.Empty:
                return
            self._photo_busy = False
            # The store crosses from the worker here, on the loop thread --
            # _stores is read by discard and retention, both loop-side.
            if store is not None:
                self._stores[store.dir] = store
            c.camera.saving = False
            gone = c not in self._clients or not c.authed
            if status == PHOTO_SAVED_OK and (gone or c.camera.cancelled):
                # The camera closed, or the card left, while this was being
                # saved: no composer will ever name it, so it is not kept.
                c.camera.cancelled = False
                if store is not None and path is not None and store.delete(path):
                    self._emit("picture discarded: the camera closed before "
                               "it was saved")
                continue
            if status == PHOTO_SAVED_OK:
                self._emit(f"picture saved ({size // 1024} KB)")
            else:
                self._emit(f"picture not saved: {kinds.get(status, status)}")
            if gone:
                continue
            if status == PHOTO_SAVED_OK:
                c.camera.last_photo_at = time.monotonic()
                photo_id = self._next_photo_id(c)
                c.camera.photo_ids[photo_id] = path
                self._send_to(c, encode_photo_saved(status, photo_id, wire))
            else:
                self._send_to(c, encode_photo_saved(status, 0, ""))

    @staticmethod
    def _next_photo_id(c: ClientState) -> int:
        """1..65535, wrapping, skipping ids still mapped to a file."""
        for _ in range(0xFFFF):
            c.camera.next_photo_id = c.camera.next_photo_id % 0xFFFF + 1
            if c.camera.next_photo_id not in c.camera.photo_ids:
                return c.camera.next_photo_id
        raise ProtocolError("no free photo id")  # 65535 pictures live: refuse

    def _prune_photo_dirs(self, startup: bool = False) -> None:
        """Retention: every folder saved into this run, plus -- at start --
        the cwd's .dsterm/photos if it already exists (never created here)."""
        dirs = set(self._stores)
        if startup:
            project = Path(self.cwd or os.getcwd())
            base = project / ".dsterm" / "photos"
            try:
                if base.is_dir() and not base.is_symlink():
                    dirs.add(base)
            except OSError:
                pass
        for d in dirs:
            try:
                store = self._stores.get(d)
                if store is None:
                    # Only the cwd's own folder reaches here unsaved-into:
                    # checked as a project store, so a symlinked .dsterm
                    # above it is refused rather than pruned through.
                    store = dscamera.PhotoStore.for_project(
                        d.parent.parent, self.photo_keep, on_log=self._emit)
                store.prune()
            except Exception as exc:  # noqa: BLE001 -- prune is best-effort
                self._emit(f"photo prune of {d} failed: {exc}")

    # -- the camera feed (Unix socket) ---------------------------------------

    def _open_feed(self) -> None:
        """Bind the camera feed socket, or run without a feed.

        Path: <config_dir>/run/camera.sock, mode 0600, in a run/ that is 0700
        even if it pre-exists. A stale socket (nothing answers connect) is
        replaced; a live one belongs to another server, so the feed stays off
        rather than stealing it.
        """
        path = self.config_dir / "run" / "camera.sock"
        self._feed_path = path
        text = str(path)
        if len(text.encode()) > FEED_SOCK_MAX:
            self._emit(f"camera feed off: {len(text)}-byte socket path")
            return
        try:
            run_dir = path.parent
            run_dir.mkdir(parents=True, exist_ok=True)
            if os.lstat(run_dir).st_mode & 0o077:
                os.chmod(run_dir, 0o700)
        except OSError as exc:
            self._emit(f"camera feed off: {exc}")
            return
        if os.path.lexists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(text)
            except OSError:
                # Refused/absent peer: stale, ours to remove.
                try:
                    os.unlink(path)
                except OSError as exc:
                    self._emit(f"camera feed off: cannot clear {text}: {exc}")
                    return
            else:
                self._emit(f"camera feed off: {text} is a live socket")
                return
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_umask = os.umask(0o177)
            try:
                sock.bind(text)
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            sock.listen(FEED_MAX_VIEWERS)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            self._emit(f"camera feed off: cannot bind {text}: {exc}")
            return
        self._feed_sock = sock
        self._feed_bound = True
        self._sel.register(sock, selectors.EVENT_READ, "camera-feed")

    def _accept_feed(self) -> None:
        try:
            conn, _ = self._feed_sock.accept()
        except (BlockingIOError, OSError):
            return
        conn.setblocking(False)
        # On Linux the socket's peer credentials are checkable, and anything
        # but this uid is refused outright. On macOS there is no cheap peer
        # credential for a Unix socket, so the 0700 run/ directory and the
        # 0600 socket mode are the boundary -- which is sufficient: only this
        # user can reach the file.
        if sys.platform.startswith("linux"):
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET,
                                        socket.SO_PEERCRED, 12)
                uid = struct.unpack("3i", creds)[1]
            except OSError:
                uid = -1
            if uid != os.getuid():
                conn.close()
                return
        if len(self._feed_viewers) >= FEED_MAX_VIEWERS:
            conn.close()
            return
        viewer = FeedViewer(conn)
        self._feed_viewers.append(viewer)
        self._sel.register(conn, selectors.EVENT_READ, viewer)
        self._feed_send(viewer, self._feed_message())

    def _feed_message(self) -> bytes:
        v = self.camera_view
        # On without a preview yet is on with a 0x0 frame: still "on", no
        # pixels to send.
        has = v.on and bool(v.rgb)
        return dscamera.feed_message(v.on, v.camera, v.still, v.seq,
                                     v.w if has else 0, v.h if has else 0,
                                     v.rgb if has else b"")

    def _feed_broadcast(self) -> None:
        """Push the current view to every viewer that has caught up. One that
        has not simply misses this frame: the feed is a tail, not a queue."""
        if not self._feed_viewers:
            return
        msg = self._feed_message()
        for v in list(self._feed_viewers):
            self._feed_send(v, msg)

    def _feed_send(self, v: FeedViewer, msg: bytes) -> None:
        if v.pending:
            return
        try:
            sent = v.sock.send(msg)
        except (BlockingIOError, InterruptedError):
            sent = 0
        except OSError:
            self._close_feed_viewer(v)
            return
        if sent < len(msg):
            v.pending += msg[sent:]
            v.pending_since = time.monotonic()
            self._sel.modify(v.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, v)

    def _read_feed_viewer(self, v: FeedViewer) -> None:
        try:
            data = v.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_feed_viewer(v)
            return
        if not data:
            self._close_feed_viewer(v)

    def _write_feed_viewer(self, v: FeedViewer) -> None:
        try:
            sent = v.sock.send(v.pending)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_feed_viewer(v)
            return
        del v.pending[:sent]
        if not v.pending:
            self._sel.modify(v.sock, selectors.EVENT_READ, v)

    def _close_feed_viewer(self, v: FeedViewer) -> None:
        try:
            self._feed_viewers.remove(v)
        except ValueError:
            return
        try:
            self._sel.unregister(v.sock)
        except (KeyError, ValueError):
            pass
        try:
            v.sock.close()
        except OSError:
            pass

    def _feed_housekeeping(self) -> None:
        """Close viewers that have stopped draining: pending over the limit,
        or a tail that has not moved in FEED_STUCK_SECONDS."""
        now = time.monotonic()
        for v in list(self._feed_viewers):
            if v.pending and (len(v.pending) > FEED_PENDING_LIMIT
                              or now - v.pending_since > FEED_STUCK_SECONDS):
                self._close_feed_viewer(v)

    def _close_feed(self) -> None:
        for v in list(self._feed_viewers):
            self._close_feed_viewer(v)
        if self._feed_sock is not None:
            try:
                self._sel.unregister(self._feed_sock)
            except (KeyError, ValueError):
                pass
            self._feed_sock.close()
            self._feed_sock = None
        # Unlink only a socket this server bound: a live one belongs to
        # another server and _open_feed declined to take it.
        if self._feed_path is not None and self._feed_bound:
            try:
                os.unlink(self._feed_path)
            except OSError:
                pass
            self._feed_bound = False

    def _open_popups(self) -> None:
        """Wire the popup controller: only under tmux, only while popups are
        enabled. The command the popup runs is a `dsterm camera --popup`
        pointed at the feed socket."""
        target = self._tmux_target()
        # No feed of our own (another server holds the socket, or binding
        # failed): a popup would show that other server's camera, or nothing.
        if target is None or not self._camera_popups or self._feed_sock is None:
            return
        sock, name = target
        feed = str(self._feed_path)
        self._popups = dscamera.PopupController(
            runner=lambda argv: subprocess.run(
                argv, capture_output=True, text=True, timeout=8),
            socket_name=sock, session_name=f"={name}",
            own_pid_fn=lambda: (self._session.pid if self._session else None)
                or -1,
            # -P: tmux starts the popup in the session's directory -- the
            # project -- and `-m` would put that first on sys.path, so a
            # repository carrying a dsterm/ or a module cli.py imports would
            # run on every attached terminal when the camera opened.
            command_fn=lambda: shlex.join(
                [sys.executable, "-P", "-m", "dsterm.cli", "camera",
                 "--popup", "--socket", feed]),
            cwd=str(self._feed_path.parent),
            on_log=self._emit)

    def _on_camera(self, c: ClientState, msg: Message) -> None:
        """The camera's messages, from a card that negotiated the camera."""
        # Refused exactly as an unnegotiated REMOTE is: the card sends
        # these only after CAMERA_READY, which it only saw because it
        # announced "camera" to a server that offers it.
        if not c.caps.camera:
            self._drop(c, f"unexpected message {msg.type.name} "
                          "(camera was not negotiated)")
            return
        self._handle_camera(c, msg)

    # -- what the core calls (server.py) -------------------------------------

    def _init_camera(self, camera_enabled: bool, photo_dir, photo_keep: int,
                     camera_popups: bool) -> None:
        """The camera's state on the server. Only this module touches it
        (tests/test_layout.py), except camera_view, which the dashboard and
        `dsterm view`'s status line read."""
        # PHOTO. Off under --no-camera: the HELLO token is then never
        # honoured, READY never sent, and any camera frame drops the client.
        self.camera_enabled = camera_enabled
        # The picture lives under the project (<project>/.dsterm/photos) --
        # resolved at save time, on the worker, so it is the directory the
        # person is working in NOW, not the one the server started in.
        # --photo-dir overrides it (and then the wire path is absolute).
        self.photo_dir = Path(photo_dir) if photo_dir else None
        self.photo_keep = photo_keep
        # What the live viewfinder last looked like; swapped atomically.
        self.camera_view = CameraView()
        # Save results: (client, status, path, wire_path, size) tuples the
        # worker produces and _tick turns into PHOTO_SAVED. One worker at a
        # time is the busy gate the card's BUSY answer depends on.
        self._photo_results: queue.Queue = queue.Queue()
        self._photo_busy = False
        # PhotoStores keyed by directory so a discard reuses the exact folder
        # the save checked; also the set of folders retention sweeps.
        self._stores: dict[Path, dscamera.PhotoStore] = {}
        # The Unix feed: a listener plus at most FEED_MAX_VIEWERS viewers.
        self._feed_sock: socket.socket | None = None
        self._feed_path: Path | None = None
        # Whether the socket at _feed_path is the one we bound -- only that
        # one is ever unlinked.
        self._feed_bound = False
        self._feed_viewers: list[FeedViewer] = []
        # tmux popups, one per attached client, while the camera is on.
        self._popups: dscamera.PopupController | None = None
        self._camera_popups = camera_popups

    def _camera_offered(self) -> bool:
        return self.camera_enabled

    def _start_camera(self) -> None:
        if self.camera_enabled:
            self._open_feed()
            self._prune_photo_dirs(startup=True)
            self._open_popups()

    def _stop_camera(self) -> None:
        if self._popups is not None:
            self._popups.close()
            self._popups = None
        self._close_feed()

    def _camera_event(self, viewer: FeedViewer, mask: int) -> None:
        """A feed viewer's socket is ready."""
        if mask & selectors.EVENT_WRITE:
            self._write_feed_viewer(viewer)
        if viewer in self._feed_viewers and mask & selectors.EVENT_READ:
            self._read_feed_viewer(viewer)

    def _camera_tick(self) -> None:
        # The camera's housekeeping does not need a card: worker results are
        # collected (and the busy gate freed) even when the sender has gone,
        # and a viewer that stopped reading is closed either way.
        self._collect_photo_results()
        self._feed_housekeeping()
        if self._popups is not None:
            self._popups.reconcile(self.camera_view.on)

    def _camera_card_gone(self, c: ClientState) -> None:
        """An authenticated card's connection ended. Its camera was the feed:
        a drop mid-upload leaves no file (the assembler is gone with it) and
        the viewfinder is off."""
        if self.camera_view.on:
            self.camera_view = CameraView(at=time.time())
            self._feed_broadcast()
        if c.camera.assembler is not None:
            c.camera.assembler.reset()
