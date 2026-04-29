from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from tkinter import filedialog, messagebox
from typing import Callable, Iterable

import customtkinter as ctk

from app_metadata import APP_NAME
from config import AppConfig, load_config, save_config
from session_parser import (
    delete_local_session_files,
    get_remote_session_list,
    get_session_messages,
    repair_local_session_providers,
    restore_sessions,
)
from sync_manager import SyncManager


WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
REMOTE_VIEW = "云端记录"
LOCAL_VIEW = "本地记录"


def normalize_local_path(path_value: str) -> str:
    cleaned_value = path_value.strip().strip('"').strip("'")
    if not cleaned_value:
        return ""
    expanded_value = os.path.expandvars(os.path.expanduser(cleaned_value))
    return os.path.normpath(expanded_value)


def format_session_timestamp(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_session_cwd(cwd: str) -> str:
    return format_display_path(cwd)


def format_display_path(path_value: str) -> str:
    cleaned_path = path_value.strip()
    if not cleaned_path:
        return ""
    if WINDOWS_PATH_PATTERN.match(cleaned_path) or "\\" in cleaned_path:
        return str(PureWindowsPath(cleaned_path))
    return str(PurePosixPath(cleaned_path))


def truncate_middle(text: str, max_length: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_length:
        return cleaned
    if max_length <= 3:
        return cleaned[:max_length]
    head_length = (max_length - 3) // 2
    tail_length = max_length - 3 - head_length
    return f"{cleaned[:head_length]}...{cleaned[-tail_length:]}"


def get_session_display_title(session: dict[str, str]) -> str:
    thread_name = session.get("thread_name", "").strip()
    if thread_name:
        return thread_name

    file_name = session.get("file_name", "").strip()
    if file_name:
        return file_name

    return session.get("id", "").strip() or "未命名会话"


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: "CodexMemoryApp",
        config: AppConfig,
        on_save: Callable[[AppConfig], None],
    ) -> None:
        super().__init__(master)
        self.title("设置")
        self.geometry("720x260")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._on_save = on_save

        self.grid_columnconfigure(1, weight=1)

        self.codex_session_var = tk.StringVar(value=config.codex_session_dir)
        self.git_remote_var = tk.StringVar(value=config.git_remote_url)
        self.local_sync_var = tk.StringVar(value=config.local_sync_temp_dir)

        title_label = ctk.CTkLabel(
            self,
            text="同步设置",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title_label.grid(row=0, column=0, columnspan=3, padx=20, pady=(20, 16), sticky="w")

        self._build_path_row(
            row=1,
            label_text="Codex 会话目录",
            variable=self.codex_session_var,
            browse_command=lambda: self._browse_directory(self.codex_session_var),
        )
        self._build_text_row(row=2, label_text="Git 远程仓库", variable=self.git_remote_var)
        self._build_path_row(
            row=3,
            label_text="本地同步临时目录",
            variable=self.local_sync_var,
            browse_command=lambda: self._browse_directory(self.local_sync_var),
        )

        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.grid(row=4, column=0, columnspan=3, padx=20, pady=(16, 20), sticky="e")

        cancel_button = ctk.CTkButton(button_frame, text="取消", width=90, command=self.destroy)
        cancel_button.pack(side="right")

        save_button = ctk.CTkButton(button_frame, text="保存", width=90, command=self._save)
        save_button.pack(side="right", padx=(0, 10))

    def _build_path_row(
        self,
        row: int,
        label_text: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None],
    ) -> None:
        label = ctk.CTkLabel(self, text=label_text)
        label.grid(row=row, column=0, padx=(20, 12), pady=8, sticky="w")

        entry = ctk.CTkEntry(self, textvariable=variable)
        entry.grid(row=row, column=1, padx=(0, 12), pady=8, sticky="ew")

        button = ctk.CTkButton(self, text="浏览", width=72, command=browse_command)
        button.grid(row=row, column=2, padx=(0, 20), pady=8)

    def _build_text_row(self, row: int, label_text: str, variable: tk.StringVar) -> None:
        label = ctk.CTkLabel(self, text=label_text)
        label.grid(row=row, column=0, padx=(20, 12), pady=8, sticky="w")

        entry = ctk.CTkEntry(self, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=2, padx=(0, 20), pady=8, sticky="ew")

    def _browse_directory(self, target_var: tk.StringVar) -> None:
        initial_dir = normalize_local_path(target_var.get()) or os.path.expanduser("~")
        selected_dir = filedialog.askdirectory(parent=self, initialdir=initial_dir)
        if selected_dir:
            target_var.set(normalize_local_path(selected_dir))

    def _save(self) -> None:
        codex_session_dir = normalize_local_path(self.codex_session_var.get())
        local_sync_temp_dir = normalize_local_path(self.local_sync_var.get())
        git_remote_url = self.git_remote_var.get().strip()

        if not codex_session_dir:
            messagebox.showerror("保存失败", "请填写 Codex 会话目录。", parent=self)
            return

        if not local_sync_temp_dir:
            messagebox.showerror("保存失败", "请填写本地同步临时目录。", parent=self)
            return

        new_config = AppConfig(
            codex_session_dir=codex_session_dir,
            git_remote_url=git_remote_url,
            local_sync_temp_dir=local_sync_temp_dir,
        )
        self._on_save(new_config)
        self.destroy()


class CodexMemoryApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title(APP_NAME)
        self.geometry("1120x720")
        self.minsize(960, 620)

        self.app_config = load_config()
        self.sync_manager = SyncManager(self.app_config)
        self.session_state: dict[str, tk.BooleanVar] = {}
        self.remote_session_state: dict[str, bool] = {}
        self.remote_group_checkbox_vars: dict[str, tk.BooleanVar] = {}
        self.remote_group_members: dict[str, list[str]] = {}
        self.remote_group_expanded_state: dict[str, bool] = {}
        self.all_remote_sessions: list[dict[str, str]] = []
        self.remote_row_frames: dict[str, ctk.CTkFrame] = {}
        self.selected_remote_session_path: str | None = None
        self.local_session_state: dict[str, bool] = {}
        self.local_checkbox_vars: dict[str, tk.BooleanVar] = {}
        self.local_group_checkbox_vars: dict[str, tk.BooleanVar] = {}
        self.local_group_members: dict[str, list[str]] = {}
        self.local_group_expanded_state: dict[str, bool] = {}
        self.current_sessions: list[dict[str, str]] = []
        self.all_local_sessions: list[dict[str, str]] = []
        self.local_row_frames: dict[str, ctk.CTkFrame] = {}
        self.session_messages_cache: dict[str, list[dict[str, str]]] = {}
        self.selected_local_session_path: str | None = None
        self.current_detail_session: dict[str, str] | None = None
        self.current_detail_messages: list[dict[str, str]] | None = None
        self.current_detail_placeholder: tuple[str, str] | None = None
        self.detail_resize_after_id: str | None = None
        self.current_view = REMOTE_VIEW
        self.is_busy = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="准备就绪")
        self.hint_var = tk.StringVar()
        self.detail_title_var = tk.StringVar(value="本地会话详情")
        self.detail_meta_var = tk.StringVar(value="切换到“本地记录”后，点击左侧 .jsonl 文件查看内容。")
        self.local_search_var = tk.StringVar()
        self.local_count_var = tk.StringVar(value="")

        self._build_sidebar()
        self._build_main_area()
        self._update_view_state()

        self.after(150, self.refresh_current_sessions)

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(4, weight=1)

        title_label = ctk.CTkLabel(
            sidebar,
            text="Codex Memory",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        title_label.grid(row=0, column=0, padx=20, pady=(24, 8), sticky="w")

        subtitle_label = ctk.CTkLabel(
            sidebar,
            text="会话同步桌面客户端",
            text_color=("gray35", "gray70"),
        )
        subtitle_label.grid(row=1, column=0, padx=20, pady=(0, 24), sticky="w")

        self.settings_button = ctk.CTkButton(sidebar, text="设置", command=self.open_settings_dialog)
        self.settings_button.grid(row=2, column=0, padx=20, pady=(0, 12), sticky="ew")

        self.push_button = ctk.CTkButton(
            sidebar,
            text="推送本机记录到云端",
            command=self.push_local_sessions,
        )
        self.push_button.grid(row=3, column=0, padx=20, pady=(0, 12), sticky="ew")

        self.status_label = ctk.CTkLabel(
            sidebar,
            textvariable=self.status_var,
            justify="left",
            wraplength=180,
            text_color=("gray35", "gray70"),
        )
        self.status_label.grid(row=5, column=0, padx=20, pady=(0, 24), sticky="sw")

    def _build_main_area(self) -> None:
        main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main_frame.grid(row=0, column=1, sticky="nsew")
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(1, weight=1)

        header_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=24, pady=(24, 12), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            header_frame,
            text="会话记录",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title_label.grid(row=0, column=0, sticky="w")

        self.source_switch = ctk.CTkSegmentedButton(
            header_frame,
            values=[REMOTE_VIEW, LOCAL_VIEW],
            command=self._on_view_changed,
        )
        self.source_switch.grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.source_switch.set(REMOTE_VIEW)

        self.refresh_button = ctk.CTkButton(
            header_frame,
            text="刷新当前列表",
            width=120,
            command=self.refresh_current_sessions,
        )
        self.refresh_button.grid(row=0, column=2, padx=(12, 0), sticky="e")

        self.hint_label = ctk.CTkLabel(
            header_frame,
            textvariable=self.hint_var,
            text_color=("gray35", "gray70"),
        )
        self.hint_label.grid(row=1, column=0, columnspan=3, pady=(6, 0), sticky="w")

        self.local_toolbar = ctk.CTkFrame(header_frame, fg_color="transparent")
        self.local_toolbar.grid(row=2, column=0, columnspan=3, pady=(10, 0), sticky="ew")
        self.local_toolbar.grid_columnconfigure(1, weight=1)

        self.local_count_label = ctk.CTkLabel(
            self.local_toolbar,
            textvariable=self.local_count_var,
            text_color=("gray35", "gray70"),
        )
        self.local_count_label.grid(row=0, column=0, sticky="w")

        self.local_search_entry = ctk.CTkEntry(
            self.local_toolbar,
            textvariable=self.local_search_var,
            placeholder_text="搜索标题、文件路径或工作目录",
            width=280,
        )
        self.local_search_entry.grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.local_search_entry.bind("<KeyRelease>", lambda _event: self._apply_local_filter())

        self.content_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        self.content_frame.grid(row=1, column=0, padx=24, pady=(0, 12), sticky="nsew")
        self.content_frame.grid_rowconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(0, weight=5, minsize=360)
        self.content_frame.grid_columnconfigure(1, weight=6, minsize=420)

        self.list_panel = ctk.CTkFrame(self.content_frame, corner_radius=12)
        self.list_panel.grid(row=0, column=0, padx=(0, 0), sticky="nsew")
        self.list_panel.grid_rowconfigure(0, weight=1)
        self.list_panel.grid_columnconfigure(0, weight=1)

        self.list_canvas = tk.Canvas(
            self.list_panel,
            highlightthickness=0,
            bd=0,
            bg=self._apply_appearance_mode(self.list_panel.cget("fg_color")),
        )
        self.list_canvas.grid(row=0, column=0, padx=(14, 0), pady=14, sticky="nsew")

        self.list_scrollbar = ctk.CTkScrollbar(
            self.list_panel,
            orientation="vertical",
            command=self.list_canvas.yview,
        )
        self.list_scrollbar.grid(row=0, column=1, padx=(10, 14), pady=14, sticky="ns")
        self.list_canvas.configure(yscrollcommand=self.list_scrollbar.set)

        self.list_frame = ctk.CTkFrame(self.list_canvas, fg_color="transparent")
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.list_canvas_window = self.list_canvas.create_window(
            (0, 0),
            window=self.list_frame,
            anchor="nw",
        )
        self.list_frame.bind("<Configure>", lambda _event: self._refresh_list_scrollregion())
        self.list_canvas.bind("<Configure>", self._fit_list_frame_width)

        self.detail_panel = ctk.CTkFrame(self.content_frame)
        self.detail_panel.grid(row=0, column=1, padx=(12, 0), sticky="nsew")
        self.detail_panel.grid_rowconfigure(1, weight=1)
        self.detail_panel.grid_columnconfigure(0, weight=1)

        self.detail_header = ctk.CTkFrame(self.detail_panel, fg_color="transparent")
        self.detail_header.grid(row=0, column=0, padx=18, pady=(18, 12), sticky="ew")
        self.detail_header.grid_columnconfigure(0, weight=1)

        self.detail_title_label = ctk.CTkLabel(
            self.detail_header,
            textvariable=self.detail_title_var,
            font=ctk.CTkFont(size=18, weight="bold"),
            justify="left",
            anchor="w",
            wraplength=480,
        )
        self.detail_title_label.grid(row=0, column=0, sticky="ew")

        self.detail_meta_label = ctk.CTkLabel(
            self.detail_header,
            textvariable=self.detail_meta_var,
            justify="left",
            anchor="w",
            wraplength=480,
            text_color=("gray35", "gray70"),
        )
        self.detail_meta_label.grid(row=1, column=0, pady=(8, 0), sticky="ew")

        self.detail_messages_frame = ctk.CTkScrollableFrame(
            self.detail_panel,
            corner_radius=12,
            fg_color="transparent",
        )
        self.detail_messages_frame.grid(row=1, column=0, padx=18, pady=(0, 18), sticky="nsew")
        self.detail_messages_frame.grid_columnconfigure(0, weight=1)
        self.detail_panel.bind("<Configure>", self._on_detail_panel_configure)
        self.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")

        bottom_frame = ctk.CTkFrame(main_frame)
        bottom_frame.grid(row=2, column=0, padx=24, pady=(0, 24), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)
        bottom_frame.grid_columnconfigure(1, weight=1)

        self.restore_button = ctk.CTkButton(
            bottom_frame,
            text="恢复选中的会话到本机",
            height=42,
            command=self.restore_selected_sessions,
        )
        self.restore_button.grid(row=0, column=0, padx=(16, 8), pady=16, sticky="ew")

        self.delete_local_button = ctk.CTkButton(
            bottom_frame,
            text="删除选中的本地文件",
            height=42,
            fg_color=("#dc2626", "#b91c1c"),
            hover_color=("#b91c1c", "#991b1b"),
            command=self.delete_selected_local_sessions,
        )
        self.delete_local_button.grid(row=0, column=1, padx=(8, 16), pady=16, sticky="ew")

        self._render_session_list([])

    def open_settings_dialog(self) -> None:
        SettingsDialog(self, self.app_config, self._save_settings)

    def _save_settings(self, new_config: AppConfig) -> None:
        save_config(new_config)
        self.app_config = new_config
        self.sync_manager = SyncManager(new_config)
        self.all_remote_sessions = []
        self.remote_session_state = {}
        self.remote_group_checkbox_vars = {}
        self.remote_group_members = {}
        self.remote_group_expanded_state = {}
        self.remote_row_frames = {}
        self.selected_remote_session_path = None
        self.all_local_sessions = []
        self.local_session_state = {}
        self.local_checkbox_vars = {}
        self.local_group_checkbox_vars = {}
        self.local_group_members = {}
        self.local_group_expanded_state = {}
        self.session_messages_cache = {}
        self.selected_local_session_path = None
        self.current_detail_session = None
        self.current_detail_messages = None
        self.current_detail_placeholder = None
        self.status_var.set("设置已保存，正在刷新会话记录...")
        self.refresh_current_sessions()

    def _on_view_changed(self, selected_view: str) -> None:
        self.current_view = selected_view
        self._update_view_state()
        self.refresh_current_sessions()

    def refresh_current_sessions(self) -> None:
        if self.current_view == LOCAL_VIEW:
            self.refresh_local_sessions()
            return
        self.refresh_remote_sessions()

    def refresh_remote_sessions(self) -> None:
        def task() -> list[dict[str, str]]:
            if self.app_config.git_remote_url.strip():
                self.sync_manager.pull_sessions()
            else:
                self.sync_manager.init_repo()
            return get_remote_session_list(self.app_config.local_sync_temp_dir)

        self._run_task(
            status_text="正在加载云端会话记录...",
            task=task,
            on_success=self._on_sessions_loaded,
            on_error=self._on_task_error,
        )

    def refresh_local_sessions(self) -> None:
        def task() -> list[dict[str, str]]:
            return get_remote_session_list(self.app_config.codex_session_dir)

        self._run_task(
            status_text="正在加载本机会话记录...",
            task=task,
            on_success=self._on_sessions_loaded,
            on_error=self._on_task_error,
        )

    def push_local_sessions(self) -> None:
        if not self.app_config.git_remote_url.strip():
            messagebox.showerror("无法推送", "请先在设置中填写 Git 远程仓库地址。", parent=self)
            return

        def task() -> int:
            pushed_files = self.sync_manager.push_sessions()
            return len(pushed_files)

        self._run_task(
            status_text="正在推送本机会话记录...",
            task=task,
            on_success=self._on_push_success,
            on_error=self._on_task_error,
        )

    def restore_selected_sessions(self) -> None:
        if self.current_view == LOCAL_VIEW:
            self.repair_selected_local_sessions()
            return

        selected_ids = [
            session_id
            for session_id, is_checked in self.remote_session_state.items()
            if is_checked
        ]
        if not selected_ids:
            messagebox.showwarning("未选择会话", "请先勾选至少一个会话。", parent=self)
            return

        def task() -> int:
            restored_files = restore_sessions(
                session_ids=selected_ids,
                temp_dir=self.app_config.local_sync_temp_dir,
                codex_dir=self.app_config.codex_session_dir,
            )
            return len(restored_files)

        self._run_task(
            status_text="正在恢复选中的会话...",
            task=task,
            on_success=self._on_restore_success,
            on_error=self._on_task_error,
        )

    def repair_selected_local_sessions(self) -> None:
        selected_files = [
            file_path
            for file_path, is_checked in self.local_session_state.items()
            if is_checked
        ]
        if not selected_files:
            messagebox.showwarning("未选择文件", "请先勾选至少一个本地 .jsonl 文件。", parent=self)
            return

        def task() -> dict[str, object]:
            return repair_local_session_providers(
                session_files=selected_files,
                codex_dir=self.app_config.codex_session_dir,
            )

        self._run_task(
            status_text="正在修正选中的本地会话...",
            task=task,
            on_success=self._on_local_repair_success,
            on_error=self._on_task_error,
        )

    def delete_selected_local_sessions(self) -> None:
        if self.current_view != LOCAL_VIEW:
            return

        selected_files = [
            file_path
            for file_path, is_checked in self.local_session_state.items()
            if is_checked
        ]
        if not selected_files:
            messagebox.showwarning("未选择文件", "请先勾选至少一个本地 .jsonl 文件。", parent=self)
            return

        should_delete = messagebox.askyesno(
            "确认删除",
            f"确认删除选中的 {len(selected_files)} 个本地 .jsonl 文件吗？\n此操作不可撤销。",
            parent=self,
        )
        if not should_delete:
            return

        def task() -> int:
            deleted_files = delete_local_session_files(
                session_files=selected_files,
                codex_dir=self.app_config.codex_session_dir,
            )
            return len(deleted_files)

        self._run_task(
            status_text="正在删除选中的本地文件...",
            task=task,
            on_success=self._on_local_delete_success,
            on_error=self._on_task_error,
        )

    def _on_sessions_loaded(self, sessions: list[dict[str, str]]) -> None:
        if self.current_view == LOCAL_VIEW:
            available_paths = {
                session.get("file_path", "")
                for session in sessions
                if session.get("file_path", "")
            }
            available_providers = {
                self._get_session_provider(session)
                for session in sessions
            }
            self.local_session_state = {
                file_path: is_checked
                for file_path, is_checked in self.local_session_state.items()
                if file_path in available_paths
            }
            self.local_group_expanded_state = {
                provider: is_expanded
                for provider, is_expanded in self.local_group_expanded_state.items()
                if provider in available_providers
            }
            for provider in available_providers:
                self.local_group_expanded_state.setdefault(provider, False)
            self.all_local_sessions = sessions
            filtered_sessions = self._filter_local_sessions(sessions)
            self._render_session_list(filtered_sessions)
            self._update_local_count_label(len(filtered_sessions), len(sessions))
            self.status_var.set(f"已加载 {len(sessions)} 条本地会话记录")
            return

        available_ids = {
            session.get("id", "")
            for session in sessions
            if session.get("id", "")
        }
        available_providers = {
            self._get_session_provider(session)
            for session in sessions
        }
        self.remote_session_state = {
            session_id: is_checked
            for session_id, is_checked in self.remote_session_state.items()
            if session_id in available_ids
        }
        self.remote_group_expanded_state = {
            provider: is_expanded
            for provider, is_expanded in self.remote_group_expanded_state.items()
            if provider in available_providers
        }
        for provider in available_providers:
            self.remote_group_expanded_state.setdefault(provider, False)
        self.all_remote_sessions = sessions
        self._render_session_list(sessions)
        self.status_var.set(f"已加载 {len(sessions)} 条云端会话记录")

    def _on_push_success(self, pushed_count: int) -> None:
        self.status_var.set(f"推送完成，共处理 {pushed_count} 个会话文件")
        messagebox.showinfo(
            "推送完成",
            f"推送流程已完成，共处理 {pushed_count} 个会话文件。",
            parent=self,
        )
        self.refresh_current_sessions()

    def _on_restore_success(self, restored_count: int) -> None:
        self.status_var.set(f"已恢复 {restored_count} 个会话文件到本机")
        messagebox.showinfo(
            "恢复完成",
            f"已恢复 {restored_count} 个会话文件到本机，请重启 Codex。",
            parent=self,
        )

    def _on_local_repair_success(self, result: dict[str, object]) -> None:
        provider = str(result.get("provider") or "")
        updated_files = list(result.get("updated_files") or [])
        unchanged_files = list(result.get("unchanged_files") or [])
        updated_count = len(updated_files)
        unchanged_count = len(unchanged_files)

        for file_path, is_checked in list(self.local_session_state.items()):
            if is_checked:
                self.local_session_state[file_path] = False

        if updated_files:
            self._apply_local_provider_updates(updated_files, provider)

        if updated_count:
            self.status_var.set(f"已将 {updated_count} 个本地会话修正为当前 Provider：{provider}")
        else:
            self.status_var.set(f"选中的本地会话已是当前 Provider：{provider}")

        if updated_count and unchanged_count:
            message = (
                f"已将 {updated_count} 个本地会话修正为当前 Provider：{provider}。\n"
                f"另有 {unchanged_count} 个文件本来就是当前 Provider。"
            )
        elif updated_count:
            message = f"已将 {updated_count} 个本地会话修正为当前 Provider：{provider}。"
        else:
            message = f"选中的 {unchanged_count} 个本地会话本来就是当前 Provider：{provider}。"

        messagebox.showinfo("修正完成", f"{message}\n请重启 Codex。", parent=self)
        self.refresh_local_sessions()

    def _on_local_delete_success(self, deleted_count: int) -> None:
        deleted_paths = [
            file_path
            for file_path, is_checked in list(self.local_session_state.items())
            if is_checked
        ]
        deleted_path_set = {
            str(Path(file_path).expanduser().resolve())
            for file_path in deleted_paths
        }

        self.local_session_state = {
            file_path: is_checked
            for file_path, is_checked in self.local_session_state.items()
            if str(Path(file_path).expanduser().resolve()) not in deleted_path_set
        }
        self.all_local_sessions = [
            session
            for session in self.all_local_sessions
            if str(Path(session.get("file_path", "")).expanduser().resolve()) not in deleted_path_set
        ]

        for deleted_path in list(self.session_messages_cache.keys()):
            if str(Path(deleted_path).expanduser().resolve()) in deleted_path_set:
                self.session_messages_cache.pop(deleted_path, None)

        if self.selected_local_session_path and (
            str(Path(self.selected_local_session_path).expanduser().resolve()) in deleted_path_set
        ):
            self.selected_local_session_path = None
            self.current_detail_session = None
            self.current_detail_messages = None

        filtered_sessions = self._filter_local_sessions(self.all_local_sessions)
        self._render_session_list(filtered_sessions)
        self._update_local_count_label(len(filtered_sessions), len(self.all_local_sessions))

        self.status_var.set(f"已删除 {deleted_count} 个本地 .jsonl 文件")
        messagebox.showinfo(
            "删除完成",
            f"已删除 {deleted_count} 个本地 .jsonl 文件。",
            parent=self,
        )
        self.refresh_local_sessions()

    def _apply_local_provider_updates(self, updated_files: Iterable[Path], provider: str) -> None:
        updated_paths = {str(path.resolve()) for path in updated_files}

        for session in self.all_local_sessions:
            file_path = session.get("file_path", "")
            if not file_path:
                continue
            if str(Path(file_path).expanduser().resolve()) in updated_paths:
                session["provider"] = provider

        filtered_sessions = self._filter_local_sessions(self.all_local_sessions)
        self._render_session_list(filtered_sessions)
        self._update_local_count_label(len(filtered_sessions), len(self.all_local_sessions))

    def _on_task_error(self, error: Exception) -> None:
        self.status_var.set("操作失败")
        messagebox.showerror("操作失败", str(error), parent=self)

    def _render_session_list(self, sessions: list[dict[str, str]]) -> None:
        self.current_sessions = sessions
        self.session_state = {}
        self.remote_group_checkbox_vars = {}
        self.remote_group_members = {}
        self.remote_row_frames = {}
        self.local_checkbox_vars = {}
        self.local_group_checkbox_vars = {}
        self.local_group_members = {}
        self.local_row_frames = {}

        for child in self.list_frame.winfo_children():
            child.destroy()

        if not sessions:
            if self.current_view == LOCAL_VIEW:
                self.selected_local_session_path = None
                self._render_detail_placeholder(
                    "本地会话详情",
                    "当前没有可查看的本地会话内容。",
                )
            else:
                self.selected_remote_session_path = None
                self._render_detail_placeholder(
                    "云端会话详情",
                    "当前没有可查看的云端会话内容。",
                )
            empty_label = ctk.CTkLabel(
                self.list_frame,
                text=self._build_empty_state_message(),
                text_color=("gray35", "gray70"),
                justify="left",
                anchor="w",
                wraplength=760,
            )
            empty_label.grid(row=0, column=0, padx=16, pady=20, sticky="w")
            self._update_action_states()
            self.after_idle(self._refresh_list_scrollregion)
            return

        if self.current_view == LOCAL_VIEW:
            self._render_local_session_list(sessions)
            self._update_action_states()
            self.after_idle(self._refresh_list_scrollregion)
            return
        self._render_remote_session_list(sessions)
        self._update_action_states()
        self.after_idle(self._refresh_list_scrollregion)

    def _render_local_session_list(self, sessions: list[dict[str, str]]) -> None:
        row_wraplength = max(260, self.list_canvas.winfo_width() - 88)
        row_index = 0

        for provider, provider_sessions in self._group_sessions_by_provider(sessions):
            provider_paths = [
                session.get("file_path", "")
                for session in provider_sessions
                if session.get("file_path", "")
            ]
            self.local_group_members[provider] = provider_paths
            is_expanded = self.local_group_expanded_state.get(provider, False)

            group_frame = ctk.CTkFrame(self.list_frame, fg_color=("gray90", "gray18"))
            group_frame.grid(row=row_index, column=0, padx=8, pady=(8, 4), sticky="ew")
            group_frame.grid_columnconfigure(2, weight=1)

            toggle_button = ctk.CTkButton(
                group_frame,
                text="▾" if is_expanded else "▸",
                width=28,
                height=28,
                corner_radius=8,
                fg_color="transparent",
                text_color=("gray20", "gray85"),
                hover_color=("gray82", "gray24"),
                command=lambda current_provider=provider: self._toggle_local_group_expanded(current_provider),
            )
            toggle_button.grid(row=0, column=0, padx=(10, 6), pady=10, sticky="w")

            group_var = tk.BooleanVar(
                value=bool(provider_paths)
                and all(self.local_session_state.get(file_path, False) for file_path in provider_paths)
            )
            self.local_group_checkbox_vars[provider] = group_var

            group_checkbox = ctk.CTkCheckBox(
                group_frame,
                text="",
                variable=group_var,
                width=24,
                command=lambda current_provider=provider, current_var=group_var: self._toggle_local_provider_group(
                    current_provider,
                    current_var.get(),
                ),
            )
            group_checkbox.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="w")

            group_label = ctk.CTkLabel(
                group_frame,
                text=f"{provider} ({len(provider_sessions)})",
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            )
            group_label.grid(row=0, column=2, padx=(0, 14), pady=10, sticky="ew")
            self._bind_row_click(
                widgets=(group_frame, group_label),
                callback=lambda _event, current_provider=provider: self._toggle_local_group_expanded(
                    current_provider
                ),
            )

            row_index += 1

            if not is_expanded:
                continue

            for session in provider_sessions:
                file_path = session.get("file_path", "")
                row_frame = ctk.CTkFrame(self.list_frame)
                row_frame.grid(row=row_index, column=0, padx=8, pady=(0, 8), sticky="ew")
                row_frame.grid_columnconfigure(1, weight=1)
                self.local_row_frames[file_path] = row_frame

                checkbox_var = tk.BooleanVar(value=self.local_session_state.get(file_path, False))
                self.local_checkbox_vars[file_path] = checkbox_var
                checkbox = ctk.CTkCheckBox(
                    row_frame,
                    text="",
                    variable=checkbox_var,
                    width=24,
                    command=lambda current_path=file_path, current_var=checkbox_var, current_provider=provider: self._toggle_local_session(
                        current_path,
                        current_var.get(),
                        current_provider,
                    ),
                )
                checkbox.grid(row=0, column=0, rowspan=3, padx=(14, 10), pady=14, sticky="n")

                time_label = ctk.CTkLabel(
                    row_frame,
                    text=format_session_timestamp(session["timestamp"]),
                    font=ctk.CTkFont(size=13, weight="bold"),
                    anchor="w",
                    justify="left",
                    wraplength=row_wraplength,
                )
                time_label.grid(row=0, column=1, padx=(0, 14), pady=(12, 4), sticky="ew")

                title_label = ctk.CTkLabel(
                    row_frame,
                    text=get_session_display_title(session),
                    font=ctk.CTkFont(size=15, weight="bold"),
                    justify="left",
                    anchor="w",
                    wraplength=row_wraplength,
                )
                title_label.grid(row=1, column=1, padx=(0, 14), pady=(0, 4), sticky="ew")

                path_label = ctk.CTkLabel(
                    row_frame,
                    text=format_display_path(file_path),
                    text_color=("#1d4ed8", "#93c5fd"),
                    justify="left",
                    anchor="w",
                    wraplength=row_wraplength,
                    font=ctk.CTkFont(size=12),
                    cursor="hand2",
                )
                path_label.grid(row=2, column=1, padx=(0, 14), pady=(0, 12), sticky="ew")
                path_label.bind(
                    "<Button-1>",
                    lambda _event, current_path=file_path: self._open_session_directory(current_path),
                    add="+",
                )

                click_handler = lambda _event, current_session=session: self._select_local_session(current_session)
                self._bind_row_click(
                    widgets=(row_frame, time_label, title_label),
                    callback=click_handler,
                )
                row_index += 1

        if self.selected_local_session_path:
            selected_session = next(
                (
                    session
                    for session in sessions
                    if session.get("file_path", "") == self.selected_local_session_path
                ),
                None,
            )
            if selected_session is not None:
                self._select_local_session(selected_session)
                return

        self.selected_local_session_path = None
        self._update_local_row_styles()
        self._render_detail_placeholder(
            "本地会话详情",
            "展开左侧 Provider 分组后，点击具体 .jsonl 文件查看对话内容。",
        )

    def _render_remote_session_list(self, sessions: list[dict[str, str]]) -> None:
        row_wraplength = max(260, self.list_canvas.winfo_width() - 88)
        row_index = 0

        for provider, provider_sessions in self._group_sessions_by_provider(sessions):
            provider_ids = [
                session.get("id", "")
                for session in provider_sessions
                if session.get("id", "")
            ]
            self.remote_group_members[provider] = provider_ids
            is_expanded = self.remote_group_expanded_state.get(provider, False)

            group_frame = ctk.CTkFrame(self.list_frame, fg_color=("gray90", "gray18"))
            group_frame.grid(row=row_index, column=0, padx=8, pady=(8, 4), sticky="ew")
            group_frame.grid_columnconfigure(2, weight=1)

            toggle_button = ctk.CTkButton(
                group_frame,
                text="▾" if is_expanded else "▸",
                width=28,
                height=28,
                corner_radius=8,
                fg_color="transparent",
                text_color=("gray20", "gray85"),
                hover_color=("gray82", "gray24"),
                command=lambda current_provider=provider: self._toggle_remote_group_expanded(current_provider),
            )
            toggle_button.grid(row=0, column=0, padx=(10, 6), pady=10, sticky="w")

            group_var = tk.BooleanVar(
                value=bool(provider_ids)
                and all(self.remote_session_state.get(session_id, False) for session_id in provider_ids)
            )
            self.remote_group_checkbox_vars[provider] = group_var

            group_checkbox = ctk.CTkCheckBox(
                group_frame,
                text="",
                variable=group_var,
                width=24,
                command=lambda current_provider=provider, current_var=group_var: self._toggle_remote_provider_group(
                    current_provider,
                    current_var.get(),
                ),
            )
            group_checkbox.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="w")

            group_label = ctk.CTkLabel(
                group_frame,
                text=f"{provider} ({len(provider_sessions)})",
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            )
            group_label.grid(row=0, column=2, padx=(0, 14), pady=10, sticky="ew")
            self._bind_row_click(
                widgets=(group_frame, group_label),
                callback=lambda _event, current_provider=provider: self._toggle_remote_group_expanded(
                    current_provider
                ),
            )

            row_index += 1

            if not is_expanded:
                continue

            for session in provider_sessions:
                session_id = session.get("id", "")
                file_path = session.get("file_path", "")
                row_frame = ctk.CTkFrame(self.list_frame)
                row_frame.grid(row=row_index, column=0, padx=8, pady=(0, 8), sticky="ew")
                row_frame.grid_columnconfigure(1, weight=1)
                self.remote_row_frames[file_path] = row_frame

                checkbox_var = tk.BooleanVar(value=self.remote_session_state.get(session_id, False))
                self.session_state[session_id] = checkbox_var
                checkbox = ctk.CTkCheckBox(
                    row_frame,
                    text="",
                    variable=checkbox_var,
                    width=24,
                    command=lambda current_id=session_id, current_var=checkbox_var, current_provider=provider: self._toggle_remote_session(
                        current_id,
                        current_var.get(),
                        current_provider,
                    ),
                )
                checkbox.grid(row=0, column=0, rowspan=3, padx=(14, 10), pady=14, sticky="n")

                time_label = ctk.CTkLabel(
                    row_frame,
                    text=format_session_timestamp(session["timestamp"]),
                    font=ctk.CTkFont(size=13, weight="bold"),
                    anchor="w",
                    justify="left",
                    wraplength=row_wraplength,
                )
                time_label.grid(row=0, column=1, padx=(0, 14), pady=(12, 4), sticky="ew")

                title_label = ctk.CTkLabel(
                    row_frame,
                    text=get_session_display_title(session),
                    font=ctk.CTkFont(size=15, weight="bold"),
                    justify="left",
                    anchor="w",
                    wraplength=row_wraplength,
                )
                title_label.grid(row=1, column=1, padx=(0, 14), pady=(0, 4), sticky="ew")

                path_label = ctk.CTkLabel(
                    row_frame,
                    text=format_display_path(file_path),
                    text_color=("#1d4ed8", "#93c5fd"),
                    justify="left",
                    anchor="w",
                    wraplength=row_wraplength,
                    font=ctk.CTkFont(size=12),
                    cursor="hand2",
                )
                path_label.grid(row=2, column=1, padx=(0, 14), pady=(0, 12), sticky="ew")
                path_label.bind(
                    "<Button-1>",
                    lambda _event, current_path=file_path: self._open_session_directory(current_path),
                    add="+",
                )

                click_handler = lambda _event, current_session=session: self._select_remote_session(current_session)
                self._bind_row_click(
                    widgets=(row_frame, time_label, title_label),
                    callback=click_handler,
                )
                row_index += 1

        if self.selected_remote_session_path:
            selected_session = next(
                (
                    session
                    for session in sessions
                    if session.get("file_path", "") == self.selected_remote_session_path
                ),
                None,
            )
            if selected_session is not None:
                self._select_remote_session(selected_session)
                return

        self.selected_remote_session_path = None
        self._update_remote_row_styles()
        self._render_detail_placeholder(
            "云端会话详情",
            "展开左侧 Provider 分组后，点击具体会话查看对话内容。",
        )

    def _group_sessions_by_provider(
        self,
        sessions: list[dict[str, str]],
    ) -> list[tuple[str, list[dict[str, str]]]]:
        grouped_sessions: dict[str, list[dict[str, str]]] = {}

        for session in sessions:
            provider = self._get_session_provider(session)
            grouped_sessions.setdefault(provider, []).append(session)

        return list(grouped_sessions.items())

    def _toggle_local_group_expanded(self, provider: str) -> None:
        self.local_group_expanded_state[provider] = not self.local_group_expanded_state.get(provider, False)
        filtered_sessions = self._filter_local_sessions(self.all_local_sessions)
        self._render_session_list(filtered_sessions)
        self._update_local_count_label(len(filtered_sessions), len(self.all_local_sessions))

    def _toggle_remote_group_expanded(self, provider: str) -> None:
        self.remote_group_expanded_state[provider] = not self.remote_group_expanded_state.get(provider, False)
        self._render_session_list(self.all_remote_sessions)

    def _select_local_session(self, session: dict[str, str]) -> None:
        self.selected_local_session_path = session.get("file_path", "")
        self._update_local_row_styles()

        file_path = session.get("file_path", "")
        if file_path in self.session_messages_cache:
            messages = self.session_messages_cache[file_path]
        else:
            messages = get_session_messages(file_path)
            self.session_messages_cache[file_path] = messages

        self._render_session_detail(session, messages)

    def _select_remote_session(self, session: dict[str, str]) -> None:
        self.selected_remote_session_path = session.get("file_path", "")
        self._update_remote_row_styles()

        file_path = session.get("file_path", "")
        if file_path in self.session_messages_cache:
            messages = self.session_messages_cache[file_path]
        else:
            messages = get_session_messages(file_path)
            self.session_messages_cache[file_path] = messages

        self._render_session_detail(session, messages)

    def _toggle_local_session(self, file_path: str, is_checked: bool, provider: str) -> None:
        self.local_session_state[file_path] = is_checked
        self._update_local_group_checkbox_state(provider)
        self._update_action_states()

    def _toggle_remote_session(self, session_id: str, is_checked: bool, provider: str) -> None:
        self.remote_session_state[session_id] = is_checked
        self._update_remote_group_checkbox_state(provider)
        self._update_action_states()

    def _toggle_local_provider_group(self, provider: str, is_checked: bool) -> None:
        for file_path in self.local_group_members.get(provider, []):
            self.local_session_state[file_path] = is_checked
            checkbox_var = self.local_checkbox_vars.get(file_path)
            if checkbox_var is not None:
                checkbox_var.set(is_checked)

        group_var = self.local_group_checkbox_vars.get(provider)
        if group_var is not None:
            group_var.set(is_checked)

        self._update_action_states()

    def _toggle_remote_provider_group(self, provider: str, is_checked: bool) -> None:
        for session_id in self.remote_group_members.get(provider, []):
            self.remote_session_state[session_id] = is_checked
            checkbox_var = self.session_state.get(session_id)
            if checkbox_var is not None:
                checkbox_var.set(is_checked)

        group_var = self.remote_group_checkbox_vars.get(provider)
        if group_var is not None:
            group_var.set(is_checked)

        self._update_action_states()

    def _update_local_group_checkbox_state(self, provider: str) -> None:
        group_var = self.local_group_checkbox_vars.get(provider)
        group_paths = self.local_group_members.get(provider, [])
        if group_var is None or not group_paths:
            return
        group_var.set(all(self.local_session_state.get(file_path, False) for file_path in group_paths))

    def _update_remote_group_checkbox_state(self, provider: str) -> None:
        group_var = self.remote_group_checkbox_vars.get(provider)
        group_ids = self.remote_group_members.get(provider, [])
        if group_var is None or not group_ids:
            return
        group_var.set(all(self.remote_session_state.get(session_id, False) for session_id in group_ids))

    def _get_session_provider(self, session: dict[str, str]) -> str:
        provider = session.get("provider", "").strip()
        return provider or "未知 Provider"

    def _update_local_row_styles(self) -> None:
        for file_path, row_frame in self.local_row_frames.items():
            is_selected = file_path == self.selected_local_session_path
            row_frame.configure(
                fg_color=("gray82", "gray24") if is_selected else ("gray92", "gray16")
            )

    def _update_remote_row_styles(self) -> None:
        for file_path, row_frame in self.remote_row_frames.items():
            is_selected = file_path == self.selected_remote_session_path
            row_frame.configure(
                fg_color=("gray82", "gray24") if is_selected else ("gray92", "gray16")
            )

    def _filter_local_sessions(self, sessions: list[dict[str, str]]) -> list[dict[str, str]]:
        keyword = self.local_search_var.get().strip().lower()
        if not keyword:
            return sessions

        filtered_sessions: list[dict[str, str]] = []
        for session in sessions:
            haystacks = [
                session.get("thread_name", ""),
                session.get("file_name", ""),
                session.get("file_path", ""),
                session.get("relative_path", ""),
                session.get("cwd", ""),
                self._get_session_provider(session),
            ]
            combined = " ".join(haystacks).lower()
            if keyword in combined:
                filtered_sessions.append(session)
        return filtered_sessions

    def _apply_local_filter(self) -> None:
        if self.current_view != LOCAL_VIEW:
            return

        filtered_sessions = self._filter_local_sessions(self.all_local_sessions)
        self._render_session_list(filtered_sessions)
        self._update_local_count_label(len(filtered_sessions), len(self.all_local_sessions))

    def _update_local_count_label(self, visible_count: int, total_count: int) -> None:
        if self.current_view != LOCAL_VIEW:
            self.local_count_var.set("")
            return

        if visible_count == total_count:
            self.local_count_var.set(f"共 {total_count} 个 .jsonl 文件，左侧列表可滚动查看更多。")
            return

        self.local_count_var.set(
            f"当前显示 {visible_count} / {total_count} 个 .jsonl 文件。"
        )

    def _render_session_detail(
        self,
        session: dict[str, str],
        messages: list[dict[str, str]],
        reset_scroll: bool = True,
    ) -> None:
        self.current_detail_session = session
        self.current_detail_messages = messages
        self.current_detail_placeholder = None

        detail_title = get_session_display_title(session)
        detail_meta_parts = [
            format_session_timestamp(session.get("timestamp", "")),
            format_display_path(session.get("file_path", "")),
            format_session_cwd(session.get("cwd", "")),
        ]
        detail_meta = "\n".join(part for part in detail_meta_parts if part)
        self.detail_title_var.set(detail_title)
        self.detail_meta_var.set(detail_meta)
        self._update_detail_header_wraplengths()

        for child in self.detail_messages_frame.winfo_children():
            child.destroy()

        if not messages:
            empty_label = ctk.CTkLabel(
                self.detail_messages_frame,
                text="该会话文件中没有可展示的用户/助手文本消息。",
                justify="left",
                anchor="w",
                wraplength=self._get_detail_body_wraplength(),
                text_color=("gray35", "gray70"),
            )
            empty_label.grid(row=0, column=0, padx=12, pady=12, sticky="w")
            if reset_scroll:
                self.after_idle(self._scroll_detail_to_top)
            return

        bubble_wraplength = self._get_detail_bubble_wraplength()
        bubble_side_padding = self._get_detail_bubble_side_padding()
        for row_index, message in enumerate(messages):
            outer_frame = ctk.CTkFrame(self.detail_messages_frame, fg_color="transparent")
            outer_frame.grid(row=row_index, column=0, padx=4, pady=6, sticky="ew")
            outer_frame.grid_columnconfigure(0, weight=1)
            outer_frame.grid_columnconfigure(1, weight=1)

            is_user = message["role"] == "user"
            role_text = "你" if is_user else "Codex"
            if not is_user and message.get("phase") == "commentary":
                role_text = "Codex"

            bubble = ctk.CTkFrame(
                outer_frame,
                fg_color=("#dbeafe", "#1d4ed8") if is_user else ("gray88", "gray20"),
                corner_radius=14,
            )
            bubble.grid(
                row=0,
                column=1 if is_user else 0,
                padx=(bubble_side_padding, 0) if is_user else (0, bubble_side_padding),
                sticky="e" if is_user else "w",
            )

            role_label = ctk.CTkLabel(
                bubble,
                text=role_text,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=("#1e3a8a", "#dbeafe") if is_user else ("gray25", "gray80"),
            )
            role_label.grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")

            content_label = ctk.CTkLabel(
                bubble,
                text=message["content"],
                justify="left",
                anchor="w",
                wraplength=bubble_wraplength,
            )
            content_label.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")

        if reset_scroll:
            self.after_idle(self._scroll_detail_to_top)

    def _render_detail_placeholder(self, title: str, message: str, reset_scroll: bool = True) -> None:
        self.current_detail_session = None
        self.current_detail_messages = None
        self.current_detail_placeholder = (title, message)

        self.detail_title_var.set(title)
        self.detail_meta_var.set(message)
        self._update_detail_header_wraplengths()

        for child in self.detail_messages_frame.winfo_children():
            child.destroy()

        placeholder_label = ctk.CTkLabel(
            self.detail_messages_frame,
            text=message,
            justify="left",
            anchor="w",
            wraplength=self._get_detail_body_wraplength(),
            text_color=("gray35", "gray70"),
        )
        placeholder_label.grid(row=0, column=0, padx=12, pady=12, sticky="w")
        if reset_scroll:
            self.after_idle(self._scroll_detail_to_top)

    def _bind_row_click(
        self,
        widgets: Iterable[tk.Misc],
        callback: Callable[[tk.Event], None],
    ) -> None:
        for widget in widgets:
            widget.bind("<Button-1>", callback, add="+")

    def _open_session_directory(self, file_path: str) -> None:
        if not file_path.strip():
            messagebox.showerror("路径为空", "当前会话没有可打开的文件路径。", parent=self)
            return

        session_path = Path(file_path).expanduser()
        target_dir = session_path if session_path.is_dir() else session_path.parent

        if not target_dir.exists():
            messagebox.showerror(
                "目录不存在",
                f"找不到目录：\n{target_dir}",
                parent=self,
            )
            return

        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(target_dir)])
            elif os.name == "nt":
                os.startfile(str(target_dir))
            else:
                subprocess.Popen(["xdg-open", str(target_dir)])
        except OSError as exc:
            messagebox.showerror(
                "打开目录失败",
                f"无法打开目录：\n{target_dir}\n\n{exc}",
                parent=self,
            )

    def _on_detail_panel_configure(self, _event: tk.Event) -> None:
        self._update_detail_header_wraplengths()
        if self.detail_resize_after_id is not None:
            self.after_cancel(self.detail_resize_after_id)
        self.detail_resize_after_id = self.after(80, self._rerender_current_detail)

    def _rerender_current_detail(self) -> None:
        self.detail_resize_after_id = None
        if self.current_detail_session is not None and self.current_detail_messages is not None:
            self._render_session_detail(
                self.current_detail_session,
                self.current_detail_messages,
                reset_scroll=False,
            )
            return
        if self.current_detail_placeholder is not None:
            title, message = self.current_detail_placeholder
            self._render_detail_placeholder(title, message, reset_scroll=False)

    def _get_detail_available_width(self) -> int:
        panel_width = self.detail_panel.winfo_width()
        if panel_width <= 1:
            panel_width = 520
        return max(240, panel_width - 72)

    def _get_detail_body_wraplength(self) -> int:
        return max(220, self._get_detail_available_width() - 12)

    def _get_detail_bubble_wraplength(self) -> int:
        return max(180, int(self._get_detail_available_width() * 0.72))

    def _get_detail_bubble_side_padding(self) -> int:
        return max(20, int(self._get_detail_available_width() * 0.08))

    def _update_detail_header_wraplengths(self) -> None:
        wraplength = self._get_detail_body_wraplength()
        self.detail_title_label.configure(wraplength=wraplength)
        self.detail_meta_label.configure(wraplength=wraplength)

    def _scroll_detail_to_top(self) -> None:
        detail_canvas = getattr(self.detail_messages_frame, "_parent_canvas", None)
        if detail_canvas is None:
            return
        self.update_idletasks()
        detail_canvas.yview_moveto(0.0)

    def _set_busy(self, value: bool, status_text: str | None = None) -> None:
        self.is_busy = value
        self._update_action_states()
        if status_text is not None:
            self.status_var.set(status_text)

    def _update_view_state(self) -> None:
        if self.current_view == REMOTE_VIEW:
            self.hint_var.set("这里展示的是云端同步仓库中的记录；展开左侧 Provider 分组可查看会话内容，勾选后可恢复到本机。")
            self.restore_button.configure(text="恢复选中的会话到本机")
            self.restore_button.grid_configure(column=0, columnspan=2, padx=16)
            self.delete_local_button.grid_remove()
            self.list_panel.grid_configure(column=0, columnspan=1, padx=(0, 0))
            self.detail_panel.grid()
            self.local_toolbar.grid_remove()
            if not self.selected_remote_session_path:
                self._render_detail_placeholder(
                    "云端会话详情",
                    "展开左侧 Provider 分组后，点击具体会话查看对话内容。",
                )
        else:
            self.hint_var.set("这里展示的是本机 Codex 会话目录中的记录；勾选 .jsonl 文件后，可修正 Provider 或直接删除本地文件。")
            self.restore_button.configure(text="修正本地会话")
            self.restore_button.grid_configure(column=0, columnspan=1, padx=(16, 8))
            self.delete_local_button.grid()
            self.list_panel.grid_configure(column=0, columnspan=1, padx=(0, 0))
            self.detail_panel.grid()
            self.local_toolbar.grid()
            if not self.selected_local_session_path:
                self._render_detail_placeholder(
                    "本地会话详情",
                    "点击左侧 .jsonl 文件查看用户与 Codex 的对话内容。",
                )
        self._update_action_states()
        self.after_idle(self._refresh_list_scrollregion)

    def _update_action_states(self) -> None:
        common_state = "disabled" if self.is_busy else "normal"
        restore_state = "disabled"
        delete_state = "disabled"
        if not self.is_busy:
            if self.current_view == REMOTE_VIEW and any(self.remote_session_state.values()):
                restore_state = "normal"
            elif self.current_view == LOCAL_VIEW and any(self.local_session_state.values()):
                restore_state = "normal"
                delete_state = "normal"
        local_search_state = "disabled" if self.is_busy or self.current_view != LOCAL_VIEW else "normal"
        self.settings_button.configure(state=common_state)
        self.push_button.configure(state=common_state)
        self.refresh_button.configure(state=common_state)
        self.source_switch.configure(state=common_state)
        self.restore_button.configure(state=restore_state)
        self.delete_local_button.configure(state=delete_state)
        self.local_search_entry.configure(state=local_search_state)

    def _build_empty_state_message(self) -> str:
        if self.current_view == LOCAL_VIEW:
            local_session_count = self._count_jsonl_files(self.app_config.codex_session_dir)
            if local_session_count > 0:
                return "本地目录中存在会话文件，但没有解析出可显示的会话元数据。"
            return (
                "当前本地 Codex 会话目录中没有可显示的记录。\n"
                f"请检查设置中的 Codex 会话目录：{self.app_config.codex_session_dir}"
            )

        temp_session_count = self._count_jsonl_files(self.app_config.local_sync_temp_dir)
        local_session_count = self._count_jsonl_files(self.app_config.codex_session_dir)

        if temp_session_count > 0:
            return "当前没有可显示的云端会话记录。"

        if not self.app_config.git_remote_url.strip():
            return (
                "当前云端同步缓存为空。\n"
                "请先点击左侧“设置”填写 Git 远程仓库地址，然后点击“推送本机记录到云端”。\n"
                f"本机 Codex 会话目录中目前检测到 {local_session_count} 个 .jsonl 文件。"
            )

        return (
            "当前云端同步缓存为空。\n"
            "如果你刚完成配置，请点击左侧“推送本机记录到云端”，或者点击上方“刷新当前列表”重新拉取。\n"
            f"本机 Codex 会话目录中目前检测到 {local_session_count} 个 .jsonl 文件。"
        )

    def _count_jsonl_files(self, directory: str) -> int:
        path = Path(directory).expanduser()
        if not path.exists():
            return 0
        return sum(1 for _ in path.rglob("*.jsonl"))

    def _fit_list_frame_width(self, event: tk.Event) -> None:
        self.list_canvas.itemconfigure(self.list_canvas_window, width=event.width)
        self._refresh_list_scrollregion()

    def _refresh_list_scrollregion(self) -> None:
        self.update_idletasks()
        self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))
        self.list_canvas.yview_moveto(0.0 if not self.current_sessions else self.list_canvas.yview()[0])

    def _get_mousewheel_delta(self, event: tk.Event) -> int:
        if sys.platform.startswith("darwin"):
            if event.delta == 0:
                return 0
            return -1 if event.delta > 0 else 1
        if not event.delta:
            return 0
        normalized = -int(event.delta / 120)
        if normalized == 0:
            return -1 if event.delta > 0 else 1
        return normalized

    def _is_pointer_inside(self, widget: tk.Misc) -> bool:
        try:
            x_root = self.winfo_pointerx()
            y_root = self.winfo_pointery()
            x0 = widget.winfo_rootx()
            y0 = widget.winfo_rooty()
            x1 = x0 + widget.winfo_width()
            y1 = y0 + widget.winfo_height()
        except tk.TclError:
            return False
        return x0 <= x_root <= x1 and y0 <= y_root <= y1

    def _scroll_canvas(self, canvas: tk.Canvas, event: tk.Event) -> str:
        if canvas.yview() == (0.0, 1.0):
            return "break"

        delta = self._get_mousewheel_delta(event)
        if delta:
            canvas.yview_scroll(delta, "units")
        return "break"

    def _on_global_mousewheel(self, event: tk.Event) -> str | None:
        if self._is_pointer_inside(self.list_panel):
            return self._scroll_canvas(self.list_canvas, event)

        detail_canvas = getattr(self.detail_messages_frame, "_parent_canvas", None)
        if detail_canvas is not None and self._is_pointer_inside(self.detail_panel):
            return self._scroll_canvas(detail_canvas, event)

        return None

    def _run_task(
        self,
        status_text: str,
        task: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if self.is_busy:
            return

        self._set_busy(True, status_text)

        def worker() -> None:
            try:
                result = task()
            except Exception as exc:
                self.after(
                    0,
                    lambda error=exc: self._finish_task_with_error(error, on_error),
                )
                return
            self.after(
                0,
                lambda task_result=result: self._finish_task_with_success(
                    task_result,
                    on_success,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_task_with_success(
        self,
        result: object,
        on_success: Callable[[object], None],
    ) -> None:
        self._set_busy(False)
        on_success(result)

    def _finish_task_with_error(
        self,
        error: Exception,
        on_error: Callable[[Exception], None],
    ) -> None:
        self._set_busy(False)
        on_error(error)


if __name__ == "__main__":
    app = CodexMemoryApp()
    app.mainloop()
