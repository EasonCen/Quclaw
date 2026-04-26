"""Built-in tool for agent capabilities"""

import asyncio
from tools.base import tool
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent import AgentSession


@tool(
    name="read",
    description="Read the contents of a text file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."}
        },
        "required": ["path"],
    },
)
async def read_file(path: str, session: "AgentSession") -> str:
    """Read and return the contents of a file at the given path."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied reading: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool(
    name="write",
    description="Write text to a file, optionally appending.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write."},
            "content": {"type": "string", "description": "Text content to write."},
            "append": {
                "type": "boolean",
                "description": "If true, append to the file instead of overwriting it.",
                "default": False,
            },
            "create_dirs": {
                "type": "boolean",
                "description": "If true, create parent directories when missing.",
                "default": True,
            },
        },
        "required": ["path", "content"],
    },
)
async def write_file(
    path: str,
    content: str,
    session: "AgentSession",
    append: bool = False,
    create_dirs: bool = True,
) -> str:
    """Write content to a file and return a status message."""
    try:
        file_path = Path(path)
        if create_dirs and not file_path.parent.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)

        if append:
            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)
            action = "Appended"
        else:
            file_path.write_text(content, encoding="utf-8")
            action = "Wrote"

        return f"{action} {len(content)} chars to {path}"
    except PermissionError:
        return f"Error: Permission denied writing: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool(
    name="edit",
    description="Edit a file by replacing text.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit."},
            "old_text": {
                "type": "string",
                "description": "Exact text to find in the file.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace all occurrences; otherwise replace only the first.",
                "default": False,
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
)
async def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    session: "AgentSession",
    replace_all: bool = False,
) -> str:
    """Replace text in a file and return a status message."""
    if old_text == "":
        return "Error editing file: old_text cannot be empty"

    try:
        file_path = Path(path)
        content = file_path.read_text(encoding="utf-8")
        occurrences = content.count(old_text)

        if occurrences == 0:
            return f"Error editing file: text not found in {path}"

        if replace_all:
            updated = content.replace(old_text, new_text)
            replaced = occurrences
        else:
            updated = content.replace(old_text, new_text, 1)
            replaced = 1

        file_path.write_text(updated, encoding="utf-8")
        return f"Edited {path}: replaced {replaced} occurrence(s)"
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied editing: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


@tool(
    name="bash",
    description="Run a shell command and return stdout/stderr.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds.",
                "default": 60,
            },
        },
        "required": ["command"],
    },
)
async def bash(
    command: str,
    session: "AgentSession",
    timeout: int = 60,
) -> str:
    """Execute a shell command asynchronously."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error running command: timed out after {timeout} seconds"

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        result = [f"Exit code: {proc.returncode}"]
        if out:
            result.append(f"STDOUT:\n{out}")
        if err:
            result.append(f"STDERR:\n{err}")
        if not out and not err:
            result.append("No output.")
        return "\n\n".join(result)
    except Exception as e:
        return f"Error running command: {e}"
