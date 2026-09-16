"""Preview regression: unrelated coordinate groups must not be joined by default."""
import time
import unittest
from pathlib import Path

from matplotlib.collections import LineCollection
import numpy as np

from coordtool.core import Point
from coordtool.ui import CoordinateApp


class LineModeUI(unittest.TestCase):
    def test_independent_points_and_explicit_connection_toggle(self):
        app = CoordinateApp(Path(__file__).resolve().parents[1])
        app.root.withdraw()
        try:
            points = [Point(f"G{group}-{i}", group * 1000 + i * 3,
                            group * 500 + i * 2, -2.0)
                      for group, count in enumerate((12, 7, 13)) for i in range(count)]
            app._changed_points(points, "回归测试")

            def settle(line, closed=False):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    app.root.update()
                    if (app.preview_job is None and len(app.labels) == 32
                            and hasattr(app, "render_settings")
                            and app.render_settings.draw_line == line
                            and app.render_settings.closed == closed):
                        return
                    time.sleep(.005)
                self.fail("preview did not settle")

            def segments():
                return next(a for a in app.artists if isinstance(a, LineCollection)).get_segments()

            settle(False)
            self.assertFalse(app.settings().draw_line)
            self.assertEqual(len(segments()), 32 * 4)  # Only crosses and label leaders.
            self.assertLess(max(np.linalg.norm(s[1]-s[0]) for s in segments()), 100)

            def walk(widget):
                yield widget
                for child in widget.winfo_children():
                    yield from walk(child)

            toggle = next(w for w in walk(app.root)
                          if "text" in w.keys() and str(w.cget("text")) == "连接全部点")
            toggle.invoke()
            settle(True)
            self.assertEqual(len(segments()), 32 * 4 + 31)
            app.closed_line.set(True)
            app._settings_changed()
            settle(True, True)
            self.assertEqual(len(segments()), 32 * 4 + 32)
            toggle.invoke()
            settle(False, True)
            self.assertEqual(len(segments()), 32 * 4)
            self.assertEqual(app.points, points)
        finally:
            app.dirty = False
            app.close()


if __name__ == "__main__":
    unittest.main()
