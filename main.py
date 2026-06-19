#!/usr/bin/env python3
"""
MEOE Overlay — Android Entry Point
Mobile Legends Economy Overlay Engine

Predicts creep gold, tracks GPM, highlights optimal farm targets
on the minimap, and shows item purchase progress in real time.
"""

import os
import sys
import json
import time
import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Callable
from enum import Enum, auto

# ── Kivy environment must be set BEFORE any kivy import ─────────────────────
os.environ['KIVY_NO_ARGS'] = '1'
os.environ['KIVY_WINDOW'] = 'sdl2'

from kivy.app import App
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.properties import (
    StringProperty, NumericProperty, ListProperty, BooleanProperty
)
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.widget import Widget
from kivy.graphics import Color, Ellipse, Rectangle, Line
from kivy.utils import platform

# ── Android-only imports ─────────────────────────────────────────────────────
if platform == 'android':
    from android.permissions import request_permissions, Permission
    from android.runnable import run_on_ui_thread
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Context        = autoclass('android.content.Context')
    WindowManager  = autoclass('android.view.WindowManager')
    LayoutParams   = autoclass('android.view.WindowManager$LayoutParams')
    View           = autoclass('android.view.View')
    Gravity        = autoclass('android.view.Gravity')
    PixelFormat    = autoclass('android.graphics.PixelFormat')
else:
    PythonActivity = None


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class GamePhase(Enum):
    EARLY = auto()
    MID   = auto()
    LATE  = auto()


@dataclass
class CreepEconomy:
    creep_type:         str
    base_gold:          float = 0.0
    scaling_per_minute: float = 0.0
    spawn_interval:     float = 0.0
    first_spawn_time:   float = 0.0
    observed_gold_values: List[Tuple[float, float]] = field(default_factory=list)
    calibrated_formula: Optional[Callable[[float], float]] = None


@dataclass
class CalibrationProfile:
    profile_id:       str
    games_calibrated: int   = 0
    confidence_score: float = 0.0
    creep_economies:  Dict[str, CreepEconomy] = field(default_factory=dict)

    # ── serialisation ────────────────────────────────────────────────────────
    def to_json(self) -> str:
        data = {
            'profile_id':       self.profile_id,
            'games_calibrated': self.games_calibrated,
            'confidence_score': self.confidence_score,
            'creep_economies':  {
                k: {
                    'creep_type':         v.creep_type,
                    'base_gold':          v.base_gold,
                    'scaling_per_minute': v.scaling_per_minute,
                    'spawn_interval':     v.spawn_interval,
                    'first_spawn_time':   v.first_spawn_time,
                    'observed':           v.observed_gold_values[-30:],
                }
                for k, v in self.creep_economies.items()
            },
        }
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> 'CalibrationProfile':
        parsed  = json.loads(raw)
        profile = cls(profile_id=parsed['profile_id'])
        profile.games_calibrated = parsed.get('games_calibrated', 0)
        profile.confidence_score = parsed.get('confidence_score', 0.0)

        # FIX: restore creep_economies (previously never deserialised)
        for key, ce in parsed.get('creep_economies', {}).items():
            profile.creep_economies[key] = CreepEconomy(
                creep_type=ce.get('creep_type', key),
                base_gold=ce.get('base_gold', 0.0),
                scaling_per_minute=ce.get('scaling_per_minute', 0.0),
                spawn_interval=ce.get('spawn_interval', 0.0),
                first_spawn_time=ce.get('first_spawn_time', 0.0),
                observed_gold_values=ce.get('observed', []),
            )
        return profile


@dataclass
class OverlayConfig:
    screen_resolution:  Tuple[int, int]         = (2400, 1080)
    minimap_rect:       Tuple[int, int, int, int] = (1850, 50, 500, 500)
    overlay_opacity:    float = 0.85
    update_interval_ms: int   = 500
    target_item:        Optional[str] = None
    target_item_cost:   float = 0.0
    show_gold_calc:     bool  = True
    show_optimal:       bool  = True
    show_minimap:       bool  = True


