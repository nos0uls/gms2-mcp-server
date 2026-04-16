# GMS2 MCP Server

MCP-сервер для работы с проектами GameMaker Studio 2 в AI-редакторах (Cursor, Windsurf/Antigravity, Claude Desktop).

## Быстрый старт

### 1. Сервер теперь находится в

```
C:\Users\n0souls\gms2-mcp-server\
```

(Перемещён из System32 — там нет прав на запись)

### 2. Конфиг для Windsurf / Antigravity

Открой **Settings → MCP Servers** и добавь:

```json
{
  "mcpServers": {
    "gms2-mcp": {
      "command": "C:/Users/n0souls/gms2-mcp-server/venv/Scripts/python.exe",
      "args": ["C:/Users/n0souls/gms2-mcp-server/mcp-serv/server.py"],
      "env": {
        "GMS2_PROJECT_PATH": "C:/path/to/your/gms2/project"
      }
    }
  }
}
```

### 3. Конфиг для Cursor

Файл уже создан: `.cursor/mcp.json` — поменяй только `GMS2_PROJECT_PATH`.

### 4. Конфиг для Claude Desktop

Файл: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "gms2-mcp": {
      "command": "C:/Users/n0souls/gms2-mcp-server/venv/Scripts/python.exe",
      "args": ["C:/Users/n0souls/gms2-mcp-server/mcp-serv/server.py"],
      "env": {
        "GMS2_PROJECT_PATH": "C:/path/to/your/gms2/project"
      }
    }
  }
}
```

> **Важно:** `GMS2_PROJECT_PATH` — путь к **папке** проекта (где лежит `.yyp` файл).  
> Используй прямые слэши `/` даже на Windows.

---

## 21 инструмент

### 📖 Чтение

| Инструмент | Описание | ~Токенов |
|:---|:---|:---|
| `get_project_summary` | Компактная сводка: имя + количество ассетов по категориям | ~100 |
| `scan_project` | Список ассетов с именами и количеством GML файлов | ~300 |
| `list_assets` | Плоский список ассетов с пагинацией | ~200 |
| `get_gml_content` | Содержимое GML файла (с поддержкой диапазона строк) | variable |
| `get_object_info` | Свойства объекта: спрайт, родитель, события (decoded), переменные | ~300 |
| `get_room_info` | Параметры комнаты: размеры, слои, скорость | ~200 |
| `get_sprite_info` | Метаданные спрайта: размеры, origin, bbox, кадры | ~150 |
| `get_shader_info` | GLSL код шейдера (.vsh + .fsh) и список uniforms | variable |
| `get_macro_constants` | Все `#macro` константы из GML файлов проекта | ~200 |

### 🔍 Анализ

| Инструмент | Описание |
|:---|:---|
| `find_asset_references` | Кто ссылается на ассет X — во всех GML и .yy файлах |
| `decode_object_events` | Числовые eventType/eventNum → человекочитаемые названия |
| `get_object_hierarchy` | Дерево наследования: родители + дети + события на каждом уровне |
| `get_room_instances` | Все экземпляры в комнате с точными (x, y) координатами |
| `get_gml_definitions_index` | Индекс: все функции, global-переменные, #macro в проекте |
| `validate_project` | Проверка целостности: битые ссылки, пустые файлы, ошибки парсинга |

### 🔎 Поиск

| Инструмент | Описание |
|:---|:---|
| `search_in_project` | Grep по всем GML файлам с фильтром по категории |

### ✏️ Запись

| Инструмент | Описание |
|:---|:---|
| `write_gml_file` | Запись GML файла с автоматическим `.bak` бэкапом |
| `create_script` | Создание нового Script-ассета (.yy + .gml) |
| `rename_asset` | Каскадное переименование: папка + .yy + .gml + все упоминания в GML |

### 📤 Экспорт / Diff

| Инструмент | Описание |
|:---|:---|
| `export_project_data` | Пагинированный экспорт (limit=5 файлов за раз, используй `next_offset`) |
| `diff_gml_file` | git diff GML файла против HEAD |

---

## Архитектура

```
gms2-mcp-server/
├── mcp-serv/
│   ├── server.py        ← FastMCP сервер, 21 инструмент
│   └── gms2_parser.py   ← CachedParser с TTL-кешем
├── venv/                ← Python virtual environment
├── requirements.txt     ← Зависимости (только mcp==1.11.0)
└── .cursor/mcp.json     ← Конфиг для Cursor IDE
```

**Ключевые решения:**
- **FastMCP** — декораторный API вместо 500 строк бойлерплейта
- **CachedParser** — синглтон с TTL 60с + инвалидация по mtime `.yyp` файла
- **Компактный вывод** — структурированный JSON без лишних полей
- **Пагинация** — `export_project_data` не выгружает всё сразу

---

## Требования

- Python 3.8+ (рекомендуется 3.10+, протестировано на 3.12)
- GameMaker Studio 2 (любая версия с .yyp проектами)

## Устранение проблем

**Сервер красный / не запускается:**
- Проверь что в конфиге стоит путь к `venv/Scripts/python.exe`, не к системному `python`
- Проверь что `GMS2_PROJECT_PATH` указывает на папку с `.yyp` файлом

**Инструменты не видны (0 tools):**
- Перезапусти IDE
- Проверь stderr лог сервера

**git diff не работает:**
- Убедись что git установлен и доступен в PATH
- Убедись что проект GMS2 находится под git-контролем
