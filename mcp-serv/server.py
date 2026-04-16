"""
server.py — MCP сервер для работы с проектами GameMaker Studio 2.

Использует FastMCP (высокоуровневый API библиотеки mcp) для регистрации
инструментов через декораторы. Все тяжёлые операции делегированы в gms2_parser.py.

Запуск:
    python server.py

Конфигурация через переменную окружения:
    GMS2_PROJECT_PATH — абсолютный путь к папке проекта GMS2 (где лежит .yyp файл)

Пример конфига для Cursor / Windsurf / Claude Desktop (mcp.json):
    {
        "mcpServers": {
            "gms2-mcp": {
                "command": "C:/Users/n0souls/gms2-mcp-server/venv/Scripts/python.exe",
                "args": ["C:/Users/n0souls/gms2-mcp-server/mcp-serv/server.py"],
                "env": {
                    "GMS2_PROJECT_PATH": "C:/Users/n0souls/Documents/GitHub/Undefinedtale-888/Undefinedtale888"
                }
            }
        }
    }

---
АРХИТЕКТУРА КОНКУРЕНТНОСТИ:
    FastMCP использует asyncio event loop. Все тяжёлые операции (чтение файлов,
    os.walk, поиск) — синхронный (блокирующий) I/O. Чтобы параллельные вызовы
    не замораживали друг друга, каждый инструмент запускает парсер через
    asyncio.to_thread() — это выполняет блокирующую функцию в отдельном потоке
    ThreadPoolExecutor, не блокируя основной asyncio цикл.

    Это значит N параллельных вызовов = N потоков читают файлы одновременно,
    но asyncio остаётся отзывчивым.
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Добавляем папку mcp-serv в sys.path, чтобы найти gms2_parser
sys.path.insert(0, str(Path(__file__).parent))
from gms2_parser import CachedParser

# ---------------------------------------------------------------------------
# Логирование — только в stderr, чтобы не ломать stdio-транспорт MCP
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gms2-mcp")

# ---------------------------------------------------------------------------
# Создаём экземпляр FastMCP — сердце сервера
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "gms2-mcp-server",
    instructions=(
        "GMS2 MCP Server provides tools to read, analyze and modify "
        "GameMaker Studio 2 projects. "
        "Always start with get_project_summary to understand the project. "
        "Use search_in_project to find code before reading full files. "
        "Use export_project_data with small limit (3-5) to avoid context overflow."
    ),
)

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _parser() -> CachedParser:
    """
    Возвращает кешированный экземпляр парсера для текущего проекта.
    Путь берётся из переменной окружения GMS2_PROJECT_PATH.
    Если переменная не задана — бросает RuntimeError (MCP вернёт ошибку клиенту).
    """
    project_path = os.environ.get("GMS2_PROJECT_PATH", "").strip()
    if not project_path:
        raise RuntimeError(
            "GMS2_PROJECT_PATH environment variable is not set. "
            "Add it to your MCP client config (mcp.json)."
        )
    return CachedParser.get(project_path)


def _fmt(data: dict) -> str:
    """
    Сериализует результат в компактный JSON для ответа клиенту.
    Использует ensure_ascii=False чтобы сохранить кириллицу как есть.
    """
    return json.dumps(data, indent=2, ensure_ascii=False)


# Семафор: максимум 2 тяжёлых операции одновременно.
# Это предотвращает «лавину» — когда AI шлёт 5+ параллельных search_in_project,
# и все 5 начинают одновременно читать сотни файлов с диска.
# С семафором: 2 работают, остальные 3 ждут в очереди (мгновенно, не таймаутятся).
# Значение 2 — компромисс: достаточно для скорости, но не убивает диск.
_semaphore = asyncio.Semaphore(2)


async def _run(fn) -> str:
    """
    Запускает синхронную функцию fn() в отдельном потоке.

    Три уровня защиты:
    1. asyncio.Semaphore — не более 2 одновременных тяжёлых операций
    2. asyncio.to_thread — блокирующий I/O не замораживает event loop
    3. try/except — любая ошибка возвращается как JSON, а не крашит сервер
    """
    async with _semaphore:
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            logger.error("Tool execution error: %s", e, exc_info=True)
            return _fmt({"error": f"Internal server error: {str(e)}"})

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ READ (async) =============================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_project_summary() -> str:
    """
    Return a compact overview of the GMS2 project: name, path,
    and asset counts by category. Use this first to understand the project.

    ## Return Format
    JSON with: project_name, project_path, asset_counts (dict), total_assets
    """
    return await _run(lambda: _fmt(_parser().get_project_summary()))


@mcp.tool()
async def scan_project(category: Optional[str] = None) -> str:
    """
    Return a list of all assets in the project grouped by category.
    Each asset shows its name, GML file count, and whether a .yy file exists.

    Args:
        category: Optional filter. One of: Objects, Scripts, Rooms, Sprites,
                  Sounds, Fonts, Shaders, Tile Sets, Timelines, Notes, Extensions, Sequences.

    ## Return Format
    JSON with: project_name, categories (dict of lists with name/gml_files/has_yy)
    """
    return await _run(lambda: _fmt(_parser().scan_project(category=category)))


@mcp.tool()
async def list_assets(
    category: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """
    Return a paginated flat list of all assets in the project.
    Use offset + limit to page through large projects.

    Args:
        category: Optional category filter (Objects, Scripts, Rooms, etc.)
        offset:   Start index (default 0)
        limit:    Max items to return (default 50)

    ## Return Format
    JSON with: total, offset, limit, has_more, assets (list)
    """
    return await _run(lambda: _fmt(_parser().list_assets(category=category, offset=offset, limit=limit)))


@mcp.tool()
async def get_gml_content(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    Read the contents of a GML file. Supports partial reads via line range.
    file_path can be relative to project root (e.g. "objects/obj_player/Create_0.gml")
    or absolute.

    Args:
        file_path:  Path to the GML file
        start_line: First line to include (1-indexed, optional)
        end_line:   Last line to include (1-indexed, optional)

    ## Return Format
    JSON with: file (relative path), total_lines, content (or range if specified)
    """
    return await _run(lambda: _fmt(_parser().get_gml_content(file_path, start_line=start_line, end_line=end_line)))


