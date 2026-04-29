from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


MODEL_PROVIDER_PATTERN = re.compile(
    r'^\s*model_provider\s*=\s*["\'](?P<provider>[^"\']+)["\']\s*$',
    re.MULTILINE,
)
USER_REQUEST_MARKER = "## My request for Codex:"
SESSION_INDEX_PATH = Path.home() / ".codex" / "session_index.jsonl"
POSIX_INLINE_PATH_PATTERN = re.compile(r"/Users/[^,，;；\n]+")
WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"^[A-Za-z]:\\")
WINDOWS_INLINE_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^,，;；\n]+")
_session_index_title_cache: dict[str, str] | None = None


def get_remote_session_list(temp_dir: str | Path) -> list[dict[str, str]]:
    temp_path = Path(temp_dir).expanduser()
    sessions: list[dict[str, str]] = []

    for jsonl_file in _iter_jsonl_files(temp_path):
        metadata = _read_session_metadata(jsonl_file, base_dir=temp_path)
        if metadata is None:
            continue
        sessions.append(metadata)

    sessions.sort(
        key=lambda item: _parse_timestamp(item["timestamp"]),
        reverse=True,
    )
    return sessions


def restore_sessions(
    session_ids: Iterable[str],
    temp_dir: str | Path,
    codex_dir: str | Path,
) -> list[Path]:
    requested_ids = _normalize_session_ids(session_ids)
    if not requested_ids:
        return []

    temp_path = Path(temp_dir).expanduser()
    codex_path = Path(codex_dir).expanduser()
    session_file_map = _build_session_file_map(temp_path)

    missing_ids = [session_id for session_id in requested_ids if session_id not in session_file_map]
    if missing_ids:
        raise FileNotFoundError(
            f"Session file(s) not found for id(s): {', '.join(missing_ids)}"
        )

    codex_path.mkdir(parents=True, exist_ok=True)
    restored_files: list[Path] = []

    for session_id in requested_ids:
        source_file = session_file_map[session_id]
        relative_path = source_file.relative_to(temp_path)
        target_file = codex_path / relative_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        restored_files.append(target_file)

    return restored_files


def repair_local_session_providers(
    session_files: Iterable[str | Path],
    codex_dir: str | Path,
) -> dict[str, Any]:
    requested_files = _normalize_session_files(session_files)
    target_provider = _get_current_model_provider(codex_dir)
    if not requested_files:
        return {
            "provider": target_provider,
            "updated_files": [],
            "unchanged_files": [],
        }

    codex_path = Path(codex_dir).expanduser().resolve()
    updated_files: list[Path] = []
    unchanged_files: list[Path] = []

    for session_file in requested_files:
        session_path = Path(session_file).expanduser().resolve()
        if not session_path.exists():
            raise FileNotFoundError(f"Session file does not exist: {session_path}")

        try:
            session_path.relative_to(codex_path)
        except ValueError as exc:
            raise ValueError(f"Session file is not inside the local Codex directory: {session_path}") from exc

        if _rewrite_session_provider(session_path, target_provider):
            updated_files.append(session_path)
        else:
            unchanged_files.append(session_path)

    return {
        "provider": target_provider,
        "updated_files": updated_files,
        "unchanged_files": unchanged_files,
    }


def delete_local_session_files(
    session_files: Iterable[str | Path],
    codex_dir: str | Path,
) -> list[Path]:
    requested_files = _normalize_session_files(session_files)
    if not requested_files:
        return []

    codex_path = Path(codex_dir).expanduser().resolve()
    deleted_files: list[Path] = []

    for session_file in requested_files:
        session_path = Path(session_file).expanduser().resolve()
        if not session_path.exists():
            continue

        try:
            session_path.relative_to(codex_path)
        except ValueError as exc:
            raise ValueError(f"Session file is not inside the local Codex directory: {session_path}") from exc

        session_path.unlink()
        _remove_empty_parent_dirs(session_path.parent, codex_path)
        deleted_files.append(session_path)

    return deleted_files


