# КРИТИЧНО: Порядок импортов определяет работу tkinterdnd2 + customtkinter
import tkinter as tk
from tkinterdnd2 import TkinterDnD, DND_FILES
import customtkinter as ctk
import os
import urllib.parse
import sys
from tkinter import filedialog, messagebox
from core import FileTransferCore

class TransferGUI:
    """
    Графический интерфейс полностью на customtkinter.
    - Списки реализованы через CTkScrollableFrame + динамические виджеты
    - Drag & Drop зарегистрирован на главном окне и области списка файлов
    - Потокобезопасное обновление через polling
    - Пакетное разрешение, поддержка папок, русский язык
    """

    def __init__(self):
        self.root = TkinterDnD.Tk()
        self.root.title("Передача файлов по локальной сети")
        self.root.geometry("950x650")
        self.root.minsize(750, 500)
        self.root.configure(bg='black')

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.core = FileTransferCore()
        self.core.start()
        self.selected_paths = []
        
        # Хранилища виджетов для эффективного обновления без мерцания
        self.peer_checkboxes = {}  # ip -> CTkCheckBox
        self.file_frames = {}      # path -> CTkFrame (строка с файлом)

        self._setup_ui()
        self._init_drag_drop()

        self.root.after(2000, self._poll_peers)
        self.root.after(500, self._poll_logs)
        self.root.after(500, self._poll_requests)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _setup_ui(self):
        # 1. Верхняя панель
        top_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        top_frame.pack(fill="x", padx=20, pady=(15, 5))

        ctk.CTkLabel(top_frame, text="Имя устройства:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 8))
        self.name_var = tk.StringVar(value=self.core.device_name)
        ctk.CTkEntry(top_frame, textvariable=self.name_var, width=200).pack(side="left", padx=2)
        ctk.CTkButton(top_frame, text="Применить", command=self._apply_name, width=90).pack(side="left")

        # 2. Основная область (две колонки)
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # ЛЕВАЯ КОЛОНКА: Пользователи
        peer_col = ctk.CTkFrame(main_frame)
        peer_col.pack(side="left", fill="both", expand=True, padx=(0, 8))

        ctk.CTkLabel(peer_col, text="Обнаруженные пользователи", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        self.peer_scroll = ctk.CTkScrollableFrame(peer_col, label_text="")
        self.peer_scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # ПРАВАЯ КОЛОНКА: Файлы и управление
        file_col = ctk.CTkFrame(main_frame)
        file_col.pack(side="right", fill="both", expand=True)

        ctk.CTkLabel(file_col, text="Элементы для отправки", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        
        # Список файлов (скроллируемый)
        self.file_scroll = ctk.CTkScrollableFrame(file_col, label_text="")
        self.file_scroll.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        # Кнопки управления
        path_btn_frame = ctk.CTkFrame(file_col, fg_color="transparent")
        path_btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(path_btn_frame, text="Добавить файлы...", command=self._add_files, width=120).pack(side="left", padx=2)
        ctk.CTkButton(path_btn_frame, text="Добавить папку...", command=self._add_folder, width=120).pack(side="left", padx=2)
        ctk.CTkButton(path_btn_frame, text="Очистить всё", command=self._clear_paths, width=100).pack(side="left", padx=2)
        ctk.CTkButton(path_btn_frame, text="Отправить выбранным", command=self._send_paths, width=150).pack(side="right", padx=2)

        # 3. Журнал событий
        log_frame = ctk.CTkFrame(self.root)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        ctk.CTkLabel(log_frame, text="Журнал событий", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5), anchor="w", padx=10)
        self.log_text = ctk.CTkTextbox(log_frame, height=120, state="disabled", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _init_drag_drop(self):
        """Регистрация Drag & Drop на корневом окне и области списка файлов."""
        try:
            self.root.tk.eval('package require tkdnd 2.9')
        except tk.TclError as e:
            self.core._log(f"Библиотека tkdnd не загружена. DnD отключён. ({e})")
            return

        if not hasattr(self.root, 'drop_target_register'):
            self.core._log("Атрибут drop_target_register отсутствует.")
            return

        try:
            # Регистрируем главное окно и область списка файлов
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self._on_drop)
            
            self.file_scroll.drop_target_register(DND_FILES)
            self.file_scroll.dnd_bind('<<Drop>>', self._on_drop)
            
            self.core._log("Drag & Drop активирован (работает по всему окну).")
        except Exception as e:
            self.core._log(f"Сбой регистрации DnD: {e}")

    # ================= УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ =================
    def _poll_peers(self):
        peers = self.core.get_peers()
        current_ips = set(self.peer_checkboxes.keys())
        new_ips = {p["ip"] for p in peers}

        for ip in (current_ips - new_ips):
            self.peer_checkboxes[ip].destroy()
            del self.peer_checkboxes[ip]

        for p in peers:
            if p["ip"] not in self.peer_checkboxes:
                cb = ctk.CTkCheckBox(
                    self.peer_scroll, 
                    text=f"{p['name']} ({p['ip']}:{p['port']})",
                    font=ctk.CTkFont(size=12)
                )
                cb.pack(anchor="w", padx=5, pady=2, fill="x")
                self.peer_checkboxes[p["ip"]] = cb
            else:
                cb = self.peer_checkboxes[p["ip"]]
                expected_text = f"{p['name']} ({p['ip']}:{p['port']})"
                if cb.cget("text") != expected_text:
                    cb.configure(text=expected_text)

        self.root.after(2000, self._poll_peers)

    def _get_selected_peers(self):
        targets = []
        for ip, cb in self.peer_checkboxes.items():
            if cb.get() == 1:
                text = cb.cget("text")
                ip_port = text.split("(")[1].rstrip(")")
                ip_addr, port = ip_port.split(":")
                targets.append({"ip": ip_addr, "port": int(port)})
        return targets

    # ================= УПРАВЛЕНИЕ ФАЙЛАМИ =================
    def _refresh_files(self):
        for path in list(self.file_frames.keys()):
            if path not in self.selected_paths:
                self.file_frames[path].destroy()
                del self.file_frames[path]

        for path in self.selected_paths:
            if path not in self.file_frames:
                self._create_file_widget(path)

    def _create_file_widget(self, path):
        frame = ctk.CTkFrame(self.file_scroll, fg_color="transparent")
        frame.pack(anchor="w", padx=5, pady=2, fill="x")

        name = os.path.basename(path)
        if os.path.isdir(path):
            name += " [ПАПКА]"
            
        label = ctk.CTkLabel(frame, text=name, anchor="w", font=ctk.CTkFont(size=11))
        label.pack(side="left", fill="x", expand=True)

        btn = ctk.CTkButton(frame, text="X", width=30, height=25, command=lambda p=path: self._remove_file(p))
        btn.pack(side="right", padx=(5, 0))

        self.file_frames[path] = frame

    def _remove_file(self, path):
        self.selected_paths.remove(path)
        if path in self.file_frames:
            self.file_frames[path].destroy()
            del self.file_frames[path]

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Выбор файлов")
        if paths:
            for p in paths:
                if p not in self.selected_paths:
                    self.selected_paths.append(p)
            self._refresh_files()

    def _add_folder(self):
        path = filedialog.askdirectory(title="Выбор папки")
        if path and path not in self.selected_paths:
            self.selected_paths.append(path)
            self._refresh_files()

    def _clear_paths(self):
        self.selected_paths.clear()
        for frame in self.file_frames.values():
            frame.destroy()
        self.file_frames.clear()

    # ================= DRAG & DROP =================
    def _clean_path(self, raw):
        p = raw.strip()
        if p.startswith('{') and p.endswith('}'):
            p = p[1:-1]
        p = urllib.parse.unquote(p)
        for prefix in ["file:///", "file://"]:
            if p.lower().startswith(prefix.lower()):
                p = p[len(prefix):]
                break
        if os.name == "nt" and p.startswith('/'):
            p = p[1:]
        return os.path.normpath(p)

    def _on_drop(self, event):
        try:
            raw_paths = self.root.tk.splitlist(event.data)
        except Exception:
            raw_paths = [event.data]

        valid = []
        for raw in raw_paths:
            clean = self._clean_path(raw)
            if os.path.exists(clean):
                valid.append(clean)

        if valid:
            new_items = [p for p in valid if p not in self.selected_paths]
            self.selected_paths.extend(new_items)
            self._refresh_files()
            self.core._log(f"Добавлено {len(new_items)} элемент(ов) через перетаскивание.")

    # ================= ОТПРАВКА =================
    def _send_paths(self):
        targets = self._get_selected_peers()
        if not targets:
            messagebox.showwarning("Внимание", "Отметьте хотя бы одного получателя!")
            return
        if not self.selected_paths:
            messagebox.showwarning("Внимание", "Список отправки пуст!")
            return

        self.core.send_to_multiple(targets, list(self.selected_paths))
        self.core._log("Отправка пакета запущена в фоне.")

    def _apply_name(self):
        success, msg = self.core.update_device_name(self.name_var.get())
        if success:
            self.core._log(msg)
        else:
            messagebox.showwarning("Внимание", msg)

    # ================= ФОНОВЫЕ ОПРОСЫ =================
    def _poll_logs(self):
        while not self.core.log_queue.empty():
            try:
                msg = self.core.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state="disabled")
            except Exception:
                break
        self.root.after(500, self._poll_logs)

    def _poll_requests(self):
        while not self.core.gui_request_queue.empty():
            try:
                req = self.core.gui_request_queue.get_nowait()
                self._handle_incoming_request(req)
            except Exception:
                break
        self.root.after(500, self._poll_requests)

    def _handle_incoming_request(self, req):
        meta = req["meta"]
        file_preview = "\n".join(f["path"] for f in meta["files"][:6])
        if meta["count"] > 6:
            file_preview += f"\n... и ещё {meta['count'] - 6} файл(ов)"

        msg = (f"Разрешить загрузку {meta['count']} элемент(ов)?\n\n"
               f"Общий размер: {self.core._format_size(meta['total_size'])}\n"
               f"Отправитель: {meta['sender_ip']}\n\n"
               f"Содержимое:\n{file_preview}")
        
        accepted = messagebox.askyesno("Входящая передача", msg)
        self.core.respond_to_transfer(req["req_id"], accepted)
        self.core._log("Пакет принят." if accepted else "Пакет отклонён.")

    def _on_close(self):
        self.root.destroy()
        sys.exit(0)

if __name__ == "__main__":
    TransferGUI()