@mcp.tool()
async def get_object_info(object_name: str) -> str:
    """
    Get detailed information about a GMS2 object from its .yy file.
    Includes sprite, parent, events (decoded to human-readable names), variables, physics.

    Args:
        object_name: Name of the object (e.g. "obj_player")

    ## Return Format
    JSON with: name, sprite, parent, events (list), variables, physics_enabled, etc.
    """
    return await _run(lambda: _fmt(_parser().get_object_info(object_name)))


@mcp.tool()
async def get_room_info(room_name: str) -> str:
    """
    Get information about a GMS2 room from its .yy file.
    Includes dimensions, speed, persistence flag, and layer list.

    Args:
        room_name: Name of the room (e.g. "rm_game")

    ## Return Format
    JSON with: name, width, height, speed, persistent, layers (list), layer_count
    """
    return await _run(lambda: _fmt(_parser().get_room_info(room_name)))


@mcp.tool()
async def get_sprite_info(sprite_name: str) -> str:
    """
    Get metadata for a GMS2 sprite from its .yy file.
    Includes dimensions, origin point, bounding box, frame count, playback speed.

    Args:
        sprite_name: Name of the sprite (e.g. "spr_player")

    ## Return Format
    JSON with: name, width, height, origin_x/y, frame_count, bbox_*, collision_kind
    """
    return await _run(lambda: _fmt(_parser().get_sprite_info(sprite_name)))


@mcp.tool()
async def get_shader_info(shader_name: str) -> str:
    """
    Read a GMS2 shader asset: vertex (.vsh) and fragment (.fsh) GLSL code.
    Also extracts a list of uniform variables from the shader source.

    Args:
        shader_name: Name of the shader asset (e.g. "sh_outline")

    ## Return Format
    JSON with: name, vertex (GLSL code), fragment (GLSL code), uniforms (list), shader_type
    """
    return await _run(lambda: _fmt(_parser().get_shader_info(shader_name)))