# ═══════════════════════════════════════════════════════════════════════════════
# CORE PREDICTOR
# ═══════════════════════════════════════════════════════════════════════════════

class EconomyPredictor:
    def __init__(self, calibration: CalibrationProfile, config: OverlayConfig):
        self.calibration = calibration
        self.config      = config

        # FIX: all instance attributes are now correctly indented inside __init__
        self.game_start_time:    Optional[float]           = None
        self.current_game_time:  float                     = 0.0
        self.gold_current:       float                     = 0.0
        self.kill_streak:        int                       = 0
        self.is_roam:            bool                      = False
        self.is_alive:           bool                      = True
        self.gold_history:       List[Tuple[float, float]] = []
        self.gpm_history:        List[float]               = []

        # FIX: lock for thread-safe list mutations
        self._lock = threading.Lock()

        self._init_defaults()
        self._load_calibrated()

    # ── defaults ─────────────────────────────────────────────────────────────
    def _init_defaults(self):
        self.defaults: Dict[str, CreepEconomy] = {
            'melee_creep':  CreepEconomy('melee_creep',  42.0, 1.5,  30.0,  10.0),
            'ranged_creep': CreepEconomy('ranged_creep', 52.0, 1.8,  30.0,  10.0),
            'siege_creep':  CreepEconomy('siege_creep',  85.0, 2.5,  90.0, 190.0),
            'jungle_small': CreepEconomy('jungle_small', 35.0, 1.2,  20.0,  20.0),
            'jungle_large': CreepEconomy('jungle_large', 65.0, 2.0,  30.0,  20.0),
            'red_buff':     CreepEconomy('red_buff',     80.0, 2.0,  90.0,  20.0),
            'blue_buff':    CreepEconomy('blue_buff',    80.0, 2.0,  90.0,  20.0),
            'turtle':       CreepEconomy('turtle',      120.0, 5.0, 120.0, 180.0),
            'lord':         CreepEconomy('lord',        200.0, 8.0, 180.0, 480.0),
        }

    def _load_calibrated(self):
        """Blend saved calibration data with hardcoded defaults."""
        for key, default in self.defaults.items():
            if key in self.calibration.creep_economies:
                cal    = self.calibration.creep_economies[key]
                weight = min(self.calibration.games_calibrated / 10.0, 0.7)
                default.base_gold          = cal.base_gold          * weight + default.base_gold          * (1 - weight)
                default.scaling_per_minute = cal.scaling_per_minute * weight + default.scaling_per_minute * (1 - weight)
                default.observed_gold_values = cal.observed_gold_values
            self.calibration.creep_economies[key] = default

    # ── game lifecycle ────────────────────────────────────────────────────────
    def start_game(self, hero_id: str, is_roam: bool = False):
        self.game_start_time  = time.time()
        self.gold_current     = 0.0
        self.kill_streak      = 0
        self.is_roam          = is_roam
        self.is_alive         = True
        self.current_game_time = 0.0
        with self._lock:
            self.gold_history.clear()
            self.gpm_history.clear()

    def update_time(self):
        if self.game_start_time:
            self.current_game_time = time.time() - self.game_start_time

    # ── phase ────────────────────────────────────────────────────────────────
    def get_phase(self) -> str:
        t = self.current_game_time
        if t < 240:  return 'EARLY'
        if t < 600:  return 'MID'
        return 'LATE'

    # ── gold prediction ───────────────────────────────────────────────────────
    def predict_creep_gold(self, creep_type: str, game_time: float) -> float:
        if creep_type not in self.calibration.creep_economies:
            return 0.0
        creep = self.calibration.creep_economies[creep_type]
        if game_time < creep.first_spawn_time:
            return 0.0

        minutes   = game_time / 60.0
        predicted = creep.base_gold + (minutes * creep.scaling_per_minute)

        # blend with calibrated formula if confident enough
        if creep.calibrated_formula and self.calibration.confidence_score > 0.5:
            try:
                cal = creep.calibrated_formula(game_time)
                w   = self.calibration.confidence_score
                predicted = cal * w + predicted * (1 - w)
            except Exception:
                pass  # keep linear prediction on any formula error

        # roam role gets halved lane-creep gold after 3 min
        if self.is_roam and creep_type in ('melee_creep', 'ranged_creep', 'siege_creep'):
            if game_time >= 180:
                predicted *= 0.5

        return round(predicted, 1)

    def predict_next_spawn(self, creep_type: str) -> float:
        if creep_type not in self.calibration.creep_economies:
            return float('inf')
        creep = self.calibration.creep_economies[creep_type]
        t = self.current_game_time
        if t < creep.first_spawn_time:
            return creep.first_spawn_time
        elapsed   = t - creep.first_spawn_time
        intervals = int(elapsed / creep.spawn_interval)
        return creep.first_spawn_time + (intervals + 1) * creep.spawn_interval

    # ── GPM ──────────────────────────────────────────────────────────────────
    def calculate_gpm(self) -> float:
        with self._lock:
            history_copy = list(self.gold_history)

        if len(history_copy) < 2:
            return 0.0

        # use only the last 60 s of data
        recent = [
            (gt, g) for gt, g in history_copy
            if (self.current_game_time - gt) <= 60.0
        ]
        if len(recent) < 2:
            return 0.0

        # FIX: td and gd are now correctly indented inside calculate_gpm
        td = recent[-1][0] - recent[0][0]
        gd = recent[-1][1] - recent[0][1]

        if td > 0:
            gpm = (gd / td) * 60.0
            with self._lock:
                self.gpm_history.append(gpm)
            return round(gpm, 1)
        return 0.0

    def predict_gold_at(self, target_time: float) -> float:
        if target_time <= self.current_game_time:
            return self.gold_current
        diff      = target_time - self.current_game_time
        gpm       = self.calculate_gpm()
        predicted = self.gold_current + (gpm / 60.0) * diff

        for ct, creep in self.calibration.creep_economies.items():
            ns = self.predict_next_spawn(ct)
            if ns <= target_time:
                spawns    = int((target_time - max(ns, self.current_game_time)) / creep.spawn_interval) + 1
                predicted += spawns * self.predict_creep_gold(ct, ns)

        return round(predicted, 1)

    # ── optimal target ────────────────────────────────────────────────────────
    def find_optimal_target(self, travel_time: float = 5.0) -> Dict:
        best     = None
        best_eff = 0.0
        for ct, creep in self.calibration.creep_economies.items():
            ns  = self.predict_next_spawn(ct)
            ttf = max(0.0, ns - self.current_game_time) + travel_time
            ft  = self.current_game_time + ttf
            gv  = self.predict_creep_gold(ct, ft)
            eff = gv / ttf if ttf > 0 else 0.0
            if eff > best_eff:
                best_eff = eff
                best = {
                    'type':       ct,
                    'next_spawn': ns,
                    'gold':       gv,
                    'efficiency': round(eff, 2),
                    'priority':   round(eff * 100, 1),
                }
        return best or {'type': 'none', 'efficiency': 0, 'gold': 0}

    # ── item progress ─────────────────────────────────────────────────────────
    def item_progress(self) -> Dict:
        if not self.config.target_item or self.config.target_item_cost <= 0:
            return {
                'current': self.gold_current, 'target': 0,
                'missing': 0, 'pct': 0, 'eta': 0, 'gpm': 0,
            }
        missing = max(0.0, self.config.target_item_cost - self.gold_current)
        pct     = (self.gold_current / self.config.target_item_cost) * 100
        gpm     = self.calculate_gpm()
        eta     = (missing / gpm * 60.0) if gpm > 0 else -1
        return {
            'current': round(self.gold_current, 0),
            'target':  self.config.target_item_cost,
            'missing': round(missing, 0),
            'pct':     round(pct, 1),
            'eta':     round(eta, 1) if eta > 0 else -1,
            'gpm':     gpm,
        }

    # ── minimap dots ──────────────────────────────────────────────────────────
    def get_minimap_dots(self) -> List[Dict]:
        dots   = []
        window = 30.0
        for ct, creep in self.calibration.creep_economies.items():
            ns = self.predict_next_spawn(ct)
            tu = ns - self.current_game_time
            if 0 <= tu <= window:
                gv = self.predict_creep_gold(ct, ns)
                dots.append({
                    'type':     ct,
                    'pos':      self._creep_pos(ct),
                    'spawn_in': round(tu, 1),
                    'gold':     gv,
                    'priority': 'high' if gv > 60 else 'med',
                    'color':    '#FFD700' if gv > 60 else '#FFA500',
                })
        dots.sort(key=lambda x: x['gold'], reverse=True)
        return dots[:5]

    def _creep_pos(self, ct: str) -> Tuple[float, float]:
        positions = {
            'melee_creep':  (0.15, 0.5),
            'ranged_creep': (0.15, 0.5),
            'siege_creep':  (0.15, 0.5),
            'jungle_small': (0.3,  0.3),
            'jungle_large': (0.3,  0.7),
            'red_buff':     (0.8,  0.8),
            'blue_buff':    (0.2,  0.2),
            'turtle':       (0.5,  0.5),
            'lord':         (0.5,  0.5),
        }
        return positions.get(ct, (0.5, 0.5))

    # ── recording ─────────────────────────────────────────────────────────────
    def record_gold(self, observed: float):
        self.gold_current = observed
        with self._lock:
            self.gold_history.append((self.current_game_time, observed))
        self.calculate_gpm()

    def record_kill(self, victim_streak: int = 0):
        self.kill_streak += 1
        base        = 200.0
        multipliers = {0: 1.0, 1: 1.0, 2: 1.15, 3: 1.3, 4: 1.5}
        tier        = min(victim_streak, 4)

        # FIX: mult and time_scale now correctly indented inside record_kill
        mult       = multipliers.get(tier, 1.5)
        time_scale = 1.0 + (self.current_game_time / 600.0) * 0.5
        bonus      = base * mult * time_scale

        self.gold_current += bonus
        self.record_gold(self.gold_current)

    def record_death(self):
        self.kill_streak = 0
        self.is_alive    = False

    def record_respawn(self):
        self.is_alive = True

    # ── main tick ─────────────────────────────────────────────────────────────
    def tick(self) -> Dict:
        self.update_time()
        return {
            'game_time':  round(self.current_game_time, 1),
            'phase':      self.get_phase(),
            'gold':       round(self.gold_current, 0),
            'gpm':        self.calculate_gpm(),
            'optimal':    self.find_optimal_target(),
            'item':       self.item_progress(),
            'dots':       self.get_minimap_dots(),
            'streak':     self.kill_streak,
            'alive':      self.is_alive,
            'confidence': round(self.calibration.confidence_score, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigManager:
    def __init__(self):
        self.app_dir = self._get_app_dir()
        os.makedirs(self.app_dir, exist_ok=True)
        os.makedirs(os.path.join(self.app_dir, 'profiles'), exist_ok=True)
        self.config_path    = os.path.join(self.app_dir, 'config.json')
        self.overlay_config = OverlayConfig()
        self.active_profile: Optional[CalibrationProfile] = None
        self._load()

    def _get_app_dir(self) -> str:
        if platform == 'android':
            from android.storage import app_storage_path
            return app_storage_path()
        return os.path.expanduser('~/.meoe')

    def _load(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
                self.overlay_config.target_item      = data.get('target_item')
                self.overlay_config.target_item_cost = data.get('target_item_cost', 0.0)
                self.overlay_config.show_gold_calc   = data.get('show_gold_calc', True)
                self.overlay_config.show_optimal     = data.get('show_optimal', True)
                self.overlay_config.show_minimap     = data.get('show_minimap', True)
                pid = data.get('active_profile', 'default')
                self.load_profile(pid)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # FIX: specific exceptions instead of bare except
                print(f'[ConfigManager] config load error: {e}')
                self.active_profile = CalibrationProfile('default')
        else:
            self.active_profile = CalibrationProfile('default')

    def save(self):
        data = {
            'target_item':      self.overlay_config.target_item,
            'target_item_cost': self.overlay_config.target_item_cost,
            'show_gold_calc':   self.overlay_config.show_gold_calc,
            'show_optimal':     self.overlay_config.show_optimal,
            'show_minimap':     self.overlay_config.show_minimap,
            'active_profile':   self.active_profile.profile_id if self.active_profile else 'default',
        }
        with open(self.config_path, 'w') as f:
            json.dump(data, f, indent=2)

    def load_profile(self, pid: str):
        path = os.path.join(self.app_dir, 'profiles', f'{pid}.json')
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    self.active_profile = CalibrationProfile.from_json(f.read())
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f'[ConfigManager] profile load error: {e}')
                self.active_profile = CalibrationProfile(pid)
        else:
            self.active_profile = CalibrationProfile(pid)

    def save_profile(self, profile: CalibrationProfile):
        path = os.path.join(self.app_dir, 'profiles', f'{profile.profile_id}.json')
        with open(path, 'w') as f:
            f.write(profile.to_json())
        self.save()

    def set_target_item(self, name: str, cost: float):
        self.overlay_config.target_item      = name
        self.overlay_config.target_item_cost = cost
        self.save()


# ═══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════════════════

class Diagnostics:
    def __init__(self, app_dir: str):
        self.log_dir    = os.path.join(app_dir, 'logs')
        os.makedirs(self.log_dir, exist_ok=True)
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_file   = os.path.join(self.log_dir, f'{self.session_id}.jsonl')
        self.events:    List[Dict] = []
        self.gold_obs:  List[Dict] = []
        self.start_time = time.time()

    def log(self, event_type: str, data: Dict):
        entry = {
            't':    round(time.time() - self.start_time, 2),
            'type': event_type,
            'data': data,
        }
        self.events.append(entry)
        with open(self.log_file, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def log_gold(self, game_time: float, observed: float, predicted: Optional[float] = None):
        entry = {'gt': game_time, 'obs': observed, 'pred': predicted}
        self.gold_obs.append(entry)
        self.log('GOLD', entry)

    def log_cal(self, creep_type: str, old: float, new: float):
        self.log('CALIBRATE', {'creep': creep_type, 'old': old, 'new': new})

    def summary(self) -> Dict:
        if len(self.gold_obs) < 2:
            return {'duration': round(time.time() - self.start_time, 1), 'obs': 0}
        accs = [
            1.0 - abs(g['pred'] - g['obs']) / max(g['obs'], 1)
            for g in self.gold_obs if g['pred'] is not None
        ]
        return {
            'duration': round(time.time() - self.start_time, 1),
            'obs':      len(self.gold_obs),
            'accuracy': round(sum(accs) / len(accs), 3) if accs else 0,
            'events':   len(self.events),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# OVERLAY UI WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class OverlayWidget(FloatLayout):
    gold_text       = StringProperty('ЗОЛОТО: 0')
    target_text     = StringProperty('ФАРМ: ---')
    timer_text      = StringProperty('0:00')
    confidence_text = StringProperty('Калибровка: 0%')
    calibration_visible = BooleanProperty(False)

    def __init__(
        self,
        predictor:   EconomyPredictor,
        config:      OverlayConfig,
        diagnostics: Diagnostics,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.predictor  = predictor
        self.config     = config
        self.diag       = diagnostics
        self.dots: List = []
        self._setup_ui()
        Clock.schedule_interval(self.update, 0.5)

    def _setup_ui(self):
        # ── gold calculator (top-left) ────────────────────────────────────────
        with self.canvas:
            Color(0, 0, 0, 0.7)
            self.gold_bg = Rectangle(
                pos=(20, Window.height - 220),
                size=(380, 200),
            )

        self.gold_label = Label(
            text=self.gold_text,
            pos=(20, Window.height - 220),
            size=(380, 200),
            color=(1, 1, 1, 1),
            font_size=16,
            halign='left',
            valign='top',
            markup=True,
            text_size=(360, 180),
        )
        self.add_widget(self.gold_label)

        # ── optimal target (top-right) ────────────────────────────────────────
        with self.canvas:
            Color(0, 0, 0, 0.7)
            self.target_bg = Rectangle(
                pos=(Window.width - 400, Window.height - 180),
                size=(380, 160),
            )

        self.target_label = Label(
            text=self.target_text,
            pos=(Window.width - 400, Window.height - 180),
            size=(380, 160),
            color=(0.2, 1, 0.4, 1),
            font_size=16,
            halign='left',
            valign='top',
            markup=True,
            text_size=(360, 140),
        )
        self.add_widget(self.target_label)

        # ── timer (bottom-centre) ─────────────────────────────────────────────
        with self.canvas:
            Color(0, 0, 0, 0.6)
            self.timer_bg = Rectangle(
                pos=(Window.width // 2 - 60, 10),
                size=(120, 40),
            )

        self.timer_label = Label(
            text=self.timer_text,
            pos=(Window.width // 2 - 60, 10),
            size=(120, 40),
            color=(1, 0.85, 0.2, 1),
            font_size=20,
            halign='center',
            valign='middle',
        )
        self.add_widget(self.timer_label)

    # ── update loop ───────────────────────────────────────────────────────────
    def update(self, dt):
        state = self.predictor.tick()

        # timer
        secs = int(state['game_time'])
        self.timer_label.text = f"{secs // 60}:{secs % 60:02d}"

        # gold panel
        item  = state['item']
        lines = [
            f"[b]ЗОЛОТО:[/b] {int(state['gold'])}  GPM: {state['gpm']}",
            f"Фаза: {state['phase']}   Серия: {state['streak']}",
        ]
        if item['target'] > 0:
            eta_str = f"{item['eta']}с" if item['eta'] > 0 else '—'
            lines += [
                f"Цель: {self.config.target_item}  ({int(item['pct'])}%)",
                f"Осталось: {int(item['missing'])} зл  ETA: {eta_str}",
            ]
        lines.append(f"Точность: {int(state['confidence'] * 100)}%")
        self.gold_label.text = '\n'.join(lines)

        # optimal target panel
        opt = state['optimal']
        if opt['type'] != 'none':
            spawn_in = round(max(0.0, opt['next_spawn'] - state['game_time']), 1)
            self.target_label.text = (
                f"[b]ЛУЧШИЙ ФАРМ[/b]\n"
                f"{opt['type'].replace('_', ' ').upper()}\n"
                f"Золото: {opt['gold']}  через {spawn_in}с\n"
                f"Эффект: {opt['efficiency']}"
            )
        else:
            self.target_label.text = 'ФАРМ: нет данных'

        # minimap dots
        self._draw_minimap_dots(state['dots'])

    # ── minimap dots ──────────────────────────────────────────────────────────
    def _draw_minimap_dots(self, dots: List[Dict]):
        """Draw spawn-prediction dots over the minimap area."""
        self.canvas.after.clear()
        if not self.config.show_minimap:
            return

        mx, my, mw, mh = self.config.minimap_rect
        # convert to Kivy coordinates (origin bottom-left)
        ky_base = Window.height - my - mh

        with self.canvas.after:
            for dot in dots:
                fx, fy = dot['pos']
                cx = mx + fx * mw
                cy = ky_base + fy * mh
                r, g, b = (1.0, 0.84, 0.0) if dot['priority'] == 'high' else (1.0, 0.65, 0.0)
                Color(r, g, b, 0.9)
                Ellipse(pos=(cx - 8, cy - 8), size=(16, 16))
                Color(1, 1, 1, 0.8)
                # pulse ring
                Line(circle=(cx, cy, 12), width=1.2)


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS POPUP
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsPopup(Popup):
    def __init__(self, config_manager: ConfigManager, **kwargs):
        super().__init__(**kwargs)
        self.cfg     = config_manager
        self.title   = 'Настройки MEOE'
        self.size_hint = (0.8, 0.7)
        self._build()

    def _build(self):
        layout = BoxLayout(orientation='vertical', padding=12, spacing=8)

        # item name
        layout.add_widget(Label(text='Предмет цели:', size_hint_y=None, height=30))
        self.item_input = TextInput(
            text=self.cfg.overlay_config.target_item or '',
            multiline=False,
            size_hint_y=None,
            height=36,
        )
        layout.add_widget(self.item_input)

        # item cost
        layout.add_widget(Label(text='Стоимость:', size_hint_y=None, height=30))
        self.cost_input = TextInput(
            text=str(int(self.cfg.overlay_config.target_item_cost)),
            multiline=False,
            input_filter='int',
            size_hint_y=None,
            height=36,
        )
        layout.add_widget(self.cost_input)

        # save button
        btn = Button(text='Сохранить', size_hint_y=None, height=44)
        btn.bind(on_release=self._save)
        layout.add_widget(btn)

        self.content = layout

    def _save(self, *_):
        try:
            cost = float(self.cost_input.text or '0')
        except ValueError:
            cost = 0.0
        self.cfg.set_target_item(self.item_input.text.strip(), cost)
        self.dismiss()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ═══════════════════════════════════════════════════════════════════════════════

class MEOEApp(App):
    def build(self):
        # request Android permissions first
        if platform == 'android':
            request_permissions([
                Permission.SYSTEM_ALERT_WINDOW,
                Permission.FOREGROUND_SERVICE,
                Permission.WRITE_EXTERNAL_STORAGE,
                Permission.READ_EXTERNAL_STORAGE,
                Permission.INTERNET,
            ])

        # bootstrap
        self.cfg_manager = ConfigManager()
        self.diag        = Diagnostics(self.cfg_manager.app_dir)
        self.predictor   = EconomyPredictor(
            self.cfg_manager.active_profile,
            self.cfg_manager.overlay_config,
        )

        # keep screen on (Android)
        if platform == 'android':
            self._keep_screen_on()

        Window.clearcolor = (0, 0, 0, 0)   # transparent background

        root = FloatLayout()

        self.overlay = OverlayWidget(
            predictor=self.predictor,
            config=self.cfg_manager.overlay_config,
            diagnostics=self.diag,
        )
        root.add_widget(self.overlay)

        # ── control buttons (bottom-right) ────────────────────────────────────
        btn_bar = BoxLayout(
            orientation='horizontal',
            size_hint=(None, None),
            size=(260, 44),
            pos=(Window.width - 270, 10),
            spacing=8,
        )

        btn_start = Button(text='СТАРТ')
        btn_start.bind(on_release=lambda *_: self._start_game())
        btn_bar.add_widget(btn_start)

        btn_cfg = Button(text='НАСТР.')
        btn_cfg.bind(on_release=lambda *_: SettingsPopup(self.cfg_manager).open())
        btn_bar.add_widget(btn_cfg)

        btn_kill = Button(text='КИЛЛ')
        btn_kill.bind(on_release=lambda *_: self.predictor.record_kill())
        btn_bar.add_widget(btn_kill)

        root.add_widget(btn_bar)
        return root

    def _start_game(self):
        self.predictor.start_game('default')
        self.diag.log('GAME_START', {'ts': time.time()})

    @staticmethod
    def _keep_screen_on():
        try:
            from jnius import autoclass
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            activity.getWindow().addFlags(
                autoclass('android.view.WindowManager$LayoutParams').FLAG_KEEP_SCREEN_ON
            )
        except Exception as e:
            print(f'[MEOEApp] keep_screen_on failed: {e}')

    def on_stop(self):
        summary = self.diag.summary()
        self.diag.log('SESSION_END', summary)
        if self.cfg_manager.active_profile:
            self.cfg_manager.save_profile(self.cfg_manager.active_profile)
        print(f'[MEOEApp] session ended: {summary}')


if __name__ == '__main__':
    MEOEApp().run()
