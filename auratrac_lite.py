"""
AURAtrac Lite — simple, always-on-top counter overlay with a control panel
Windows 11 • Python 3.10+ • Tkinter • keyboard • mouse

Features
- Overlay window that stays on top of games (borderless, optional transparent background)
- Control Panel to configure tracking without restarting
- Track a key OR mouse input chosen by pressing it once (Capture…)
- Modes:
    1. Rapid Mode (is_rapid_mode=True): every press = +1 (Immediate count).
    2. Group/Burst Mode (is_rapid_mode=False): Uses Amount/Idle Time.
        - Idle=0: Multi-Click Count (every N presses).
        - Idle>0, Amount=1: Burst Count (counts +1 on the first click of a new sequence).
        - Idle>0, Amount>1: Multi-Burst Count (requires N sequences/bursts to count +1).
- Hotkeys (global):
    • -  subtract 1
    • =  add 1
    • 0  reset
    • 9  pause/resume
    • Delete  exit app
- Style controls: text color, background color, font size, bold, transparent background, **opacity**
- Resizable overlay text (scales with font size)
- Persist settings to JSON next to script (auratrac_lite.json)

Notes
- Run Python as Administrator for global hooks to work reliably across fullscreen apps.
- Some anti-cheat systems may block hooks.
- Transparent background uses Windows-specific Tk attribute '-transparentcolor'.
"""

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, asdict, replace
from typing import Literal, Any

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox

# Third-party deps: keyboard, mouse
try:
    import keyboard  # type: ignore
    import mouse  # type: ignore
    HOOK_AVAILABLE = True
except ImportError:
    HOOK_AVAILABLE = False
    print("Warning: 'keyboard' and 'mouse' modules not found. Tracking will be disabled.")


# --- Configuration and Persistence ---
CONFIG_FILE = "auratrac_lite.json"

@dataclass
class Settings:
    # Tracking
    input_type: Literal["keyboard", "mouse"] = "keyboard"
    input_code: int = 44  # Default: 'z' (keyboard scan code)
    
    # Simple toggle for rapid counting
    is_rapid_mode: bool = True 

    # These are only used if is_rapid_mode is False
    amount: int = 1      # Number of presses (if Idle=0) or bursts (if Idle>0) required for +1 count
    burst_idle_ms: int = 0  # Idle time (ms) to reset the sequence (0 means disabled)

    # Style
    font_size: int = 48
    text_color: str = "#FFD700"  # Gold
    bg_color: str = "#000000"    # Black
    is_bold: bool = True
    is_transparent: bool = True
    opacity: float = 0.85        # window opacity (0.10–1.00)

    # State
    is_paused: bool = False
    count: int = 0

# --- Utility Function for Key Name ---

def get_key_name_from_scan_code(scan_code: int) -> str:
    """Safely converts a keyboard scan code to a readable key name."""
    if not HOOK_AVAILABLE:
        return f"Code {scan_code}"
    try:
        name = keyboard.get_key_name(scan_code)
        return name.capitalize() if name else f"Code {scan_code}"
    except Exception:
        return f"Code {scan_code}"


# --- Core Logic (Runs in a separate thread) ---

