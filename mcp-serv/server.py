import os
import sys
import json
import logging
import asyncio
import time
from typing import Optional, List, Dict, Any

# Принудительно ставим UTF-8 для stdout/stderr
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# Добавляем путь к серверу в sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from gms2_parser import CachedParser

logger = logging.getLogger("gms2-mcp")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] gms2-mcp: %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("FastMCP not found.")
    exit(1)

mcp = FastMCP("gms2-mcp")

def _parser() -> CachedParser:
    project_path = os.environ.get("GMS2_PROJECT_PATH", "").strip()
    if not project_path:
        raise ValueError("GMS2_PROJECT_PATH is not set")
    return CachedParser.get(project_path)

def _fmt(data: dict) -> str:
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False)

# ГАРАНТИЯ СТАБИЛЬНОСТИ: строго последовательное выполнение.
_semaphore = asyncio.Semaphore(1)

async def _run(fn, tool_name: str = "unknown") -> str:
    start_time = time.time()
    async with _semaphore:
        try:
            logger.info(f"Executing tool '{tool_name}'...")
            result = await asyncio.wait_for(asyncio.to_thread(fn), timeout=60.0)
            elapsed = time.time() - start_time
            logger.info(f"Tool '{tool_name}' finished in {elapsed:.3f}s")
            return result
        except asyncio.TimeoutError:
            logger.error(f"Tool '{tool_name}' timed out after 60s")
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
async def scan_project() -> str:
    """
    Return a full list of all assets in the project (grouped by category).
    """
    return await _run(lambda: _fmt(_parser().scan_project()), "scan_project")

@mcp.tool()
async def list_assets(category: str) -> str:
    """
    List all assets in a specific category.

    Args:
        category: Assets category (e.g. 'Objects', 'Scripts', 'Sprites', 'Rooms')
    """
    return await _run(lambda: _fmt(_parser().list_assets(category)), "list_assets")

@mcp.tool()
async def get_gml_content(file_path: str) -> str:
    """
    Read the code (GML) from a file.

    Args:
        file_path: Path to the .gml file relative to project root.
    """
    return await _run(lambda: _fmt(_parser().get_gml_content(file_path)), "get_gml_content")

@mcp.tool()
async def get_object_info(name: str) -> str:
    """
    Get GMS2 Object metadata: sprite, parent, and all its events/files.

    Args:
        name: Name of the object.
    """
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
    return await _run(lambda: _fmt(_parser().get_sprite_info(name)), "get_sprite_info")

@mcp.tool()
async def search_in_project(
    query: str,
    case_sensitive: bool = False,
    category: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """
    Search for text in all GML files.

    Args:
        query: Text to search for.
        case_sensitive: Whether the search is case-sensitive.
        category: Optional filter by asset category (e.g. 'Objects').
        max_results: Maximum number of matches to return.
    """
    return await _run(
        lambda: _fmt(_parser().search_in_project(query, case_sensitive, category, max_results)),
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
    return await _run(lambda: _fmt(_parser().diff_gml_file(file_path)), "diff_gml_file")

@mcp.tool()
async def get_shader_info(name: str) -> str:
    """
    Get shader code (vsh/fsh) and its uniform variables.

    Args:
        name: Name of the shader.
    """
    return await _run(lambda: _fmt(_parser().get_shader_info(name)), "get_shader_info")

if __name__ == "__main__":
    project_path = os.environ.get("GMS2_PROJECT_PATH", "<not set>")
    logger.info("GMS2 MCP Server starting...")
    logger.info(f"Project path: {project_path}")
    logger.info("UI Args Restored. Concurrency: Sema(1).")
    mcp.run()
