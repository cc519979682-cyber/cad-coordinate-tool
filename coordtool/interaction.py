"""Coalesce Tk redraw requests and prepare artists before the renderer runs."""
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class ResponsiveCanvas(FigureCanvasTkAgg):
    def __init__(self, *args, **kwargs):
        self.before_draw = None
        self.on_invalidate = None
        self._frame_job = None
        self._drawing_frame = False
        self._disposed = False
        super().__init__(*args, **kwargs)

    def draw_idle(self):
        if self._disposed:
            return
        if self.on_invalidate is not None:
            self.on_invalidate()
        if self._frame_job is None and not self._drawing_frame:
            # Mouse bursts collapse into one frame and leave time for Tk input.
            self._frame_job = self.get_tk_widget().after(16, self._draw_frame)

    def _draw_frame(self):
        self._frame_job = None
        self.draw()

    def draw(self):
        if self._disposed:
            return
        if self._frame_job is not None:
            self.get_tk_widget().after_cancel(self._frame_job)
            self._frame_job = None
        self._drawing_frame = True
        try:
            if self.before_draw is not None:
                self.before_draw()
            super().draw()
        finally:
            self._drawing_frame = False

    def dispose(self):
        self._disposed = True
        if self._frame_job is not None:
            self.get_tk_widget().after_cancel(self._frame_job)
            self._frame_job = None
