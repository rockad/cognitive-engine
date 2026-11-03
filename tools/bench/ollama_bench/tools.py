"""The six tool definitions (opencode-like) and a small JSON-schema validator for tool-call arguments."""
from __future__ import annotations

from typing import Any

TODO_ACTIONS = ["add", "complete", "list", "remove"]
WRITE_MODES = ["overwrite", "append"]


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


def tool_definitions(verbose: bool = False) -> list[dict[str, Any]]:
    """The six tools. `verbose` swaps in opencode-length descriptions (used to reach a ~4k-token step)."""
    d = (lambda short, long: long if verbose else short)
    return [
        _tool("read_file", d(
            "Read a text file in the workspace.",
            "Reads a file from the workspace filesystem. The path must be relative to the workspace root; absolute "
            "paths and paths containing '..' are rejected. By default it reads the whole file, up to 2000 lines. "
            "Use offset (1-based line number) and limit (number of lines) to read a section of a long file. Lines "
            "longer than 2000 characters are truncated. You can call this tool for several files in one turn. "
            "Always read a file before you change it, so that your edit matches what is really there. If the file "
            "does not exist the tool returns an error naming the path. Do not use it on directories: use list_dir."),
            {"path": {"type": "string", "description": "Relative path of the file inside the workspace"},
             "offset": {"type": "integer", "description": "1-based line number to start reading from"},
             "limit": {"type": "integer", "description": "Maximum number of lines to read"}}, ["path"]),
        _tool("write_file", d(
            "Create or overwrite a file, or append to it.",
            "Writes a file in the workspace, creating missing parent directories. mode 'overwrite' (the default) "
            "replaces the whole file; mode 'append' adds the content to the end of an existing file. Prefer "
            "edit_file for changing part of an existing file, because overwriting loses everything you do not "
            "repeat. The path must be relative to the workspace root. The content is written exactly as given: "
            "include a trailing newline when the file should end with one. Never write secrets or credentials."),
            {"path": {"type": "string", "description": "Relative path of the file inside the workspace"},
             "content": {"type": "string", "description": "The full text to write"},
             "mode": {"type": "string", "enum": WRITE_MODES, "description": "overwrite (default) or append"}},
            ["path", "content"]),
        _tool("edit_file", d(
            "Replace exact text in an existing file.",
            "Performs an exact string replacement in an existing file. old_string must match the file content "
            "exactly, including whitespace and indentation, and must be unique in the file unless replace_all is "
            "true; otherwise the edit fails and you must add surrounding context to make it unique. new_string "
            "replaces old_string. Use replace_all to rename a symbol everywhere in the file. Make the smallest "
            "change that does the job and leave every other line untouched."),
            {"path": {"type": "string", "description": "Relative path of the file inside the workspace"},
             "old_string": {"type": "string", "description": "The exact text to replace"},
             "new_string": {"type": "string", "description": "The replacement text"},
             "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
            ["path", "old_string", "new_string"]),
        _tool("list_dir", d(
            "List the entries of a directory.",
            "Lists the files and sub-directories of a directory in the workspace, one per line, with a trailing "
            "slash on directories. Use '.' for the workspace root. With recursive true the whole tree below the "
            "directory is listed (capped at 500 entries). Use it to discover where things are before reading or "
            "writing; do not guess file names."),
            {"path": {"type": "string", "description": "Relative directory path, '.' for the root"},
             "recursive": {"type": "boolean", "description": "List the whole tree below the directory"}}, ["path"]),
        _tool("bash", d(
            "Run a shell command in the workspace.",
            "Executes a shell command in the workspace root and returns its combined stdout and stderr and the exit "
            "code. Use it to run tests, builds, linters and git. Do not use it to read or write files when read_file, "
            "write_file or edit_file can do it. Quote paths that contain spaces. Never run destructive commands "
            "such as 'rm -rf' on directories you did not create, never touch paths outside the workspace, and "
            "never run commands that need an interactive terminal. Commands time out after 120 seconds unless "
            "timeout_seconds is given."),
            {"command": {"type": "string", "description": "The shell command to run"},
             "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 120)"}},
            ["command"]),
        _tool("todo", d(
            "Manage the task todo list.",
            "Manages the todo list for the current task. Use it to plan multi-step work and to track progress: "
            "add an item when you discover a step, complete it as soon as it is done, list to see the current "
            "items, remove to drop an item that is no longer needed. The item text identifies the entry. Keep "
            "items short and imperative."),
            {"action": {"type": "string", "enum": TODO_ACTIONS, "description": "What to do with the list"},
             "item": {"type": "string", "description": "The item text (not needed for list)"}}, ["action"]),
    ]


AGENT_TOOL_NAMES = ("read_file", "write_file", "edit_file", "list_dir")


def agent_tools() -> list[dict[str, Any]]:
    return [t for t in tool_definitions() if t["function"]["name"] in AGENT_TOOL_NAMES]


def schema_for(name: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for t in tools or tool_definitions():
        if t["function"]["name"] == name:
            return t["function"]["parameters"]
    return None


_JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate_args(name: str, args: Any, tools: list[dict[str, Any]] | None = None) -> list[str]:
    """Schema problems for one tool call (empty list = valid). Unknown tool, non-object arguments, missing
    required, unknown property, wrong type and out-of-enum value are all reported."""
    schema = schema_for(name, tools)
    if schema is None:
        return [f"unknown tool {name!r}"]
    if not isinstance(args, dict):
        return [f"arguments are not an object: {type(args).__name__}"]
    problems: list[str] = []
    props = schema["properties"]
    for req in schema.get("required", []):
        if req not in args:
            problems.append(f"missing required argument {req!r}")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            problems.append(f"unknown argument {key!r}")
            continue
        check = _JSON_TYPES.get(spec.get("type", ""))
        if check is not None and not check(value):
            problems.append(f"argument {key!r} must be {spec['type']}, got {type(value).__name__}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"argument {key!r} must be one of {spec['enum']}, got {value!r}")
    return problems
