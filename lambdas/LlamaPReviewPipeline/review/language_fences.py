"""Language tags used for safe Markdown code fences."""

from __future__ import annotations

from pathlib import PurePath


_SPECIAL_FILENAMES = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
}

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".h": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".md": "markdown",
    ".rst": "rst",
    ".toml": "toml",
    ".tf": "terraform",
}


def language_fence_for_path(filename: str) -> str:
    """Return a GitHub-compatible fence tag, or ``unknown``."""

    if not isinstance(filename, str) or not filename:
        return "unknown"
    path = PurePath(filename)
    special = _SPECIAL_FILENAMES.get(path.name.lower())
    if special:
        return special
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown")
