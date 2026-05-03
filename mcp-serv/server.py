import os
import sys
import json
import logging
import logging.handlers
import asyncio
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

# Принудительно ставим UTF-8 для stdout/stderr
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# Windows: принудительно unbuffered mode, чтобы stdio transport не зависал на flush
if sys.platform == "win32":
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

# Добавляем путь к серверу в sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from gms2_parser import CachedParser

# ---------------------------------------------------------------------------
# Логирование: файл (DEBUG) + stderr (WARNING+, чтобы не мешать stdio MCP)
# ---------------------------------------------------------------------------
LOG_DIR = Path(current_dir).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "mcp-server.log"

logger = logging.getLogger("gms2-mcp")
logger.propagate = False
logger.setLevel(logging.DEBUG)

# Rotating file handler (макс 2 МБ, 3 бэкапа)
fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
fh.setLevel(logging.DEBUG)
logger.addHandler(fh)

# Stderr handler — только WARNING+, чтобы не засорять stdio transport
sh = logging.StreamHandler(sys.stderr)
sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%H:%M:%S'))
sh.setLevel(logging.WARNING)
logger.addHandler(sh)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("FastMCP not found.")
    exit(1)

mcp = FastMCP("gms2-mcp")

# Отдельный executor для тяжёлых синхронных операций.
# max_workers=8 — с запасом, т.к. при "зомби" задачах (timeout отменил Future,
# но поток всё ещё выполняет fn) worker остаётся занятым.
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gms2_mcp")

# Семафор на уровне asyncio: обрабатываем tool-вызовы строго последовательно.
# Это решает две проблемы stdio transport:
# 1. Interleaving ответов в stdout — два потока одновременно пишут JSON-RPC,
#    байты перемешиваются, клиент получает битый JSON и падает.
# 2. ThreadPoolExecutor не забивается: одна задача в пуле за раз.
_tool_semaphore = asyncio.Semaphore(1)

def _parser() -> CachedParser:
    project_path = os.environ.get("GMS2_PROJECT_PATH", "").strip()
    if not project_path:
        raise ValueError("GMS2_PROJECT_PATH is not set")
    return CachedParser.get(project_path)

def _fmt(data: dict) -> str:
    """Форматирует словарь в JSON строку с отступами (indent=2) для лучшей читаемости в UI Windsurf."""
    return json.dumps(data, indent=2, ensure_ascii=False)

async def _run(fn, tool_name: str = "unknown") -> str:
    """
    Запускает синхронную функцию fn в отдельном треде через собственный executor.
    Обработка строго последовательная (Semaphore=1) — защита stdio transport
    от interleaving JSON-RPC ответов в stdout, когда два потока завершаются
    одновременно.  Таймаут 65 с покрывает и ожидание семафора, и выполнение.
    """
    start_time = time.time()
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(65.0):
            async with _tool_semaphore:
                logger.info(f"Executing tool '{tool_name}'...")
                result = await asyncio.wait_for(
                    loop.run_in_executor(_executor, fn),
                    timeout=60.0
                )
        elapsed = time.time() - start_time
        logger.info(f"Tool '{tool_name}' finished in {elapsed:.3f}s")
        return result
    except asyncio.TimeoutError:
        logger.error(f"Tool '{tool_name}' timed out after 60s (execution) or 65s (total)")
        return _fmt({"error": f"Timeout: {tool_name}"})
    except Exception as e:
        logger.error(f"Tool '{tool_name}' error: {e}", exc_info=True)
        return _fmt({"error": f"Error in {tool_name}: {str(e)}"})

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ (с восстановленными аргументами для UI) ===================
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_project_summary() -> str:
    """
    Return a compact overview of the GMS2 project: name, path, and asset counts. I recommend using this tool first.
    """
    return await _run(lambda: _fmt(_parser().get_project_summary()), "get_project_summary")

@mcp.tool()
async def scan_project(category: Optional[str] = None) -> str:
    """
    Return a full list of all assets in the project.
    
    Args:
        category: Optional filter by category (e.g. 'Objects', 'Scripts').
    """
    return await _run(lambda: _fmt(_parser().scan_project(category)), "scan_project")

