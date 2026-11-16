"""The program only displays what Zabbix reports: device names are read again on every refresh and are never written to disk.
(What the layout file does hold is the administrator's own configuration: host ids, interface keys, labels, positions.)   Run:  python3 -m unittest discover -s tests -v"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("trunkvista_under_test", ROOT / "trunkvista.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

OLD = {"version": 1,
       "maps": {"m1": {"id": "m1", "name": "Core", "nodes": [{"id": "n1", "host": "10001", "hostname": "core-sw-1", "label": "", "x": 10, "y": 20}], "links": []}},
       "tabs": [{"id": "t1", "name": "Core", "type": "map", "map": "m1"},
                {"id": "t2", "name": "Some", "type": "devices", "hosts": ["10001"], "hostnames": {"10001": "core-sw-1"}, "groups": [], "ifaces": 4}],
       "playlists": [], "settings": {}}


class NoDeviceNamesOnDisk(unittest.TestCase):
    def test_a_saved_layout_holds_no_zabbix_names(self):
        with tempfile.TemporaryDirectory() as d:
            st = mod.Store(Path(d) / "layout.json", lambda: OLD)
            st.put(json.loads(json.dumps(OLD)))
            text = (Path(d) / "layout.json").read_text(encoding="utf-8")
            self.assertNotIn("core-sw-1", text)
            self.assertIn("10001", text)      # the host id is the configuration: it says which device the map shows

    def test_an_older_file_is_cleaned_on_start_and_leaves_no_backup_with_the_names(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "layout.json"
            p.write_text(json.dumps(OLD), encoding="utf-8")
            p.with_suffix(".bak").write_text(json.dumps(OLD), encoding="utf-8")
            mod.Store(p, lambda: OLD)
            self.assertNotIn("core-sw-1", p.read_text(encoding="utf-8"))
            self.assertFalse(p.with_suffix(".bak").exists())

    def test_the_page_reads_names_from_the_live_view(self):
        self.assertIn("const liveName = n =>", mod.PAGE)
        self.assertIn("const nodeName = n => n.label || liveName(n) || n.host;", mod.PAGE)
        self.assertIn("(liveName(n) || \"?\") + \" (host \"", mod.PAGE)


if __name__ == "__main__":
    unittest.main()