def get_session_messages(session_file: str | Path) -> list[dict[str, str]]:
    session_path = Path(session_file).expanduser()
    messages: list[dict[str, str]] = []

    if not session_path.exists():
        raise FileNotFoundError(f"Session file does not exist: {session_path}")

    with session_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("type") != "response_item":
                continue

            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue

            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue

            content = _extract_message_content(payload.get("content", []), role)
            if not content or _should_skip_message(role, content):
                continue

            message: dict[str, str] = {
                "role": role,
                "content": content,
                "phase": str(payload.get("phase") or ""),
            }

            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                message["timestamp"] = timestamp

            messages.append(message)

    return _merge_consecutive_messages(messages)


def _iter_jsonl_files(base_dir: Path) -> Iterable[Path]:
    if not base_dir.exists():
        return []
    return sorted(base_dir.rglob("*.jsonl"))


def _read_session_metadata(
    jsonl_file: Path,
    base_dir: Path | None = None,
) -> dict[str, str] | None:
    try:
        with jsonl_file.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
            thread_name, inferred_title = _extract_session_labels(handle)
    except OSError:
        return None

    if not first_line:
        return None

    try:
        first_record = json.loads(first_line)
    except json.JSONDecodeError:
        return None

    if not isinstance(first_record, dict):
        return None

    payload = first_record.get("payload")
    if not isinstance(payload, dict):
        return None

    session_id = payload.get("id")
    timestamp = payload.get("timestamp")
    cwd = payload.get("cwd")

    if not isinstance(session_id, str) or not isinstance(timestamp, str) or not isinstance(cwd, str):
        return None

    session_title = (
        thread_name
        or _get_session_index_title(session_id)
        or inferred_title
    )

    metadata = {
        "id": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "provider": payload.get("model_provider", "") if isinstance(payload.get("model_provider"), str) else "",
        "thread_name": session_title,
    }
    metadata["file_name"] = jsonl_file.name
    metadata["file_path"] = str(jsonl_file)
    if base_dir is not None:
        try:
            metadata["relative_path"] = str(jsonl_file.relative_to(base_dir))
        except ValueError:
            metadata["relative_path"] = jsonl_file.name
    else:
        metadata["relative_path"] = jsonl_file.name
    return metadata


def _extract_session_labels(handle: Iterable[str]) -> tuple[str, str]:
    thread_name = ""
    inferred_title = ""

    for line in handle:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(record, dict):
            continue

        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        if payload.get("type") == "thread_name_updated":
            candidate = payload.get("thread_name")
            if isinstance(candidate, str) and candidate.strip():
                thread_name = candidate.strip()
            continue

        if inferred_title:
            continue

        if record.get("type") != "response_item":
            continue

        if payload.get("type") != "message" or payload.get("role") != "user":
            continue

        content = _extract_message_content(payload.get("content", []), "user")
        if not content or _should_skip_message("user", content):
            continue

        inferred_title = _derive_title_from_message(content)

    return thread_name, inferred_title


def _derive_title_from_message(content: str) -> str:
    lines = [line.strip() for line in content.splitlines()]
    next_line_is_request = False

    for line in lines:
        if not line:
            continue

        if line == USER_REQUEST_MARKER:
            next_line_is_request = True
            continue

        if line.startswith("# Files mentioned by the user:"):
            continue
        if line.startswith("## ") and ": /" in line:
            continue
        if line.startswith("/Users/") or WINDOWS_ABSOLUTE_PATH_PATTERN.match(line):
            continue
        if line == "[图片]":
            continue

        normalized_line = _normalize_title_line(line)
        if not normalized_line:
            continue

        if next_line_is_request:
            return _truncate_title(normalized_line)

        return _truncate_title(normalized_line)

    return ""


