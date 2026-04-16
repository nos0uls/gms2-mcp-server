"""
gms2_parser.py — Модуль парсинга проектов GameMaker Studio 2.

Предоставляет CachedParser — синглтон, который читает структуру GMS2 проекта
и кеширует результаты, чтобы не сканировать файловую систему при каждом запросе.

Используется из server.py через вызов CachedParser.get(project_path).
"""

import os
import re
import json
import time
import shutil
import logging
import threading
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

# Логгер для этого модуля (вывод в stderr, чтобы не мешать stdio-транспорту MCP)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GMS2 Таблица событий: eventType × eventNum → человекочитаемое название
# Числовые коды берутся из официальных исходников GMS2 runtime.
# ---------------------------------------------------------------------------

# Коды клавиш VK → название (основные)
_VK_NAMES: Dict[int, str] = {
    8: "Backspace", 9: "Tab", 13: "Enter", 16: "Shift", 17: "Control",
    18: "Alt", 19: "Pause", 27: "Escape", 32: "Space",
    37: "Left Arrow", 38: "Up Arrow", 39: "Right Arrow", 40: "Down Arrow",
    46: "Delete", 48: "0", 49: "1", 50: "2", 51: "3", 52: "4",
    53: "5", 54: "6", 55: "7", 56: "8", 57: "9",
    65: "A", 66: "B", 67: "C", 68: "D", 69: "E", 70: "F", 71: "G",
    72: "H", 73: "I", 74: "J", 75: "K", 76: "L", 77: "M", 78: "N",
    79: "O", 80: "P", 81: "Q", 82: "R", 83: "S", 84: "T", 85: "U",
    86: "V", 87: "W", 88: "X", 89: "Y", 90: "Z",
    112: "F1", 113: "F2", 114: "F3", 115: "F4", 116: "F5", 117: "F6",
    118: "F7", 119: "F8", 120: "F9", 121: "F10", 122: "F11", 123: "F12",
    186: "Semicolon", 187: "Equals", 188: "Comma", 189: "Minus",
    190: "Period", 191: "Slash", 192: "Backtick",
}

# Основная таблица событий GMS2
# Формат: GMS2_EVENTS[eventType][eventNum] = "Название события"
GMS2_EVENTS: Dict[int, Dict[int, str]] = {
    0: {0: "Create"},
    1: {0: "Destroy"},
    # Alarm 0-11
    2: {i: f"Alarm {i}" for i in range(12)},
    # Step-события
    3: {0: "Step", 1: "Begin Step", 2: "End Step"},
    # Collision — eventNum хранит ID объекта, обрабатывается отдельно
    4: {},
    # Keyboard (Hold) — eventNum это VK-код, обрабатывается через _VK_NAMES
    5: {},
    # Mouse-события
    6: {
        0: "Left Button Pressed",   1: "Right Button Pressed",   2: "Middle Button Pressed",
        3: "Left Button Released",  4: "Right Button Released",  5: "Middle Button Released",
        6: "Left Button Down",      7: "Right Button Down",      8: "Middle Button Down",
        9: "No Button",            10: "Left Button",           11: "Right Button",
        12: "Middle Button",
        50: "Mouse Enter",         51: "Mouse Leave",
        60: "Wheel Up",            61: "Wheel Down",
    },
    # Other-события
    7: {
        0: "Outside Room",         1: "Intersect Boundary",
        2: "Game Start",           3: "Game End",
        4: "Room Start",           5: "Room End",
        6: "Animation End",        7: "Path End",
        8:  "User Event 0",        9:  "User Event 1",
        10: "User Event 2",        11: "User Event 3",
        12: "User Event 4",        13: "User Event 5",
        14: "User Event 6",        15: "User Event 7",
        16: "User Event 8",        17: "User Event 9",
        18: "User Event 10",       19: "User Event 11",
        20: "User Event 12",       21: "User Event 13",
        22: "User Event 14",       23: "User Event 15",
        25: "Animation Update",    26: "Animation Event",
        30: "Async – Image Loaded", 31: "Async – Sound Playback Ended",
    },
    # Draw-события
    8: {
        0: "Draw",           1: "Draw GUI",        2: "Window Resize",
        3: "Draw Begin",     4: "Draw End",
        5: "Draw GUI Begin", 6: "Draw GUI End",
        7: "Pre Draw",       8: "Post Draw",
    },
    # Key Press / Key Release — те же VK, обрабатываются через _VK_NAMES
    9:  {},
    10: {},
    # Cleanup
    12: {0: "Cleanup"},
    # Gesture-события
    13: {
        0: "Tap",         1: "Double Tap",   2: "Drag Start",
        3: "Drag Move",   4: "Drag End",     5: "Flick",
        6: "Pinch Start", 7: "Pinch In",     8: "Pinch Out",
        9: "Pinch End",   10: "Rotate Start", 11: "Rotating",
        12: "Rotate End",
    },
    # Async-события
    14: {
        0: "Audio Playback",      1: "Audio Recording",   2: "Social",
        3: "Push Notification",   4: "In-App Purchase",   5: "Cloud",
        6: "Networking",          7: "Steam",              8: "Dialog",
        9: "HTTP Request",        11: "System Event",
        62: "Audio Playback Ended", 63: "Audio Error",
    },
}

# Папки ассетов GMS2: отображаемое имя → папка на диске
ASSET_CATEGORIES: Dict[str, str] = {
    "Objects":    "objects",
    "Scripts":    "scripts",
    "Rooms":      "rooms",
    "Sprites":    "sprites",
    "Sounds":     "sounds",
    "Fonts":      "fonts",
    "Shaders":    "shaders",
    "Tile Sets":  "tilesets",
    "Timelines":  "timelines",
    "Notes":      "notes",
    "Extensions": "extensions",
    "Sequences":  "sequences",
}

# ---------------------------------------------------------------------------
# CachedParser — основной класс
# ---------------------------------------------------------------------------

