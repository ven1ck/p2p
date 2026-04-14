import pytest
import os
import tempfile
import threading
import time
import queue
from core import FileTransferCore

class TestLocalTransfer:
    @pytest.fixture(autouse=True)
    def setup_dirs(self, tmp_path):
        self.sender_dir = tmp_path / "sender"
        self.recv_dir = tmp_path / "receiver"
        os.makedirs(self.sender_dir)
        os.makedirs(self.recv_dir)
        yield

    def test_single_file_transfer_accept(self):
        # Создаём тестовый файл
        test_file = self.sender_dir / "test.txt"
        test_file.write_text("Hello integration test!")

        # Запускаем получатель
        receiver = FileTransferCore(tcp_port=19999, udp_port=20000, recv_dir=str(self.recv_dir))
        receiver.start()
        time.sleep(0.5)  # Даем серверу стартануть

        # Автоматически принимаем файл
        def auto_accept():
            while True:
                try:
                    req = receiver.gui_request_queue.get(timeout=5)
                    receiver.respond_to_transfer(req["req_id"], True)
                    break
                except queue.Empty:
                    continue

        threading.Thread(target=auto_accept, daemon=True).start()

        # Отправляем
        sender = FileTransferCore(tcp_port=18888, udp_port=19000)
        sender.start()
        success, msg = sender.send_to_multiple([{"ip": "127.0.0.1", "port": 19999}], [str(test_file)])
        assert success is True
        time.sleep(1)

        # Проверяем результат
        received_file = self.recv_dir / "test.txt"
        assert received_file.exists()
        assert received_file.read_text() == "Hello integration test!"

    def test_transfer_decline(self):
        test_file = self.sender_dir / "decline.txt"
        test_file.write_text("Should not arrive")

        receiver = FileTransferCore(tcp_port=19998, udp_port=20001, recv_dir=str(self.recv_dir))
        receiver.start()
        time.sleep(0.5)

        # Автоматически отклоняем
        def auto_decline():
            while True:
                try:
                    req = receiver.gui_request_queue.get(timeout=5)
                    receiver.respond_to_transfer(req["req_id"], False)
                    break
                except queue.Empty:
                    continue

        threading.Thread(target=auto_decline, daemon=True).start()

        sender = FileTransferCore(tcp_port=18887, udp_port=19001)
        sender.start()
        sender.send_to_multiple([{"ip": "127.0.0.1", "port": 19998}], [str(test_file)])
        time.sleep(1)

        # Файл НЕ должен появиться
        assert not (self.recv_dir / "decline.txt").exists()