def _normalize_title_line(line: str) -> str:
    normalized_line = line.strip()
    if normalized_line.startswith("## "):
        normalized_line = normalized_line[3:].strip()
    elif normalized_line.startswith("# "):
        normalized_line = normalized_line[2:].strip()

    normalized_line = POSIX_INLINE_PATH_PATTERN.sub("", normalized_line)
    normalized_line = WINDOWS_INLINE_PATH_PATTERN.sub("", normalized_line)
    normalized_line = re.sub(r"https?://\S+", "", normalized_line)
    normalized_line = re.sub(r"路径为(?=[,，;；]|$)", "", normalized_line)
    normalized_line = re.sub(r"\s+", " ", normalized_line)
    return normalized_line.strip(" ,，;；")


def _truncate_title(title: str, max_length: int = 48) -> str:
    for delimiter in ("。", "！", "？", "；", ";", "，", ","):
        position = title.find(delimiter)
        if 8 <= position <= max_length:
            return title[:position].strip()

    if len(title) <= max_length:
        return title
    return f"{title[: max_length - 1].rstrip()}..."


def _get_session_index_title(session_id: str) -> str:
    session_index_map = _load_session_index_title_cache()
    return session_index_map.get(session_id, "")


def _load_session_index_title_cache() -> dict[str, str]:
    global _session_index_title_cache

    if _session_index_title_cache is not None:
        return _session_index_title_cache

    session_index_map: dict[str, str] = {}
    if not SESSION_INDEX_PATH.exists():
        _session_index_title_cache = session_index_map
        return session_index_map

    try:
        with SESSION_INDEX_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(record, dict):
                    continue

                session_id = record.get("id")
                thread_name = record.get("thread_name")
                if not isinstance(session_id, str) or not isinstance(thread_name, str):
                    continue

                cleaned_title = thread_name.strip()
                if cleaned_title:
                    session_index_map[session_id] = cleaned_title
    except OSError:
        pass

    _session_index_title_cache = session_index_map
    return session_index_map


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_session_ids(session_ids: Iterable[str]) -> list[str]:
    normalized_ids: list[str] = []
    seen: set[str] = set()

    for session_id in session_ids:
        cleaned_id = session_id.strip()
        if not cleaned_id or cleaned_id in seen:
            continue
        seen.add(cleaned_id)
        normalized_ids.append(cleaned_id)

    return normalized_ids


def _build_session_file_map(temp_path: Path) -> dict[str, Path]:
    session_entries: list[dict[str, Any]] = []

    for jsonl_file in _iter_jsonl_files(temp_path):
        metadata = _read_session_metadata(jsonl_file)
        if metadata is None:
            continue
        session_entries.append(
            {
                "id": metadata["id"],
                "timestamp": metadata["timestamp"],
                "path": jsonl_file,
            }
        )

    session_entries.sort(
        key=lambda item: _parse_timestamp(str(item["timestamp"])),
        reverse=True,
    )

    session_file_map: dict[str, Path] = {}
    for entry in session_entries:
        session_id = str(entry["id"])
        session_file_map.setdefault(session_id, Path(entry["path"]))

    return session_file_map


def _normalize_session_files(session_files: Iterable[str | Path]) -> list[Path]:
    normalized_files: list[Path] = []
    seen: set[Path] = set()

    for session_file in session_files:
        session_path = Path(session_file).expanduser().resolve()
        if session_path in seen:
            continue
        seen.add(session_path)
        normalized_files.append(session_path)

    return normalized_files


def _get_current_model_provider(codex_dir: str | Path) -> str:
    config_path = _resolve_codex_config_path(codex_dir)
    if not config_path.exists():
        raise FileNotFoundError(f"Codex config file does not exist: {config_path}")

    config_text = config_path.read_text(encoding="utf-8")
    match = MODEL_PROVIDER_PATTERN.search(config_text)
    if match is None:
        raise ValueError(f"Codex config does not define a valid model_provider: {config_path}")

    return match.group("provider").strip()