@mcp.tool()
async def get_macro_constants() -> str:
    """
    Collect all #macro definitions from all GML files in the project.
    Macros are global constants like: #macro MAX_HEALTH 100

    ## Return Format
    JSON with: total, macros (list of {name, value, source})
    """
    return await _run(lambda: _fmt(_parser().get_macro_constants()))

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ ANALYSIS (async) =========================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def find_asset_references(asset_name: str) -> str:
    """
    Find all references to a named asset across all GML files and .yy files.
    Use this to understand dependencies: which objects use sprite X, which scripts
    are called from object Y, etc.

    Args:
        asset_name: The name to find (e.g. "spr_player", "obj_enemy", "scr_utils")

    ## Return Format
    JSON with: asset_name, gml_ref_count, yy_ref_count,
               gml_references (file, line, context), yy_references (file paths)
    """
    return await _run(lambda: _fmt(_parser().find_asset_references(asset_name)))


@mcp.tool()
async def decode_object_events(object_name: str) -> str:
    """
    Decode the numeric event IDs of a GMS2 object into human-readable event names.
    GMS2 stores events as numbers (e.g. eventType=8, eventNum=0 → "Draw").
    This tool translates all of them.

    Args:
        object_name: Name of the object (e.g. "obj_player")

    ## Return Format
    JSON with: object, event_count, events (list of {event, event_type, event_num})
    """
    return await _run(lambda: _fmt(_parser().decode_object_events(object_name)))


@mcp.tool()
async def get_object_hierarchy(object_name: str) -> str:
    """
    Build the full inheritance tree for a GMS2 object.
    Returns the chain of parent objects (up), list of direct children (down),
    and the events defined at each level of the hierarchy.

    Args:
        object_name: Name of the object (e.g. "obj_enemy_boss")

    ## Return Format
    JSON with: object, parent_chain (list), depth, children (list), hierarchy (list)
    """
    return await _run(lambda: _fmt(_parser().get_object_hierarchy(object_name)))


@mcp.tool()
async def get_room_instances(room_name: str) -> str:
    """
    Get all object instances placed in a room with their exact (x, y) coordinates,
    scale, rotation, layer name, and whether they have creation code.
    More detailed than get_room_info.

    Args:
        room_name: Name of the room (e.g. "rm_level_01")

    ## Return Format
    JSON with: room, total_instances, unique_objects, object_counts (dict),
               instances (list of {object, x, y, layer, scaleX, scaleY, rotation})
    """
    return await _run(lambda: _fmt(_parser().get_room_instances(room_name)))


@mcp.tool()
async def get_gml_definitions_index() -> str:
    """
    Parse all GML files and build an index of defined functions, global variables,
    and #macro constants across the entire project.
    Use this to discover what already exists before adding new code.

    ## Return Format
    JSON with: function_count, global_count, macro_count,
               functions (list of {name, params, file}),
               global_variables (list of {name, file}),
               macros (list of {name, value, file})
    """
    return await _run(lambda: _fmt(_parser().get_gml_definitions_index()))