class CounterCore(threading.Thread):
    def __init__(self, settings: Settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.count = settings.count
        self.is_running = True
        
        # Communication queues
        self.update_q = queue.Queue() # For sending count/status updates to UI
        self.event_q = queue.Queue()  # For receiving events (quit/hotkeys)
        self.setting_q = queue.Queue() # For receiving new settings from UI

        # State management for Burst/Group Mode
        self.sequence_presses = 0
        self.last_press_time = 0.0
        self.burst_count_tracker = 0

        # Scroll Wheel Debounce State
        self.last_scroll_time = 0.0
        self.scroll_debounce_ms = 100

        # Hooks
        self.key_hook: Any = None
        self.mouse_hook: Any = None
        self._setup_hooks()

    def _update_count(self, delta: int):
        if not self.settings.is_paused:
            self.count += delta
            self.update_q.put(("count", self.count))

    def _handle_input(self):
        current_time = time.time() * 1000  # ms
        
        # 1. RAPID MODE
        if self.settings.is_rapid_mode:
            self._update_count(1)
            self.last_press_time = current_time 
            self.sequence_presses = 0 
            self.burst_count_tracker = 0
            self.update_q.put(("status", "Rapid Mode: +1"))
            return

        # 2. BURST/GROUP MODE
        idle_duration = current_time - self.last_press_time

        # 2a. Sequence reset via idle time
        if self.settings.burst_idle_ms > 0 and self.last_press_time != 0.0:
            if idle_duration > self.settings.burst_idle_ms:
                if self.sequence_presses > 0:
                    self.burst_count_tracker += 1
                    self.update_q.put(("status", f"Burst Completed ({self.burst_count_tracker}/{self.settings.amount})"))
                self.sequence_presses = 0
                
        # 2b. Increment sequence and update time
        self.sequence_presses += 1 
        self.last_press_time = current_time 

        # 3. Apply count logic
        if self.settings.burst_idle_ms == 0:
            # Multi-Click Count
            if self.sequence_presses % self.settings.amount == 0:
                self._update_count(1)
                self.update_q.put(("status", f"Multi-Click Count ({self.settings.amount} met)"))
        else:
            if self.settings.amount == 1:
                # Burst Count (first press of sequence)
                if self.sequence_presses == 1:
                    self._update_count(1)
                    self.update_q.put(("status", "Burst Count (First Press)"))
            elif self.settings.amount > 1:
                # Multi-Burst: count on 1st press of Nth burst
                if self.sequence_presses == 1 and self.burst_count_tracker >= self.settings.amount:
                    self._update_count(1)
                    self.update_q.put(("status", f"Multi-Burst Count ({self.settings.amount} bursts met)"))
                    self.burst_count_tracker = 0

        self.update_q.put(("sequence_presses", self.sequence_presses))

    def _key_callback(self, event):
        if event.event_type == keyboard.KEY_DOWN and event.scan_code == self.settings.input_code:
            self._handle_input()
        
    def _mouse_callback(self, event):
        button_name_map = {
            1: mouse.LEFT,
            2: mouse.MIDDLE,
            3: mouse.RIGHT,
            4: 'x',
            5: 'x2',
        }
        if isinstance(event, mouse.ButtonEvent) and event.event_type == mouse.DOWN:
            target_button_name = button_name_map.get(self.settings.input_code)
            if target_button_name and event.button == target_button_name:
                self._handle_input()
        elif isinstance(event, mouse.WheelEvent):
            if (event.delta > 0 and self.settings.input_code == 10) or \
               (event.delta < 0 and self.settings.input_code == 11):
                current_time = time.time() * 1000
                if current_time - self.last_scroll_time < self.scroll_debounce_ms:
                    return
                self.last_scroll_time = current_time
                self._handle_input()

    def _setup_hooks(self):
        if not HOOK_AVAILABLE: return
        self._teardown_hooks()

        # Global hotkeys
        keyboard.add_hotkey('-', lambda: self._update_count(-1))
        keyboard.add_hotkey('=', lambda: self._update_count(1))
        keyboard.add_hotkey('0', lambda: self.set_count(0))
        keyboard.add_hotkey('9', lambda: self.toggle_pause())
        keyboard.add_hotkey('delete', lambda: self.event_q.put("quit"))

        # Tracking hook
        if self.settings.input_type == "keyboard":
            self.key_hook = keyboard.hook(self._key_callback)
            key_name = get_key_name_from_scan_code(self.settings.input_code)
            self.update_q.put(("status", f"Tracking Key: {key_name}"))
        elif self.settings.input_type == "mouse":
            self.mouse_hook = mouse.hook(self._mouse_callback)
            mouse_btn_name_map = {
                1: "Left Click", 2: "Middle Click", 3: "Right Click",
                4: "Thumb 1 (Back)", 5: "Thumb 2 (Forward)",
                10: "Scroll Wheel Up", 11: "Scroll Wheel Down"
            }
            mouse_btn_name = mouse_btn_name_map.get(self.settings.input_code, 'Unknown')
            self.update_q.put(("status", f"Tracking Mouse: {mouse_btn_name}"))

    def _teardown_hooks(self):
        if not HOOK_AVAILABLE: return
        keyboard.unhook_all()
        if self.key_hook:
            try: keyboard.unhook(self.key_hook)
            except ValueError: pass
            self.key_hook = None
        if self.mouse_hook:
            try: mouse.unhook(self.mouse_hook)
            except ValueError: pass
            self.mouse_hook = None

    def run(self):
        self.update_q.put(("status", "Core started. Waiting for input..."))
        while self.is_running:
            try:
                new_settings = self.setting_q.get_nowait()
                self.settings = new_settings
                self.count = new_settings.count
                self.sequence_presses = 0
                self.burst_count_tracker = 0
                self._setup_hooks()
                self.update_q.put(("count", self.count))
                self.update_q.put(("paused", self.settings.is_paused))
            except queue.Empty:
                pass
            time.sleep(0.01)

    def stop(self):
        self.is_running = False
        self._teardown_hooks()

    def set_count(self, new_count: int):
        self.count = new_count
        self.update_q.put(("count", self.count))
        self.sequence_presses = 0
        self.burst_count_tracker = 0

    def toggle_pause(self):
        self.settings.is_paused = not self.settings.is_paused
        self.update_q.put(("paused", self.settings.is_paused))
        
    def update_settings(self, new_settings: Settings):
        self.setting_q.put(new_settings)


# --- UI: Overlay Window ---

class Overlay(tk.Toplevel):
    def __init__(self, master, core: CounterCore, settings: Settings):
        super().__init__(master)
        self.core = core
        self.settings = settings
        
        self.master.withdraw()
        self.title("AURAtrac Lite Overlay")
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)

        self.count_var = tk.StringVar(value=str(settings.count))
        self.count_label = tk.Label(
            self, 
            textvariable=self.count_var,
            font=("Inter", settings.font_size, "bold" if settings.is_bold else "normal"),
            fg=settings.text_color,
            bg=settings.bg_color,
            bd=0,
            padx=10,
            pady=5
        )
        self.count_label.pack(expand=True, fill=tk.BOTH)

        # --- make overlay draggable ---
        self._drag_off_x = 0
        self._drag_off_y = 0
        for widget in (self, self.count_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._do_drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        self.apply_style()
        self.after(100, self._poll_core_updates)

    # drag handlers
    def _start_drag(self, event: tk.Event):
        try:
            self._drag_off_x = event.x_root - self.winfo_x()
            self._drag_off_y = event.y_root - self.winfo_y()
            self.configure(cursor="fleur")
        except Exception:
            pass

    def _do_drag(self, event: tk.Event):
        try:
            new_x = event.x_root - self._drag_off_x
            new_y = event.y_root - self._drag_off_y
            self.geometry(f"+{new_x}+{new_y}")
        except Exception:
            pass

    def _end_drag(self, _event: tk.Event):
        self.configure(cursor="")

    def apply_style(self):
        # Font & colors
        font_weight = "bold" if self.settings.is_bold else "normal"
        self.count_label.config(
            font=("Inter", self.settings.font_size, font_weight),
            fg=self.settings.text_color,
            bg=self.settings.bg_color
        )

        # Opacity (whole window)
        clamped_opacity = max(0.10, min(1.00, float(self.settings.opacity)))
        self.wm_attributes("-alpha", clamped_opacity)

        # Transparent BG (Windows only)
        if self.settings.is_transparent and os.name == 'nt':
            transparent_color = "#FE00FE"  # color key
            self.config(bg=transparent_color)
            self.wm_attributes("-transparentcolor", transparent_color)
            self.count_label.config(bg=transparent_color)
        else:
            self.wm_attributes("-transparentcolor", "")
            self.config(bg=self.settings.bg_color)
            self.count_label.config(bg=self.settings.bg_color)

    def update_count(self, new_count: int):
        self.count_var.set(str(new_count))

    def _poll_core_updates(self):
        try:
            while True:
                update_type, value = self.core.update_q.get_nowait()
                if update_type == "count":
                    self.update_count(value)
                elif update_type == "paused":
                    self.update_count("PAUSED" if value else self.core.count)
                elif update_type == "status":
                    pass
                elif update_type == "sequence_presses":
                    pass
        except queue.Empty:
            pass
        self.after(100, self._poll_core_updates)


# --- UI: Control Panel Window ---

class ControlPanel(tk.Toplevel):
    def __init__(self, master, core: CounterCore, settings: Settings, overlay: Overlay):
        super().__init__(master)
        self.core = core
        self.settings = settings
        self.overlay = overlay

        self.title("AURAtrac Lite Control")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Style setup
        style = ttk.Style()
        style.configure("TButton", padding=6)
        style.configure("TLabel", padding=2)

        # Variables
        initial_input_name = "...Press Key/Mouse..."
        if settings.input_type == "keyboard":
            initial_input_name = get_key_name_from_scan_code(settings.input_code)
        elif settings.input_type == "mouse":
            mouse_map = {
                1: "Left Click", 2: "Middle Click", 3: "Right Click",
                4: "Thumb 1 (Back)", 5: "Thumb 2 (Forward)",
                10: "Scroll Wheel Up", 11: "Scroll Wheel Down"
            }
            initial_input_name = mouse_map.get(settings.input_code, f"Mouse Code {settings.input_code}")

        self.input_var = tk.StringVar(value=initial_input_name)
        self.rapid_mode_var = tk.BooleanVar(value=settings.is_rapid_mode)
        self.amount_var = tk.IntVar(value=settings.amount)
        self.burst_idle_var = tk.IntVar(value=settings.burst_idle_ms)
        self.amount_label_var = tk.StringVar() 

        self.font_size_var = tk.IntVar(value=settings.font_size)
        self.text_color_var = tk.StringVar(value=settings.text_color)
        self.bg_color_var = tk.StringVar(value=settings.bg_color)
        self.bold_var = tk.BooleanVar(value=settings.is_bold)
        self.transparent_var = tk.BooleanVar(value=settings.is_transparent)

        # Opacity UI vars (0.10–1.00 mapped to 10–100)
        self.opacity_var = tk.DoubleVar(value=round(settings.opacity * 100.0, 1))
        self.opacity_label_var = tk.StringVar(value=f"{int(self.opacity_var.get())}%")

        self.status = tk.StringVar(value="Ready")
        self.is_capturing = False

        self._setup_ui()
        self.apply_inputs()
        self.status.set("Settings loaded")

        self.after(100, self._poll_core_events)
        self.after(100, self._check_pending_apply)
        
    def _setup_ui(self):
        main_frame = ttk.Frame(self, padding="10 10 10 10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Tracking Frame ---
        settings_frame = ttk.LabelFrame(main_frame, text="Tracking Configuration", padding="10")
        settings_frame.pack(fill=tk.X, pady=10)

        # Input Capture
        input_capture_frame = ttk.Frame(settings_frame)
        input_capture_frame.pack(fill=tk.X, pady=5)
        ttk.Label(input_capture_frame, text="Input:").pack(side=tk.LEFT, padx=(5, 0))
        self.input_label = ttk.Label(input_capture_frame, textvariable=self.input_var, relief=tk.SUNKEN, width=20)
        self.input_label.pack(side=tk.LEFT, padx=5)
        ttk.Button(input_capture_frame, text="Capture...", command=self._start_capture).pack(side=tk.LEFT)
        
        # Rapid Mode Checkbox
        rapid_mode_frame = ttk.Frame(settings_frame)
        rapid_mode_frame.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(
            rapid_mode_frame, text="Rapid Mode (Count every press)", 
            variable=self.rapid_mode_var, 
            command=self._toggle_mode_labels
        ).pack(side=tk.LEFT, padx=5)

        # Idle Time
        burst_frame = ttk.Frame(settings_frame)
        burst_frame.pack(fill=tk.X, pady=5)
        ttk.Label(burst_frame, text="Idle Time (ms) to reset sequence (0=Off):").pack(side=tk.LEFT, padx=(5, 0))
        self.burst_idle_spinbox = ttk.Spinbox(
            burst_frame,
            textvariable=self.burst_idle_var,
            from_=0, 
            to=10000, 
            increment=10,
            width=6,
            command=self._toggle_mode_labels
        )
        self.burst_idle_spinbox.pack(side=tk.LEFT, padx=(0, 5))

        # Amount (Dynamic label)
        amount_frame = ttk.Frame(settings_frame)
        amount_frame.pack(fill=tk.X, pady=5)
        self.amount_dynamic_label = ttk.Label(amount_frame, textvariable=self.amount_label_var)
        self.amount_dynamic_label.pack(side=tk.LEFT, padx=(5, 0))
        self.amount_spinbox = ttk.Spinbox( 
            amount_frame, textvariable=self.amount_var, from_=1, to=9999, increment=1, width=6, command=self._apply_pending_settings
        )
        self.amount_spinbox.pack(side=tk.LEFT, padx=(0, 5))
        self._toggle_mode_labels(skip_apply=True)
        
        # --- Style Frame ---
        style_frame = ttk.LabelFrame(main_frame, text="Overlay Style", padding="10")
        style_frame.pack(fill=tk.X, pady=10)

        # Font Size & Bold
        font_frame = ttk.Frame(style_frame)
        font_frame.pack(fill=tk.X, pady=5)
        ttk.Label(font_frame, text="Font Size:").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(
            font_frame, textvariable=self.font_size_var, from_=12, to=256, increment=4, width=4, command=self._apply_pending_settings
        ).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(
            font_frame, text="Bold", variable=self.bold_var, command=self._apply_pending_settings
        ).pack(side=tk.LEFT, padx=10)
        
        # Colors + Transparent BG
        color_frame = ttk.Frame(style_frame)
        color_frame.pack(fill=tk.X, pady=5)
        ttk.Label(color_frame, text="Text Color:").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(color_frame, textvariable=self.text_color_var, command=lambda: self._choose_color(self.text_color_var)).pack(side=tk.LEFT, padx=5)
        ttk.Label(color_frame, text="Background:").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(color_frame, textvariable=self.bg_color_var, command=lambda: self._choose_color(self.bg_color_var)).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(
            color_frame, text="Transparent BG", variable=self.transparent_var, command=self._apply_pending_settings
        ).pack(side=tk.LEFT, padx=10)

        # Opacity
        opacity_frame = ttk.Frame(style_frame)
        opacity_frame.pack(fill=tk.X, pady=5)
        ttk.Label(opacity_frame, text="Opacity:").pack(side=tk.LEFT, padx=(5, 0))
        self.opacity_scale = ttk.Scale(
            opacity_frame,
            orient=tk.HORIZONTAL,
            from_=10.0,
            to=100.0,
            variable=self.opacity_var,
            command=self._on_opacity_change  # updates label + schedules apply
        )
        self.opacity_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Label(opacity_frame, textvariable=self.opacity_label_var, width=5, anchor="e").pack(side=tk.LEFT)

        # --- Actions Frame ---
        actions_frame = ttk.LabelFrame(main_frame, text="Actions", padding="10")
        actions_frame.pack(fill=tk.X, pady=10)
        actions_row1 = ttk.Frame(actions_frame)
        actions_row1.pack(fill=tk.X, pady=5)
        ttk.Button(actions_row1, text="Reset Count (0)", command=lambda: self.core.update_settings(replace(self.settings, count=0))).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        ttk.Button(actions_row1, text="Pause/Resume (9)", command=self.core.toggle_pause).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        actions_row2 = ttk.Frame(actions_frame)
        actions_row2.pack(fill=tk.X, pady=5)
        ttk.Button(actions_row2, text="Save Settings", command=self.save_settings).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        ttk.Button(actions_row2, text="Exit (Delete)", command=self._on_close).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        
        # --- Status Bar ---
        status_label = ttk.Label(main_frame, textvariable=self.status, relief=tk.SUNKEN, anchor=tk.W)
        status_label.pack(fill=tk.X, pady=(10, 0))

    # --- Methods ---
    def _on_opacity_change(self, _val: str):
        # Update small % label and schedule apply
        try:
            self.opacity_label_var.set(f"{int(float(self.opacity_var.get()))}%")
        except Exception:
            pass
        self._apply_pending_settings()

    def _toggle_mode_labels(self, skip_apply=False):
        """Updates UI based on Rapid Mode and Idle Time settings."""
        is_rapid = self.rapid_mode_var.get()
        is_idle_on = self.burst_idle_var.get() > 0
        
        if is_rapid:
            state = 'disabled'
            self.amount_label_var.set("Amount (N/A):")
            self.status.set("Mode: Rapid (1:1 counting)")
        else:
            state = 'normal'
            if is_idle_on:
                if self.amount_var.get() == 1:
                    self.status.set("Mode: Burst (Count on 1st click, idle resets)")
                else:
                    self.status.set(f"Mode: Multi-Burst (Count on 1st click of {self.amount_var.get()}th burst)")
                self.amount_label_var.set("Amount (N bursts):")
            else:
                self.status.set(f"Mode: Multi-Click (Count every {self.amount_var.get()} presses)")
                self.amount_label_var.set("Amount (N presses):")
        
        self.amount_spinbox.config(state=state)
        self.burst_idle_spinbox.config(state=state)
        if not skip_apply:
            self._apply_pending_settings()

    def _choose_color(self, var: tk.StringVar):
        color_code = colorchooser.askcolor(title="Choose Color", initialcolor=var.get())
        if color_code and color_code[1]:
            var.set(color_code[1].upper())
            self._apply_pending_settings()

    def _start_capture(self):
        if not HOOK_AVAILABLE:
            self.status.set("ERROR: 'keyboard' and 'mouse' modules required for capture.")
            return

        self.is_capturing = True
        self.input_label.config(relief=tk.RAISED)
        self.status.set("Press a key or mouse button now...")
        
        self.master.config(cursor="watch")
        self.config(cursor="watch")

        keyboard.hook(self._capture_key)
        mouse.hook(self._capture_mouse)

    def _end_capture(self):
        self.is_capturing = False
        self.input_label.config(relief=tk.SUNKEN)
        self.master.config(cursor="")
        self.config(cursor="")

        if HOOK_AVAILABLE:
            keyboard.unhook_all()
            mouse.unhook_all()
        
        self.core.update_settings(self.settings) 

    def _capture_key(self, event):
        if self.is_capturing and event.event_type == keyboard.KEY_DOWN:
            self.settings.input_type = "keyboard"
            self.settings.input_code = event.scan_code
            
            key_name = get_key_name_from_scan_code(event.scan_code)
            self.input_var.set(key_name)
            
            self._end_capture()
            self._apply_pending_settings()
            self.status.set(f"Input captured: Key '{key_name}'")
            return False 
        
    def _capture_mouse(self, event):
        if self.is_capturing:
            self.settings.input_type = "mouse"
            mouse_code = 0
            mouse_name = "Unknown"
            
            if isinstance(event, mouse.ButtonEvent) and event.event_type == mouse.DOWN:
                if event.button == mouse.LEFT:
                    mouse_code, mouse_name = 1, "Left Click"
                elif event.button == mouse.MIDDLE:
                    mouse_code, mouse_name = 2, "Middle Click"
                elif event.button == mouse.RIGHT:
                    mouse_code, mouse_name = 3, "Right Click"
                elif event.button == 'x': 
                    mouse_code, mouse_name = 4, "Thumb 1 (Back)"
                elif event.button == 'x2':
                    mouse_code, mouse_name = 5, "Thumb 2 (Forward)"
            elif isinstance(event, mouse.WheelEvent):
                if event.delta > 0:
                    mouse_code, mouse_name = 10, "Scroll Wheel Up"
                elif event.delta < 0:
                    mouse_code, mouse_name = 11, "Scroll Wheel Down"

            if mouse_code != 0:
                self.settings.input_code = mouse_code
                self.input_var.set(mouse_name)
                self._end_capture()
                self._apply_pending_settings()
                self.status.set(f"Input captured: Mouse '{mouse_name}'")
                return False

    _pending_apply_settings = False

    def _apply_pending_settings(self):
        self._pending_apply_settings = True

    def _check_pending_apply(self):
        if self._pending_apply_settings:
            self.apply_inputs()
            self._pending_apply_settings = False
            self.status.set("Settings updated (unsaved)")
        self.after(200, self._check_pending_apply)

    def apply_inputs(self):
        # Tracking
        self.settings.is_rapid_mode = self.rapid_mode_var.get()
        self.settings.amount = max(1, self.amount_var.get()) 
        self.settings.burst_idle_ms = max(0, self.burst_idle_var.get()) 
        
        # Style updates
        self.settings.font_size = self.font_size_var.get()
        self.settings.text_color = self.text_color_var.get()
        self.settings.bg_color = self.bg_color_var.get()
        self.settings.is_bold = self.bold_var.get()
        self.settings.is_transparent = self.transparent_var.get()
        # Opacity (map 10–100% → 0.10–1.00, clamp)
        try:
            self.settings.opacity = max(0.10, min(1.00, float(self.opacity_var.get()) / 100.0))
        except Exception:
            self.settings.opacity = 0.85

        # Update the input label display based on current settings
        if self.settings.input_type == "keyboard":
            name = get_key_name_from_scan_code(self.settings.input_code)
            self.input_var.set(name)
        elif self.settings.input_type == "mouse":
            mouse_map = {
                1: "Left Click", 
                2: "Middle Click", 
                3: "Right Click", 
                4: "Thumb 1 (Back)", 
                5: "Thumb 2 (Forward)", 
                10: "Scroll Wheel Up",
                11: "Scroll Wheel Down"
            }
            name = mouse_map.get(self.settings.input_code, f"Code {self.settings.input_code}")
            self.input_var.set(name)
        
        # Mode labels
        self._toggle_mode_labels(skip_apply=True)

        # Apply style to the overlay (includes opacity)
        self.overlay.apply_style()

        # Push tracking/state settings to the core thread
        self.core.update_settings(self.settings)

    def save_settings(self):
        try:
            self.apply_inputs() 
            save_data = asdict(self.settings)
            # Don't persist volatile runtime state
            del save_data['count']
            del save_data['is_paused']

            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=4)
            self.status.set(f"Settings saved to {CONFIG_FILE}")
        except Exception as e:
            self.status.set(f"ERROR saving settings: {e}")

    # --- Events ---
    def _poll_core_events(self):
        try:
            msg = self.core.event_q.get_nowait()
            if msg == "quit":
                self.master.quit()
                return
        except queue.Empty:
            pass
        self.after(100, self._poll_core_events)

    def _on_close(self):
        if messagebox.askokcancel("Quit", "Exit AURAtrac Lite?"):
            self.master.quit()


# --- Main Application Setup ---

def load_or_default() -> Settings:
    """Loads settings from JSON file or returns default settings."""
    settings = Settings()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if 'group_n' in data:
                data['amount'] = data.pop('group_n')
            for k, v in data.items():
                if hasattr(settings, k):
                    setattr(settings, k, v)
            settings.amount = max(1, settings.amount)
            # Clamp opacity if coming from older/odd files
            try:
                settings.opacity = max(0.10, min(1.00, float(settings.opacity)))
            except Exception:
                settings.opacity = 0.85
            return settings
        except Exception:
            print(f"Warning: Could not load or parse {CONFIG_FILE}. Using default settings.")
    return settings


def main():
    if HOOK_AVAILABLE:
        try:
            if os.name == 'nt':
                 pass
            elif os.getuid() != 0:
                print("Note: Running without Administrator privileges. Global hooks may be unreliable in fullscreen applications.")
        except AttributeError:
            pass

    settings = load_or_default()
    core = CounterCore(settings)

    root = tk.Tk()
    root.withdraw()

    overlay = Overlay(root, core, settings)

    root_x = 100
    root_y = 100
    if os.name == 'nt':
        try:
            import win32api 
            root_x = win32api.GetSystemMetrics(0) // 2 - 200
        except ImportError:
            pass

    overlay.geometry(f"+{root_x}+{root_y}")

    control = ControlPanel(root, core, settings, overlay)
    
    control.update_idletasks()
    control_x = root_x + overlay.winfo_width() + 50
    control.geometry(f"+{control_x}+100")

    core.start()
    
    root.protocol("WM_DELETE_WINDOW", control._on_close)

    try:
        root.mainloop()
    finally:
        core.stop()
        if core.is_alive():
            core.join(timeout=1.0)
        control.save_settings()


if __name__ == "__main__":
    main()
