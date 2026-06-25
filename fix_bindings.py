"""
FixBindings - Global keyboard hotkey rebinder with per-application profiles.
Toggle on/off with F10. Works globally including in games (via low-level hooks).
"""

import sys
import json
import os
import threading
import time
import ctypes
import ctypes.wintypes
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import keyboard
import win32gui
import win32process
import psutil

# --------------------------------------------------------------------------- #
# Low-level SendInput helpers (games require this over keybd_event)           #
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

# Virtual-key name → VK code (covers common keys; keyboard lib fills the rest)
_VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "caps lock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "spacebar": 0x20,
    "page up": 0x21, "page down": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "num lock": 0x90, "scroll lock": 0x91,
    "left shift": 0xA0, "right shift": 0xA1,
    "left ctrl": 0xA2, "right ctrl": 0xA3,
    "left alt": 0xA4, "right alt": 0xA5,
    "semicolon": 0xBA, "equal": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "grave": 0xC0,
    "open bracket": 0xDB, "backslash": 0xDC, "close bracket": 0xDD,
    "apostrophe": 0xDE,
}

# Extended keys whose scan codes need the EXTENDEDKEY flag
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("_u", _INPUTunion)]


def _vk_for(key_name: str) -> int:
    """Resolve a key name string to a Windows virtual-key code."""
    name = key_name.lower().strip()
    if name in _VK_MAP:
        return _VK_MAP[name]
    # Single printable character
    if len(name) == 1:
        vk = user32.VkKeyScanW(ord(name)) & 0xFF
        if vk:
            return vk
    # Fall back to keyboard library's scan-code lookup
    try:
        sc = keyboard.key_to_scan_codes(name)[0]
        # MapVirtualKey: scan→VK
        vk = user32.MapVirtualKeyW(sc, 1)
        if vk:
            return vk
    except Exception:
        pass
    return 0


def _scan_for_vk(vk: int) -> int:
    return user32.MapVirtualKeyW(vk, 0)


def sendinput_key(key_name: str, key_up: bool = False):
    """Send a key event via SendInput — works in DirectInput/raw-input games."""
    vk = _vk_for(key_name)
    if not vk:
        return
    sc = _scan_for_vk(vk)
    flags = KEYEVENTF_SCANCODE
    if key_up:
        flags |= KEYEVENTF_KEYUP
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._u.ki.wVk = 0           # must be 0 when using scan code
    inp._u.ki.wScan = sc
    inp._u.ki.dwFlags = flags
    inp._u.ki.time = 0
    inp._u.ki.dwExtraInfo = ctypes.pointer(ctypes.c_ulong(0))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