def _resolve_codex_config_path(codex_dir: str | Path) -> Path:
    codex_path = Path(codex_dir).expanduser().resolve()
    candidates = []

    if codex_path.name == "sessions":
        candidates.append(codex_path.parent / "config.toml")
    candidates.append(codex_path / "config.toml")
    candidates.append(Path.home() / ".codex" / "config.toml")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate

    return candidates[0]


def _rewrite_session_provider(session_path: Path, target_provider: str) -> bool:
    temp_path: Path | None = None

    try:
        with session_path.open("r", encoding="utf-8") as source_handle:
            first_line = source_handle.readline()
            if not first_line.strip():
                raise ValueError(f"Session file is empty: {session_path}")

            try:
                first_record = json.loads(first_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Session file has invalid JSON header: {session_path}") from exc

            if not isinstance(first_record, dict):
                raise ValueError(f"Session file has invalid session header: {session_path}")
            if first_record.get("type") != "session_meta":
                raise ValueError(f"Session file does not start with session_meta: {session_path}")

            payload = first_record.get("payload")
            if not isinstance(payload, dict):
                raise ValueError(f"Session file has invalid session payload: {session_path}")

            if payload.get("model_provider") == target_provider:
                return False

            payload["model_provider"] = target_provider
            newline = "\n" if first_line.endswith("\n") else ""
            updated_first_line = json.dumps(first_record, ensure_ascii=False, separators=(",", ":"))

            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=session_path.parent,
                delete=False,
            ) as temp_handle:
                temp_path = Path(temp_handle.name)
                temp_handle.write(updated_first_line)
                if newline:
                    temp_handle.write(newline)
                shutil.copyfileobj(source_handle, temp_handle)

        if temp_path is None:
            raise RuntimeError(f"Failed to create temporary file for: {session_path}")

        shutil.copystat(session_path, temp_path)
        temp_path.replace(session_path)
        return True
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _remove_empty_parent_dirs(start_dir: Path, stop_dir: Path) -> None:
    current_dir = start_dir
    stop_dir = stop_dir.resolve()

    while current_dir != stop_dir:
        try:
            current_dir.rmdir()
        except OSError:
            return
        current_dir = current_dir.parent


def _extract_message_content(content_items: Any, role: str) -> str:
    if not isinstance(content_items, list):
        return ""

    parts: list[str] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type in {"input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        elif role == "user" and item_type == "input_image":
            parts.append("[图片]")

    return "\n\n".join(parts).strip()


def _should_skip_message(role: str, content: str) -> bool:
    if role != "user":
        return False

    stripped = content.strip()
    ignored_prefixes = (
        "<environment_context>",
        "<turn_aborted>",
    )
    return any(stripped.startswith(prefix) for prefix in ignored_prefixes)


def _merge_consecutive_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if not messages:
        return []

    merged_messages: list[dict[str, str]] = [messages[0].copy()]

    for message in messages[1:]:
        previous = merged_messages[-1]
        if message["role"] == previous["role"] and message.get("phase", "") == previous.get("phase", ""):
            previous["content"] = f"{previous['content']}\n\n{message['content']}".strip()
            continue
        merged_messages.append(message.copy())

    return merged_messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse and restore Codex session JSONL files.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    list_parser = subparsers.add_parser("list", help="List sessions from the temp Git directory.")
    list_parser.add_argument("temp_dir", help="Temporary Git directory containing session .jsonl files.")

    restore_parser = subparsers.add_parser("restore", help="Restore selected sessions to the local Codex directory.")
    restore_parser.add_argument("temp_dir", help="Temporary Git directory containing session .jsonl files.")
    restore_parser.add_argument("codex_dir", help="Local Codex sessions directory.")
    restore_parser.add_argument("session_ids", nargs="+", help="One or more session IDs to restore.")

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()

    if args.action == "list":
        sessions = get_remote_session_list(args.temp_dir)
        print(json.dumps(sessions, indent=2, ensure_ascii=False))
    elif args.action == "restore":
        restored = restore_sessions(args.session_ids, args.temp_dir, args.codex_dir)
        for path in restored:
            print(path)
