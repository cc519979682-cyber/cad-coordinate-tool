"""Short-lived permission and one-shot CAD road requests in a private session.

The CAD command returns before the UI consumes its request. This deliberately
does not reopen a completed event journal or execute user-supplied Lisp.
"""
from datetime import datetime, timedelta
from pathlib import Path
import errno
import os
import re
import time


def _sharing_violation(error):
    return getattr(error, "winerror", None) in (32, 33)


class ZBBridge:
    TTL_SECONDS = 12
    REFRESH_SECONDS = 2

    def __init__(self, directory, token):
        self.directory = Path(directory)
        self.token = token
        self.ready_path = self.directory / "zb.ready"
        self.request_path = self.directory / "zb.request"
        self._last_published = float("-inf")
        self._expires_at = float("-inf")
        self._enabled = False
        self._consumed = False
        self._claimed = False

    def publish(self):
        if self._consumed or self._claimed:
            return
        now = time.monotonic()
        if self._enabled and now - self._last_published < self.REFRESH_SECONDS:
            return
        wall_now = datetime.now()
        deadline = (wall_now + timedelta(seconds=self.TTL_SECONDS)).replace(microsecond=0)
        expiry = deadline.strftime("%Y%m%d.%H%M%S")
        temporary = self.directory / "zb.ready.tmp"
        try:
            temporary.write_text(f"CGP_ZB_V1\n{self.token}\n{expiry}\n", encoding="ascii")
            os.replace(temporary, self.ready_path)
        except OSError as exc:
            if _sharing_violation(exc):
                # CAD briefly opens the lease for reading. The next UI tick
                # retries publication; an expired lease cannot authorize ZB.
                return
            raise
        self._last_published = now
        self._expires_at = now + (deadline - wall_now).total_seconds()
        self._enabled = True

    def disable(self):
        self._enabled = False
        self._remove_ready()

    def _remove_ready(self):
        try:
            self.ready_path.unlink(missing_ok=True)
        except OSError as exc:
            if not _sharing_violation(exc):
                raise
            # A claimed request cannot renew this lease. External disable also
            # revokes its in-process permission before trying to remove it.

    def _complete(self):
        self._consumed = True
        self.disable()

    def take_request(self):
        """Claim one closed, atomically renamed request; malformed data fails once."""
        if self._consumed:
            return None
        # Save the request for diagnosis; never repeatedly act on the same file.
        claimed = self.directory / "zb.request.handled"
        if not self._claimed:
            if not self.request_path.is_file():
                return None
            try:
                os.replace(self.request_path, claimed)
            except OSError as exc:
                if _sharing_violation(exc):
                    return None  # Claim failed; keep state unchanged for next tick.
                raise
            self._claimed = True
            self._remove_ready()
        if not self._enabled or time.monotonic() >= self._expires_at:
            self._complete()
            raise ValueError("ZB 续接已失效，请从生成器重新开始取点。")
        try:
            with claimed.open("rb") as stream:
                data = stream.read(4097)
        except OSError as exc:
            # Windows CRT open() can report a sharing lock as errno EACCES
            # without winerror. Keep this claimed file until its original
            # lease expires; neither retry nor publish extends permission.
            if _sharing_violation(exc) or (isinstance(exc, PermissionError)
                    and exc.errno == errno.EACCES and getattr(exc, "winerror", None) is None):
                return None
            self._complete()
            raise
        permitted = self._enabled and time.monotonic() < self._expires_at
        self._complete()
        if not permitted:
            raise ValueError("ZB 续接已失效，请从生成器重新开始取点。")
        if len(data) > 4096:
            raise ValueError("ZB 请求过长，未启动取点。")
        try:
            line = data.decode("ascii")
        except UnicodeError as exc:
            raise ValueError("ZB 请求编码无效，未启动取点。") from exc
        match = re.fullmatch(r"ZB1\t([^\t\r\n]+)\t([0-9]+(?:,[0-9]+){0,99})\r?\n", line)
        if not match or match[1] != self.token:
            raise ValueError("ZB 请求不属于当前会话或格式无效，未启动取点。")
        codes = [int(value) for value in match[2].split(",")]
        if any(code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF
               or code > 0x10FFFF for code in codes):
            raise ValueError("ZB 道路名称包含无效字符，未启动取点。")
        name = "".join(map(chr, codes))
        if not name or name != name.strip():
            raise ValueError("ZB 道路名称不能为空或包含首尾空白。")
        return name
