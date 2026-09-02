#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nuitka_build.py - one-shot native binary builder for Python projects (powered by Nuitka).

Give it a project directory or a .py file and it will:

  1. Find the real entry point
       - explicit  : --entry FILE | --entry pkg | --entry module:function
       - pyproject : [project.scripts] / [project.gui-scripts] / [tool.poetry.scripts]
       - setup.py / setup.cfg console_scripts
       - package __main__.py (compiled like `python -m package`)
       - well-known names (main.py, app.py, cli.py, run.py, manage.py ...)
       - any file with an `if __name__ == "__main__":` guard (scored, best wins)
  2. Collect dependencies from every common source
       - PEP 723 inline script metadata
       - pyproject.toml   (PEP 621 dependencies + optional-dependencies, Poetry, Hatch)
       - setup.py install_requires / setup.cfg [options] install_requires
       - Pipfile / Pipfile.lock
       - requirements*.txt, requirements/*.txt, constraints.txt
       - environment.yml (conda) pip: section
       - fallback: import scanning + import-name -> PyPI-name map (best effort)
  3. Create an isolated virtual environment (uv if present, otherwise venv + pip),
     install the dependencies and Nuitka itself.
  4. Auto-detect Nuitka plugins (Qt bindings, tkinter, matplotlib, ...) and
     data directories (assets/, data/, templates/, ...).
  5. Run Nuitka (--mode=onefile by default) and print the produced binary.

Examples
--------
  python nuitka_build.py ./myproject
  python nuitka_build.py ./myproject/main.py --mode standalone
  python nuitka_build.py ./myproject --entry mypkg.cli:main --name mytool --lto
  python nuitka_build.py ./gui_app --gui --icon icon.png --onefile-cache
  python nuitka_build.py ./proj --python 3.12 --extras gui --run
  python nuitka_build.py ./proj --dry-run
  python nuitka_build.py ./proj -- --include-package=plugins --nofollow-import-to=*.tests

Everything after a literal `--` is passed to Nuitka verbatim.
Requires Python 3.8+ on the host; Nuitka needs a C compiler (clang / gcc / MSVC or
MinGW64 which Nuitka can download itself on Windows).
"""
import sys

# This guard must stay ABOVE every other import: `dataclasses` (3.7+) and modern syntax
# would otherwise fail first and hide the real problem on RHEL/Rocky 8 (python3 == 3.6).
if sys.version_info < (3, 8):
    sys.stderr.write("nuitka_build.py needs Python 3.8+ on the host (found %d.%d). On RHEL/Rocky: "
                     "dnf install -y python3.11 python3.11-devel python3.11-pip"
                     " && python3.11 nuitka_build.py ...\n" % sys.version_info[:2])
    sys.exit(2)

import argparse
import ast
import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

__version__ = "1.2.0"


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

BUILD_DIRNAME = ".nuitka-build"

EXCLUDED_DIR_NAMES = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", ".env", "envs",
    "node_modules", "build", "dist", BUILD_DIRNAME, ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".eggs", "site-packages", ".idea", ".vscode",
    ".cache", "htmlcov", "__pypackages__", ".ipynb_checkpoints",
}

# File names that are strong entry-point hints, most likely first.
ENTRY_NAME_PRIORITY = [
    "__main__.py", "main.py", "app.py", "run.py", "cli.py", "manage.py", "start.py",
    "server.py", "application.py", "launch.py", "gui.py", "program.py", "index.py",
]
# Files that are never the program entry point.
NEVER_ENTRY_FILES = {
    "setup.py", "conftest.py", "noxfile.py", "tasks.py", "fabfile.py", "versioneer.py",
    "nuitka_build.py", "__init__.py",
}
# Directory names that indicate "not the application".
NON_ENTRY_DIR_NAMES = {
    "tests", "test", "testing", "examples", "example", "docs", "doc", "benchmarks",
    "benchmark", "scripts", "tools", "migrations", "samples", "sample", "demo", "demos",
    "ci", "notebooks",
}

# import name -> Nuitka plugin (plugins that are NOT auto-enabled in Nuitka >= 2.x)
PLUGIN_BY_IMPORT: Dict[str, str] = {
    "tkinter": "tk-inter", "Tkinter": "tk-inter", "customtkinter": "tk-inter",
    "ttkbootstrap": "tk-inter", "ttkthemes": "tk-inter", "tkinterdnd2": "tk-inter",
    "PySide6": "pyside6", "PySide2": "pyside2", "PyQt5": "pyqt5", "PyQt6": "pyqt6",
    "matplotlib": "matplotlib", "mpl_toolkits": "matplotlib", "seaborn": "matplotlib",
    "gevent": "gevent", "kivy": "kivy", "webview": "pywebview", "spacy": "spacy",
    "playwright": "playwright", "glfw": "glfw", "OpenGL": "glfw",
    "dill": "dill-compat", "cloudpickle": "dill-compat", "Pmw": "pmw-freezer",
    "pbr": "pbr-compat",
}
# normalized distribution name -> Nuitka plugin (catches dynamically imported libs)
PLUGIN_BY_DIST: Dict[str, str] = {
    "pyside6": "pyside6", "pyside2": "pyside2", "pyqt5": "pyqt5", "pyqt6": "pyqt6",
    "matplotlib": "matplotlib", "seaborn": "matplotlib", "customtkinter": "tk-inter",
    "ttkbootstrap": "tk-inter", "gevent": "gevent", "kivy": "kivy", "pywebview": "pywebview",
    "spacy": "spacy", "playwright": "playwright", "glfw": "glfw", "pyopengl": "glfw",
    "dill": "dill-compat", "cloudpickle": "dill-compat", "pmw": "pmw-freezer",
    "pbr": "pbr-compat",
}

# Directory names treated as data directories when they contain no Python code.
DATA_DIR_NAMES = {
    "assets", "asset", "data", "static", "templates", "template", "resources", "resource",
    "res", "locale", "locales", "i18n", "fonts", "images", "img", "icons", "sounds",
    "audio", "media", "config", "configs", "shaders", "textures", "themes", "models",
}

# import name -> PyPI distribution name, used only for the import-scan fallback.
IMPORT_TO_DIST: Dict[str, str] = {
    "cv2": "opencv-python", "PIL": "pillow", "yaml": "PyYAML", "sklearn": "scikit-learn",
    "skimage": "scikit-image", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
    "attr": "attrs", "dateutil": "python-dateutil", "Crypto": "pycryptodome", "jwt": "PyJWT",
    "serial": "pyserial", "usb": "pyusb", "wx": "wxPython", "gi": "PyGObject",
    "google.protobuf": "protobuf", "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient",
    "docx": "python-docx", "pptx": "python-pptx", "fitz": "PyMuPDF",
    "Levenshtein": "python-Levenshtein", "magic": "python-magic",
    "websocket": "websocket-client", "telegram": "python-telegram-bot",
    "discord": "discord.py", "OpenSSL": "pyOpenSSL", "nacl": "PyNaCl", "zmq": "pyzmq",
    "Xlib": "python-xlib", "github": "PyGithub", "gitlab": "python-gitlab", "git": "GitPython",
    "markdown": "Markdown", "win32api": "pywin32", "win32con": "pywin32", "win32gui": "pywin32",
    "pywintypes": "pywin32", "win32com": "pywin32", "ldap": "python-ldap",
    "flask_sqlalchemy": "Flask-SQLAlchemy", "flask_cors": "Flask-Cors",
    "flask_login": "Flask-Login", "flask_wtf": "Flask-WTF", "sqlalchemy": "SQLAlchemy",
    "jose": "python-jose", "multipart": "python-multipart", "snappy": "python-snappy",
    "pkg_resources": "setuptools", "ruamel": "ruamel.yaml",
    "googleapiclient": "google-api-python-client", "cairo": "pycairo", "IPython": "ipython",
    "Cython": "Cython", "PyQt5": "PyQt5", "PyQt6": "PyQt6", "PySide6": "PySide6",
    "PySide2": "PySide2", "kivy": "Kivy", "webview": "pywebview", "OpenGL": "PyOpenGL",
    "pygame": "pygame", "numpy": "numpy", "pandas": "pandas", "scipy": "scipy",
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp", "flask": "Flask",
    "django": "Django", "fastapi": "fastapi", "uvicorn": "uvicorn", "pydantic": "pydantic",
    "typer": "typer", "click": "click", "rich": "rich", "textual": "textual",
    "tqdm": "tqdm", "matplotlib": "matplotlib", "seaborn": "seaborn", "torch": "torch",
    "torchvision": "torchvision", "tensorflow": "tensorflow", "keras": "keras",
    "transformers": "transformers", "openai": "openai", "anthropic": "anthropic",
    "boto3": "boto3", "botocore": "botocore", "redis": "redis", "pymongo": "pymongo",
    "psutil": "psutil", "lxml": "lxml", "shapely": "shapely", "cowsay": "cowsay",
    "playwright": "playwright", "selenium": "selenium", "paramiko": "paramiko",
    "cryptography": "cryptography", "toml": "toml", "tomli": "tomli", "orjson": "orjson",
    "ujson": "ujson", "msgpack": "msgpack", "jinja2": "Jinja2", "markupsafe": "MarkupSafe",
    "werkzeug": "Werkzeug", "colorama": "colorama", "tabulate": "tabulate",
    "openpyxl": "openpyxl", "xlrd": "xlrd", "xlsxwriter": "XlsxWriter", "docopt": "docopt",
    "pexpect": "pexpect", "sh": "sh", "plumbum": "plumbum", "loguru": "loguru",
    "structlog": "structlog", "arrow": "arrow", "pendulum": "pendulum", "pytz": "pytz",
    "tzlocal": "tzlocal", "networkx": "networkx", "sympy": "sympy", "numba": "numba",
    "dask": "dask", "polars": "polars", "pyarrow": "pyarrow", "h5py": "h5py",
    "netCDF4": "netCDF4", "xarray": "xarray", "PyInstaller": "pyinstaller",
    "customtkinter": "customtkinter", "ttkbootstrap": "ttkbootstrap", "pyautogui": "PyAutoGUI",
    "pynput": "pynput", "keyboard": "keyboard", "mouse": "mouse", "pyperclip": "pyperclip",
    "plyer": "plyer", "notify2": "notify2", "schedule": "schedule", "apscheduler": "APScheduler",
    "celery": "celery", "kombu": "kombu", "pika": "pika", "grpc": "grpcio", "thrift": "thrift",
    "graphene": "graphene", "strawberry": "strawberry-graphql", "gql": "gql",
    "socketio": "python-socketio", "engineio": "python-engineio", "websockets": "websockets",
    "prompt_toolkit": "prompt_toolkit", "pygments": "Pygments", "yaml_env": "yaml-env",
    "sounddevice": "sounddevice", "soundfile": "soundfile", "pyaudio": "PyAudio",
    "pydub": "pydub", "moviepy": "moviepy", "imageio": "imageio", "av": "av",
    "reportlab": "reportlab", "fpdf": "fpdf2", "pypdf": "pypdf", "PyPDF2": "PyPDF2",
    "pdfplumber": "pdfplumber", "pytesseract": "pytesseract", "easyocr": "easyocr",
    "qrcode": "qrcode", "barcode": "python-barcode", "pyzbar": "pyzbar", "geopy": "geopy",
    "folium": "folium", "plotly": "plotly", "bokeh": "bokeh", "dash": "dash",
    "streamlit": "streamlit", "gradio": "gradio", "nicegui": "nicegui", "flet": "flet",
    "toga": "toga", "dearpygui": "dearpygui", "pyglet": "pyglet", "arcade": "arcade",
    "ursina": "ursina", "panda3d": "Panda3D", "pymunk": "pymunk", "Box2D": "Box2D",
    "ffmpeg": "ffmpeg-python", "Xlib": "python-xlib", "dbus": "dbus-python",
    "pystray": "pystray", "win10toast": "win10toast", "winreg": "winreg",
}

# Minimal stdlib fallback for Python < 3.10 (sys.stdlib_module_names is preferred).
_STDLIB_FALLBACK = set("""
abc argparse array ast asyncio atexit base64 bdb binascii bisect builtins bz2 calendar cgi
cmath cmd code codecs collections colorsys compileall concurrent configparser contextlib
contextvars copy copyreg cProfile csv ctypes curses dataclasses datetime dbm decimal difflib
dis doctest email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch fractions
ftplib functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http
imaplib imghdr importlib inspect io ipaddress itertools json keyword linecache locale logging
lzma mailbox marshal math mimetypes mmap multiprocessing netrc numbers operator optparse os
pathlib pdb pickle pickletools pkgutil platform plistlib poplib posix posixpath pprint profile
pstats pty pwd py_compile pyclbr pydoc queue quopri random re readline reprlib resource runpy
sched secrets select selectors shelve shlex shutil signal site smtplib socket socketserver
sqlite3 ssl stat statistics string stringprep struct subprocess sunau symtable sys sysconfig
syslog tabnanny tarfile telnetlib tempfile termios textwrap threading time timeit tkinter token
tokenize tomllib trace traceback tracemalloc tty turtle types typing unicodedata unittest urllib
uu uuid venv warnings wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp
zipfile zipimport zlib zoneinfo _thread __future__ distutils
""".split())

STDLIB_NAMES: Set[str] = set(getattr(sys, "stdlib_module_names", ())) or _STDLIB_FALLBACK

# --------------------------------------------------------------------------------------
# Logging helpers
# --------------------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not IS_WINDOWS or bool(os.environ.get("FORCE_COLOR"))
VERBOSE = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def info(msg: str) -> None:
    print(_c("36", "[nuitka-build]"), msg, flush=True)


def step(msg: str) -> None:
    print(_c("1;34", f"\n==> {msg}"), flush=True)


def ok(msg: str) -> None:
    print(_c("32", "[ok]"), msg, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "[warn]"), msg, file=sys.stderr, flush=True)


def debug(msg: str) -> None:
    if VERBOSE:
        print(_c("90", "[debug]"), msg, flush=True)


def die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print(_c("31", "[error]"), msg, file=sys.stderr, flush=True)
    sys.exit(code)


def fmt_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def rel_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------------------
# TOML loading (tomllib on 3.11+, tomli fallback)
# --------------------------------------------------------------------------------------

def _load_toml_module():
    try:
        import tomllib  # type: ignore[import-not-found]
        return tomllib
    except ModuleNotFoundError:
        pass
    try:
        import tomli  # type: ignore[import-not-found]
        return tomli
    except ModuleNotFoundError:
        return None


_TOML = _load_toml_module()


def load_toml(path: Path) -> dict:
    if _TOML is None:
        warn(f"Cannot parse {path.name}: no TOML parser (Python < 3.11 needs `pip install tomli`).")
        return {}
    try:
        with open(path, "rb") as fh:
            return _TOML.load(fh)
    except Exception as exc:  # noqa: BLE001
        warn(f"Failed to parse {path}: {exc}")
        return {}


# --------------------------------------------------------------------------------------
# Project discovery
# --------------------------------------------------------------------------------------

PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "environment.yml", "environment.yaml", ".git", "poetry.lock", "uv.lock",
)


def is_excluded_dir(d: Path) -> bool:
    if d.name in EXCLUDED_DIR_NAMES or d.name.endswith(".egg-info"):
        return True
    return (d / "pyvenv.cfg").is_file()  # any virtualenv


def find_project_root(start: Path) -> Path:
    """Walk upwards from `start` looking for a project marker (max 8 levels).

    Without a marker, a file inside a package resolves to the directory that
    contains the top-level package (so `.nuitka-build/` never lands inside it).
    """
    cur = start if start.is_dir() else start.parent
    for _ in range(8):
        if any((cur / m).exists() for m in PROJECT_MARKERS):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    top = top_package_dir(start)
    if top is not None:
        return top.parent
    return start if start.is_dir() else start.parent


def iter_py_files(root: Path, skip_non_entry_dirs: bool = False) -> Iterable[Path]:
    """Yield all .py files under root, skipping virtualenvs, build dirs, VCS dirs, ..."""
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = sorted(
            n for n in dirnames
            if not is_excluded_dir(d / n)
            and not (skip_non_entry_dirs and n in NON_ENTRY_DIR_NAMES)
        )
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield d / fn


def is_package_dir(p: Path) -> bool:
    return (p / "__init__.py").is_file()


def top_package_dir(path: Path) -> Optional[Path]:
    """For a file/dir inside a regular package, return the top-most package directory."""
    d = path if path.is_dir() else path.parent
    if not is_package_dir(d):
        return None
    while is_package_dir(d.parent):
        d = d.parent
    return d


def import_roots_for(root: Path) -> List[Path]:
    """Directories that must be on sys.path for the project's imports to resolve."""
    roots = [root]
    src = root / "src"
    if src.is_dir() and not is_package_dir(src):
        if any(is_package_dir(p) or p.suffix == ".py" for p in src.iterdir()):
            roots.append(src)
    return roots


def local_top_level_names(import_roots: Sequence[Path]) -> Set[str]:
    names: Set[str] = set()
    for r in import_roots:
        if not r.is_dir():
            continue
        for p in r.iterdir():
            if p.is_dir() and is_package_dir(p) and not is_excluded_dir(p):
                names.add(p.name)
            elif p.suffix == ".py" and p.stem.isidentifier():
                names.add(p.stem)
    return names


def module_name_for(file: Path, import_root: Path) -> str:
    rel = file.relative_to(import_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --------------------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------------------

def parse_py(path: Path) -> Optional[ast.Module]:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError):
        return None


def top_level_imports(tree: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def dotted_imports(tree: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def has_relative_imports(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.level > 0 for n in ast.walk(tree))


def main_guard(tree: ast.Module) -> Optional[ast.If]:
    """Return the `if __name__ == "__main__":` node if present at module level."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare) and len(test.comparators) == 1:
            left, right = test.left, test.comparators[0]
            names = {getattr(left, "id", None), getattr(right, "id", None)}
            values = {getattr(left, "value", None), getattr(right, "value", None)}
            if "__name__" in names and "__main__" in values:
                return node
    return None


def defines_function(tree: ast.Module, name: str) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for n in tree.body)


def guard_source(path: Path, guard: ast.If) -> str:
    """Source code of the body of the __main__ guard, dedented."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    first = guard.body[0]
    last = guard.body[-1]
    start = first.lineno - 1
    end = getattr(last, "end_lineno", last.lineno)
    return textwrap.dedent("\n".join(lines[start:end])) + "\n"


# --------------------------------------------------------------------------------------
# Entry point detection
# --------------------------------------------------------------------------------------

@dataclass
class Entry:
    kind: str                     # "script" | "package" | "launcher"
    path: Path                    # script file, package dir, or launcher target module file
    import_root: Path             # must be on sys.path while compiling
    package: Optional[str] = None # top-level package name (if inside a package)
    module: Optional[str] = None  # dotted module name (package / launcher)
    func: Optional[str] = None    # function to call (launcher); None -> run __main__ guard
    source: str = ""              # human description of how it was found
    name_hint: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "package":
            return f"package `{self.module}` (python -m {self.module}) from {self.path}"
        if self.kind == "launcher":
            target = f"{self.module}:{self.func}" if self.func else f"{self.module} (__main__ guard)"
            return f"launcher -> {target} ({self.path})"
        return f"script {self.path}"


def entry_from_file(file: Path, explicit: bool, source: str) -> Entry:
    file = file.resolve()
    top = top_package_dir(file)
    if top is None:
        return Entry("script", file, file.parent, source=source)

    import_root = top.parent
    package = top.name
    module = module_name_for(file, import_root)
    if file.name == "__main__.py":
        return Entry("package", file.parent, import_root, package=package,
                     module=module.rsplit(".", 1)[0] if module.endswith(".__main__") else module,
                     source=source)

    tree = parse_py(file)
    needs_pkg_semantics = tree is not None and (
        has_relative_imports(tree) or package in top_level_imports(tree)
    )
    if not needs_pkg_semantics:
        # Plain script that happens to live in a package directory; run it as a script.
        return Entry("script", file, file.parent, package=package, source=source)

    func = None
    if tree is not None and defines_function(tree, "main"):
        func = "main"
    return Entry("launcher", file, import_root, package=package, module=module, func=func,
                 source=source)


def resolve_module(module: str, import_roots: Sequence[Path]) -> Optional[Path]:
    parts = module.split(".")
    for root in import_roots:
        base = root.joinpath(*parts)
        if base.is_dir() and is_package_dir(base):
            return base
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
    return None


def entry_from_object_ref(ref: str, import_roots: Sequence[Path], source: str,
                          name_hint: Optional[str] = None) -> Optional[Entry]:
    """Resolve `module:function` or `module` (entry-point style) to an Entry."""
    module, _, func = ref.partition(":")
    module = module.strip()
    func = func.strip() or None
    target = resolve_module(module, import_roots)
    if target is None:
        return None
    if target.is_dir():
        import_root = _root_of(target, import_roots)
        package = module.split(".")[0]
        if func is None:
            if (target / "__main__.py").is_file():
                return Entry("package", target, import_root, package=package, module=module,
                             source=source, name_hint=name_hint)
            return None
        return Entry("launcher", target / "__init__.py", import_root, package=package,
                     module=module, func=func, source=source, name_hint=name_hint)

    import_root = _root_of(target, import_roots)
    top = top_package_dir(target)
    package = top.name if top else None
    if func is None:
        if target.name == "__main__.py":
            return Entry("package", target.parent, import_root, package=package,
                         module=module.rsplit(".", 1)[0], source=source, name_hint=name_hint)
        e = entry_from_file(target, True, source)
        e.name_hint = name_hint
        return e
    return Entry("launcher", target, import_root, package=package, module=module, func=func,
                 source=source, name_hint=name_hint)


def _root_of(path: Path, import_roots: Sequence[Path]) -> Path:
    for r in import_roots:
        try:
            path.relative_to(r)
            return r
        except ValueError:
            continue
    return path.parent


def scripts_from_pyproject(root: Path) -> List[Tuple[str, str, str]]:
    """Return [(script_name, object_ref, source)] from pyproject.toml."""
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return []
    data = load_toml(pp)
    out: List[Tuple[str, str, str]] = []
    project = data.get("project", {}) or {}
    for section in ("scripts", "gui-scripts"):
        for name, ref in (project.get(section, {}) or {}).items():
            if isinstance(ref, str):
                out.append((name, ref, f"pyproject [project.{section}]"))
    poetry = (data.get("tool", {}) or {}).get("poetry", {}) or {}
    for name, ref in (poetry.get("scripts", {}) or {}).items():
        if isinstance(ref, str):
            out.append((name, ref, "pyproject [tool.poetry.scripts]"))
        elif isinstance(ref, dict) and isinstance(ref.get("callable"), str):
            out.append((name, ref["callable"], "pyproject [tool.poetry.scripts]"))
    return out


def scripts_from_setup(root: Path) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    cfg = root / "setup.cfg"
    if cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg, encoding="utf-8")
            raw = parser.get("options.entry_points", "console_scripts", fallback="") + "\n" + \
                parser.get("options.entry_points", "gui_scripts", fallback="")
            for line in raw.splitlines():
                if "=" in line:
                    name, ref = line.split("=", 1)
                    out.append((name.strip(), ref.strip(), "setup.cfg entry_points"))
        except configparser.Error:
            pass
    sp = root / "setup.py"
    if sp.is_file():
        tree = parse_py(sp)
        if tree is not None:
            ep = _setup_kwarg(tree, "entry_points")
            if isinstance(ep, dict):
                for key in ("console_scripts", "gui_scripts"):
                    for item in ep.get(key, []) or []:
                        if isinstance(item, str) and "=" in item:
                            name, ref = item.split("=", 1)
                            out.append((name.strip(), ref.strip(), "setup.py entry_points"))
    return out


def _setup_kwarg(tree: ast.Module, kwarg: str):
    """Best-effort: evaluate a literal keyword argument of the setup(...) call."""
    assignments: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup":
            for kw in node.keywords:
                if kw.arg == kwarg:
                    value = kw.value
                    if isinstance(value, ast.Name) and value.id in assignments:
                        value = assignments[value.id]
                    try:
                        return ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        return None
    return None


def score_candidate(file: Path, root: Path, tree: ast.Module) -> int:
    rel = file.relative_to(root)
    depth = len(rel.parts) - 1
    score = 0
    name = file.name
    if name in ENTRY_NAME_PRIORITY:
        score += 60 - ENTRY_NAME_PRIORITY.index(name) * 3
    if name == f"{root.name}.py" or name == f"{root.name.replace('-', '_')}.py":
        score += 40
    score += {0: 30, 1: 15, 2: 5}.get(depth, -5 * depth)
    if any(p in NON_ENTRY_DIR_NAMES for p in rel.parts[:-1]):
        score -= 60
    if name.startswith("test_") or name.endswith("_test.py"):
        score -= 80
    if defines_function(tree, "main"):
        score += 15
    imports = top_level_imports(tree)
    if imports & {"argparse", "click", "typer", "docopt", "fire"}:
        score += 10
    if imports & {"tkinter", "PySide6", "PySide2", "PyQt5", "PyQt6", "kivy", "wx", "pygame"}:
        score += 8
    if rel.parts[0] == "src":
        score += 5
    return score


def detect_entry(root: Path, import_roots: Sequence[Path], explicit: Optional[str]) -> Entry:
    # 1) explicit
    if explicit:
        p = Path(explicit)
        cand = p if p.is_absolute() else (root / p)
        if cand.is_file():
            return entry_from_file(cand, True, "--entry (file)")
        if cand.is_dir():
            if (cand / "__main__.py").is_file():
                e = entry_from_file(cand / "__main__.py", True, "--entry (package dir)")
                return e
            die(f"--entry directory has no __main__.py: {cand}")
        e = entry_from_object_ref(explicit, import_roots, "--entry (module ref)")
        if e is None:
            die(f"Could not resolve --entry {explicit!r} as file, package dir or module[:function].")
        return e

    # 2) declared console scripts
    declared = scripts_from_pyproject(root) + scripts_from_setup(root)
    for name, ref, source in declared:
        e = entry_from_object_ref(ref, import_roots, source, name_hint=name)
        if e is not None:
            if len(declared) > 1:
                info(f"Multiple declared scripts, using first: {name} = {ref}  "
                     f"(others: {', '.join(n for n, _, _ in declared[1:])}; override with --entry)")
            return e
        warn(f"Declared script {name} = {ref} ({source}) could not be resolved on disk; ignoring.")

    # 2b) Django: manage.py is the canonical entry (migrate, runserver, custom commands)
    manage = root / "manage.py"
    if manage.is_file() and "django" in manage.read_text(encoding="utf-8", errors="replace"):
        return Entry("script", manage.resolve(), root, source="Django manage.py")

    # 3) packages with __main__.py directly under an import root
    pkg_mains: List[Path] = []
    for r in import_roots:
        for p in sorted(r.iterdir()):
            if p.is_dir() and is_package_dir(p) and (p / "__main__.py").is_file() and not is_excluded_dir(p):
                if p.name not in NON_ENTRY_DIR_NAMES:
                    pkg_mains.append(p / "__main__.py")
    if len(pkg_mains) == 1:
        return entry_from_file(pkg_mains[0], False, "package __main__.py")

    # 4) scored scan: files with a __main__ guard, a well-known name, or PEP 723 metadata
    candidates: List[Tuple[int, Path, ast.Module]] = []
    for f in iter_py_files(root):
        if f.name in NEVER_ENTRY_FILES:
            continue
        tree = parse_py(f)
        if tree is None:
            continue
        pep723 = has_pep723_block(f)
        if main_guard(tree) is None and f.name not in ENTRY_NAME_PRIORITY and not pep723:
            continue
        # A PEP 723 block marks a file as a runnable script by definition.
        candidates.append((score_candidate(f, root, tree) + (35 if pep723 else 0), f, tree))
    if pkg_mains:
        for m in pkg_mains:
            tree = parse_py(m)
            if tree is not None:
                candidates.append((score_candidate(m, root, tree) + 20, m, tree))

    if not candidates:
        # Last resort: a project holding a single script (no main guard, no known name).
        lone = [f for f in iter_py_files(root, skip_non_entry_dirs=True)
                if f.name not in NEVER_ENTRY_FILES]
        if len(lone) == 1:
            info(f"Only one Python file in the project; using {lone[0].relative_to(root)} as entry point.")
            return entry_from_file(lone[0], False, "single script in project")
        die("No entry point found. Pass one explicitly: --entry path/to/main.py "
            "or --entry package.module:function")

    candidates.sort(key=lambda t: (-t[0], len(t[1].parts), str(t[1])))
    best_score, best, _ = candidates[0]
    if len(candidates) > 1:
        others = ", ".join(f"{c[1].relative_to(root)}({c[0]})" for c in candidates[1:6])
        info(f"Entry candidates (score): {best.relative_to(root)}({best_score}) <- chosen; others: {others}")
        if candidates[1][0] == best_score:
            warn("Top candidates have equal score; pass --entry to be explicit.")
    return entry_from_file(best, False, "heuristic scan")


# --------------------------------------------------------------------------------------
# Dependency collection
# --------------------------------------------------------------------------------------

@dataclass
class Deps:
    specs: List[str] = field(default_factory=list)           # PEP 508 requirement strings
    req_files: List[Path] = field(default_factory=list)      # passed with -r
    constraint_files: List[Path] = field(default_factory=list)  # passed with -c
    sources: List[str] = field(default_factory=list)
    install_project: bool = False                            # `pip install <root>` needed
    inferred: List[str] = field(default_factory=list)
    project_name: Optional[str] = None
    project_version: Optional[str] = None
    dist_names: Set[str] = field(default_factory=set)        # normalized names (for plugins)
    locked: Optional[str] = None                             # lock file that pinned everything

    def add_specs(self, specs: Iterable[str], source: str) -> None:
        added = 0
        for s in specs:
            s = s.strip()
            if not s or s.startswith("#"):
                continue
            if s not in self.specs:
                self.specs.append(s)
                added += 1
            self.dist_names.add(normalize_name(spec_name(s)))
        if added:
            self.sources.append(f"{source} ({added})")

    def is_empty(self) -> bool:
        return not (self.specs or self.req_files or self.install_project or self.locked)


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def spec_name(spec: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return m.group(1) if m else spec


PEP723_RE = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def has_pep723_block(path: Path) -> bool:
    """Cheap check (no TOML parser needed): does this file carry PEP 723 metadata?"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(m.group("type") == "script" for m in PEP723_RE.finditer(text))


def parse_pep723(script: Path) -> List[str]:
    text = script.read_text(encoding="utf-8", errors="replace")
    blocks = [m for m in PEP723_RE.finditer(text) if m.group("type") == "script"]
    if not blocks:
        return []
    content = "".join(line[2:] if line.startswith("# ") else line[1:]
                      for line in blocks[0].group("content").splitlines(keepends=True))
    if _TOML is None:
        warn("PEP 723 block found but no TOML parser available (pip install tomli).")
        return []
    try:
        data = _TOML.loads(content)
    except Exception as exc:  # noqa: BLE001
        warn(f"Invalid PEP 723 metadata in {script.name}: {exc}")
        return []
    return [d for d in data.get("dependencies", []) if isinstance(d, str)]


def poetry_constraint_to_pep440(version: str) -> str:
    """Convert Poetry caret/tilde/wildcard constraints to PEP 440."""
    version = version.strip()
    if version in ("", "*"):
        return ""
    if "||" in version:  # OR constraints cannot be expressed; take the first alternative
        version = version.split("||")[0].strip()
    parts = [p.strip() for p in version.split(",")]
    out: List[str] = []
    for p in parts:
        if p.startswith("^"):
            nums = _version_nums(p[1:])
            if nums is None:
                out.append(f">={p[1:]}")
                continue
            upper = list(nums)
            idx = next((i for i, n in enumerate(nums) if n != 0), len(nums) - 1)
            idx = min(idx, len(nums) - 1)
            upper = nums[: idx + 1]
            upper[-1] += 1
            upper += [0] * (len(nums) - len(upper))
            out.append(f">={p[1:]},<{'.'.join(map(str, upper))}")
        elif p.startswith("~") and not p.startswith("~="):
            nums = _version_nums(p[1:])
            if nums is None:
                out.append(f">={p[1:]}")
                continue
            if len(nums) == 1:
                upper = [nums[0] + 1]
            else:
                upper = [nums[0], nums[1] + 1] + [0] * (len(nums) - 2)
            out.append(f">={p[1:]},<{'.'.join(map(str, upper))}")
        elif p.endswith(".*") and not p.startswith(("=", "<", ">", "!", "~")):
            out.append(f"=={p}")
        elif p[0].isdigit():
            out.append(f"=={p}")
        else:
            out.append(p)
    return ",".join(out)


def _version_nums(v: str) -> Optional[List[int]]:
    v = v.split("+")[0]
    if not re.fullmatch(r"\d+(\.\d+)*", v):
        return None
    return [int(x) for x in v.split(".")]


def poetry_dep_to_spec(name: str, value, root: Path) -> Optional[str]:
    if normalize_name(name) == "python":
        return None
    if isinstance(value, list):  # multiple constraints, take the first
        value = value[0] if value else "*"
    if isinstance(value, str):
        return f"{name}{poetry_constraint_to_pep440(value)}"
    if isinstance(value, dict):
        if value.get("optional"):
            return None
        extras = value.get("extras") or []
        base = name + (f"[{','.join(extras)}]" if extras else "")
        marker = value.get("markers")
        suffix = f" ; {marker}" if marker else ""
        if value.get("git"):
            rev = value.get("rev") or value.get("tag") or value.get("branch")
            url = value["git"]
            if not url.startswith("git+"):
                url = "git+" + url
            return f"{base} @ {url}{'@' + rev if rev else ''}{suffix}"
        if value.get("url"):
            return f"{base} @ {value['url']}{suffix}"
        if value.get("path"):
            p = (root / value["path"]).resolve()
            return f"{base} @ {p.as_uri()}{suffix}"
        return f"{base}{poetry_constraint_to_pep440(str(value.get('version', '*')))}{suffix}"
    return str(name)


def _poetry_specs(table: dict, root: Path) -> List[str]:
    specs = []
    for name, value in table.items():
        spec = poetry_dep_to_spec(name, value, root)
        if spec:
            specs.append(spec)
    return specs


def deps_from_pyproject(root: Path, deps: Deps, extras: Sequence[str], dev: bool) -> None:
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return
    data = load_toml(pp)
    if not data:
        return
    project = data.get("project", {}) or {}
    tool = data.get("tool", {}) or {}
    poetry = tool.get("poetry", {}) or {}

    deps.project_name = project.get("name") or poetry.get("name") or deps.project_name
    version = project.get("version") or poetry.get("version")
    if isinstance(version, str):
        deps.project_version = version

    # PEP 621
    pdeps = project.get("dependencies")
    if isinstance(pdeps, list):
        deps.add_specs([d for d in pdeps if isinstance(d, str)], "pyproject [project].dependencies")
    opt = project.get("optional-dependencies", {}) or {}
    for extra in extras:
        if extra in opt:
            deps.add_specs([d for d in opt[extra] if isinstance(d, str)],
                           f"pyproject optional-dependencies[{extra}]")
        elif not poetry.get("extras", {}).get(extra):
            warn(f"Requested extra {extra!r} not found in pyproject.toml")
    if dev:
        for grp, items in (data.get("dependency-groups", {}) or {}).items():
            deps.add_specs([d for d in items if isinstance(d, str)], f"pyproject dependency-groups[{grp}]")
    dynamic = project.get("dynamic", []) or []
    if "dependencies" in dynamic:
        info("pyproject declares dynamic dependencies -> will `pip install` the project itself.")
        deps.install_project = True

    # Poetry
    pd = poetry.get("dependencies", {}) or {}
    if pd:
        deps.add_specs(_poetry_specs(pd, root), "pyproject [tool.poetry.dependencies]")
        for extra in extras:
            names = poetry.get("extras", {}).get(extra, []) or []
            for n in names:
                if n in pd and isinstance(pd[n], dict):
                    v = dict(pd[n])
                    v.pop("optional", None)
                    s = poetry_dep_to_spec(n, v, root)
                    if s:
                        deps.add_specs([s], f"poetry extras[{extra}]")
    if dev:
        for grp, table in (poetry.get("group", {}) or {}).items():
            gd = (table or {}).get("dependencies", {}) or {}
            deps.add_specs(_poetry_specs(gd, root), f"poetry group[{grp}]")
        dd = poetry.get("dev-dependencies", {}) or {}
        deps.add_specs(_poetry_specs(dd, root), "poetry dev-dependencies")

    # Hatch (rare, but Tuitka supported it)
    hatch_deps = ((tool.get("hatch", {}) or {}).get("metadata", {}) or {}).get("dependencies")
    if isinstance(hatch_deps, list):
        deps.add_specs([d for d in hatch_deps if isinstance(d, str)], "pyproject [tool.hatch.metadata]")


def deps_from_setup(root: Path, deps: Deps) -> None:
    cfg = root / "setup.cfg"
    if cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg, encoding="utf-8")
            raw = parser.get("options", "install_requires", fallback="")
            specs = [line.strip() for line in raw.splitlines() if line.strip()]
            deps.add_specs(specs, "setup.cfg install_requires")
            name = parser.get("metadata", "name", fallback=None)
            if name and not deps.project_name:
                deps.project_name = name
            ver = parser.get("metadata", "version", fallback=None)
            if ver and not ver.startswith("attr:") and not deps.project_version:
                deps.project_version = ver
        except configparser.Error:
            pass
    sp = root / "setup.py"
    if sp.is_file():
        tree = parse_py(sp)
        if tree is None:
            return
        reqs = _setup_kwarg(tree, "install_requires")
        if isinstance(reqs, (list, tuple)) and all(isinstance(r, str) for r in reqs):
            deps.add_specs(reqs, "setup.py install_requires")
        elif reqs is None and _setup_call_present(tree) and not deps.specs:
            info("setup.py install_requires is not a literal -> will `pip install` the project itself.")
            deps.install_project = True
        name = _setup_kwarg(tree, "name")
        if isinstance(name, str) and not deps.project_name:
            deps.project_name = name
        ver = _setup_kwarg(tree, "version")
        if isinstance(ver, str) and not deps.project_version:
            deps.project_version = ver


def _setup_call_present(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "setup"
               for n in ast.walk(tree))


def deps_from_pipfile(root: Path, deps: Deps, dev: bool) -> None:
    lock = root / "Pipfile.lock"
    if lock.is_file():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            warn(f"Pipfile.lock unreadable: {exc}")
            data = {}
        sections = ["default"] + (["develop"] if dev else [])
        for sec in sections:
            specs = []
            for name, meta in (data.get(sec, {}) or {}).items():
                if not isinstance(meta, dict):
                    continue
                extras = meta.get("extras") or []
                base = name + (f"[{','.join(extras)}]" if extras else "")
                marker = meta.get("markers")
                suffix = f" ; {marker}" if marker else ""
                if meta.get("git"):
                    url = meta["git"] if meta["git"].startswith("git+") else "git+" + meta["git"]
                    ref = meta.get("ref")
                    specs.append(f"{base} @ {url}{'@' + ref if ref else ''}{suffix}")
                elif meta.get("file"):
                    specs.append(f"{base} @ {meta['file']}{suffix}")
                elif meta.get("path"):
                    specs.append(f"{base} @ {(root / meta['path']).resolve().as_uri()}{suffix}")
                else:
                    specs.append(f"{base}{meta.get('version', '')}{suffix}")
            deps.add_specs(specs, f"Pipfile.lock [{sec}]")
        if deps.specs:
            return
    pf = root / "Pipfile"
    if not pf.is_file():
        return
    data = load_toml(pf)
    sections = ["packages"] + (["dev-packages"] if dev else [])
    for sec in sections:
        specs = []
        for name, value in (data.get(sec, {}) or {}).items():
            if isinstance(value, str):
                v = value.strip()
                if v in ("", "*"):
                    specs.append(name)
                elif v[0].isdigit():
                    specs.append(f"{name}=={v}")
                else:
                    specs.append(f"{name}{v}")
            elif isinstance(value, dict):
                s = poetry_dep_to_spec(name, {k: v for k, v in value.items() if k != "optional"}, root)
                if s:
                    specs.append(s)
        deps.add_specs(specs, f"Pipfile [{sec}]")


REQ_FILE_PRIMARY = [
    "requirements.txt", "requirements/requirements.txt", "requirements/base.txt",
    "requirements/main.txt", "requirements/prod.txt", "requirements/production.txt",
    "requirements/common.txt", "requirements/core.txt", "requirements/runtime.txt",
    "requirements-prod.txt", "requirements-production.txt", "requirements_prod.txt",
]
REQ_FILE_DEV_HINTS = ("dev", "test", "lint", "doc", "ci", "build", "typing", "format", "check")


def deps_from_requirement_files(root: Path, deps: Deps, all_files: bool, dev: bool,
                                explicit: Sequence[str], explicit_only: bool = False) -> None:
    seen: Set[Path] = set()

    def add(p: Path, source: str) -> None:
        p = p.resolve()
        if p in seen or not p.is_file():
            return
        seen.add(p)
        deps.req_files.append(p)
        deps.sources.append(source)
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith(("-", "http", "git+", "/", ".")):
                deps.dist_names.add(normalize_name(spec_name(line)))

    for e in explicit:
        p = Path(e) if Path(e).is_absolute() else root / e
        if not p.is_file():
            die(f"--req file not found: {p}")
        add(p, f"--req {e}")

    if explicit_only:
        return
    candidates: List[Path] = []
    for rel in REQ_FILE_PRIMARY:
        candidates.append(root / rel)
    if all_files or dev:
        candidates += sorted(root.glob("requirements*.txt")) + sorted(root.glob("requirements/*.txt")) \
            + sorted(root.glob("requirements*.in"))
    for c in candidates:
        if not c.is_file():
            continue
        lowered = c.name.lower()
        is_dev = any(h in lowered for h in REQ_FILE_DEV_HINTS)
        if is_dev and not dev:
            debug(f"skipping dev-ish requirements file {c.relative_to(root)}")
            continue
        add(c, f"requirements file {c.relative_to(root)}")

    for c in (root / "constraints.txt", root / "requirements" / "constraints.txt"):
        if c.is_file() and c.resolve() not in seen:
            deps.constraint_files.append(c.resolve())
            deps.sources.append(f"constraints {c.relative_to(root)}")


def deps_from_environment_yml(root: Path, deps: Deps) -> None:
    for name in ("environment.yml", "environment.yaml", "conda.yml", "conda.yaml"):
        p = root / name
        if not p.is_file():
            continue
        pip_specs: List[str] = []
        conda_names: List[str] = []
        in_deps = False
        in_pip = False
        pip_indent = -1
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            if indent == 0:
                in_deps = stripped.startswith("dependencies:")
                in_pip = False
                continue
            if not in_deps:
                continue
            if in_pip and indent > pip_indent:
                if stripped.startswith("- "):
                    pip_specs.append(stripped[2:].strip().strip("'\""))
                continue
            in_pip = False
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip("'\"")
                if item.rstrip(":") == "pip" and item.endswith(":"):
                    in_pip = True
                    pip_indent = indent
                    continue
                if item.startswith("pip:"):
                    in_pip = True
                    pip_indent = indent
                    continue
                m = re.match(r"([A-Za-z0-9_.-]+)\s*(=+)\s*([^=\s]+)?", item)
                if m:
                    n = m.group(1)
                    if normalize_name(n) in ("python", "pip", "setuptools", "wheel"):
                        continue
                    ver = m.group(3)
                    eq = m.group(2)
                    if not ver:
                        conda_names.append(n)
                    elif eq == "=" and _version_nums(ver) and len(_version_nums(ver)) < 3:
                        conda_names.append(f"{n}=={ver}.*")   # conda "pkg=1.2" means 1.2.*
                    else:
                        conda_names.append(f"{n}=={ver}")
                elif re.fullmatch(r"[A-Za-z0-9_.-]+", item):
                    if normalize_name(item) not in ("python", "pip", "setuptools", "wheel"):
                        conda_names.append(item)
        if pip_specs:
            deps.add_specs(pip_specs, f"{name} pip section")
        if conda_names:
            warn(f"{name}: conda packages mapped to PyPI names 1:1 (best effort): "
                 + ", ".join(conda_names[:8]) + (" ..." if len(conda_names) > 8 else ""))
            deps.add_specs(conda_names, f"{name} conda packages")
        break


def _dist_names_from_req_file(path: Path, deps: Deps) -> None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith(("-", "http", "git+", "/", ".")):
            deps.dist_names.add(normalize_name(spec_name(line)))


def deps_from_lockfiles(root: Path, deps: Deps, args: argparse.Namespace, uv: Optional[str],
                        build_dir: Path, out_name: str = "requirements.lock.txt") -> None:
    """Exact pins from uv.lock / poetry.lock / pdm.lock. A lock is what the project really
    runs with, and it spares the resolver from large loose dependency graphs."""
    if args.no_lock:
        return
    uv_lock = root / "uv.lock"
    if uv_lock.is_file():
        if uv and uv != "uv" or (uv == "uv" and args.dry_run):
            out = build_dir / out_name
            cmd = [uv, "export", "--directory", str(root), "--frozen", "--no-hashes",
                   "--no-emit-project", "--no-header", "--no-annotate", "--output-file", str(out)]
            if not args.dev:
                cmd.append("--no-dev")
            for extra in args.extras:
                cmd += ["--extra", extra]
            if args.dry_run:
                info("[dry-run] $ " + fmt_cmd(cmd))
                deps.locked = "uv.lock"
                deps.sources.append("uv.lock (uv export)")
                return
            build_dir.mkdir(parents=True, exist_ok=True)
            res = run(cmd, check=False, capture=True)
            if res.returncode == 0 and out.is_file():
                deps.req_files.append(out)
                deps.sources.append("uv.lock (uv export)")
                deps.locked = "uv.lock"
                _dist_names_from_req_file(out, deps)
                return
            warn("uv export failed; parsing uv.lock directly. Output:\n" + (res.stdout or "")[-1500:])
        specs = parse_uv_lock(uv_lock)
        if specs:
            deps.add_specs(specs, "uv.lock (parsed)")
            deps.locked = "uv.lock"
        return
    poetry_lock = root / "poetry.lock"
    if poetry_lock.is_file():
        specs = parse_poetry_lock(poetry_lock, args.extras, args.dev)
        if specs:
            deps.add_specs(specs, "poetry.lock")
            deps.locked = "poetry.lock"
        return
    pdm_lock = root / "pdm.lock"
    if pdm_lock.is_file():
        specs = parse_pdm_lock(pdm_lock, args.dev)
        if specs:
            deps.add_specs(specs, "pdm.lock")
            deps.locked = "pdm.lock"


def _lock_marker(entry: dict) -> str:
    markers = entry.get("resolution-markers") or ([entry["markers"]] if entry.get("markers") else [])
    markers = [m for m in markers if isinstance(m, str) and m.strip()]
    if not markers:
        return ""
    return " ; " + (markers[0] if len(markers) == 1 else " or ".join(f"({m})" for m in markers))


def parse_uv_lock(path: Path) -> List[str]:
    data = load_toml(path)
    specs: List[str] = []
    for pkg in data.get("package", []) or []:
        name, version, source = pkg.get("name"), pkg.get("version"), pkg.get("source") or {}
        if not name or not version:
            continue
        if any(k in source for k in ("virtual", "editable", "directory", "path")):
            continue                                   # the project itself / local paths
        marker = _lock_marker(pkg)
        if "git" in source:
            url = source["git"]
            specs.append(f"{name} @ git+{url}{marker}")
        elif "url" in source:
            specs.append(f"{name} @ {source['url']}{marker}")
        else:
            specs.append(f"{name}=={version}{marker}")
    return specs


def parse_poetry_lock(path: Path, extras: Sequence[str], dev: bool) -> List[str]:
    data = load_toml(path)
    wanted_optional: Set[str] = set()
    for extra in extras:
        for item in (data.get("extras", {}) or {}).get(extra, []) or []:
            wanted_optional.add(normalize_name(str(item).split(" ")[0]))
    specs: List[str] = []
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        if pkg.get("optional") and normalize_name(name) not in wanted_optional:
            continue
        groups = pkg.get("groups")
        category = pkg.get("category")
        if groups is not None and "main" not in groups and not dev:
            continue
        if category not in (None, "main") and not dev:
            continue
        source = pkg.get("source") or {}
        marker = _lock_marker(pkg)
        if source.get("type") == "git":
            ref = source.get("resolved_reference") or source.get("reference")
            specs.append(f"{name} @ git+{source.get('url')}{'@' + ref if ref else ''}{marker}")
        elif source.get("type") in ("directory", "file"):
            continue
        elif source.get("type") == "url":
            specs.append(f"{name} @ {source.get('url')}{marker}")
        else:
            specs.append(f"{name}=={version}{marker}")
    return specs


def parse_pdm_lock(path: Path, dev: bool) -> List[str]:
    data = load_toml(path)
    specs: List[str] = []
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        groups = pkg.get("groups") or ["default"]
        if "default" not in groups and not dev:
            continue
        marker = f" ; {pkg['marker']}" if pkg.get("marker") else ""
        if pkg.get("git"):
            ref = pkg.get("revision") or pkg.get("ref")
            specs.append(f"{name} @ git+{pkg['git']}{'@' + ref if ref else ''}{marker}")
        elif pkg.get("path") or pkg.get("editable"):
            continue
        else:
            specs.append(f"{name}=={version}{marker}")
    return specs


def infer_from_imports(root: Path, import_roots: Sequence[Path], entry: Entry) -> List[str]:
    """Fallback: third-party top-level imports across the project -> PyPI names."""
    local = local_top_level_names(import_roots)
    found: Set[str] = set()
    for f in iter_py_files(root, skip_non_entry_dirs=True):
        tree = parse_py(f)
        if tree is None:
            continue
        for name in dotted_imports(tree):
            top = name.split(".")[0]
            if top in STDLIB_NAMES or top in local or top.startswith("_"):
                continue
            if name in IMPORT_TO_DIST:
                found.add(IMPORT_TO_DIST[name])
            elif top in IMPORT_TO_DIST:
                found.add(IMPORT_TO_DIST[top])
            elif top:
                found.add(top)
    return sorted(found)


def collect_dependencies(root: Path, import_roots: Sequence[Path], entry: Entry,
                         args: argparse.Namespace, uv: Optional[str] = None) -> Deps:
    deps = Deps()
    entry_script = entry.path if entry.kind != "package" else entry.path / "__main__.py"
    if entry_script.is_file():
        pep = parse_pep723(entry_script)
        if pep:
            deps.add_specs(pep, f"PEP 723 metadata in {entry_script.name}")
    deps_from_lockfiles(root, deps, args, uv, root / BUILD_DIRNAME)
    if deps.locked:
        # exact pins win; manifests are still read for name/version/extras metadata
        meta = Deps()
        deps_from_pyproject(root, meta, args.extras, args.dev)
        deps_from_setup(root, meta)
        deps.project_name, deps.project_version = meta.project_name, meta.project_version
        deps.dist_names |= meta.dist_names
        deps.install_project = meta.install_project
        deps_from_requirement_files(root, deps, False, args.dev, args.req, explicit_only=True)
    else:
        deps_from_pyproject(root, deps, args.extras, args.dev)
        deps_from_setup(root, deps)
        deps_from_pipfile(root, deps, args.dev)
        deps_from_requirement_files(root, deps, args.all_requirements, args.dev, args.req)
        deps_from_environment_yml(root, deps)
    if args.install_project:
        deps.install_project = True
    if deps.install_project and not ((root / "pyproject.toml").is_file() or (root / "setup.py").is_file()):
        warn("--install-project requested but the project has no pyproject.toml/setup.py; ignoring.")
        deps.install_project = False

    if deps.is_empty() and not args.no_infer:
        inferred = infer_from_imports(root, import_roots, entry)
        if inferred:
            warn("No dependency manifest found; inferring from imports (best effort): "
                 + ", ".join(inferred))
            deps.inferred = inferred
            deps.add_specs(inferred, "import scan (inferred)")
    return deps


# --------------------------------------------------------------------------------------
# Environment (venv / uv) management
# --------------------------------------------------------------------------------------

NUITKA_SUPPORTED_MINORS = list(range(14, 7, -1))   # 3.14 ... 3.8, highest first


def parse_version(v: str) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def _pad(t: Sequence[int], n: int = 3) -> Tuple[int, ...]:
    return tuple(t) + (0,) * (n - len(t))


def version_satisfies(version: Sequence[int], spec: Optional[str]) -> bool:
    """Minimal PEP 440 specifier check for interpreter versions (no `packaging` needed).
    `==3.12` and `==3.12.*` both mean "any 3.12.x" here, which is what people intend."""
    if not spec:
        return True
    v = _pad(version)
    for clause in spec.split(","):
        clause = clause.strip()
        m = re.match(r"(===|==|!=|~=|>=|<=|>|<)\s*([\d.*]+)$", clause)
        if not m:
            continue                       # unknown clause: be permissive
        op, raw = m.group(1), m.group(2)
        target = parse_version(raw)
        n = len(target)
        if op in ("==", "==="):
            if tuple(v[:n]) != target:
                return False
        elif op == "!=":
            if tuple(v[:n]) == target:
                return False
        elif op == "~=":
            if v < _pad(target):
                return False
            upper = list(target[:-1]) or [target[0]]
            upper[-1] += 1
            if v >= _pad(upper):
                return False
        elif op == ">=" and v < _pad(target):
            return False
        elif op == ">" and v <= _pad(target):
            return False
        elif op == "<=" and v > _pad(target):
            return False
        elif op == "<" and v >= _pad(target):
            return False
    return True


def read_requires_python(root: Path) -> Optional[str]:
    """`requires-python` from pyproject (PEP 621 or Poetry) or uv.lock."""
    pp = root / "pyproject.toml"
    spec = None
    if pp.is_file():
        data = load_toml(pp)
        spec = (data.get("project", {}) or {}).get("requires-python")
        if not spec:
            poetry_py = (((data.get("tool", {}) or {}).get("poetry", {}) or {})
                         .get("dependencies", {}) or {}).get("python")
            if isinstance(poetry_py, str):
                spec = poetry_constraint_to_pep440(poetry_py) or None
    if not spec and (root / "uv.lock").is_file():
        m = re.search(r'^requires-python\s*=\s*"([^"]+)"',
                      (root / "uv.lock").read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            spec = m.group(1)
    return spec.strip() if isinstance(spec, str) and spec.strip() else None


def base_python_of(exe: str) -> str:
    """The real interpreter behind a venv python (needed after the tools-venv re-exec)."""
    cfg = Path(exe).resolve().parent.parent / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("home"):
                home = Path(line.split("=", 1)[1].strip())
                for cand in (Path(exe).name, "python3", "python"):
                    if (home / cand).exists():
                        return str(home / cand)
    return exe


HOST_PYTHON = base_python_of(sys.executable)


def tools_dir(root: Path, args: argparse.Namespace) -> Path:
    """Where the tools venv (uv, tomli) lives: shared per host Python version."""
    tag = "py%d.%d" % sys.version_info[:2]
    if args.cache_dir:
        return Path(args.cache_dir).resolve() / "tools" / tag
    if home_writable():
        base = Path(os.environ.get("LOCALAPPDATA", "")) if IS_WINDOWS and os.environ.get("LOCALAPPDATA") \
            else Path.home() / ".cache"
        return base / "nuitka_build" / "tools" / tag
    return root / BUILD_DIRNAME / "tools" / tag


def ensure_tools_env(root: Path, args: argparse.Namespace, packages: Sequence[str]) -> Optional[str]:
    """Create (once) a small venv with the host Python and pip-install `packages` there."""
    tdir = tools_dir(root, args)
    py = venv_python(tdir)
    if not py.exists():
        if args.dry_run:
            return None
        step(f"Creating tools environment at {tdir}")
        run([HOST_PYTHON, "-m", "venv", str(tdir)], check=False)
        if not py.exists():
            warn("Could not create the tools environment (python -m venv failed).")
            return None
        ensure_pip(str(py), False)
    missing = [pkg for pkg in packages
               if subprocess.run([str(py), "-c", f"import {pkg}"], capture_output=True).returncode != 0]
    if missing and not args.dry_run:
        cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q"] + missing
        if args.index_url:
            cmd += ["--index-url", args.index_url]
        run(cmd, check=False)
    return str(py)


def find_uv(root: Path, args: argparse.Namespace) -> Optional[str]:
    """uv on PATH, else bootstrapped into the tools venv (unless --installer pip)."""
    if args.installer == "pip":
        return None
    found = shutil.which("uv")
    if found:
        return found
    tdir = tools_dir(root, args)
    uv_bin = tdir / ("Scripts/uv.exe" if IS_WINDOWS else "bin/uv")
    if uv_bin.exists():
        return str(uv_bin)
    if args.dry_run:
        info("uv not on PATH; a real run bootstraps it into the tools environment (pip install uv).")
        return "uv"
    info("uv not on PATH -> bootstrapping it (fast resolver; pip's resolver gives up on large projects).")
    ensure_tools_env(root, args, ["uv"])
    if uv_bin.exists():
        return str(uv_bin)
    if args.installer == "uv":
        die("--installer uv requested but uv could not be installed.")
    warn("uv bootstrap failed; falling back to pip.")
    return None


@dataclass
class PythonChoice:
    request: str            # interpreter path, or "3.X" when uv should provide it
    version: Optional[Tuple[int, ...]]
    note: str
    managed: bool = False   # True: uv must use/download its own CPython, not a system one

    @property
    def is_version(self) -> bool:
        return looks_like_version(self.request)


def choose_python(args: argparse.Namespace, required: Optional[str], uv: Optional[str]) -> PythonChoice:
    """Pick the interpreter for the build env, honouring the project's requires-python."""
    if args.python:
        req = resolve_python(args.python, allow_version=bool(uv))
        ver = parse_version(req) if looks_like_version(req) else parse_version(python_version_of(req))
        if required and ver and not version_satisfies(ver, required):
            warn(f"--python {args.python} is {'.'.join(map(str, ver))} but the project requires "
                 f"Python {required}; continuing as requested.")
        return PythonChoice(req, ver or None, "--python")

    def usable(exe: str) -> bool:
        # With uv around, skip interpreters that lack Python.h (distro pythons without the
        # -devel package): uv's downloads ship the headers, so they are the better choice.
        return not (uv and headers_problem(exe))

    host_ver = tuple(sys.version_info[:3])
    if version_satisfies(host_ver, required) and usable(HOST_PYTHON):
        return PythonChoice(HOST_PYTHON, host_ver, "host interpreter")

    for minor in NUITKA_SUPPORTED_MINORS:
        if not version_satisfies((3, minor, 0), required):
            continue
        exe = shutil.which(f"python3.{minor}")
        if exe and usable(exe):
            ver = parse_version(python_version_of(exe))
            if version_satisfies(ver, required):
                return PythonChoice(exe, ver, "found on PATH")

    if uv:
        # prefer the host's minor (fewest surprises), then recent versions with broad wheel support
        order = [sys.version_info[1]] + [m for m in (13, 12, 14, 11, 10, 9, 8) if m != sys.version_info[1]]
        for minor in order:
            if minor in NUITKA_SUPPORTED_MINORS and version_satisfies((3, minor, 0), required):
                why = "uv-managed (downloaded on demand"
                if version_satisfies(host_ver, required):
                    why += "; host Python has no Python.h"
                return PythonChoice(f"3.{minor}", (3, minor), why + ")", managed=True)

    host = "%d.%d" % sys.version_info[:2]
    die(f"Project requires Python {required} but this is {host} and no matching interpreter is "
        f"on PATH. Install one (RHEL/Rocky: dnf install python3.12 python3.12-devel), pass "
        f"--python /path/to/python, or install uv so it can be downloaded automatically.")


def run(cmd: Sequence[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
        check: bool = True, capture: bool = False, dry_run: bool = False) -> subprocess.CompletedProcess:
    info(("$ " if not dry_run else "[dry-run] $ ") + fmt_cmd(cmd) + (f"   (cwd={cwd})" if cwd else ""))
    if dry_run:
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    try:
        return subprocess.run(list(map(str, cmd)), cwd=str(cwd) if cwd else None, env=env,
                              check=check, text=True,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None)
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            sys.stderr.write(exc.stdout[-4000:])
        die(f"Command failed with exit code {exc.returncode}: {fmt_cmd(cmd)}", exc.returncode or 1)
    except FileNotFoundError:
        die(f"Command not found: {cmd[0]}")


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def looks_like_version(spec: str) -> bool:
    return bool(re.fullmatch(r"3(\.\d+){0,2}", spec))


def resolve_python(spec: Optional[str], allow_version: bool) -> str:
    if not spec:
        return sys.executable
    p = Path(spec)
    if p.exists():
        return str(p.resolve())
    w = shutil.which(spec)
    if w:
        return w
    if looks_like_version(spec):
        if allow_version:
            return spec  # uv can download/resolve "3.12" itself
        w = shutil.which(f"python{spec}")
        if w:
            return w
        if IS_WINDOWS and shutil.which("py"):
            try:
                out = subprocess.run(["py", f"-{spec}", "-c", "import sys;print(sys.executable)"],
                                     capture_output=True, text=True, check=True).stdout.strip()
                if out:
                    return out
            except subprocess.CalledProcessError:
                pass
    die(f"Python interpreter {spec!r} not found. Give a path (e.g. /usr/bin/python3.12) "
        f"or install uv, which can fetch versions on demand.")


def ensure_pip(python: str, dry_run: bool) -> bool:
    """uv-created venvs ship without pip; try ensurepip before giving up on the venv."""
    if dry_run:
        return True
    if subprocess.run([python, "-c", "import pip"], capture_output=True).returncode == 0:
        return True
    info("pip is missing in the environment; running ensurepip")
    subprocess.run([python, "-m", "ensurepip", "--upgrade", "--default-pip"], capture_output=True)
    return subprocess.run([python, "-c", "import pip"], capture_output=True).returncode == 0


def python_version_of(python: str) -> str:
    try:
        return subprocess.run([python, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "?"


@dataclass
class Env:
    python: str          # interpreter used to run nuitka
    installer: str       # "uv" | "pip"
    venv_dir: Optional[Path]
    env_vars: Optional[dict] = None   # environment for subprocesses (set after creation)
    uv: Optional[str] = None          # uv executable when installer == "uv"

    def install(self, pkgs: Sequence[str], args: argparse.Namespace, cwd: Optional[Path] = None) -> None:
        if not pkgs:
            return
        if self.installer == "uv":
            cmd = [self.uv or "uv", "pip", "install", "--python", self.python] + list(pkgs)
        else:
            cmd = [self.python, "-m", "pip", "install", "--disable-pip-version-check"]
            if not VERBOSE:
                cmd.append("-q")
            cmd += list(pkgs)
        if args.index_url:
            cmd += ["--index-url", args.index_url]
        run(cmd, cwd=cwd, env=self.env_vars, dry_run=args.dry_run)


def prepare_env(root: Path, args: argparse.Namespace, uv: Optional[str], choice: PythonChoice) -> Env:
    installer = "uv" if uv else "pip"
    if choice.is_version and not uv:
        die(f"Python {choice.request} was requested by version, which needs uv (pip install uv).")

    if args.system:
        py = choice.request if not choice.is_version else resolve_python(choice.request, allow_version=False)
        warn(f"--system: installing into the interpreter itself: {py}")
        return Env(python=py, installer=installer, venv_dir=None, uv=uv)

    venv_dir = (Path(args.venv).resolve() if args.venv else root / BUILD_DIRNAME / "venv")
    py = venv_python(venv_dir)
    env_vars = build_env_vars(root, args, venv_dir)

    if args.clean and venv_dir.exists():
        info(f"Removing existing environment {venv_dir}")
        if not args.dry_run:
            shutil.rmtree(venv_dir)

    if py.exists():
        have = python_version_of(str(py))
        want = ".".join(map(str, choice.version[:2])) if choice.version else None
        if have == "?":
            warn(f"Environment at {venv_dir} does not run (created by another interpreter or on "
                 f"another machine/container?); recreating it.")
        elif want and not have.startswith(want + "."):
            info(f"Environment is Python {have} but Python {want} is needed ({choice.note}); recreating it.")
        elif installer == "pip" and not ensure_pip(str(py), args.dry_run):
            warn(f"Environment at {venv_dir} has no usable pip (created by uv?); recreating it.")
        else:
            info(f"Reusing environment {venv_dir} (Python {have})")
            return Env(python=str(py), installer=installer, venv_dir=venv_dir, uv=uv, env_vars=env_vars)
        if not args.dry_run:
            shutil.rmtree(venv_dir)

    step(f"Creating virtual environment ({installer}, Python {choice.request}, {choice.note}) at {venv_dir}")
    if installer == "uv":
        cmd = [uv, "venv", "--python", choice.request, str(venv_dir)]
        if choice.managed:
            # a system python of the same version may lack headers; insist on uv's build
            cmd.insert(2, "--python-preference"); cmd.insert(3, "only-managed")
        res = run(cmd, env=env_vars, dry_run=args.dry_run, check=False)
        if res.returncode != 0 and choice.managed:
            warn("uv could not provide a managed Python (offline?); retrying with any interpreter.")
            run([uv, "venv", "--python", choice.request, str(venv_dir)], env=env_vars, dry_run=args.dry_run)
        elif res.returncode != 0:
            die(f"uv venv failed with exit code {res.returncode}")
    else:
        run([choice.request, "-m", "venv", str(venv_dir)], dry_run=args.dry_run)
        # keep pip fresh enough for modern wheels / pyproject builds
        run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
             "--upgrade", "pip"], dry_run=args.dry_run, check=False)
    return Env(python=str(py), installer=installer, venv_dir=venv_dir, uv=uv, env_vars=env_vars)


def install_everything(env: Env, deps: Deps, root: Path, args: argparse.Namespace) -> None:
    build_dir = root / BUILD_DIRNAME
    build_dir.mkdir(parents=True, exist_ok=True)
    gitignore = build_dir / ".gitignore"
    if not gitignore.exists() and not args.dry_run:
        gitignore.write_text("# created by nuitka_build.py\n*\n", encoding="utf-8")

    # 1) Nuitka + helpers (ordered-set speeds compilation, zstandard compresses onefile)
    nuitka_spec = args.nuitka_version or "nuitka"
    helpers = [nuitka_spec, "ordered-set", "zstandard"]
    if IS_LINUX:
        helpers.append("patchelf")  # Nuitka needs patchelf for standalone/onefile on Linux
    step(f"Installing Nuitka ({nuitka_spec}) + helpers")
    env.install(helpers, args)

    # 2) project dependencies
    pkgs: List[str] = []
    if deps.specs:
        gen = build_dir / "requirements.generated.txt"
        header = "# Generated by nuitka_build.py from: " + "; ".join(deps.sources) + "\n"
        if not args.dry_run:
            gen.write_text(header + "\n".join(deps.specs) + "\n", encoding="utf-8")
        pkgs += ["-r", str(gen)]
    for rf in deps.req_files:
        pkgs += ["-r", str(rf)]
    for cf in deps.constraint_files:
        pkgs += ["-c", str(cf)]
    pkgs += list(args.extra_req)
    if pkgs:
        step(f"Installing project dependencies ({len(deps.specs)} specs, {len(deps.req_files)} files)")
        env.install(pkgs, args, cwd=root)
    if deps.install_project:
        step("Installing the project itself (pip install <project>) to resolve its metadata")
        env.install([str(root)], args, cwd=root)
    if not pkgs and not deps.install_project:
        info("No third-party dependencies to install.")


# --------------------------------------------------------------------------------------
# Plugin / data / include detection
# --------------------------------------------------------------------------------------

def scan_project_imports(root: Path, entry: Entry) -> Set[str]:
    names: Set[str] = set()
    files = list(iter_py_files(root, skip_non_entry_dirs=True))
    entry_file = entry.path if entry.kind != "package" else entry.path / "__main__.py"
    if entry_file.is_file() and entry_file not in files:
        files.append(entry_file)
    for f in files:
        tree = parse_py(f)
        if tree is not None:
            names |= top_level_imports(tree)
    return names


def detect_plugins(imports: Set[str], deps: Deps, user_plugins: Sequence[str],
                   disabled: bool) -> List[str]:
    plugins: List[str] = list(dict.fromkeys(user_plugins))
    if disabled:
        return plugins
    for name in sorted(imports):
        p = PLUGIN_BY_IMPORT.get(name)
        if p and p not in plugins:
            plugins.append(p)
    for dist in sorted(deps.dist_names):
        p = PLUGIN_BY_DIST.get(dist)
        if p and p not in plugins:
            plugins.append(p)
    qt = [p for p in plugins if p in ("pyside6", "pyside2", "pyqt5", "pyqt6")]
    if len(qt) > 1:
        warn(f"Several Qt bindings detected {qt}; Nuitka allows only one. Keeping {qt[0]}; "
             f"use --plugin/--no-auto-plugins to override.")
        for extra in qt[1:]:
            plugins.remove(extra)
    return plugins


def dir_has_python(d: Path, limit: int = 2000) -> bool:
    n = 0
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [x for x in dirnames if x != "__pycache__"]
        for fn in filenames:
            n += 1
            if fn.endswith((".py", ".pyc", ".so", ".pyd", ".dll", ".dylib")):
                return True
            if n > limit:
                return False
    return False


def dir_size(d: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(d):
        for fn in filenames:
            try:
                total += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass
    return total


def detect_data_dirs(root: Path, import_roots: Sequence[Path], entry: Entry,
                     user_dirs: Sequence[str], disabled: bool) -> List[Tuple[Path, str]]:
    result: List[Tuple[Path, str]] = []
    seen: Set[Path] = set()

    def add(src: Path, dst: str) -> None:
        src = src.resolve()
        if src in seen:
            return
        seen.add(src)
        result.append((src, dst.replace("\\", "/").strip("/")))

    for spec in user_dirs:
        src_s, _, dst = spec.partition("=")
        src = Path(src_s) if Path(src_s).is_absolute() else root / src_s
        if not src.is_dir():
            die(f"--data-dir not a directory: {src}")
        add(src, dst or _target_for(src, import_roots, root))

    if disabled:
        return result

    scan_parents: List[Path] = [root]
    if entry.kind == "package":
        scan_parents.append(entry.path)
    elif entry.kind == "launcher":
        top = top_package_dir(entry.path)
        if top:
            scan_parents.append(top)
    else:
        scan_parents.append(entry.path.parent)
    for r in import_roots:
        scan_parents.append(r)

    for parent in dict.fromkeys(scan_parents):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or child.name.lower() not in DATA_DIR_NAMES or is_excluded_dir(child):
                continue
            if not any(child.rglob("*")):
                continue
            if dir_has_python(child):
                debug(f"skipping {child} as data dir: contains code")
                continue
            size = dir_size(child)
            if size > 512 * 1024 * 1024:
                warn(f"Data dir {child} is {human_size(size)}; skipping auto-include (add with --data-dir).")
                continue
            add(child, _target_for(child, import_roots, root))
    return result


def _target_for(src: Path, import_roots: Sequence[Path], root: Path) -> str:
    src = src.resolve()
    for r in sorted(import_roots, key=lambda p: -len(str(p))):
        try:
            return src.relative_to(r.resolve()).as_posix()
        except ValueError:
            continue
    try:
        return src.relative_to(root.resolve()).as_posix()
    except ValueError:
        return src.name


def detect_include_packages(root: Path, import_roots: Sequence[Path], entry: Entry,
                            imports: Set[str]) -> List[str]:
    """Local packages imported by the project; included explicitly so dynamic imports work."""
    local = local_top_level_names(import_roots)
    include: List[str] = []
    if entry.package:
        include.append(entry.package)
    for name in sorted(imports & local):
        if name in NON_ENTRY_DIR_NAMES or name in include:
            continue
        for r in import_roots:
            if (r / name).is_dir() and is_package_dir(r / name):
                include.append(name)
                break
    return include


# --------------------------------------------------------------------------------------
# Test-package exclusion (precise: keeps test packages that runtime code imports)
# --------------------------------------------------------------------------------------

TEST_DIR_NAMES = {"tests", "test"}


def find_test_packages(import_roots: Sequence[Path]) -> Dict[str, Tuple[Path, Path]]:
    """dotted name -> (directory, import root) for every `tests`/`test` package in the project."""
    found: Dict[str, Tuple[Path, Path]] = {}
    for r in import_roots:
        for top in sorted(r.iterdir()):
            if not (top.is_dir() and is_package_dir(top)) or is_excluded_dir(top):
                continue
            for dirpath, dirnames, _ in os.walk(top):
                d = Path(dirpath)
                dirnames[:] = sorted(n for n in dirnames if not is_excluded_dir(d / n))
                if d.name in TEST_DIR_NAMES and is_package_dir(d):
                    found[module_name_for(d / "__init__.py", r)] = (d, r)
                    dirnames[:] = []          # no need to look inside a test package
    return found


def absolute_imports_of(file: Path, tree: ast.Module, import_root: Path) -> Set[str]:
    """All imported module names of a file, with relative imports made absolute."""
    names: Set[str] = set()
    modname = module_name_for(file, import_root)
    pkg_parts = modname.split(".") if file.name == "__init__.py" else modname.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                keep = len(pkg_parts) - (node.level - 1)
                base_parts = pkg_parts[:keep] if keep > 0 else []
                base = ".".join(base_parts + ([node.module] if node.module else []))
            if base:
                names.add(base)
                names.update(f"{base}.{a.name}" for a in node.names if a.name != "*")
    return names


def test_package_of(dotted: str, test_pkgs: Dict[str, Tuple[Path, Path]]) -> Optional[str]:
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])
        if prefix in test_pkgs:
            return prefix
    return None


def plan_test_exclusion(import_roots: Sequence[Path]) -> Tuple[List[str], Dict[str, str]]:
    """Return (test packages safe to exclude, test packages needed at runtime -> importer)."""
    test_pkgs = find_test_packages(import_roots)
    if not test_pkgs:
        return [], {}
    needed: Dict[str, str] = {}

    def scan(files: Iterable[Tuple[Path, Path]]) -> Dict[str, str]:
        hits: Dict[str, str] = {}
        for f, r in files:
            tree = parse_py(f)
            if tree is None:
                continue
            for name in absolute_imports_of(f, tree, r):
                tp = test_package_of(name, test_pkgs)
                if tp and tp not in needed and tp not in hits:
                    hits[tp] = module_name_for(f, r)
        return hits

    runtime: List[Tuple[Path, Path]] = []
    for r in import_roots:
        for f in iter_py_files(r):
            rel = f.relative_to(r)
            if f.name == "conftest.py" or f.name.startswith("test_") or any(p in TEST_DIR_NAMES for p in rel.parts[:-1]):
                continue
            runtime.append((f, r))
    frontier = scan(runtime)
    while frontier:                      # transitive: kept test packages may import other ones
        needed.update(frontier)
        files = [(f, test_pkgs[tp][1]) for tp in frontier for f in iter_py_files(test_pkgs[tp][0])]
        frontier = scan(files)
    excluded = sorted(tp for tp in test_pkgs if tp not in needed
                      and not any(tp.startswith(k + ".") or k.startswith(tp + ".") for k in needed))
    return excluded, needed


# --------------------------------------------------------------------------------------
# Runtime import sanity check (catches dev-only deps reached from runtime code)
# --------------------------------------------------------------------------------------

def unguarded_top_level_imports(tree: ast.Module) -> Set[str]:
    """Top-level names of absolute imports that are NOT inside try/except or TYPE_CHECKING."""
    names: Set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Try):
                for handler in child.handlers:      # only the fallback branches are "sure"
                    visit(handler)
                continue
            if isinstance(child, ast.If):
                test = child.test
                if getattr(test, "id", None) == "TYPE_CHECKING" or getattr(test, "attr", None) == "TYPE_CHECKING":
                    for n in child.orelse:
                        visit(n)
                    continue
            if isinstance(child, ast.Import):
                names.update(a.name.split(".")[0] for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                names.add(child.module.split(".")[0])
            visit(child)

    visit(tree)
    return names


def project_third_party_imports(import_roots: Sequence[Path], kept_tests: Dict[str, str]) -> Set[str]:
    local = local_top_level_names(import_roots)
    test_pkgs = find_test_packages(import_roots)
    files: List[Path] = []
    for r in import_roots:
        for f in iter_py_files(r):
            rel = f.relative_to(r)
            if f.name == "conftest.py" or f.name.startswith("test_") or any(p in TEST_DIR_NAMES for p in rel.parts[:-1]):
                continue
            files.append(f)
    for tp in kept_tests:
        if tp in test_pkgs:
            files.extend(iter_py_files(test_pkgs[tp][0]))
    names: Set[str] = set()
    for f in files:
        tree = parse_py(f)
        if tree is not None:
            names |= unguarded_top_level_imports(tree)
    return {n for n in names if n and n not in STDLIB_NAMES and n not in local and not n.startswith("_")}


def missing_modules(python: str, modules: Sequence[str]) -> List[str]:
    if not modules:
        return []
    code = ("import importlib.util, sys\n"
            "bad = []\n"
            "for m in sys.argv[1:]:\n"
            "    try:\n"
            "        if importlib.util.find_spec(m) is None: bad.append(m)\n"
            "    except Exception: bad.append(m)\n"
            "print('\\n'.join(bad))")
    try:
        out = subprocess.run([python, "-c", code] + list(modules), capture_output=True, text=True,
                             check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [m for m in out.splitlines() if m.strip()]


def ensure_runtime_imports(root: Path, import_roots: Sequence[Path], kept_tests: Dict[str, str],
                           env: Env, deps: Deps, args: argparse.Namespace, uv: Optional[str]) -> None:
    """Every unguarded third-party import of runtime code must be installed, otherwise the
    binary fails at start. Test packages kept for runtime use often pull dev-only packages
    (pytest, freezegun, ...) - install the dev group when that happens."""
    wanted = sorted(project_third_party_imports(import_roots, kept_tests))
    missing = missing_modules(env.python, wanted)
    if not missing:
        return
    if not args.dev and not args.dry_run:
        info(f"Imported by project code but not installed: {', '.join(missing)} -> installing the "
             f"dev dependency group (kept test packages need it).")
        dev_args = argparse.Namespace(**vars(args))
        dev_args.dev = True
        extra = Deps()
        deps_from_lockfiles(root, extra, dev_args, uv, root / BUILD_DIRNAME, out_name="requirements.lock-dev.txt")
        if not extra.locked:
            deps_from_pyproject(root, extra, args.extras, True)
            deps_from_pipfile(root, extra, True)
        pkgs: List[str] = []
        for rf in extra.req_files:
            pkgs += ["-r", str(rf)]
        if extra.specs:
            gen = root / BUILD_DIRNAME / "requirements.dev.generated.txt"
            gen.write_text("\n".join(extra.specs) + "\n", encoding="utf-8")
            pkgs += ["-r", str(gen)]
        if pkgs:
            env.install(pkgs, args, cwd=root)
            deps.sources.append("dev group (runtime imports)")
            missing = missing_modules(env.python, missing)
    if missing:
        warn("Modules imported by project code are still not installed: " + ", ".join(missing)
             + ". The binary will fail if those imports run; add them with --extra-req or --req.")


# --------------------------------------------------------------------------------------
# Django profile
# --------------------------------------------------------------------------------------

DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


@dataclass
class DjangoInfo:
    settings_module: str
    settings_files: List[Path]
    apps: List[str]
    dotted: Set[str]


def detect_django(root: Path, import_roots: Sequence[Path], override: Optional[str]) -> Optional[DjangoInfo]:
    manage = root / "manage.py"
    settings_module = override or os.environ.get("DJANGO_SETTINGS_MODULE")
    if manage.is_file():
        text = manage.read_text(encoding="utf-8", errors="replace")
        if "django" not in text:
            manage = None
        else:
            m = re.search(r"DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([\w.]+)[\"']", text)
            if m and not override:
                settings_module = m.group(1)
    elif not settings_module:
        return None
    if not settings_module:
        for r in import_roots:
            for p in sorted(r.iterdir()):
                if p.is_dir() and is_package_dir(p) and (p / "settings.py").is_file():
                    settings_module = f"{p.name}.settings"
                    break
            if settings_module:
                break
    if not settings_module:
        return None
    target = resolve_module(settings_module, import_roots)
    files: List[Path] = []
    if target is None:
        warn(f"Django settings module {settings_module} not found on disk; only basic Django support applied.")
    elif target.is_dir():
        files = sorted(target.glob("*.py"))
    else:
        files = [target]
    apps: List[str] = []
    dotted: Set[str] = set()
    for f in files:
        tree = parse_py(f)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and DOTTED_RE.match(node.value):
                dotted.add(node.value)
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(getattr(t, "id", None) == "INSTALLED_APPS" for t in targets) and isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            apps.append(elt.value)
    return DjangoInfo(settings_module, files, apps, dotted)


def site_packages_of(python: str) -> Optional[Path]:
    try:
        out = subprocess.run([python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_dotted(dotted: str, search: Sequence[Path]) -> Optional[Tuple[str, bool]]:
    """Longest prefix of a dotted string that exists as a package/module on disk."""
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        rel = Path(*parts[:n])
        for base in search:
            d = base / rel
            if d.is_dir() and (is_package_dir(d) or any(d.glob("*.py"))):
                return ".".join(parts[:n]), True
            if d.with_suffix(".py").is_file():
                return ".".join(parts[:n]), False
    return None


def django_nuitka_flags(info: DjangoInfo, import_roots: Sequence[Path], python: str,
                        already: Sequence[str], excluded_tests: Sequence[str]) -> List[str]:
    """--include-* flags for everything Django loads by string (apps, middleware, backends)."""
    search = list(import_roots)
    sp = site_packages_of(python)
    if sp is not None and sp.is_dir():
        search.append(sp)
    local = local_top_level_names(import_roots)
    packages: List[str] = ["django"]
    modules: List[str] = []
    data: List[str] = []            # django's own data files come via Nuitka's package config
    included = set(already) | {"django"}

    def covered(name: str) -> bool:
        return any(name == inc or name.startswith(inc + ".") for inc in included)

    for app in info.apps:
        app_pkg = app.split(".apps.")[0] if ".apps." in app else app
        if app_pkg.split(".")[0] in local or covered(app_pkg):
            continue
        res = resolve_dotted(app_pkg, search)
        if res and res[1]:
            packages.append(res[0]); data.append(res[0]); included.add(res[0])
    for dotted in sorted(info.dotted):
        top = dotted.split(".")[0]
        if top in STDLIB_NAMES or covered(dotted):
            continue
        if any(dotted == ex or dotted.startswith(ex + ".") for ex in excluded_tests):
            continue
        res = resolve_dotted(dotted, search)
        if not res or covered(res[0]):
            continue
        (packages if res[1] else modules).append(res[0])
        included.add(res[0])
    if info.settings_module and not covered(info.settings_module):
        modules.append(info.settings_module)
    flags = [f"--include-package={p}" for p in dict.fromkeys(packages)]
    flags += [f"--include-module={m}" for m in dict.fromkeys(modules)]
    flags += [f"--include-package-data={d}" for d in dict.fromkeys(data)]
    # Deliberately NOT passing --module-parameter=django-settings-module=...: it makes
    # Nuitka's own Django package configuration import and evaluate the settings at compile
    # time, which crashed on Saleor's settings ("'int' object has no attribute 'rsplit'").
    # The includes derived above cover what that configuration would have added; Nuitka's
    # options-nanny warning about the missing parameter is expected and harmless.
    return flags


# --------------------------------------------------------------------------------------
# Launcher generation
# --------------------------------------------------------------------------------------

def launcher_stem(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "app"
    return f"{safe}__launcher"


def write_launcher(entry: Entry, root: Path, name: str, dry_run: bool) -> Path:
    launcher_dir = root / BUILD_DIRNAME / "launcher"
    # The launcher's directory becomes sys.path[0]; a launcher called `mytool.py` would
    # shadow a package called `mytool`, so the file name gets a suffix that cannot clash.
    launcher = launcher_dir / f"{launcher_stem(name)}.py"
    if entry.func:
        body = f"""\
# Auto-generated by nuitka_build.py - safe to delete.
import sys
from {entry.module} import {entry.func}

if __name__ == "__main__":
    sys.exit({entry.func}())
"""
    else:
        tree = parse_py(entry.path)
        guard = main_guard(tree) if tree is not None else None
        if guard is None:
            die(f"{entry.path} has neither a main() function nor an `if __name__ == '__main__':` "
                f"block; cannot build a launcher. Use --entry module:function.")
        src = guard_source(entry.path, guard)
        body = f"""\
# Auto-generated by nuitka_build.py - safe to delete.
# Runs the `if __name__ == "__main__":` block of {entry.module} with package semantics.
import sys
import {entry.module} as _target

_GUARD = {src!r}

if __name__ == "__main__":
    exec(compile(_GUARD, _target.__file__, "exec"), _target.__dict__)
"""
    info(f"Launcher: {launcher}")
    if not dry_run:
        # Wipe stale launchers: this directory is sys.path[0] for the compiled program and
        # any leftover .py file here could shadow a real package.
        if launcher_dir.is_dir():
            shutil.rmtree(launcher_dir)
        launcher_dir.mkdir(parents=True, exist_ok=True)
        launcher.write_text(body, encoding="utf-8")
    return launcher


# --------------------------------------------------------------------------------------
# Nuitka command
# --------------------------------------------------------------------------------------

GENERIC_STEMS = {"main", "app", "run", "cli", "start", "server", "application", "launch",
                 "program", "index", "__main__", "tool", "script", "manage", "wsgi", "asgi"}
# directory names that say nothing about the project (container mount points, layouts)
GENERIC_DIRS = {"src", "app", "apps", "work", "workspace", "project", "code", "source", "build",
                "home", "mnt", "data", "opt", "tmp", "repo", "python", "scripts"}


def decide_name(args: argparse.Namespace, entry: Entry, deps: Deps, root: Path) -> str:
    if args.name:
        return args.name
    if entry.name_hint:
        return entry.name_hint
    if entry.kind == "package" and entry.module:
        return entry.module.split(".")[-1]
    stem = entry.path.stem
    if stem in GENERIC_STEMS:
        if deps.project_name:
            return normalize_name(deps.project_name).replace("-", "_")
        if root.name.lower() not in GENERIC_DIRS:
            return re.sub(r"[^A-Za-z0-9_.-]+", "_", root.name) or stem
        warn(f"Could not derive a good binary name (entry '{stem}', directory '{root.name}'); "
             f"using '{stem}'. Pass --name to choose one.")
    return stem


def numeric_version(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    nums = re.findall(r"\d+", v)[:4]
    return ".".join(nums) if nums else None


def cpu_count() -> int:
    return os.cpu_count() or 1


def build_nuitka_command(env: Env, args: argparse.Namespace, entry: Entry, root: Path,
                         name: str, plugins: Sequence[str], data_dirs: Sequence[Tuple[Path, str]],
                         include_pkgs: Sequence[str], deps: Deps, main_file: Path,
                         output_dir: Path, extra_flags: Sequence[str] = (),
                         django: Optional[DjangoInfo] = None,
                         test_excludes: Sequence[str] = ()) -> Tuple[List[str], Path, dict]:
    mode = args.mode
    if mode is None:
        mode = "app" if (args.gui and IS_MACOS) else "onefile"
    app_bundle = mode == "app" and IS_MACOS

    cmd = [env.python, "-m", "nuitka", f"--mode={mode}", f"--output-dir={output_dir}",
           "--assume-yes-for-downloads"]
    if not args.keep_build:
        cmd.append("--remove-output")
    if not args.no_report:
        cmd.append(f"--report={root / BUILD_DIRNAME / f'{name}-compilation-report.xml'}")

    if app_bundle:
        cmd.append(f"--macos-app-name={name}")
        ver = numeric_version(args.app_version or deps.project_version)
        if ver:
            cmd.append(f"--macos-app-version={ver}")
        cmd.append(f"--macos-app-icon={Path(args.icon).resolve() if args.icon else 'none'}")
        if args.gui:
            cmd.append("--macos-app-mode=gui")
    else:
        out_name = name + (".exe" if IS_WINDOWS and not name.lower().endswith(".exe") else "")
        cmd.append(f"--output-filename={out_name}")
        if args.icon:
            icon = Path(args.icon).resolve()
            if IS_WINDOWS:
                cmd.append(f"--windows-icon-from-ico={icon}")
            elif IS_LINUX:
                cmd.append(f"--linux-icon={icon}")
            else:
                cmd.append(f"--macos-app-icon={icon}")

    if entry.kind == "package":
        cmd.append("--python-flag=-m")

    if args.gui and IS_WINDOWS:
        cmd.append("--windows-console-mode=disable")

    for p in plugins:
        cmd.append(f"--enable-plugin={p}")
    for pkg in include_pkgs:
        cmd.append(f"--include-package={pkg}")
        if args.package_data:
            cmd.append(f"--include-package-data={pkg}")
    for src, dst in data_dirs:
        cmd.append(f"--include-data-dir={src}={dst}")
    cmd += [f for f in extra_flags if f not in cmd]
    for pkg in test_excludes:
        cmd.append(f"--nofollow-import-to={pkg}")

    if args.jobs:
        cmd.append(f"--jobs={args.jobs}")
    if args.lto:
        cmd.append("--lto=yes")
    if args.clang:
        cmd.append("--clang")
    if args.mingw64:
        cmd.append("--mingw64")
    if args.low_memory:
        cmd.append("--low-memory")
    if args.deployment:
        cmd.append("--deployment")
    if args.quiet:
        cmd.append("--quiet")
    if args.no_compression and mode in ("onefile", "app"):
        cmd.append("--onefile-no-compression")
    for flag in args.python_flag:
        cmd.append(f"--python-flag={flag}")
    for mod in args.nofollow:
        cmd.append(f"--nofollow-import-to={mod}")

    company = args.company or (deps.project_name or name)
    ver = numeric_version(args.app_version or deps.project_version)
    if args.onefile_cache and mode in ("onefile", "app") and not app_bundle:
        cmd.append("--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}")
        cmd.append(f"--company-name={company}")
        cmd.append(f"--product-name={name}")
        cmd.append(f"--product-version={ver or '1.0.0'}")
        cmd.append(f"--file-version={ver or '1.0.0'}")
    elif ver and not app_bundle:
        cmd.append(f"--product-version={ver}")
        cmd.append(f"--file-version={ver}")
        if args.company:
            cmd.append(f"--company-name={args.company}")

    cmd += list(args.nuitka_args)

    if entry.kind == "package":
        cwd = entry.import_root
        cmd.append(str(entry.path.relative_to(entry.import_root)))
    else:
        cwd = root
        cmd.append(str(main_file))

    env_vars = dict(env.env_vars or build_env_vars(root, args, env.venv_dir))
    roots = [str(entry.import_root)] + [str(r) for r in import_roots_for(root)]
    existing = env_vars.get("PYTHONPATH")
    env_vars["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots + ([existing] if existing else [])))
    if django is not None:
        env_vars.setdefault("DJANGO_SETTINGS_MODULE", django.settings_module)
    return cmd, cwd, env_vars


def home_writable() -> bool:
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return bool(home) and os.path.isdir(home) and os.access(home, os.W_OK)


def build_env_vars(root: Path, args: argparse.Namespace, venv_dir: Optional[Path]) -> dict:
    """Environment for pip/uv/Nuitka: venv bin on PATH, cache dirs that work in containers."""
    env_vars = dict(os.environ)
    env_vars.setdefault("PYTHONUTF8", "1")
    if venv_dir is not None:
        # makes venv-installed tools (patchelf, ccache, ...) visible to Nuitka
        env_vars["PATH"] = str(venv_python(venv_dir).parent) + os.pathsep + env_vars.get("PATH", "")
    cache_root = Path(args.cache_dir).resolve() if args.cache_dir else None
    if cache_root is None and not home_writable():
        cache_root = root / BUILD_DIRNAME / "cache"   # e.g. docker --user without a HOME
    if cache_root is not None:
        env_vars.setdefault("NUITKA_CACHE_DIR", str(cache_root / "nuitka"))
        env_vars.setdefault("PIP_CACHE_DIR", str(cache_root / "pip"))
        env_vars.setdefault("UV_CACHE_DIR", str(cache_root / "uv"))
        env_vars.setdefault("UV_PYTHON_INSTALL_DIR", str(cache_root / "uv-python"))
        env_vars.setdefault("CCACHE_DIR", str(cache_root / "ccache"))
        if not args.dry_run:
            cache_root.mkdir(parents=True, exist_ok=True)
    return env_vars


# --------------------------------------------------------------------------------------
# Artifact discovery / smoke run
# --------------------------------------------------------------------------------------

def find_artifact(output_dir: Path, name: str, stem: str, mode: str) -> Optional[Path]:
    exe = ".exe" if IS_WINDOWS else ""
    cands: List[Path] = []
    if mode == "app" and IS_MACOS:
        cands += [output_dir / f"{stem}.app", output_dir / f"{name}.app"]
    if mode == "standalone":
        for d in (output_dir / f"{stem}.dist", output_dir / f"{name}.dist"):
            cands += [d / f"{name}{exe}", d / name, d / f"{stem}{exe}", d / stem, d]
    cands += [output_dir / f"{name}{exe}", output_dir / name, output_dir / f"{name}.bin",
              output_dir / f"{stem}{exe}", output_dir / f"{stem}.bin", output_dir / stem]
    for c in cands:
        if c.exists():
            return c
    return None


def tidy_output(output_dir: Path, artifact: Optional[Path], name: str, stem: str) -> Optional[Path]:
    """Rename `<stem>.dist` / `<stem>.app` to `<name>.*` and remove empty leftovers."""
    if artifact is not None and stem != name:
        for suffix in (".dist", ".app"):
            old_dir = output_dir / f"{stem}{suffix}"
            new_dir = output_dir / f"{name}{suffix}"
            try:
                if old_dir.is_dir() and (old_dir == artifact or old_dir in artifact.parents):
                    if new_dir.exists():
                        shutil.rmtree(new_dir)
                    old_dir.rename(new_dir)
                    artifact = new_dir / artifact.relative_to(old_dir) if artifact != old_dir else new_dir
            except OSError as exc:
                warn(f"Could not rename {old_dir.name} -> {new_dir.name}: {exc}")
    for p in list(output_dir.iterdir()):
        if p.is_dir() and p.suffix in (".dist", ".build", ".onefile-build") and not any(p.iterdir()):
            p.rmdir()
    return artifact


def executable_of(artifact: Path, name: str, stem: str) -> Optional[Path]:
    if artifact.is_file():
        return artifact
    if artifact.suffix == ".app":
        macos = artifact / "Contents" / "MacOS"
        plist = artifact / "Contents" / "Info.plist"
        if plist.is_file():
            try:
                import plistlib
                with open(plist, "rb") as fh:
                    exe_name = plistlib.load(fh).get("CFBundleExecutable")
                if exe_name and (macos / exe_name).is_file():
                    return macos / exe_name
            except Exception:  # noqa: BLE001
                pass
        for cand in (name, stem):
            if (macos / cand).is_file():
                return macos / cand
        return None
    if artifact.is_dir():
        exe = ".exe" if IS_WINDOWS else ""
        for cand in (f"{name}{exe}", name, f"{stem}{exe}", stem):
            if (artifact / cand).is_file():
                return artifact / cand
        for f in sorted(artifact.iterdir()):
            if f.is_file() and os.access(f, os.X_OK) and f.suffix not in (".so", ".dylib", ".dll", ".pyd") \
                    and f.name != "Python":
                return f
    return None


def smoke_run(exe: Path, run_args: str, timeout: int) -> int:
    step(f"Smoke run: {exe} {run_args}".rstrip())
    try:
        proc = subprocess.run([str(exe)] + shlex.split(run_args), timeout=timeout, text=True,
                              capture_output=True)
    except subprocess.TimeoutExpired:
        warn(f"Smoke run did not finish within {timeout}s (GUI/server apps are expected to keep running).")
        return 0
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(textwrap.indent(out.strip()[-3000:], "    "))
    (ok if proc.returncode == 0 else warn)(f"Binary exited with code {proc.returncode}")
    return proc.returncode


# --------------------------------------------------------------------------------------
# Environment sanity checks
# --------------------------------------------------------------------------------------

def linux_pkg_hint(packages_rpm: str, packages_deb: str) -> str:
    if Path("/etc/redhat-release").exists() or Path("/etc/rocky-release").exists():
        return f"dnf install -y {packages_rpm}"
    if Path("/etc/debian_version").exists():
        return f"apt-get install -y {packages_deb}"
    return f"install with your package manager: {packages_rpm}"


def compiler_problem() -> Optional[str]:
    if IS_WINDOWS:
        if not (shutil.which("cl") or shutil.which("gcc") or shutil.which("clang")):
            info("No C compiler on PATH; Nuitka will download MinGW64 automatically "
                 "(--assume-yes-for-downloads).")
        return None
    if IS_MACOS:
        try:
            subprocess.run(["xcode-select", "-p"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "No C compiler: Xcode Command Line Tools are missing. Run: xcode-select --install"
        return None
    if not (shutil.which("gcc") or shutil.which("clang") or shutil.which("cc")):
        return "No C compiler found. " + linux_pkg_hint("gcc gcc-c++ make", "build-essential")
    if not shutil.which("ccache"):
        debug("ccache not found; installing it speeds up rebuilds (" +
              linux_pkg_hint("ccache", "ccache") + ")")
    return None


def headers_problem(python: str) -> Optional[str]:
    """Nuitka compiles against Python.h; distro Pythons ship it in a separate -devel package."""
    if IS_WINDOWS:
        return None
    code = ("import sysconfig,os;p=sysconfig.get_paths()['include'];"
            "print(os.path.exists(os.path.join(p,'Python.h')));print(p)")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if out and out[0] == "False":
        ver = ".".join(python_version_of(python).split(".")[:2])
        return (f"Python.h not found in {out[1] if len(out) > 1 else '?'}; Nuitka cannot compile. "
                + linux_pkg_hint(f"python{ver}-devel (python3-devel for the distro default python3)",
                                 f"python{ver}-dev"))
    return None


def preflight(args: argparse.Namespace, python: Optional[str] = None) -> None:
    """Verify the C toolchain before spending minutes on installs and compilation."""
    problems = [p for p in (compiler_problem(), headers_problem(python) if python else None) if p]
    if not problems:
        return
    for p in problems:
        warn(p)
    if args.skip_checks:
        info("--skip-checks: continuing anyway.")
        return
    die("Build tools are missing (see above). Install them, or pass --skip-checks to try anyway.")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    argv = list(argv)
    nuitka_args: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        nuitka_args = argv[i + 1:]
        argv = argv[:i]

    p = argparse.ArgumentParser(
        prog="nuitka_build.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build a native executable from a Python project with Nuitka.",
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s ./myproject
              %(prog)s ./myproject/main.py --mode standalone
              %(prog)s ./myproject --entry mypkg.cli:main --name mytool --lto
              %(prog)s ./gui_app --gui --icon icon.png --onefile-cache
              %(prog)s ./proj --python 3.12 --extras gui --run
              %(prog)s ./proj -- --include-package=plugins --nofollow-import-to=*.tests
            """))
    p.add_argument("path", help="project directory or entry .py file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = p.add_argument_group("entry point")
    g.add_argument("--entry", help="entry: file path, package dir, or module[:function]")
    g.add_argument("--name", help="output binary name (default: script name / project name)")

    g = p.add_argument_group("dependencies")
    g.add_argument("--req", action="append", default=[], metavar="FILE",
                   help="extra requirements file (repeatable)")
    g.add_argument("--extra-req", action="append", default=[], metavar="SPEC",
                   help="extra pip requirement spec to install (repeatable)")
    g.add_argument("--extras", action="append", default=[], metavar="NAME",
                   help="pyproject optional-dependencies / poetry extras group to include (repeatable)")
    g.add_argument("--dev", action="store_true", help="also install dev/test dependency groups")
    g.add_argument("--all-requirements", action="store_true",
                   help="install every requirements*.txt / requirements/*.txt found")
    g.add_argument("--install-project", action="store_true",
                   help="also `pip install` the project itself (resolves dynamic metadata)")
    g.add_argument("--no-infer", action="store_true",
                   help="never infer dependencies from import statements")
    g.add_argument("--no-lock", action="store_true",
                   help="ignore uv.lock / poetry.lock / pdm.lock and resolve from the manifests")
    g.add_argument("--index-url", help="alternative PyPI index URL")

    g = p.add_argument_group("environment")
    g.add_argument("--python", help="interpreter for the build env: path, name or version (e.g. 3.12)")
    g.add_argument("--venv", default=os.environ.get("NUITKA_BUILD_VENV") or None,
                   help=f"virtualenv location (default: <project>/{BUILD_DIRNAME}/venv, or $NUITKA_BUILD_VENV; "
                        "in containers keep it off bind mounts, e.g. /opt/nuitka-venv)")
    g.add_argument("--installer", choices=["auto", "uv", "pip"], default="auto",
                   help="package installer (default: uv if available, else pip)")
    g.add_argument("--system", action="store_true",
                   help="no venv; install into the given/current interpreter (not recommended)")
    g.add_argument("--clean", action="store_true", help="recreate the virtualenv from scratch")
    g.add_argument("--no-install", action="store_true",
                   help="skip all installs (reuse an already prepared environment)")
    g.add_argument("--nuitka-version", metavar="SPEC", help='e.g. "nuitka==2.7.7"')
    g.add_argument("--cache-dir", metavar="DIR", default=os.environ.get("NUITKA_BUILD_CACHE_DIR") or None,
                   help="root for Nuitka/pip/uv/ccache caches (default: user cache or $NUITKA_BUILD_CACHE_DIR; "
                        f"falls back to <project>/{BUILD_DIRNAME}/cache when HOME is not writable)")

    g = p.add_argument_group("build")
    g.add_argument("--mode", choices=["onefile", "standalone", "app", "accelerated"],
                   help="Nuitka mode (default: onefile; app = onefile, but .app bundle on macOS)")
    g.add_argument("-o", "--output-dir", help="output directory (default: <project>/dist)")
    g.add_argument("--gui", action="store_true",
                   help="GUI app: no console on Windows, .app bundle on macOS")
    g.add_argument("--icon", help="icon file (.ico on Windows, .icns/.png on macOS, .png on Linux)")
    g.add_argument("--plugin", action="append", default=[], metavar="NAME",
                   help="enable a Nuitka plugin (repeatable), e.g. tk-inter, pyside6")
    g.add_argument("--no-auto-plugins", action="store_true", help="disable plugin auto-detection")
    g.add_argument("--data-dir", action="append", default=[], metavar="SRC[=DST]",
                   help="include a data directory (repeatable)")
    g.add_argument("--no-auto-data", action="store_true",
                   help="do not auto-include assets/, data/, templates/ ... directories")
    g.add_argument("--package-data", action="store_true",
                   help="add --include-package-data for the project's own packages")
    g.add_argument("--nofollow", action="append", default=[], metavar="MODULE",
                   help="--nofollow-import-to=MODULE (repeatable)")
    g.add_argument("--keep-tests", action="store_true",
                   help="compile the project's tests packages too (default: exclude those no runtime code imports)")
    g.add_argument("--no-django", action="store_true",
                   help="disable the Django profile (settings-driven --include-* flags, manage.py entry)")
    g.add_argument("--django-settings", metavar="MODULE",
                   help="Django settings module (default: from manage.py / DJANGO_SETTINGS_MODULE)")
    g.add_argument("--python-flag", action="append", default=[], metavar="FLAG",
                   help="Nuitka --python-flag (repeatable): no_site, -O, no_docstrings, ...")
    g.add_argument("--jobs", type=int, help="parallel C compile jobs (default: all CPUs)")
    g.add_argument("--lto", action="store_true", help="link-time optimisation (smaller/faster, slower build)")
    g.add_argument("--clang", action="store_true", help="force clang")
    g.add_argument("--mingw64", action="store_true", help="force MinGW64 on Windows")
    g.add_argument("--low-memory", action="store_true", help="Nuitka --low-memory")
    g.add_argument("--deployment", action="store_true",
                   help="Nuitka --deployment (disable dev-time safety checks for release builds)")
    g.add_argument("--onefile-cache", action="store_true",
                   help="cache onefile extraction in a stable dir (faster start, fewer AV/firewall issues)")
    g.add_argument("--no-compression", action="store_true", help="disable onefile payload compression")
    g.add_argument("--company", help="company name for version metadata")
    g.add_argument("--app-version", help="version for metadata (numeric, e.g. 1.2.3); default from pyproject")
    g.add_argument("--keep-build", action="store_true", help="keep Nuitka's .build directories")
    g.add_argument("--no-report", action="store_true", help="do not write the XML compilation report")

    g = p.add_argument_group("after build")
    g.add_argument("--run", action="store_true", help="run the produced binary once (smoke test)")
    g.add_argument("--run-args", default="", help="arguments for --run (single string)")
    g.add_argument("--run-timeout", type=int, default=120, help="seconds before the smoke run is abandoned")

    g = p.add_argument_group("misc")
    g.add_argument("--skip-checks", action="store_true",
                   help="do not stop when the C compiler or Python headers look missing")
    g.add_argument("--dry-run", action="store_true", help="show what would happen, run nothing")
    g.add_argument("--no-build", action="store_true",
                   help="prepare the environment and install everything, but do not run Nuitka")
    g.add_argument("-v", "--verbose", action="store_true")
    g.add_argument("--quiet", action="store_true", help="pass --quiet to Nuitka")

    ns = p.parse_args(argv)
    ns.nuitka_args = nuitka_args
    return ns


def project_needs_toml(root: Path, target: Path) -> bool:
    if any((root / n).is_file() for n in ("pyproject.toml", "Pipfile")):
        return True
    if target.is_file():
        return has_pep723_block(target)
    # A directory was given: any script in it may carry PEP 723 metadata (bounded scan,
    # this only runs when the interpreter has no TOML parser at all).
    for i, f in enumerate(iter_py_files(root, skip_non_entry_dirs=True)):
        if i >= 300:
            break
        if has_pep723_block(f):
            return True
    return False


def bootstrap_toml_parser(root: Path, target: Path, args: argparse.Namespace) -> None:
    """Python < 3.11 without `tomli` cannot read pyproject/Pipfile/PEP 723. Instead of
    failing, create the build environment first, install tomli there and re-run this
    script with that interpreter (RHEL/Rocky default Pythons are 3.6/3.9)."""
    if _TOML is not None or os.environ.get("NUITKA_BUILD_BOOTSTRAPPED"):
        return
    if not project_needs_toml(root, target):
        return
    if args.dry_run:
        warn("Host Python has no tomllib/tomli: TOML manifests are ignored in this dry run "
             "(a real run bootstraps a venv with tomli and re-executes itself).")
        return
    info("Host Python has no tomllib/tomli -> installing tomli (and uv) into the tools environment "
         "and re-running with it.")
    tools_py = ensure_tools_env(root, args, ["tomli", "uv"])
    if not tools_py:
        warn("Tools environment unavailable; TOML manifests will be ignored (pip install tomli to fix).")
        return
    child_env = dict(os.environ)
    child_env["NUITKA_BUILD_BOOTSTRAPPED"] = "1"
    cmd = [tools_py, os.path.abspath(__file__)] + sys.argv[1:]
    info("$ " + fmt_cmd(cmd))
    sys.exit(subprocess.call(cmd, env=child_env))


def main(argv: Optional[Sequence[str]] = None) -> int:
    global VERBOSE
    args = parse_args(sys.argv[1:] if argv is None else argv)
    VERBOSE = args.verbose
    started = time.time()

    if sys.version_info < (3, 8):
        die("nuitka_build.py needs Python 3.8+ on the host.")

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        die(f"Path does not exist: {target}")

    # ---- project / entry -----------------------------------------------------------
    step("Analysing project")
    root = find_project_root(target) if target.is_file() else target
    bootstrap_toml_parser(root, target, args)
    if target.is_file():
        import_roots = import_roots_for(root)
        entry = entry_from_file(target, True, "path argument") if not args.entry \
            else detect_entry(root, import_roots, args.entry)
    else:
        root = target
        import_roots = import_roots_for(root)
        entry = detect_entry(root, import_roots, args.entry)
    if entry.import_root not in import_roots:
        import_roots.insert(0, entry.import_root)

    info(f"Project root : {root}")
    info(f"Import roots : {', '.join(str(r) for r in import_roots)}")
    info(f"Entry        : {entry.describe()}  [{entry.source}]")

    # ---- toolchain: uv + interpreter -------------------------------------------------
    uv = None if args.system and args.installer != "uv" else find_uv(root, args)
    required = read_requires_python(root)
    choice = choose_python(args, required, uv)
    info(f"Python       : {choice.request} ({choice.note})"
         + (f", project requires {required}" if required else ""))
    info(f"Installer    : {'uv (' + uv + ')' if uv else 'pip'}")

    django = None if args.no_django else detect_django(root, import_roots, args.django_settings)
    if django is not None:
        info(f"Django       : settings={django.settings_module}, {len(django.apps)} installed apps")
        if not args.package_data:
            args.package_data = True   # templates/static/locale/migrations inside the project package

    # ---- dependencies ---------------------------------------------------------------
    deps = collect_dependencies(root, import_roots, entry, args, uv)
    if deps.sources:
        info("Dependencies : " + "; ".join(deps.sources))
    else:
        info("Dependencies : none found (pure stdlib project?)")
    if VERBOSE and deps.specs:
        for s in deps.specs:
            debug(f"  spec {s}")

    name = decide_name(args, entry, deps, root)
    output_dir = (Path(args.output_dir).resolve() if args.output_dir else root / "dist")

    # ---- plugins / data / includes --------------------------------------------------
    imports = scan_project_imports(root, entry)
    plugins = detect_plugins(imports, deps, args.plugin, args.no_auto_plugins)
    data_dirs = detect_data_dirs(root, import_roots, entry, args.data_dir, args.no_auto_data)
    include_pkgs = detect_include_packages(root, import_roots, entry, imports)
    info(f"Plugins      : {', '.join(plugins) or '-'}")
    info("Data dirs    : " + (", ".join(f"{rel_display(s, root)} -> {d}" for s, d in data_dirs) or "-"))
    info(f"Packages     : {', '.join(include_pkgs) or '-'}")
    info(f"Binary name  : {name}")
    info(f"Output dir   : {output_dir}")

    # ---- environment ----------------------------------------------------------------
    preflight(args)
    step("Preparing build environment")
    env = prepare_env(root, args, uv, choice)
    check_python = env.python if Path(env.python).exists() else \
        (None if choice.is_version else choice.request)
    if check_python:
        preflight(args, check_python)
    if args.no_install:
        info("--no-install: skipping dependency installation")
    else:
        install_everything(env, deps, root, args)

    test_excludes: List[str] = []
    kept_tests: Dict[str, str] = {}
    if not args.keep_tests:
        test_excludes, kept_tests = plan_test_exclusion(import_roots)
        if test_excludes or kept_tests:
            info(f"Tests        : excluding {len(test_excludes)} test packages"
                 + (f"; keeping {len(kept_tests)} imported by runtime code: "
                    + ", ".join(f"{k} <- {v}" for k, v in sorted(kept_tests.items())[:6])
                    if kept_tests else ""))
    if not args.no_install and not args.dry_run:
        ensure_runtime_imports(root, import_roots, kept_tests, env, deps, args, uv)
    extra_flags: List[str] = []
    if django is not None:
        extra_flags = django_nuitka_flags(django, import_roots, env.python, include_pkgs, test_excludes)
        info(f"Django flags : {len(extra_flags)} --include-* flags derived from {django.settings_module}"
             + (" (third-party apps are resolved against the installed environment; dry-run shows fewer)"
                if args.dry_run else ""))
        if VERBOSE:
            for f in extra_flags:
                debug(f"  {f}")

    # ---- launcher -------------------------------------------------------------------
    if entry.kind == "launcher":
        main_file = write_launcher(entry, root, name, args.dry_run)
    elif entry.kind == "package":
        main_file = entry.path
    else:
        main_file = entry.path

    # ---- nuitka ---------------------------------------------------------------------
    cmd, cwd, env_vars = build_nuitka_command(env, args, entry, root, name, plugins, data_dirs,
                                              include_pkgs, deps, main_file, output_dir,
                                              extra_flags, django, test_excludes)
    mode = next(c.split("=", 1)[1] for c in cmd if c.startswith("--mode="))
    if args.no_build:
        step("Environment ready (--no-build); the compile command would be:")
        info(f"PYTHONPATH={env_vars['PYTHONPATH']}")
        info("$ " + fmt_cmd(cmd) + f"   (cwd={cwd})")
        ok(f"Prepared in {time.time() - started:.0f}s")
        return 0
    step(f"Compiling with Nuitka (mode={mode}) - this can take a few minutes")
    info(f"PYTHONPATH={env_vars['PYTHONPATH']}")
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    run(cmd, cwd=cwd, env=env_vars, dry_run=args.dry_run)
    build_seconds = time.time() - t0

    if args.dry_run:
        ok("Dry run complete; nothing was executed.")
        return 0

    stem = main_file.stem if entry.kind != "package" else entry.path.name
    artifact = find_artifact(output_dir, name, stem, mode)
    artifact = tidy_output(output_dir, artifact, name, stem)
    step("Result")
    if artifact is None:
        warn(f"Build finished but no artifact matched in {output_dir}; contents:")
        for p in sorted(output_dir.iterdir()):
            print("   ", p.name)
        return 1

    size = artifact.stat().st_size if artifact.is_file() else dir_size(artifact)
    ok(f"{artifact}  ({human_size(size)}, built in {build_seconds:.0f}s, total {time.time() - started:.0f}s)")
    if mode == "standalone":
        info("Standalone mode: distribute the whole .dist folder, not just the binary.")
    if entry.kind == "launcher" and not entry.func:
        info("Launcher executes the module's __main__ block; verify data-file paths relative to __file__.")
    if django is not None:
        info(f"Django binary: `{artifact} help` lists commands; run migrate/runserver/etc. with the same "
             f"environment variables (DATABASE_URL, SECRET_KEY, ...) the project expects.")

    build_info = {
        "project_root": str(root), "entry": entry.describe(), "entry_kind": entry.kind,
        "name": name, "mode": mode, "artifact": str(artifact), "size_bytes": size,
        "python": env.python, "installer": env.installer, "plugins": list(plugins),
        "django_settings": django.settings_module if django else None, "extra_flags": list(extra_flags),
        "lock_file": deps.locked,
        "data_dirs": [[str(s), d] for s, d in data_dirs], "include_packages": list(include_pkgs),
        "dependency_specs": deps.specs, "requirement_files": [str(f) for f in deps.req_files],
        "nuitka_command": [str(c) for c in cmd], "cwd": str(cwd),
        "build_seconds": round(build_seconds, 1), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (root / BUILD_DIRNAME / "build-info.json").write_text(json.dumps(build_info, indent=2), encoding="utf-8")

    if args.run:
        exe = executable_of(artifact, name, stem)
        if exe is None:
            warn("Could not determine executable inside the artifact for --run.")
        else:
            rc = smoke_run(exe, args.run_args, args.run_timeout)
            return 0 if rc == 0 else 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        die("Interrupted.", 130)
