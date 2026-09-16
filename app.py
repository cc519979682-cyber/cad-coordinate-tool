"""Desktop entry point; importing this module does not open a window."""
import argparse
from pathlib import Path
import sys


def resource_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser(description="坐标生成器 v22.8")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--points")
    parser.add_argument("--drawing")
    args = parser.parse_args()
    from coordtool.ui import CoordinateApp
    app = CoordinateApp(resource_dir())
    if args.demo:
        app.root.after(200, app.load_demo)
    else:
        def load_inputs():
            if args.drawing:
                app.load_base(args.drawing)
            if args.points:
                app.load_coordinates(args.points)
        app.root.after(200, load_inputs)
    app.root.mainloop()


if __name__ == "__main__":
    main()
