"""
FixBindings - Global keyboard hotkey rebinder with per-application profiles.
Toggle on/off with F10. Works globally including in games.

Hook engine uses raw SetWindowsHookEx + GetMessage pump (same as AutoHotkey),
which is the lowest level available from userspace and works in DirectInput games.
"""

import json
import os
import threading
import time
import ctypes
import ctypes.wintypes
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import keyboard          # used ONLY for key-name capture in the GUI
import win32gui
import win32process
import psutil

# --------------------------------------------------------------------------- #
# Windows API setup                                                            #
# --------------------------------------------------------------------------- #

user32  = ctypes.WinDLL("user32",  use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL  = 13
WM_KEYDOWN      = 0x0100
WM_KEYUP        = 0x0101
WM_SYSKEYDOWN   = 0x0104
WM_SYSKEYUP     = 0x0105
HC_ACTION       = 0

INPUT_KEYBOARD      = 1
KEYEVENTF_KEYUP     = 0x0002
KEYEVENTF_SCANCODE  = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

LLKHF_INJECTED = 0x10          # flag set on events WE send — lets us ignore them

MAPVK_VK_TO_VSC = 0
MAPVK_VSC_TO_VK = 1


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      ctypes.wintypes.DWORD),
        ("scanCode",    ctypes.wintypes.DWORD),
        ("flags",       ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


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


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    ctypes.wintypes.LPARAM,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

# --------------------------------------------------------------------------- #
# VK name map                                                                  #
# --------------------------------------------------------------------------- #

_VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "caps lock": 0x14, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20,
    "page up": 0x21, "pageup": 0x21,
    "page down": 0x22, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "num lock": 0x90, "numlock": 0x90, "scroll lock": 0x91, "scrolllock": 0x91,
    "left shift": 0xA0, "right shift": 0xA1,
    "left ctrl": 0xA2, "right ctrl": 0xA3,
    "left alt": 0xA4, "right alt": 0xA5,
    "semicolon": 0xBA, "equal": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "grave": 0xC0,
    "open bracket": 0xDB, "backslash": 0xDC, "close bracket": 0xDD,
    "apostrophe": 0xDE,
}

_EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24,
    0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E,
    0x5B, 0x5C,
    0xA3, 0xA5,
}


def vk_for(name: str) -> int:
    n = name.lower().strip()
    if n in _VK_MAP:
        return _VK_MAP[n]
    if len(n) == 1:
        v = user32.VkKeyScanW(ord(n)) & 0xFF
        if v:
            return v
    return 0


def sendinput_key(vk: int, key_up: bool = False):
    sc = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_SCANCODE
    if key_up:
        flags |= KEYEVENTF_KEYUP
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._u.ki.wVk = 0
    inp._u.ki.wScan = sc
    inp._u.ki.dwFlags = flags
    inp._u.ki.time = 0
    inp._u.ki.dwExtraInfo = ctypes.pointer(ctypes.c_ulong(0))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# --------------------------------------------------------------------------- #
# Raw hook engine                                                              #
# --------------------------------------------------------------------------- #