@mcp.tool()
async def list_assets(
    category: str,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """
    List all assets in a specific category.

    Args:
        category: Assets category (e.g. 'Objects', 'Scripts', 'Sprites', 'Rooms')
        offset: Start index for pagination.
        limit: Number of items to return.
    """
    return await _run(lambda: _fmt(_parser().list_assets(category, offset, limit)), "list_assets")

@mcp.tool()
async def get_gml_content(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    Read the code (GML) from a file.

    Args:
        file_path: Path to the .gml file relative to project root.
        start_line: Optional start line number (1-indexed).
        end_line: Optional end line number (inclusive).
    """
    return await _run(
        lambda: _fmt(_parser().get_gml_content(file_path, start_line, end_line)),
        "get_gml_content"
    )

@mcp.tool()
async def get_object_info(name: str) -> str:
    """
    Get GMS2 Object metadata: sprite, parent, and all its events/files.

    Args:
        name: Name of the object.
    """
    # Возвращает спрайт, родителя и список всех GML-файлов событий объекта.
    return await _run(lambda: _fmt(_parser().get_object_info(name)), "get_object_info")

@mcp.tool()
async def get_room_info(name: str) -> str:
    """
    Get GMS2 Room metadata: dimensions and layers.

    Args:
        name: Name of the room.
    """
    return await _run(lambda: _fmt(_parser().get_room_info(name)), "get_room_info")

@mcp.tool()
async def get_sprite_info(name: str) -> str:
    """
    Get GMS2 Sprite metadata: size, origin, frames.

    Args:
        name: Name of the sprite.
    """
    # Извлекает размеры, origin, bbox и количество кадров спрайта.
    return await _run(lambda: _fmt(_parser().get_sprite_info(name)), "get_sprite_info")

@mcp.tool()
async def search_in_project(
    query: str,
    is_regex: bool = False,
    case_sensitive: bool = False,
    category: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """
    Search for text in all GML files.

    Args:
        query: Text or regex to search for.
        is_regex: Use regular expressions.
        case_sensitive: Case-sensitive search.
        category: Filter by category (e.g. 'Objects').
        max_results: Maximum matches to return.
    """
    return await _run(
        lambda: _fmt(_parser().search_in_project(query, is_regex, case_sensitive, category, max_results)),
        "search_in_project"
    )

@mcp.tool()
async def write_gml_file(
    file_path: str,
    content: str,
    create_backup: bool = True,
) -> str:
    """
    Overwrite a GML file with new code.

    Args:
        file_path: Relative path to the .gml file.
        content: The new GML code.
        create_backup: Create a .bak file before overwriting.
    """
    return await _run(
        lambda: _fmt(_parser().write_gml_file(file_path, content, create_backup)),
        "write_gml_file"
    )

@mcp.tool()
async def create_script(name: str, content: str = "") -> str:
    """
    Create a new GMS2 Script asset.

    Args:
        name: Name of the new script.
        content: Initial GML code.
    """
    return await _run(lambda: _fmt(_parser().create_script(name, content)), "create_script")

@mcp.tool()
async def rename_asset(category: str, old_name: str, new_name: str) -> str:
    """
    Rename an asset and update all its references in the project.

    Args:
        category: Category of the asset.
        old_name: Current name.
        new_name: New name.
    """
    return await _run(
        lambda: _fmt(_parser().rename_asset(category, old_name, new_name)),
        "rename_asset"
    )

@mcp.tool()
async def export_project_data(
    offset: int = 0,
    limit: int = 5,
    category: Optional[str] = None,
) -> str:
    """
    Batch export GML files for analysis (with pagination).

    Args:
        offset: Start index.
        limit: Number of files to export.
        category: Optional filter by category.
    """
    return await _run(
        lambda: _fmt(_parser().export_project_data(offset, limit, category)),
        "export_project_data"
    )

@mcp.tool()
async def find_asset_references(asset_name: str) -> str:
    """
    Find all references to an asset in GML and YY files.

    Args:
        asset_name: Name of the asset to find.
    """
    return await _run(
        lambda: _fmt(_parser().find_asset_references(asset_name)),
        "find_asset_references"
    )

@mcp.tool()
async def decode_object_events(object_name: str) -> str:
    """
    Translate numeric GMS2 event IDs to names (Create, Step, etc.).

    Args:
        object_name: Name of the object.
    """
    return await _run(
        lambda: _fmt(_parser().decode_object_events(object_name)),
        "decode_object_events"
    )

@mcp.tool()
async def get_object_hierarchy(object_name: str) -> str:
    """
    Get parent-child inheritance tree for an object.

    Args:
        object_name: Name of the object.
    """
    return await _run(
        lambda: _fmt(_parser().get_object_hierarchy(object_name)),
        "get_object_hierarchy"
    )

@mcp.tool()
async def get_room_instances(room_name: str) -> str:
    """
    Get a list of all object instances in a room with coordinates.

    Args:
        room_name: Name of the room.
    """
    return await _run(
        lambda: _fmt(_parser().get_room_instances(room_name)),
        "get_room_instances"
    )

@mcp.tool()
async def get_macro_constants() -> str:
    """
    Extract all #macro definitions from the project.
    """
    return await _run(lambda: _fmt(_parser().get_macro_constants()), "get_macro_constants")

@mcp.tool()
async def get_gml_definitions_index() -> str:
    """
    Index all functions, global variables, and macros in the project.
    """
    return await _run(
        lambda: _fmt(_parser().get_gml_definitions_index()),
        "get_gml_definitions_index"
    )

@mcp.tool()
async def validate_project() -> str:
    """
    Check project for errors (orphans, missing assets, etc.).
    """
    return await _run(lambda: _fmt(_parser().validate_project()), "validate_project")

@mcp.tool()
async def diff_gml_file(file_path: str) -> str:
    """
    Show git diff for a GML file (current vs HEAD).

    Args:
        file_path: Relative path to the .gml file.
    """
    # Показывает изменения в файле через git diff. Полезно для проверки правок AI.
    return await _run(lambda: _fmt(_parser().diff_gml_file(file_path)), "diff_gml_file")

@mcp.tool()
async def get_shader_info(name: str) -> str:
    """
    Get shader code (vsh/fsh) and its uniform variables.

    Args:
        name: Name of the shader.
    """
    return await _run(lambda: _fmt(_parser().get_shader_info(name)), "get_shader_info")

@mcp.tool()
async def create_asset(
    category: str,
    name: str,
    content: str = "",
    events: Optional[List[str]] = None,
    parent_folder: Optional[str] = None,
) -> str:
    """
    Create a new GMS2 asset (Objects, Scripts, or Shaders).

    Args:
        category: Asset category - "Objects", "Scripts", or "Shaders".
        name: Name of the new asset.
        content: Initial GML code (for Scripts) or empty string.
        events: List of event names for Objects, e.g. ["Create", "Step", "Draw_64"].
        parent_folder: Optional folder path in resource tree, e.g. "folders/Objects.yy".
    """
    return await _run(
        lambda: _fmt(_parser().create_asset(category, name, content, events, parent_folder)),
        "create_asset",
    )

@mcp.tool()
async def add_object_event(
    object_name: str,
    event: str,
    content: str = "",
) -> str:
    """
    Add a new event to an existing GMS2 Object.
    Creates the GML file and updates the object's .yy file.

    Args:
        object_name: Name of the existing object.
        event: Event name, e.g. "Create", "Step", "Draw_64", "Draw GUI", "Alarm_5".
        content: Optional initial GML code for the event file.
    """
    return await _run(
        lambda: _fmt(_parser().add_object_event(object_name, event, content)),
        "add_object_event",
    )

if __name__ == "__main__":
    project_path = os.environ.get("GMS2_PROJECT_PATH", "<not set>")
    logger.info("=" * 50)
    logger.info("GMS2 MCP Server starting")
    logger.info(f"Project path: {project_path}")
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Python: {sys.version}")
    logger.info("=" * 50)
    try:
        mcp.run()
    finally:
        _executor.shutdown(wait=True)
        logger.info("GMS2 MCP Server shutdown complete")
