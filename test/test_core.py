import pytest
import os
import tempfile
from core import FileTransferCore

class TestCoreLogic:
    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            yield td

    def test_format_size(self):
        core = FileTransferCore.__new__(FileTransferCore)
        assert core._format_size(500) == "500 Б"
        assert core._format_size(1024) == "1.0 КБ"
        assert core._format_size(1048576) == "1.0 МБ"
        assert core._format_size(1073741824) == "1.00 ГБ"

    def test_flatten_files_only(self, temp_dir):
        os.makedirs(os.path.join(temp_dir, "test"))
        with open(os.path.join(temp_dir, "test", "a.txt"), "w") as f: f.write("1")
        with open(os.path.join(temp_dir, "test", "b.txt"), "w") as f: f.write("22")

        core = FileTransferCore.__new__(FileTransferCore)
        items = core._flatten_items([os.path.join(temp_dir, "test")])
        assert len(items) == 2
        
        rel_paths = [i[1] for i in items]
        # Пути содержат имя родительской папки, так как relpath строится от её родителя
        assert os.path.join("test", "a.txt") in rel_paths
        assert os.path.join("test", "b.txt") in rel_paths

    def test_flatten_nested_folders(self, temp_dir):
        base = os.path.join(temp_dir, "root")
        os.makedirs(os.path.join(base, "sub1", "sub2"))
        open(os.path.join(base, "root.txt"), "w").close()
        open(os.path.join(base, "sub1", "f1.txt"), "w").close()
        open(os.path.join(base, "sub1", "sub2", "f2.txt"), "w").close()

        core = FileTransferCore.__new__(FileTransferCore)
        items = core._flatten_items([base])
        assert len(items) == 3
        
        paths = {i[1] for i in items}
        assert os.path.join("root", "root.txt") in paths
        assert os.path.join("root", "sub1", "f1.txt") in paths
        assert os.path.join("root", "sub1", "sub2", "f2.txt") in paths

    def test_peer_timeout_logic(self):
        core = FileTransferCore.__new__(FileTransferCore)
        # Имитация локового объекта для теста
        class FakeLock:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        core._peers_lock = FakeLock()
        core.peers = []
        core._peer_last_seen = {}

        core._add_peer("PC1", "192.168.1.10", 9999)
        assert len(core.get_peers()) == 1

        # Имитируем прошедшие 11 секунд (больше порога 10 сек)
        import time
        original_time = time.time
        time.time = lambda: original_time() + 11
        try:
            assert len(core.get_peers()) == 0
        finally:
            time.time = original_time