class HookEngine:
    """
    Installs a single WH_KEYBOARD_LL hook via SetWindowsHookEx and drives it
    with a proper GetMessage pump on a dedicated thread.  This is the same
    mechanism AutoHotkey uses and works in DirectInput / raw-input games.
    """

    def __init__(self):
        self._bindings = {}   # vk_from -> vk_to
        self._toggle_vk: int = vk_for("f10")
        self._enabled = False
        self._on_toggle_cb = None             # callable to invoke on F10

        self._hook = None
        self._hook_proc_ref = None            # keep alive — ctypes GC gotcha
        self._thread_id = None
        self._ready = threading.Event()

        t = threading.Thread(target=self._pump, daemon=True, name="HookPump")
        t.start()
        self._ready.wait()

    # ------------------------------------------------------------------
    def _pump(self):
        self._thread_id = kernel32.GetCurrentThreadId()

        @LowLevelKeyboardProc
        def proc(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

                # Ignore events we injected ourselves
                if not (info.flags & LLKHF_INJECTED):
                    vk = info.vkCode
                    is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up   = wParam in (WM_KEYUP,   WM_SYSKEYUP)

                    # F10 toggle (always active, not suppressed)
                    if vk == self._toggle_vk and is_down:
                        if self._on_toggle_cb:
                            self._on_toggle_cb()

                    # Remap
                    if self._enabled and vk in self._bindings:
                        target_vk = self._bindings[vk]
                        if is_down:
                            sendinput_key(target_vk, key_up=False)
                        elif is_up:
                            sendinput_key(target_vk, key_up=True)
                        return 1   # suppress original key

            return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

        self._hook_proc_ref = proc
        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, hmod, 0)
        self._ready.set()

        # Real Windows message pump — this is what makes the hook actually fire
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------
    def set_bindings(self, bindings):
        self._bindings = bindings

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def set_toggle_callback(self, cb):
        self._on_toggle_cb = cb

    def stop(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT


# --------------------------------------------------------------------------- #
# Config / file                                                                #
# --------------------------------------------------------------------------- #

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bindings.json")
TOGGLE_KEY  = "f10"


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
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Binding manager (sits between config and HookEngine)                        #
# --------------------------------------------------------------------------- #

class BindingManager:
    def __init__(self, hook: HookEngine, config: dict):
        self._hook = hook
        self._config = config
        self._enabled = False

    def _resolve(self):
        exe = get_active_exe()
        bindings = list(self._config.get("global", []))
        for app_exe, app_bindings in self._config.get("profiles", {}).items():
            if app_exe.lower() == exe:
                bindings.extend(app_bindings)
                break

        result = {}
        for b in bindings:
            fk = vk_for(b.get("from", ""))
            tk_ = vk_for(b.get("to", ""))
            if fk and tk_:
                result[fk] = tk_
        return result

    def refresh(self):
        self._hook.set_bindings(self._resolve() if self._enabled else {})

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.refresh()

    def reload_config(self, config: dict):
        self._config = config
        self.refresh()


# --------------------------------------------------------------------------- #
# Background watcher                                                           #
# --------------------------------------------------------------------------- #

class WatcherThread(threading.Thread):
    def __init__(self, manager: BindingManager):
        super().__init__(daemon=True)
        self._manager = manager
        self._last_exe = ""

    def run(self):
        while True:
            exe = get_active_exe()
            if exe != self._last_exe:
                self._last_exe = exe
                self._manager.refresh()
            time.sleep(0.25)


# --------------------------------------------------------------------------- #
# GUI                                                                          #
# --------------------------------------------------------------------------- #

class BindingRow(tk.Frame):
    def __init__(self, parent, from_key="", to_key="", on_delete=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.from_var = tk.StringVar(value=from_key)
        self.to_var   = tk.StringVar(value=to_key)

        tk.Label(self, text="From:", width=5, anchor="e").pack(side="left")
        self.from_entry = tk.Entry(self, textvariable=self.from_var, width=14)
        self.from_entry.pack(side="left", padx=(0, 4))

        tk.Label(self, text="→  To:", width=6, anchor="e").pack(side="left")
        self.to_entry = tk.Entry(self, textvariable=self.to_var, width=14)
        self.to_entry.pack(side="left", padx=(0, 4))

        tk.Button(self, text="Capture", width=7,
                  command=self._capture_from).pack(side="left", padx=(0, 4))
        tk.Button(self, text="✕", fg="red", width=3,
                  command=lambda: on_delete(self) if on_delete else None).pack(side="left")

        self.from_entry.bind("<FocusIn>", lambda e: self._start_capture(self.from_var))
        self.to_entry.bind("<FocusIn>",   lambda e: self._start_capture(self.to_var))

    def _start_capture(self, var):
        var.set("Press a key…")
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
        self._start_capture(self.from_var)

    def get(self):
        return {"from": self.from_var.get().strip(), "to": self.to_var.get().strip()}


class ProfilePanel(tk.Frame):
    def __init__(self, parent, bindings=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.rows = []

        self.canvas    = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner     = tk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        tk.Button(self, text="+ Add Binding", command=self.add_row).pack(side="bottom", pady=4)

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
        return [b for b in (r.get() for r in self.rows) if b["from"] and b["to"]]


class CopyFromDialog(tk.Toplevel):
    def __init__(self, parent, sources):
        super().__init__(parent)
        self.title("Copy Bindings From")
        self.resizable(False, False)
        self.geometry("300x280")
        self.grab_set()
        self.result = None

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
        tk.Button(btn_row, text="Copy",   command=self._confirm).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left", expand=True, fill="x")

        self.bind("<Return>", self._confirm)
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.wait_window(self)

    def _confirm(self, event=None):
        self.result = (self._var.get(), self._mode.get())
        self.destroy()


def get_running_exes():
    seen, result = set(), []
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
        e = tk.Entry(self, textvariable=self._search_var)
        e.pack(fill="x", padx=10)
        e.focus_set()

        tk.Label(self, text="Running processes:", anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        frame = tk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10)
        sb = ttk.Scrollbar(frame, orient="vertical")
        self._lb = tk.Listbox(frame, yscrollcommand=sb.set, exportselection=False)
        sb.config(command=self._lb.yview)
        sb.pack(side="right", fill="y")
        self._lb.pack(side="left", fill="both", expand=True)
        self._lb.bind("<Double-Button-1>", self._on_select)
        self._lb.bind("<<ListboxSelect>>", self._on_lb_pick)

        self._all = get_running_exes()
        self._populate(self._all)

        btn_row = tk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=8)
        tk.Button(btn_row, text="Add Selected / Typed", command=self._on_select).pack(side="left", expand=True, fill="x", padx=(0, 4))
        tk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="left", expand=True, fill="x")

        self.bind("<Return>", self._on_select)
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.wait_window(self)

    def _populate(self, exes):
        self._lb.delete(0, "end")
        for exe in exes:
            self._lb.insert("end", exe)

    def _on_search(self, *_):
        q = self._search_var.get().lower()
        self._populate([e for e in self._all if q in e] if q else self._all)

    def _on_lb_pick(self, event=None):
        sel = self._lb.curselection()
        if sel:
            self._search_var.set(self._lb.get(sel[0]))

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
        self._hook    = HookEngine()
        self._manager = BindingManager(self._hook, self.config_data)
        self._watcher = WatcherThread(self._manager)
        self._watcher.start()

        self._hook.set_toggle_callback(lambda: self.after(0, self._toggle))

        self._enabled = tk.BooleanVar(value=False)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        top = tk.Frame(self, pady=6, padx=8)
        top.pack(fill="x")

        self.status_label = tk.Label(top, text="● DISABLED",
                                     font=("Segoe UI", 11, "bold"), fg="#c0392b")
        self.status_label.pack(side="left", padx=(0, 10))

        tk.Button(top, text=f"Toggle  (F10)", width=16,
                  command=self._toggle).pack(side="left")
        tk.Button(top, text="Save All", width=10,
                  command=self._save).pack(side="right", padx=4)

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        paned = tk.PanedWindow(self, orient="horizontal", sashrelief="raised", sashwidth=5)
        paned.pack(fill="both", expand=True, padx=4, pady=4)

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
        if sel:
            self._load_panel(self.profile_listbox.get(sel[0]))

    def _load_panel(self, name):
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
            self.config_data.setdefault("profiles", {})[name] = bindings

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
        self.config_data.setdefault("profiles", {})[name] = []
        self._populate_profile_list()
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
        all_profiles = ["Global"] + list(self.config_data.get("profiles", {}).keys())
        sources = [p for p in all_profiles if p != dest_name]
        if not sources:
            messagebox.showinfo("Nothing to copy from", "No other profiles exist yet.")
            return
        dialog = CopyFromDialog(self, sources)
        if not dialog.result:
            return
        src_name, mode = dialog.result
        if src_name == "Global":
            src_bindings = list(self.config_data.get("global", []))
        else:
            src_bindings = list(self.config_data.get("profiles", {}).get(src_name, []))
        if not src_bindings:
            messagebox.showinfo("Empty", f"'{src_name}' has no bindings to copy.")
            return
        if mode == "replace":
            for row in list(self.active_panel.rows):
                row.destroy()
            self.active_panel.rows.clear()
            for b in src_bindings:
                self.active_panel.add_row(b.get("from", ""), b.get("to", ""))
        else:
            existing = {(r.from_var.get(), r.to_var.get()) for r in self.active_panel.rows}
            for b in src_bindings:
                pair = (b.get("from", ""), b.get("to", ""))
                if pair not in existing:
                    self.active_panel.add_row(*pair)
                    existing.add(pair)

    # ------------------------------------------------------------------ toggle / save

    def _toggle(self):
        new_state = not self._enabled.get()
        self._enabled.set(new_state)
        self._manager.set_enabled(new_state)
        self._hook.set_enabled(new_state)
        if new_state:
            self.status_label.config(text="● ENABLED", fg="#27ae60")
        else:
            self.status_label.config(text="● DISABLED", fg="#c0392b")

    def _save(self):
        self._save_active_panel()
        save_config(self.config_data)
        self._manager.reload_config(self.config_data)
        messagebox.showinfo("Saved", "Bindings saved successfully.")

    def _on_close(self):
        self._hook.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
