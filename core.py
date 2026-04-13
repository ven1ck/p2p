import socket
import threading
import os
import struct
import json
import time
import queue
from datetime import datetime

class FileTransferCore:
    """
    Ядро приложения.
    Протокол TCP (пакетный режим):
      1. Клиент отправляет кол-во элементов (4 байта).
      2. Клиент отправляет метаданные каждого элемента: длина_относительного_пути(4) + путь(N) + размер(8).
      3. Сервер собирает метаданные, запрашивает ЕДИНОЕ РАЗРЕШЕНИЕ у GUI.
      4. GUI возвращает 1 байт (0x01 - принять всё, 0x00 - отклонить всё).
      5. Если принято, клиент последовательно отправляет данные. Сервер воссоздаёт структуру папок.
    """

    def __init__(self, tcp_port=9999, udp_port=50000, recv_dir="./received"):
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.recv_dir = os.path.abspath(recv_dir)
        self.device_name = socket.gethostname()
        self.local_ip = self._get_local_ip()
        
        self.log_queue = queue.Queue()
        self.gui_request_queue = queue.Queue()
        
        self.pending_requests = {}
        self._req_lock = threading.Lock()
        self._req_counter = 0
        
        self._peers_lock = threading.Lock()
        self.peers = []
        
        os.makedirs(self.recv_dir, exist_ok=True)

    def _get_local_ip(self):
        """Определяет локальный IP-адрес машины."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def start(self):
        """Запускает TCP-сервер и UDP-обнаружение в фоновых потоках."""
        threading.Thread(target=self._run_tcp_server, daemon=True).start()
        threading.Thread(target=self._run_discovery, daemon=True).start()
        self._log("Сервер и обнаружение запущены.")

    # ================= TCP СЕРВЕР =================
    def _run_tcp_server(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", self.tcp_port))
        server.listen(5)
        while True:
            try:
                client_sock, addr = server.accept()
                threading.Thread(target=self._handle_incoming, args=(client_sock, addr), daemon=True).start()
            except OSError:
                break

    def _handle_incoming(self, client_sock, addr):
        """Принимает пакет метаданных, ждёт общего разрешения, сохраняет файлы."""
        try:
            num_files = struct.unpack("!I", self._recv_exact(client_sock, 4))[0]
            batch_meta = []
            for _ in range(num_files):
                name_len = struct.unpack("!I", self._recv_exact(client_sock, 4))[0]
                rel_path = self._recv_exact(client_sock, name_len).decode("utf-8")
                file_size = struct.unpack("!Q", self._recv_exact(client_sock, 8))[0]
                batch_meta.append({"path": rel_path, "size": file_size})

            total_size = sum(m["size"] for m in batch_meta)
            req_id = f"{addr[0]}_{self._req_counter}"
            self._req_counter += 1

            evt = threading.Event()
            with self._req_lock:
                self.pending_requests[req_id] = {"sock": client_sock, "event": evt, "accepted": None}

            self.gui_request_queue.put({
                "req_id": req_id,
                "meta": {"sender_ip": addr[0], "count": num_files, "total_size": total_size, "files": batch_meta}
            })
            self._log(f"Входящий пакет: {num_files} элемент(ов) от {addr[0]}")

            # Ожидание решения пользователя (120 сек)
            if not evt.wait(timeout=120):
                self._log(f"Таймаут запроса от {addr[0]}")
                client_sock.sendall(b'\x00')
                return

            with self._req_lock:
                req = self.pending_requests.pop(req_id)

            if req["accepted"]:
                client_sock.sendall(b'\x01')
                for meta in batch_meta:
                    filepath = os.path.join(self.recv_dir, meta["path"])
                    d = os.path.dirname(filepath)
                    if d:
                        os.makedirs(d, exist_ok=True)
                    with open(filepath, "wb") as f:
                        received = 0
                        while received < meta["size"]:
                            chunk = client_sock.recv(min(4096, meta["size"] - received))
                            if not chunk:
                                raise ConnectionError("Соединение разорвано во время получения")
                            f.write(chunk)
                            received += len(chunk)
                    self._log(f"Сохранён: {meta['path']}")
                self._log("Пакет успешно сохранён.")
            else:
                client_sock.sendall(b'\x00')
                self._log(f"Пакет отклонён от {addr[0]}")
        except Exception as e:
            self._log(f"Ошибка соединения от {addr[0]}: {e}")
        finally:
            client_sock.close()

    def _recv_exact(self, sock, n):
        """Гарантирует чтение ровно n байт из TCP-потока."""
        data = bytearray()
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Соединение закрыто")
            data.extend(chunk)
        return bytes(data)

    # ================= ОТПРАВКА =================
    def _flatten_items(self, paths):
        """Преобразует список файлов/папок в плоский список (abs_path, rel_path, size)."""
        items = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isfile(p):
                items.append((p, os.path.basename(p), os.path.getsize(p)))
            elif os.path.isdir(p):
                parent = os.path.dirname(p)
                for root, _, files in os.walk(p):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, parent)
                        items.append((full, rel, os.path.getsize(full)))
        return items

    def send_to_multiple(self, targets, paths):
        items = self._flatten_items(paths)
        if not items:
            return False, "Не найдено допустимых файлов или директорий."
        for t in targets:
            threading.Thread(target=self._send_worker, args=(t["ip"], t["port"], items), daemon=True).start()
        return True, f"Начата отправка пакета ({len(items)} элемент(ов))..."

    def _send_worker(self, host, port, items):
        """Отправляет пакет элементов по одному TCP-соединению."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.sendall(struct.pack("!I", len(items)))

            for _, rel_path, size in items:
                name_bytes = rel_path.encode("utf-8")
                sock.sendall(struct.pack("!I", len(name_bytes)))
                sock.sendall(name_bytes)
                sock.sendall(struct.pack("!Q", size))

            resp = self._recv_exact(sock, 1)
            if resp != b'\x01':
                self._log(f"Передача отклонена узлом {host}")
                return

            for abs_path, _, _ in items:
                with open(abs_path, "rb") as f:
                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            break
                        sock.sendall(chunk)
            self._log(f"Пакет успешно отправлен на {host}")
        except Exception as e:
            self._log(f"Ошибка отправки на {host}: {e}")
        finally:
            sock.close()

    # ================= ОБНАРУЖЕНИЕ =================
    def _run_discovery(self):
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            udp_sock.bind(("", self.udp_port))
        except OSError:
            self._log("UDP-порт недоступен для обнаружения.")
            return

        threading.Thread(target=self._broadcast_presence, args=(udp_sock,), daemon=True).start()
        while True:
            try:
                data, addr = udp_sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
                if info.get("ip") and info["ip"] != self.local_ip:
                    self._add_peer(info["name"], info["ip"], info.get("port", self.tcp_port))
            except Exception:
                continue

    def _broadcast_presence(self, sock):
        while True:
            try:
                payload = json.dumps({
                    "name": self.device_name,
                    "ip": self.local_ip,
                    "port": self.tcp_port
                }).encode("utf-8")
                sock.sendto(payload, ("255.255.255.255", self.udp_port))
            except Exception:
                pass
            time.sleep(2)

    def _add_peer(self, name, ip, port):
        with self._peers_lock:
            for p in self.peers:
                if p["ip"] == ip:
                    p.update({"name": name, "port": port})
                    return
            self.peers.append({"name": name, "ip": ip, "port": port})

    def get_peers(self):
        with self._peers_lock:
            return list(self.peers)

    # ================= УПРАВЛЕНИЕ =================
    def update_device_name(self, new_name):
        new_name = new_name.strip()
        if not new_name:
            return False, "Имя не может быть пустым."
        self.device_name = new_name
        self._log(f"Имя устройства изменено на: {self.device_name}")
        return True, "Имя обновлено."

    def respond_to_transfer(self, req_id, accept):
        """Вызывается GUI для передачи решения пользователя."""
        with self._req_lock:
            if req_id in self.pending_requests:
                self.pending_requests[req_id]["accepted"] = accept
                self.pending_requests[req_id]["event"].set()

    def _format_size(self, size_bytes):
        if size_bytes < 1024: return f"{size_bytes} Б"
        elif size_bytes < 1024**2: return f"{size_bytes/1024:.1f} КБ"
        elif size_bytes < 1024**3: return f"{size_bytes/1024**2:.1f} МБ"
        return f"{size_bytes/1024**3:.2f} ГБ"

    def _log(self, msg):
        """Добавляет сообщение в очередь логов для GUI."""
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")