@mcp.tool()
async def validate_project() -> str:
    """
    Check the GMS2 project for integrity issues:
    - Objects referencing non-existent sprites or parent objects
    - Empty GML event files
    - Empty script files
    - .yy parse errors
    Returns lists of critical issues and warnings.

    ## Return Format
    JSON with: status ("clean" or "issues_found"), issue_count, warning_count,
               issues (list), warnings (list)
    """
    return await _run(lambda: _fmt(_parser().validate_project()))

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ SEARCH (async) ===========================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_in_project(
    query: str,
    case_sensitive: bool = False,
    category: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """
    Search for text across all GML files in the project.
    Returns file paths, line numbers, and surrounding context for each match.
    Use this before reading full files — much cheaper in tokens.

    Args:
        query:          Text to search for
        case_sensitive: Whether the search is case-sensitive (default False)
        category:       Limit search to one category (Objects, Scripts, etc.)
        max_results:    Maximum number of matches to return (default 50)

    ## Return Format
    JSON with: query, files_searched, match_count, truncated,
               matches (list of {file, line, content})
    """
    return await _run(lambda: _fmt(_parser().search_in_project(
        query=query,
        case_sensitive=case_sensitive,
        category=category,
        max_results=max_results,
    )))

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ WRITE (async) ============================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def write_gml_file(
    file_path: str,
    content: str,
    create_backup: bool = True,
) -> str:
    """
    Write content to a GML file. Creates parent directories if needed.
    If the file already exists, a .bak backup is created before overwriting (when create_backup=True).
    file_path can be relative to project root or absolute.

    Args:
        file_path:      Path to the .gml file to write
        content:        New file content as a string
        create_backup:  Whether to create a .bak backup (default True)

    ## Return Format
    JSON with: success, file (relative path), lines_written, backup (path or null)
    """
    return await _run(lambda: _fmt(_parser().write_gml_file(file_path, content, create_backup=create_backup)))


@mcp.tool()
async def create_script(name: str, content: str = "") -> str:
    """
    Create a new Script asset in the GMS2 project.
    Creates the folder scripts/<name>/, a minimal .yy file, and a .gml file with the given content.
    Note: You need to reload the project in GameMaker Studio 2 to see the new script.

    Args:
        name:    Name for the new script (e.g. "scr_collision_utils")
        content: Initial GML content for the script file (default empty)

    ## Return Format
    JSON with: success, name, path, yy_created, gml_created, note
    """
    return await _run(lambda: _fmt(_parser().create_script(name, content=content)))


@mcp.tool()
async def rename_asset(category: str, old_name: str, new_name: str) -> str:
    """
    Cascade-rename a GMS2 asset: renames the folder, .yy file, .gml files,
    and replaces all text references in every GML file across the project.
    Creates .bak backups of all modified GML files.

    WARNING: This is a destructive operation. Commit to git before calling.
    Reload the project in GameMaker Studio 2 after renaming.

    Args:
        category: Asset category (Objects, Scripts, Sprites, etc.)
        old_name: Current asset name
        new_name: New asset name

    ## Return Format
    JSON with: success, old_name, new_name, gml_files_updated, updated_files, errors, note
    """
    return await _run(lambda: _fmt(_parser().rename_asset(category, old_name, new_name)))

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ EXPORT (async) ===========================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def export_project_data(
    offset: int = 0,
    limit: int = 5,
    category: Optional[str] = None,
) -> str:
    """
    Export GML file contents with pagination. Returns `limit` files starting at `offset`.
    Check `has_more` and use `next_offset` to get the next page.

    IMPORTANT: Use small limit values (3-5) to avoid flooding the context window.
    The full project export can be thousands of tokens.

    Args:
        offset:   File index to start from (default 0)
        limit:    Number of files per page (default 5)
        category: Limit to one category (Objects, Scripts, etc.)

    ## Return Format
    JSON with: total_files, offset, limit, has_more, next_offset,
               files (list of {file, content, lines})
    """
    return await _run(lambda: _fmt(_parser().export_project_data(offset=offset, limit=limit, category=category)))

# ---------------------------------------------------------------------------
# === ИНСТРУМЕНТЫ DIFF (async) =============================================
# ---------------------------------------------------------------------------

@mcp.tool()
async def diff_gml_file(file_path: str) -> str:
    """
    Show the git diff for a GML file between the current working state and HEAD.
    Useful after AI edits to review exactly what changed.
    Requires git to be installed and the project to be in a git repository.

    Args:
        file_path: Path to the .gml file (relative to project root or absolute)

    ## Return Format
    JSON with: file, status ("modified" / "no_changes"), lines_added, lines_removed, diff (text)
    """
    return await _run(lambda: _fmt(_parser().diff_gml_file(file_path)))

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Логируем конфигурацию при старте
    project_path = os.environ.get("GMS2_PROJECT_PATH", "<not set>")
    logger.info("GMS2 MCP Server starting...")
    logger.info("Project path: %s", project_path)
    logger.info("Tools registered: 21 (all async via asyncio.to_thread)")

    # Запускаем сервер через stdio (стандарт для локальных MCP серверов)
    mcp.run()