# Always store bindings next to the script, regardless of working directory
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bindings.json")
TOGGLE_KEY = "F10"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"profiles": {}, "global": []}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_active_exe():
    """Return the exe name (lowercase) of the currently focused window."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid)
        return proc.name().lower()
    except Exception:
        return ""


class KeyboardThread(threading.Thread):
    """
    Dedicated thread that owns the keyboard library's WH_KEYBOARD_LL hook.
    All hook_key / unhook calls MUST happen on this thread so the Windows
    message pump dispatches events to them. We run a simple queue loop here.
    """

    def __init__(self):
        super().__init__(daemon=True, name="KeyboardThread")
        self._queue = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.start()
        self._ready.wait()  # block until the thread has primed the pump

    def run(self):
        # Force the keyboard library to install its hook on THIS thread
        # by doing a no-op hook immediately.
        _noop_hook = keyboard.hook(lambda e: None, suppress=False)
        keyboard.unhook(_noop_hook)
        self._ready.set()

        # Service queued callables forever
        while True:
            with self._lock:
                work = self._queue[:]
                self._queue.clear()
            for fn in work:
                try:
                    fn()
                except Exception as e:
                    print(f"KeyboardThread error: {e}")
            time.sleep(0.005)

    def call(self, fn):
        """Queue a callable to run on the keyboard thread."""
        with self._lock:
            self._queue.append(fn)

    def call_sync(self, fn):
        """Queue a callable and block until it completes, returning its result."""
        done = threading.Event()
        result = [None]
        exc = [None]

        def wrapper():
            try:
                result[0] = fn()
            except Exception as e:
                exc[0] = e
            finally:
                done.set()

        self.call(wrapper)
        done.wait()
        if exc[0]:
            raise exc[0]
        return result[0]


# Single shared keyboard thread for the whole process
_kb_thread = None


def get_kb_thread():
    global _kb_thread
    if _kb_thread is None:
        _kb_thread = KeyboardThread()
    return _kb_thread


class BindingEngine:
    """Manages active key remaps — all keyboard calls marshalled to KeyboardThread."""

    def __init__(self, config):
        self.config = config
        self.enabled = False
        self.active_hooks = []
        self._lock = threading.Lock()

    def _apply_bindings(self, bindings):
        """Must be called on the KeyboardThread."""
        for b in bindings:
            from_key = b.get("from", "").strip()
            to_key = b.get("to", "").strip()
            if not from_key or not to_key:
                continue
            try:
                def make_handler(tk_):
                    def handler(event):
                        key_up = (event.event_type == keyboard.KEY_UP)
                        sendinput_key(tk_, key_up=key_up)
                        return False  # suppress original key
                    return handler

                h = keyboard.hook_key(from_key, make_handler(to_key), suppress=True)
                self.active_hooks.append(h)
            except Exception as e:
                print(f"Failed to remap {from_key} -> {to_key}: {e}")

    def _clear_hooks(self):
        """Must be called on the KeyboardThread."""
        for h in self.active_hooks:
            try:
                keyboard.unhook(h)
            except Exception:
                pass
        self.active_hooks.clear()

    def refresh(self):
        """Compute the right binding set and re-install hooks on the keyboard thread."""
        exe = get_active_exe()
        bindings = list(self.config.get("global", []))
        profiles = self.config.get("profiles", {})
        for app_exe, app_bindings in profiles.items():
            if app_exe.lower() == exe:
                bindings.extend(app_bindings)
                break

        enabled = self.enabled

        def _do():
            self._clear_hooks()
            if enabled:
                self._apply_bindings(bindings)

        get_kb_thread().call(_do)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.refresh()

    def reload_config(self, config):
        self.config = config
        self.refresh()


class WatcherThread(threading.Thread):
    """Background thread that watches the active window and refreshes bindings."""

    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine
        self._last_exe = ""

    def run(self):
        while True:
            exe = get_active_exe()
            if exe != self._last_exe:
                self._last_exe = exe
                self.engine.refresh()
            time.sleep(0.25)


class BindingRow(tk.Frame):
    """A single from→to row in the binding editor."""

    def __init__(self, parent, from_key="", to_key="", on_delete=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.from_var = tk.StringVar(value=from_key)
        self.to_var = tk.StringVar(value=to_key)

        tk.Label(self, text="From:", width=5, anchor="e").pack(side="left")
        self.from_entry = tk.Entry(self, textvariable=self.from_var, width=14)
        self.from_entry.pack(side="left", padx=(0, 4))

        tk.Label(self, text="→  To:", width=6, anchor="e").pack(side="left")
        self.to_entry = tk.Entry(self, textvariable=self.to_var, width=14)
        self.to_entry.pack(side="left", padx=(0, 4))

        capture_btn = tk.Button(self, text="Capture", width=7,
                                command=self._capture_from)
        capture_btn.pack(side="left", padx=(0, 4))

        del_btn = tk.Button(self, text="✕", fg="red", width=3,
                            command=lambda: on_delete(self) if on_delete else None)
        del_btn.pack(side="left")

        self.from_entry.bind("<FocusIn>", lambda e: self._start_capture(self.from_var, self.from_entry))
        self.to_entry.bind("<FocusIn>", lambda e: self._start_capture(self.to_var, self.to_entry))

    def _start_capture(self, var, entry):
        """Capture next keydown into the entry then immediately remove the hook."""
        var.set("Press a key…")
        entry.update()
        hook_ref = [None]

        def on_key(event):
            if event.event_type != keyboard.KEY_DOWN:
                return
            key = event.name
            if key and key != "unknown":
                var.set(key)
                try:
                    keyboard.unhook(hook_ref[0])
                except Exception:
                    pass

        hook_ref[0] = keyboard.hook(on_key, suppress=False)

    def _capture_from(self):
        self._start_capture(self.from_var, self.from_entry)

    def _capture_to(self):
        self._start_capture(self.to_var, self.to_entry)

    def get(self):
        return {"from": self.from_var.get().strip(), "to": self.to_var.get().strip()}


class ProfilePanel(tk.Frame):
    """Editable list of key bindings for one profile."""

    def __init__(self, parent, bindings=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.rows = []

        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        add_btn = tk.Button(self, text="+ Add Binding", command=self.add_row)
        add_btn.pack(side="bottom", pady=4)

        for b in (bindings or []):
            self.add_row(b.get("from", ""), b.get("to", ""))

    def _on_frame_configure(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.window_id, width=event.width)

    def add_row(self, from_key="", to_key=""):
        row = BindingRow(self.inner, from_key=from_key, to_key=to_key,
                         on_delete=self._delete_row, bg=self["bg"])
        row.pack(fill="x", padx=4, pady=2)
        self.rows.append(row)
        self._on_frame_configure()

    def _delete_row(self, row):
        row.destroy()
        self.rows.remove(row)
        self._on_frame_configure()

    def get_bindings(self):
        result = []
        for row in self.rows:
            b = row.get()
            if b["from"] and b["to"]:
                result.append(b)
        return result


class CopyFromDialog(tk.Toplevel):
    """Pick a source profile and whether to append or replace."""

    def __init__(self, parent, sources):
        super().__init__(parent)
        self.title("Copy Bindings From")
        self.resizable(False, False)
        self.geometry("300x280")
        self.grab_set()
        self.result = None  # (source_name, mode)  mode = "append" | "replace"

        tk.Label(self, text="Copy from:", anchor="w").pack(fill="x", padx=12, pady=(12, 4))

        self._var = tk.StringVar(value=sources[0])
        for src in sources:
            tk.Radiobutton(self, text=src, variable=self._var, value=src,
                           anchor="w").pack(fill="x", padx=24)

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=8)

        tk.Label(self, text="How to apply:", anchor="w").pack(fill="x", padx=12)
        self._mode = tk.StringVar(value="append")
        tk.Radiobutton(self, text="Append  (keep existing + add new)",
                       variable=self._mode, value="append", anchor="w").pack(fill="x", padx=24)
        tk.Radiobutton(self, text="Replace  (overwrite current bindings)",
                       variable=self._mode, value="replace", anchor="w").pack(fill="x", padx=24)

        btn_row = tk.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=10)
        tk.Button(btn_row, text="Copy", command=self._confirm).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left", expand=True, fill="x")

        self.bind("<Return>", self._confirm)
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.wait_window(self)

    def _confirm(self, event=None):
        self.result = (self._var.get(), self._mode.get())
        self.destroy()


def get_running_exes():
    """Return sorted list of unique exe names for all running processes."""
    seen = set()
    result = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name and name.lower() not in seen:
                seen.add(name.lower())
                result.append(name.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(result)


class AppPickerDialog(tk.Toplevel):
    """Modal dialog: pick from running processes or type a name manually."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Add App Profile")
        self.resizable(False, True)
        self.geometry("360x460")
        self.grab_set()
        self.result = None

        tk.Label(self, text="Search or type an .exe name:", anchor="w").pack(fill="x", padx=10, pady=(10, 2))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search)
        search_entry = tk.Entry(self, textvariable=self._search_var)
        search_entry.pack(fill="x", padx=10)
        search_entry.focus_set()

        tk.Label(self, text="Running processes:", anchor="w").pack(fill="x", padx=10, pady=(8, 2))

        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10)

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        self._listbox = tk.Listbox(frame, yscrollcommand=scrollbar.set, exportselection=False)
        scrollbar.config(command=self._listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)
        self._listbox.bind("<Double-Button-1>", self._on_select)
        self._listbox.bind("<<ListboxSelect>>", self._on_listbox_pick)

        self._all_exes = get_running_exes()
        self._populate(self._all_exes)

        btn_row = tk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=8)
        tk.Button(btn_row, text="Add Selected / Typed", command=self._on_select).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left", expand=True, fill="x")

        self.bind("<Return>", self._on_select)
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.wait_window(self)

    def _populate(self, exes):
        self._listbox.delete(0, "end")
        for exe in exes:
            self._listbox.insert("end", exe)

    def _on_search(self, *_):
        q = self._search_var.get().lower()
        filtered = [e for e in self._all_exes if q in e] if q else self._all_exes
        self._populate(filtered)

    def _on_listbox_pick(self, event=None):
        sel = self._listbox.curselection()
        if sel:
            self._search_var.set(self._listbox.get(sel[0]))

    def _on_select(self, event=None):
        name = self._search_var.get().strip()
        if name:
            self.result = name
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FixBindings")
        self.resizable(True, True)
        self.minsize(560, 420)
        self.geometry("680x520")

        self.config_data = load_config()
        self.engine = BindingEngine(self.config_data)
        self.watcher = WatcherThread(self.engine)
        self.watcher.start()

        self._enabled = tk.BooleanVar(value=False)
        self._build_ui()

        # Register F10 toggle on the keyboard thread so it shares the same hook
        self._toggle_hook = None
        get_kb_thread().call(self._install_toggle_hook)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, pady=6, padx=8)
        top.pack(fill="x")

        status_frame = tk.Frame(top)
        status_frame.pack(side="left")

        self.status_label = tk.Label(status_frame, text="● DISABLED",
                                     font=("Segoe UI", 11, "bold"), fg="#c0392b")
        self.status_label.pack(side="left", padx=(0, 10))

        toggle_btn = tk.Button(top, text=f"Toggle  ({TOGGLE_KEY})", width=16,
                               command=self._toggle)
        toggle_btn.pack(side="left")

        save_btn = tk.Button(top, text="Save All", width=10, command=self._save)
        save_btn.pack(side="right", padx=4)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        # Main paned area: profile list on left, editor on right
        paned = tk.PanedWindow(self, orient="horizontal", sashrelief="raised", sashwidth=5)
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # Left: profile list
        left = tk.Frame(paned, width=180)
        paned.add(left, minsize=140)

        tk.Label(left, text="Profiles", font=("Segoe UI", 10, "bold")).pack(pady=(4, 2))

        self.profile_listbox = tk.Listbox(left, selectmode="single", exportselection=False)
        self.profile_listbox.pack(fill="both", expand=True, padx=4)
        self.profile_listbox.bind("<<ListboxSelect>>", self._on_profile_select)

        btn_row = tk.Frame(left)
        btn_row.pack(fill="x", padx=4, pady=4)
        tk.Button(btn_row, text="+ App", command=self._add_profile).pack(side="left", expand=True, fill="x")
        tk.Button(btn_row, text="✕ Del", fg="red", command=self._delete_profile).pack(side="left", expand=True, fill="x")

        btn_row2 = tk.Frame(left)
        btn_row2.pack(fill="x", padx=4, pady=(0, 4))
        tk.Button(btn_row2, text="Copy From…", command=self._copy_from_profile).pack(fill="x", expand=True)

        # Right: binding editor
        right = tk.Frame(paned)
        paned.add(right, minsize=320)

        self.profile_title = tk.Label(right, text="Select a profile",
                                      font=("Segoe UI", 10, "bold"), anchor="w")
        self.profile_title.pack(fill="x", padx=6, pady=(4, 2))

        self.panel_container = tk.Frame(right)
        self.panel_container.pack(fill="both", expand=True)

        self.active_panel = None

        self._populate_profile_list()

    def _populate_profile_list(self):
        self.profile_listbox.delete(0, "end")
        self.profile_listbox.insert("end", "Global")
        for name in self.config_data.get("profiles", {}).keys():
            self.profile_listbox.insert("end", name)

    def _on_profile_select(self, event=None):
        sel = self.profile_listbox.curselection()
        if not sel:
            return
        name = self.profile_listbox.get(sel[0])
        self._load_panel(name)

    def _load_panel(self, name):
        # Persist current panel before switching
        self._save_active_panel()

        if self.active_panel:
            self.active_panel.destroy()

        if name == "Global":
            bindings = self.config_data.get("global", [])
            title = "Global Bindings  (apply to all apps)"
        else:
            bindings = self.config_data["profiles"].get(name, [])
            title = f"App Profile: {name}"

        self.profile_title.config(text=title)
        self.active_panel = ProfilePanel(self.panel_container, bindings=bindings,
                                         bg=self.panel_container["bg"])
        self.active_panel.pack(fill="both", expand=True)
        self.active_panel._profile_name = name

    def _save_active_panel(self):
        if not self.active_panel:
            return
        name = getattr(self.active_panel, "_profile_name", None)
        if name is None:
            return
        bindings = self.active_panel.get_bindings()
        if name == "Global":
            self.config_data["global"] = bindings
        else:
            if "profiles" not in self.config_data:
                self.config_data["profiles"] = {}
            self.config_data["profiles"][name] = bindings

    def _add_profile(self):
        name = AppPickerDialog(self).result
        if not name:
            return
        name = name.strip().lower()
        if not name:
            return
        if name in self.config_data.get("profiles", {}):
            messagebox.showinfo("Exists", f"Profile '{name}' already exists.")
            return
        if "profiles" not in self.config_data:
            self.config_data["profiles"] = {}
        self.config_data["profiles"][name] = []
        self._populate_profile_list()
        # Select the new profile
        items = list(self.profile_listbox.get(0, "end"))
        idx = items.index(name)
        self.profile_listbox.selection_clear(0, "end")
        self.profile_listbox.selection_set(idx)
        self._load_panel(name)

    def _delete_profile(self):
        sel = self.profile_listbox.curselection()
        if not sel:
            return
        name = self.profile_listbox.get(sel[0])
        if name == "Global":
            messagebox.showinfo("Cannot delete", "The Global profile cannot be deleted.")
            return
        if not messagebox.askyesno("Delete", f"Delete profile '{name}'?"):
            return
        self.config_data["profiles"].pop(name, None)
        if self.active_panel and getattr(self.active_panel, "_profile_name", None) == name:
            self.active_panel.destroy()
            self.active_panel = None
            self.profile_title.config(text="Select a profile")
        self._populate_profile_list()

    def _copy_from_profile(self):
        if not self.active_panel:
            messagebox.showinfo("No profile selected", "Select a destination profile first.")
            return

        dest_name = getattr(self.active_panel, "_profile_name", None)
        if dest_name is None:
            return

        # Build list of all profiles except the current destination
        all_profiles = ["Global"] + list(self.config_data.get("profiles", {}).keys())
        sources = [p for p in all_profiles if p != dest_name]
        if not sources:
            messagebox.showinfo("Nothing to copy from", "No other profiles exist yet.")
            return

        dialog = CopyFromDialog(self, sources)
        if not dialog.result:
            return

        src_name, mode = dialog.result

        # Get source bindings
        if src_name == "Global":
            src_bindings = list(self.config_data.get("global", []))
        else:
            src_bindings = list(self.config_data.get("profiles", {}).get(src_name, []))

        if not src_bindings:
            messagebox.showinfo("Empty", f"'{src_name}' has no bindings to copy.")
            return

        if mode == "replace":
            # Wipe existing rows and load source bindings fresh
            for row in list(self.active_panel.rows):
                row.destroy()
            self.active_panel.rows.clear()
            for b in src_bindings:
                self.active_panel.add_row(b.get("from", ""), b.get("to", ""))
        else:
            # Append — skip exact duplicates
            existing = {(r.from_var.get(), r.to_var.get()) for r in self.active_panel.rows}
            for b in src_bindings:
                pair = (b.get("from", ""), b.get("to", ""))
                if pair not in existing:
                    self.active_panel.add_row(*pair)
                    existing.add(pair)

    # ------------------------------------------------------------------ toggle / save

    def _install_toggle_hook(self):
        """Called on the KeyboardThread to install the F10 hook there."""
        self._toggle_hook = keyboard.hook_key(TOGGLE_KEY, self._on_toggle_key, suppress=False)

    def _on_toggle_key(self, event):
        """Called by keyboard hook on F10 press/release — only act on keydown."""
        if event.event_type == keyboard.KEY_DOWN:
            self.after(0, self._toggle)  # marshal back to tkinter thread

    def _toggle(self):
        new_state = not self._enabled.get()
        self._enabled.set(new_state)
        self.engine.set_enabled(new_state)
        if new_state:
            self.status_label.config(text="● ENABLED", fg="#27ae60")
        else:
            self.status_label.config(text="● DISABLED", fg="#c0392b")

    def _save(self):
        self._save_active_panel()
        save_config(self.config_data)
        self.engine.reload_config(self.config_data)
        messagebox.showinfo("Saved", "Bindings saved successfully.")

    def _on_close(self):
        def _cleanup():
            self.engine._clear_hooks()
            try:
                keyboard.unhook(self._toggle_hook)
            except Exception:
                pass
        get_kb_thread().call_sync(_cleanup)
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