class CachedParser:
    """
    Синглтон-парсер для GMS2 проекта.

    Один экземпляр на каждый уникальный путь к проекту.
    Результаты дорогостоящих операций кешируются на TTL секунд.
    Кеш сбрасывается автоматически, если изменился .yyp файл проекта.
    """

    # Словарь всех экземпляров: абсолютный путь → CachedParser
    _instances: Dict[str, "CachedParser"] = {}

    # Глобальная блокировка для создания экземпляров (защита от гонки при __init__)
    _instances_lock: threading.Lock = threading.Lock()

    # Время жизни кеша в секундах
    TTL: int = 60

    @classmethod
    def get(cls, project_path: str) -> "CachedParser":
        """
        Возвращает (или создаёт) экземпляр парсера для данного пути.
        Это точка входа для server.py — всегда используй её.
        Защищена блокировкой от гонки при параллельных вызовах.
        """
        path_key = str(Path(project_path).resolve())
        # Быстрая проверка без блокировки
        if path_key in cls._instances:
            return cls._instances[path_key]
        # Медленный путь — с блокировкой (создаём только один раз)
        with cls._instances_lock:
            if path_key not in cls._instances:
                cls._instances[path_key] = cls(project_path)
        return cls._instances[path_key]

    def __init__(self, project_path: str):
        """
        Инициализация парсера.
        project_path — путь к папке проекта GMS2 (где лежит .yyp файл).
        """
        self.project_path = Path(project_path).resolve()
        # Хранилище кеша: ключ → (значение, время записи)
        self._cache: Dict[str, Tuple[Any, float]] = {}
        # mtime .yyp файла при последнем кеше — для инвалидации
        self._yyp_mtime: Optional[float] = None
        # Блокировка для потокобезопасного доступа к кешу.
        # Нужна потому что asyncio.to_thread() запускает несколько потоков параллельно,
        # и без Lock возможна гонка при одновременной записи/чтении _cache.
        self._lock: threading.Lock = threading.Lock()
        
        # Блокировка для тяжелых вычислений (чтобы предотвратить "cache stampede").
        # Если 5 потоков одновременно запросят поиск, только 1 пойдёт сканировать ФС, 
        # остальные подождут и возьмут из кеша.
        self._compute_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Внутренние утилиты: кеш
    # ------------------------------------------------------------------

    def _find_yyp(self) -> Optional[Path]:
        """Найти .yyp файл в корне проекта."""
        try:
            for f in self.project_path.iterdir():
                if f.suffix == ".yyp":
                    return f
        except OSError:
            pass
        return None

    def _cache_is_fresh(self, key: str) -> bool:
        """
        Проверяет свежесть кеша.
        Кеш устаревает если: прошло больше TTL секунд ИЛИ изменился .yyp файл.
        Вызывается только внутри захваченного self._lock.
        """
        if key not in self._cache:
            return False
        _, ts = self._cache[key]
        # Проверка по времени
        if time.time() - ts > self.TTL:
            return False
        # Проверка по изменению .yyp — если файл изменился, сбрасываем весь кеш
        yyp = self._find_yyp()
        if yyp and yyp.exists():
            current_mtime = yyp.stat().st_mtime
            if current_mtime != self._yyp_mtime:
                self._cache.clear()
                self._yyp_mtime = current_mtime
                return False
        return True

    def _cache_get(self, key: str) -> Optional[Any]:
        """Получить значение из кеша. Возвращает None, если нет или устарел."""
        with self._lock:
            if self._cache_is_fresh(key):
                return self._cache[key][0]
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        """Сохранить значение в кеше с текущей меткой времени."""
        with self._lock:
            self._cache[key] = (value, time.time())

    # ------------------------------------------------------------------
    # Внутренние утилиты: GMS2
    # ------------------------------------------------------------------

    def _check_project(self) -> Optional[str]:
        """
        Проверяет что project_path — реальный GMS2 проект.
        Возвращает строку с ошибкой или None если всё в порядке.
        """
        if not self.project_path.exists():
            return f"Project path not found: {self.project_path}"
        if not self._find_yyp():
            return f"No .yyp file found in: {self.project_path}"
        return None

    def _read_yy(self, path: Path) -> Optional[Dict]:
        """
        Читает и парсит .yy или .yyp файл (JSON, возможно с trailing commas).
        Возвращает dict или None при любой ошибке.
        """
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            # GMS2 .yy файлы могут содержать лишние запятые перед } или ]
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            return json.loads(cleaned)
        except Exception as e:
            logger.warning("Failed to read YY file %s: %s", path, e)
            return None

    def _asset_dir(self, cat_folder: str, name: str) -> Path:
        """Путь к папке конкретного ассета."""
        return self.project_path / cat_folder / name

    def _yy_file(self, cat_folder: str, name: str) -> Path:
        """Путь к .yy файлу конкретного ассета."""
        return self._asset_dir(cat_folder, name) / f"{name}.yy"

    def _all_gml_files(self) -> List[Path]:
        """
        Собирает все .gml файлы проекта.
        Использует кеш, чтобы не делать os.walk при каждом запросе.
        """
        cached = self._cache_get("all_gml_files")
        if cached is not None:
            return cached

        with self._compute_lock:
            # Double-checked locking
            cached = self._cache_get("all_gml_files")
            if cached is not None:
                return cached

            skip_dirs = {"options", "configs", "datafiles", ".git", ".vscode", "temp"}
            result: List[Path] = []
            for root, dirs, files in os.walk(self.project_path):
                root_p = Path(root)
                try:
                    rel = root_p.relative_to(self.project_path)
                except ValueError:
                    continue
                # Пропускаем технические корневые папки
                if rel.parts and rel.parts[0].lower() in skip_dirs:
                    dirs[:] = []
                    continue
                for f in sorted(files):
                    if f.endswith(".gml"):
                        result.append(root_p / f)
            
            self._cache_set("all_gml_files", result)
            return result

    def _scan_categories_raw(self) -> Dict[str, List[Dict]]:
        """
        Сканирует все категории ассетов без кеша.
        Возвращает: {category_name: [{name, yy, gml_count, gml_files, ...}]}
        """
        result: Dict[str, List[Dict]] = {}
        for display_name, folder in ASSET_CATEGORIES.items():
            cat_path = self.project_path / folder
            assets: List[Dict] = []
            if cat_path.is_dir():
                try:
                    for entry in sorted(cat_path.iterdir()):
                        if not entry.is_dir():
                            continue
                        yy_exists = (entry / f"{entry.name}.yy").exists()
                        gml_files = sorted(f.name for f in entry.iterdir() if f.suffix == ".gml")
                        fsh_files = sorted(f.name for f in entry.iterdir() if f.suffix == ".fsh")
                        vsh_files = sorted(f.name for f in entry.iterdir() if f.suffix == ".vsh")
                        assets.append({
                            "name":      entry.name,
                            "yy":        yy_exists,
                            "gml_count": len(gml_files),
                            "gml_files": gml_files,
                            "fsh_files": fsh_files,
                            "vsh_files": vsh_files,
                        })
                except OSError as e:
                    logger.warning("Cannot scan %s: %s", folder, e)
            result[display_name] = assets
        return result

    def _get_categories(self) -> Dict[str, List[Dict]]:
        """Возвращает структуру категорий (из кеша или сканирует заново)."""
        cached = self._cache_get("categories")
        if cached is not None:
            return cached
            
        with self._compute_lock:
            # Double-checked locking
            cached = self._cache_get("categories")
            if cached is not None:
                return cached
                
            data = self._scan_categories_raw()
            self._cache_set("categories", data)
            return data

    def _resolve_event(
        self,
        event_type: int,
        event_num: int,
        event_data: Optional[Dict] = None
    ) -> str:
        """
        Переводит числовые коды события GMS2 в человекочитаемую строку.
        event_data — словарь конкретного события из eventList (для Collision).
        """
        # Collision: имя объекта хранится в самом событии
        if event_type == 4:
            col_obj = (event_data or {}).get("collisionObjectId") or {}
            col_name = col_obj.get("name", f"id:{event_num}")
            return f"Collision with {col_name}"

        # Keyboard / Key Press / Key Release — VK-коды
        if event_type in (5, 9, 10):
            if event_num == 0:
                return {5: "Any Key (Hold)", 9: "Any Key (Press)", 10: "Any Key (Release)"}[event_type]
            key_name = _VK_NAMES.get(event_num, f"Key {event_num}")
            suffix = {5: " (Hold)", 9: " (Press)", 10: " (Release)"}[event_type]
            return key_name + suffix

        # Стандартный lookup по таблице GMS2_EVENTS
        inner = GMS2_EVENTS.get(event_type, {})
        if event_num in inner:
            return inner[event_num]

        # Fallback: тип по имени, номер как есть
        type_names = {
            0: "Create", 1: "Destroy", 2: "Alarm", 3: "Step",
            4: "Collision", 5: "Keyboard", 6: "Mouse", 7: "Other",
            8: "Draw", 9: "Key Press", 10: "Key Release",
            12: "Cleanup", 13: "Gesture", 14: "Async",
        }
        t_name = type_names.get(event_type, f"EventType{event_type}")
        return f"{t_name} [{event_num}]"

    # ------------------------------------------------------------------
    # TOOL 1: get_project_summary
    # ------------------------------------------------------------------

    def get_project_summary(self) -> Dict:
        """
        Ультра-компактная сводка по проекту — минимум токенов, максимум полезности.
        Показывает имя проекта и количество ассетов по каждой категории.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        cats = self._get_categories()
        counts = {cat: len(assets) for cat, assets in cats.items() if assets}
        return {
            "project_name":  self.project_path.name,
            "project_path":  str(self.project_path),
            "asset_counts":  counts,
            "total_assets":  sum(counts.values()),
        }

    # ------------------------------------------------------------------
    # TOOL 2: scan_project
    # ------------------------------------------------------------------

    def scan_project(self, category: Optional[str] = None) -> Dict:
        """
        Полный список ассетов проекта по категориям.
        Если category задан — только эта категория.
        Возвращает имена, флаг .yy и количество GML файлов.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        cats = self._get_categories()

        if category:
            # Поиск без учёта регистра
            match = next((k for k in cats if k.lower() == category.lower()), None)
            if not match:
                return {"error": f"Unknown category: {category}. Available: {list(cats.keys())}"}
            cats = {match: cats[match]}

        result: Dict[str, Any] = {
            "project_name": self.project_path.name,
            "categories": {}
        }
        for cat_name, assets in cats.items():
            if assets:
                result["categories"][cat_name] = [
                    {"name": a["name"], "gml_files": a["gml_count"], "has_yy": a["yy"]}
                    for a in assets
                ]
        return result

    # ------------------------------------------------------------------
    # TOOL 3: list_assets
    # ------------------------------------------------------------------

    def list_assets(
        self,
        category: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Dict:
        """
        Плоский список ассетов с пагинацией.
        Полезно для больших проектов, где всё сразу не влезает в контекст.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        cats = self._get_categories()

        # Собираем плоский список всех ассетов
        flat: List[Dict] = []
        if category:
            match = next((k for k in cats if k.lower() == category.lower()), None)
            if not match:
                return {"error": f"Unknown category: {category}"}
            source = {match: cats[match]}
        else:
            source = cats

        for cat_name, assets in source.items():
            for a in assets:
                flat.append({"category": cat_name, "name": a["name"], "gml_count": a["gml_count"]})

        total = len(flat)
        page = flat[offset: offset + limit]
        return {
            "total":    total,
            "offset":   offset,
            "limit":    limit,
            "has_more": (offset + limit) < total,
            "assets":   page,
        }

    # ------------------------------------------------------------------
    # TOOL 4: get_gml_content
    # ------------------------------------------------------------------

    def get_gml_content(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict:
        """
        Читает содержимое GML файла.
        file_path — относительный (от корня проекта) или абсолютный путь.
        start_line / end_line (1-indexed) — читать только нужный диапазон строк.
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_path / file_path
        if not p.exists():
            return {"error": f"File not found: {p}"}
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return {"error": f"Cannot read file: {e}"}

        total = len(lines)

        if start_line is not None or end_line is not None:
            s = max(0, (start_line or 1) - 1)
            e = min(total, end_line or total)
            return {
                "file":        str(p.relative_to(self.project_path)),
                "total_lines": total,
                "range":       f"{s + 1}-{e}",
                "content":     "\n".join(lines[s:e]),
            }

        return {
            "file":        str(p.relative_to(self.project_path)),
            "total_lines": total,
            "content":     "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # TOOL 5: get_object_info
    # ------------------------------------------------------------------

    def get_object_info(self, name: str) -> Dict:
        """
        Читает .yy файл объекта и возвращает его свойства:
        спрайт, родитель, события, переменные, физику.
        """
        yy_p = self._yy_file("objects", name)
        if not yy_p.exists():
            return {"error": f"Object '{name}' not found"}
        data = self._read_yy(yy_p)
        if data is None:
            return {"error": f"Failed to parse .yy for '{name}'"}

        sprite = (data.get("spriteId") or {}).get("name", "None")
        parent = (data.get("parentObjectId") or {}).get("name", "None")
        mask   = (data.get("spriteMaskId") or {}).get("name", "Same as Sprite")

        # Декодируем события (числовые → строки)
        events: List[str] = []
        for ev in data.get("eventList", []):
            et = ev.get("eventtype", ev.get("eventType", -1))
            en = ev.get("enumb", ev.get("eventNum", 0))
            events.append(self._resolve_event(et, en, ev))

        return {
            "name":             name,
            "sprite":           sprite,
            "parent":           parent,
            "mask":             mask,
            "visible":          data.get("visible", True),
            "solid":            data.get("solid", False),
            "persistent":       data.get("persistent", False),
            "physics_enabled":  data.get("physicsObject", False),
            "events":           events,
            "event_count":      len(events),
            "variables": [
                {
                    "name":  p.get("name", p.get("varName", "?")),
                    "value": p.get("value", p.get("varValue", "?")),
                }
                for p in data.get("properties", [])
            ],
        }

    # ------------------------------------------------------------------
    # TOOL 6: get_room_info
    # ------------------------------------------------------------------

    def get_room_info(self, name: str) -> Dict:
        """
        Читает .yy файл комнаты и возвращает её параметры: размеры, слои, скорость.
        """
        yy_p = self._yy_file("rooms", name)
        if not yy_p.exists():
            return {"error": f"Room '{name}' not found"}
        data = self._read_yy(yy_p)
        if data is None:
            return {"error": f"Failed to parse .yy for room '{name}'"}

        settings = data.get("roomSettings", {})
        layers = []
        for layer in data.get("layers", []):
            ltype = layer.get("__type", layer.get("modelName", "Unknown"))
            inst_cnt = len(layer.get("instances", []))
            layers.append({
                "name":      layer.get("name", "?"),
                "type":      ltype.replace("GM", ""),
                "instances": inst_cnt if inst_cnt else None,
            })

        return {
            "name":        name,
            "width":       settings.get("Width", "?"),
            "height":      settings.get("Height", "?"),
            "speed":       settings.get("Speed", 30),
            "persistent":  data.get("isPersistent", False),
            "layer_count": len(layers),
            "layers":      layers,
        }

    # ------------------------------------------------------------------
    # TOOL 7: get_sprite_info
    # ------------------------------------------------------------------

    def get_sprite_info(self, name: str) -> Dict:
        """
        Читает .yy файл спрайта: размеры, origin, bbox, кол-во кадров, скорость воспроизведения.
        """
        yy_p = self._yy_file("sprites", name)
        if not yy_p.exists():
            return {"error": f"Sprite '{name}' not found"}
        data = self._read_yy(yy_p)
        if data is None:
            return {"error": f"Failed to parse .yy for sprite '{name}'"}

        return {
            "name":                name,
            "width":               data.get("width", "?"),
            "height":              data.get("height", "?"),
            "origin_x":            data.get("xorig", 0),
            "origin_y":            data.get("yorig", 0),
            "frame_count":         len(data.get("frames", [])),
            "bbox_left":           data.get("bbox_left", 0),
            "bbox_right":          data.get("bbox_right", 0),
            "bbox_top":            data.get("bbox_top", 0),
            "bbox_bottom":         data.get("bbox_bottom", 0),
            "collision_kind":      data.get("collisionKind", 1),
            "playback_speed":      data.get("playbackSpeed", 1),
            "playback_speed_type": data.get("playbackSpeedType", 0),
        }

    # ------------------------------------------------------------------
    # TOOL 8: search_in_project
    # ------------------------------------------------------------------

    def search_in_project(
        self,
        query: str,
        case_sensitive: bool = False,
        category: Optional[str] = None,
        max_results: int = 50,
    ) -> Dict:
        """
        Поиск текста во всех GML файлах проекта.
        Поддерживает фильтр по категории и ограничение числа результатов.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(re.escape(query), flags)
        except re.error as e:
            return {"error": f"Invalid query: {e}"}

        # Определяем папку категории для фильтра
        cat_folder: Optional[str] = None
        if category:
            cat_folder = ASSET_CATEGORIES.get(
                next((k for k in ASSET_CATEGORIES if k.lower() == category.lower()), ""), ""
            )

        matches: List[Dict] = []
        files_searched = 0

        for gml_path in self._all_gml_files():
            rel = gml_path.relative_to(self.project_path)
            # Фильтруем по категории
            if cat_folder and (not rel.parts or rel.parts[0] != cat_folder):
                continue

            files_searched += 1
            try:
                lines = gml_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for i, line in enumerate(lines, 1):
                if pattern.search(line):
                    matches.append({
                        "file":    str(rel),
                        "line":    i,
                        "content": line.strip(),
                    })
                    if len(matches) >= max_results:
                        return {
                            "query":          query,
                            "files_searched": files_searched,
                            "match_count":    len(matches),
                            "truncated":      True,
                            "matches":        matches,
                        }

        return {
            "query":          query,
            "files_searched": files_searched,
            "match_count":    len(matches),
            "truncated":      False,
            "matches":        matches,
        }

    # ------------------------------------------------------------------
    # TOOL 9: write_gml_file
    # ------------------------------------------------------------------

    def write_gml_file(
        self,
        file_path: str,
        content: str,
        create_backup: bool = True,
    ) -> Dict:
        """
        Записывает контент в GML файл.
        Перед перезаписью создаёт .bak бэкап (если create_backup=True).
        file_path — относительный (от корня проекта) или абсолютный путь.
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_path / file_path

        if not p.suffix == ".gml":
            return {"error": "Only .gml files can be written"}

        # Безопасность: файл должен быть строго внутри папки проекта
        try:
            p.resolve().relative_to(self.project_path)
        except ValueError:
            return {"error": "File path is outside the project directory"}

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            backup_path = None
            if create_backup and p.exists():
                backup_path = p.with_suffix(".gml.bak")
                shutil.copy2(p, backup_path)

            p.write_text(content, encoding="utf-8")
            # Сбрасываем кеш — содержимое файла изменилось
            self._cache.clear()

            return {
                "success":       True,
                "file":          str(p.relative_to(self.project_path)),
                "lines_written": len(content.splitlines()),
                "backup":        str(backup_path.relative_to(self.project_path)) if backup_path else None,
            }
        except OSError as e:
            return {"error": f"Failed to write file: {e}"}

    # ------------------------------------------------------------------
    # TOOL 10: create_script
    # ------------------------------------------------------------------

    def create_script(self, name: str, content: str = "") -> Dict:
        """
        Создаёт новый Script-ассет GMS2:
        - папку scripts/<name>/
        - файл <name>.yy (минимальный валидный JSON)
        - файл <name>.gml с переданным контентом
        """
        scripts_dir = self.project_path / "scripts" / name
        if scripts_dir.exists():
            return {"error": f"Script '{name}' already exists"}

        try:
            scripts_dir.mkdir(parents=True)

            # Минимальный .yy для Script-ассета
            yy_data = {
                "resourceType":    "GMScript",
                "resourceVersion": "1.0",
                "name":            name,
                "isCompatibility": False,
                "isDnD":           False,
                "parent": {
                    "name": "Scripts",
                    "path": "folders/Scripts.yy",
                },
            }
            (scripts_dir / f"{name}.yy").write_text(
                json.dumps(yy_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (scripts_dir / f"{name}.gml").write_text(content, encoding="utf-8")

            # Инвалидируем кеш — структура проекта изменилась
            self._cache.clear()

            return {
                "success":     True,
                "name":        name,
                "path":        str(scripts_dir.relative_to(self.project_path)),
                "yy_created":  True,
                "gml_created": True,
                "note":        "Reload project in GameMaker Studio 2 to see the new script",
            }
        except OSError as e:
            return {"error": f"Failed to create script: {e}"}

    # ------------------------------------------------------------------
    # TOOL 11: export_project_data (пагинация)
    # ------------------------------------------------------------------

    def export_project_data(
        self,
        offset: int = 0,
        limit: int = 5,
        category: Optional[str] = None,
    ) -> Dict:
        """
        Экспортирует GML файлы с пагинацией — по limit файлов за запрос.
        Используй has_more + next_offset чтобы итерировать по проекту.
        Не запрашивай весь проект за раз — это тысячи токенов.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        gml_files = self._all_gml_files()

        # Фильтр по категории
        if category:
            cat_folder = ASSET_CATEGORIES.get(
                next((k for k in ASSET_CATEGORIES if k.lower() == category.lower()), ""), ""
            )
            if cat_folder:
                gml_files = [
                    f for f in gml_files
                    if f.relative_to(self.project_path).parts
                    and f.relative_to(self.project_path).parts[0] == cat_folder
                ]

        total = len(gml_files)
        page = gml_files[offset: offset + limit]

        chunks: List[Dict] = []
        for gml_path in page:
            rel = str(gml_path.relative_to(self.project_path))
            try:
                content = gml_path.read_text(encoding="utf-8", errors="replace")
                chunks.append({"file": rel, "content": content, "lines": len(content.splitlines())})
            except OSError as e:
                chunks.append({"file": rel, "error": str(e)})

        return {
            "total_files": total,
            "offset":      offset,
            "limit":       limit,
            "has_more":    (offset + limit) < total,
            "next_offset": offset + limit if (offset + limit) < total else None,
            "files":       chunks,
        }

    # ------------------------------------------------------------------
    # TOOL 12: find_asset_references
    # ------------------------------------------------------------------

    def find_asset_references(self, asset_name: str) -> Dict:
        """
        Находит все упоминания ассета в GML файлах и .yy файлах проекта.
        Полезно для анализа зависимостей: кто ссылается на спрайт X, объект Y и т.д.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        gml_refs: List[Dict] = []
        yy_refs: List[str] = []

        # Поиск в GML файлах
        for gml_path in self._all_gml_files():
            try:
                lines = gml_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines, 1):
                    if asset_name in line:
                        gml_refs.append({
                            "file":    str(gml_path.relative_to(self.project_path)),
                            "line":    i,
                            "context": line.strip(),
                        })
            except OSError:
                continue

        # Поиск в .yy файлах (как текст — быстрее чем парсинг JSON)
        for cat_folder in ASSET_CATEGORIES.values():
            cat_path = self.project_path / cat_folder
            if not cat_path.is_dir():
                continue
            for yy_file in cat_path.rglob("*.yy"):
                try:
                    if asset_name in yy_file.read_text(encoding="utf-8", errors="replace"):
                        yy_refs.append(str(yy_file.relative_to(self.project_path)))
                except OSError:
                    continue

        return {
            "asset_name":    asset_name,
            "gml_ref_count": len(gml_refs),
            "yy_ref_count":  len(yy_refs),
            "gml_references": gml_refs,
            "yy_references":  yy_refs,
        }

    # ------------------------------------------------------------------
    # TOOL 13: decode_object_events
    # ------------------------------------------------------------------

    def decode_object_events(self, object_name: str) -> Dict:
        """
        Декодирует числовые ID событий объекта в человекочитаемые названия.
        Например: eventType=8, eventNum=0 → "Draw"
        """
        yy_p = self._yy_file("objects", object_name)
        if not yy_p.exists():
            return {"error": f"Object '{object_name}' not found"}
        data = self._read_yy(yy_p)
        if data is None:
            return {"error": f"Failed to parse .yy for '{object_name}'"}

        decoded: List[Dict] = []
        for ev in data.get("eventList", []):
            et = ev.get("eventtype", ev.get("eventType", -1))
            en = ev.get("enumb", ev.get("eventNum", 0))
            decoded.append({
                "event":      self._resolve_event(et, en, ev),
                "event_type": et,
                "event_num":  en,
            })

        return {
            "object":      object_name,
            "event_count": len(decoded),
            "events":      decoded,
        }

    # ------------------------------------------------------------------
    # TOOL 14: get_object_hierarchy
    # ------------------------------------------------------------------

    def get_object_hierarchy(self, object_name: str) -> Dict:
        """
        Строит дерево наследования объекта:
        - цепочку родителей вверх (с их событиями)
        - прямых детей вниз
        Защита от циклического наследования через visited-сет.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        # Строим цепочку родителей
        chain: List[str] = [object_name]
        visited: set = {object_name}
        current = object_name

        while True:
            yy_p = self._yy_file("objects", current)
            if not yy_p.exists():
                break
            data = self._read_yy(yy_p)
            if not data:
                break
            parent_ref = data.get("parentObjectId")
            if not parent_ref:
                break
            parent_name = parent_ref.get("name", "")
            if not parent_name or parent_name in visited:
                break
            chain.append(parent_name)
            visited.add(parent_name)
            current = parent_name

        # Ищем прямых детей по всем объектам проекта
        children: List[str] = []
        cats = self._get_categories()
        for obj_a in cats.get("Objects", []):
            obj_n = obj_a["name"]
            if obj_n == object_name:
                continue
            obj_data = self._read_yy(self._yy_file("objects", obj_n))
            if obj_data:
                p_ref = obj_data.get("parentObjectId")
                if p_ref and p_ref.get("name") == object_name:
                    children.append(obj_n)

        # Собираем иерархию с событиями на каждом уровне
        hierarchy: List[Dict] = []
        for i, obj_n in enumerate(chain):
            obj_data = self._read_yy(self._yy_file("objects", obj_n)) or {}
            events = []
            for ev in obj_data.get("eventList", []):
                et = ev.get("eventtype", ev.get("eventType", -1))
                en = ev.get("enumb", ev.get("eventNum", 0))
                events.append(self._resolve_event(et, en, ev))
            hierarchy.append({
                "object": obj_n,
                "level":  i,
                "role":   "self" if i == 0 else f"parent (depth {i})",
                "events": events,
            })

        return {
            "object":       object_name,
            "parent_chain": chain,
            "depth":        len(chain) - 1,
            "children":     children,
            "hierarchy":    hierarchy,
        }

    # ------------------------------------------------------------------
    # TOOL 15: get_room_instances
    # ------------------------------------------------------------------

    def get_room_instances(self, room_name: str) -> Dict:
        """
        Возвращает все экземпляры объектов в комнате с их x, y координатами.
        Намного подробнее, чем get_room_info — для анализа расстановки.
        """
        yy_p = self._yy_file("rooms", room_name)
        if not yy_p.exists():
            return {"error": f"Room '{room_name}' not found"}
        data = self._read_yy(yy_p)
        if data is None:
            return {"error": f"Failed to parse .yy for room '{room_name}'"}

        instances: List[Dict] = []
        for layer in data.get("layers", []):
            ltype = layer.get("__type", layer.get("modelName", ""))
            if ltype != "GMInstanceLayer":
                continue
            layer_name = layer.get("name", "?")
            for inst in layer.get("instances", []):
                obj_name = (inst.get("objId") or {}).get("name", "?")
                instances.append({
                    "object":            obj_name,
                    "x":                 inst.get("x", 0),
                    "y":                 inst.get("y", 0),
                    "layer":             layer_name,
                    "scaleX":            inst.get("scaleX", 1),
                    "scaleY":            inst.get("scaleY", 1),
                    "rotation":          inst.get("rotation", 0),
                    "has_creation_code": bool(inst.get("creationCodeFile", "")),
                })

        by_object = Counter(i["object"] for i in instances)
        return {
            "room":            room_name,
            "total_instances": len(instances),
            "unique_objects":  len(by_object),
            "object_counts":   dict(sorted(by_object.items())),
            "instances":       instances,
        }

    # ------------------------------------------------------------------
    # TOOL 16: get_macro_constants
    # ------------------------------------------------------------------

    def get_macro_constants(self) -> Dict:
        """
        Собирает все #macro определения из всех GML файлов проекта.
        Это глобальные константы вида: #macro MAX_HEALTH 100
        """
        err = self._check_project()
        if err:
            return {"error": err}

        macro_pattern = re.compile(r"^#macro\s+(\w+)\s+(.+)$", re.MULTILINE)
        macros: List[Dict] = []

        for gml_path in self._all_gml_files():
            try:
                content = gml_path.read_text(encoding="utf-8", errors="replace")
                rel = str(gml_path.relative_to(self.project_path))
                for m in macro_pattern.finditer(content):
                    macros.append({
                        "name":   m.group(1).strip(),
                        "value":  m.group(2).strip(),
                        "source": rel,
                    })
            except OSError:
                continue

        return {
            "total":  len(macros),
            "macros": macros,
        }

    # ------------------------------------------------------------------
    # TOOL 17: rename_asset
    # ------------------------------------------------------------------

    def rename_asset(self, category: str, old_name: str, new_name: str) -> Dict:
        """
        Каскадное переименование ассета:
        1. Переименовывает .yy файл внутри папки
        2. Переименовывает именованные .gml файлы
        3. Переименовывает папку ассета
        4. Заменяет упоминания в ВСЕХ .gml файлах (с .bak бэкапами)
        5. Обновляет "name" внутри .yy файла

        ВНИМАНИЕ: Деструктивная операция. Сделай git commit перед вызовом.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        # Ищем папку категории без учёта регистра
        cat_folder = next(
            (v for k, v in ASSET_CATEGORIES.items() if k.lower() == category.lower()),
            None
        )
        if not cat_folder:
            return {"error": f"Unknown category: {category}. Available: {list(ASSET_CATEGORIES.keys())}"}

        old_path = self.project_path / cat_folder / old_name
        new_path = self.project_path / cat_folder / new_name

        if not old_path.exists():
            return {"error": f"Asset '{old_name}' not found in {category}"}
        if new_path.exists():
            return {"error": f"Asset '{new_name}' already exists in {category}"}

        changed_files: List[str] = []
        errors: List[str] = []

        try:
            # 1. Переименовываем .yy файл
            old_yy = old_path / f"{old_name}.yy"
            if old_yy.exists():
                old_yy.rename(old_path / f"{new_name}.yy")

            # 2. Переименовываем GML файлы с именем ассета
            for old_gml in list(old_path.glob(f"{old_name}*.gml")):
                new_gml_name = old_gml.name.replace(old_name, new_name, 1)
                old_gml.rename(old_path / new_gml_name)

            # 3. Переименовываем саму папку
            old_path.rename(new_path)

            # 4. Текстовая замена в GML файлах проекта
            for gml_path in self._all_gml_files():
                try:
                    content = gml_path.read_text(encoding="utf-8", errors="replace")
                    if old_name in content:
                        shutil.copy2(gml_path, gml_path.with_suffix(".gml.bak"))
                        gml_path.write_text(content.replace(old_name, new_name), encoding="utf-8")
                        changed_files.append(str(gml_path.relative_to(self.project_path)))
                except OSError as e:
                    errors.append(str(e))

            # 5. Обновляем поле "name" в .yy файле переименованного ассета
            new_yy = new_path / f"{new_name}.yy"
            if new_yy.exists():
                yy_data = self._read_yy(new_yy)
                if yy_data and yy_data.get("name") == old_name:
                    yy_data["name"] = new_name
                    new_yy.write_text(json.dumps(yy_data, indent=2, ensure_ascii=False), encoding="utf-8")

            # Сбрасываем весь кеш
            self._cache.clear()

            return {
                "success":            True,
                "old_name":           old_name,
                "new_name":           new_name,
                "category":           category,
                "gml_files_updated":  len(changed_files),
                "updated_files":      changed_files,
                "errors":             errors,
                "note":               "Reload project in GameMaker Studio 2 to apply changes",
            }
        except OSError as e:
            return {"error": f"Rename failed: {e}", "partial_errors": errors}

    # ------------------------------------------------------------------
    # TOOL 18: get_gml_definitions_index
    # ------------------------------------------------------------------

    def get_gml_definitions_index(self) -> Dict:
        """
        Парсит все GML файлы и составляет индекс:
        - все определённые функции (function name(...))
        - все global.переменные (global.varname = ...)
        - все #macro константы

        Полезно: AI знает какие функции уже есть, не создаёт дубликаты.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        cached = self._cache_get("definitions_index")
        if cached:
            return cached

        fn_pat     = re.compile(r"^\s*function\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
        global_pat = re.compile(r"global\.(\w+)\s*=", re.MULTILINE)
        macro_pat  = re.compile(r"^#macro\s+(\w+)\s+(.+)$", re.MULTILINE)

        functions:   List[Dict] = []
        globals_vars: List[Dict] = []
        macros:       List[Dict] = []

        for gml_path in self._all_gml_files():
            rel = str(gml_path.relative_to(self.project_path))
            try:
                content = gml_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for m in fn_pat.finditer(content):
                functions.append({
                    "name":   m.group(1),
                    "params": m.group(2).strip(),
                    "file":   rel,
                })

            seen: set = set()
            for m in global_pat.finditer(content):
                var = m.group(1)
                if var not in seen:
                    globals_vars.append({"name": f"global.{var}", "file": rel})
                    seen.add(var)

            for m in macro_pat.finditer(content):
                macros.append({"name": m.group(1), "value": m.group(2).strip(), "file": rel})

        result = {
            "function_count": len(functions),
            "global_count":   len(globals_vars),
            "macro_count":    len(macros),
            "functions":      functions,
            "global_variables": globals_vars,
            "macros":         macros,
        }
        self._cache_set("definitions_index", result)
        return result

    # ------------------------------------------------------------------
    # TOOL 19: validate_project
    # ------------------------------------------------------------------

    def validate_project(self) -> Dict:
        """
        Проверяет целостность GMS2 проекта:
        - объекты с несуществующими спрайтами / родителями
        - пустые GML файлы и скрипты
        - ошибки парсинга .yy файлов

        Возвращает списки ошибок (критичные) и предупреждений.
        """
        err = self._check_project()
        if err:
            return {"error": err}

        cats = self._get_categories()
        obj_names    = {a["name"] for a in cats.get("Objects", [])}
        sprite_names = {a["name"] for a in cats.get("Sprites", [])}

        issues:   List[Dict] = []
        warnings: List[Dict] = []

        # Проверяем каждый объект
        for obj_a in cats.get("Objects", []):
            name = obj_a["name"]
            data = self._read_yy(self._yy_file("objects", name))
            if data is None:
                issues.append({"type": "parse_error", "asset": f"objects/{name}", "detail": "Cannot read .yy file"})
                continue

            # Спрайт существует?
            sprite_ref = (data.get("spriteId") or {}).get("name")
            if sprite_ref and sprite_ref not in sprite_names:
                issues.append({
                    "type":   "missing_sprite",
                    "asset":  f"objects/{name}",
                    "detail": f"References sprite '{sprite_ref}' which does not exist",
                })

            # Родитель существует?
            parent_ref = (data.get("parentObjectId") or {}).get("name")
            if parent_ref and parent_ref not in obj_names:
                issues.append({
                    "type":   "missing_parent",
                    "asset":  f"objects/{name}",
                    "detail": f"Parent '{parent_ref}' does not exist",
                })

            # Пустые GML файлы событий
            for gml_name in obj_a.get("gml_files", []):
                gml_p = self.project_path / "objects" / name / gml_name
                try:
                    if gml_p.stat().st_size == 0:
                        warnings.append({"type": "empty_file", "asset": f"objects/{name}/{gml_name}", "detail": "GML file is empty"})
                except OSError:
                    pass

        # Проверяем скрипты — пустое содержимое
        for sc_a in cats.get("Scripts", []):
            name = sc_a["name"]
            for gml_name in sc_a.get("gml_files", []):
                gml_p = self.project_path / "scripts" / name / gml_name
                try:
                    if not gml_p.read_text(encoding="utf-8", errors="replace").strip():
                        warnings.append({"type": "empty_script", "asset": f"scripts/{name}/{gml_name}", "detail": "Script is empty"})
                except OSError:
                    pass

        return {
            "status":        "clean" if not issues else "issues_found",
            "issue_count":   len(issues),
            "warning_count": len(warnings),
            "issues":        issues,
            "warnings":      warnings,
        }

    # ------------------------------------------------------------------
    # TOOL 20: diff_gml_file
    # ------------------------------------------------------------------

    def diff_gml_file(self, file_path: str) -> Dict:
        """
        Показывает git diff GML файла между текущим состоянием и HEAD.
        Удобно после AI-правок: понять что именно изменилось.
        Требует git в PATH и что проект находится в git-репозитории.
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_path / file_path
        if not p.exists():
            return {"error": f"File not found: {p}"}

        rel = str(p.relative_to(self.project_path))
        try:
            # Создаем копию окружения и отключаем интерактивность
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_PAGER"] = "cat"
            
            # Используем --no-pager чтобы git не ждал нажатия клавиш
            # Используем -c core.quotepath=off чтобы пути с кириллицей не превращались в \320\260
            result = subprocess.run(
                ["git", "--no-pager", "-c", "core.quotepath=off", "diff", "HEAD", "--", rel],
                capture_output=True,
                cwd=str(self.project_path),
                timeout=15,  # Увеличили до 15с на всякий случай
                env=env
            )
            
            # git возвращает 128 если не git-репозиторий
            if result.returncode not in (0, 1):
                err_msg = result.stderr.decode("utf-8", errors="replace").strip()
                if "not a git repository" in err_msg.lower():
                    return {"error": "Project is not a git repository"}
                return {"error": f"git error: {err_msg}"}

            # Декодируем вывод вручную с заменой битых символов
            diff = result.stdout.decode("utf-8", errors="replace")
            if not diff:
                return {"file": rel, "status": "no_changes", "diff": ""}

            lines = diff.splitlines()
            added   = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
            return {
                "file":          rel,
                "status":        "modified",
                "lines_added":   added,
                "lines_removed": removed,
                "diff":          diff,
            }
        except FileNotFoundError:
            return {"error": "git is not installed or not in PATH"}
        except subprocess.TimeoutExpired:
            return {"error": "git diff timed out (operation took longer than 12s)"}
        except Exception as e:
            return {"error": f"Unexpected error during diff: {str(e)}"}

    # ------------------------------------------------------------------
    # TOOL 21: get_shader_info
    # ------------------------------------------------------------------

    def get_shader_info(self, name: str) -> Dict:
        """
        Читает шейдер GMS2: вершинный (.vsh) и фрагментный (.fsh) файлы.
        Дополнительно извлекает список uniform-переменных через regex по GLSL коду.
        """
        shader_dir = self._asset_dir("shaders", name)
        if not shader_dir.exists():
            return {"error": f"Shader '{name}' not found"}

        result: Dict[str, Any] = {
            "name":     name,
            "vertex":   None,
            "fragment": None,
            "uniforms": [],
        }

        for ext, key in [(".vsh", "vertex"), (".fsh", "fragment")]:
            fp = shader_dir / f"{name}{ext}"
            if fp.exists():
                try:
                    code = fp.read_text(encoding="utf-8", errors="replace")
                    result[key] = code
                    # Извлекаем uniform-переменные из GLSL (вида: uniform vec2 uTexOffset;)
                    for u in re.findall(r"uniform\s+\w+\s+(\w+)\s*;", code):
                        if u not in result["uniforms"]:
                            result["uniforms"].append(u)
                except OSError:
                    result[key] = "Error reading file"

        # Дополнительная мета из .yy файла (тип шейдера)
        yy_data = self._read_yy(shader_dir / f"{name}.yy")
        if yy_data:
            result["shader_type"] = yy_data.get("type", "?")

        return result