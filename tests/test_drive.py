from health_pipeline.sources.drive import FOLDER_MIME, DriveSource


class _Req:
    def __init__(self, resp):
        self.resp = resp

    def execute(self):
        return self.resp


class _Files:
    def __init__(self, tree):
        self.tree = tree

    def list(self, q, **kw):
        parent = q.split("'")[1]
        return _Req({"files": self.tree.get(parent, [])})


class _Svc:
    def __init__(self, tree):
        self._f = _Files(tree)

    def files(self):
        return self._f


def test_lists_json_recursively_oldest_first():
    tree = {
        "root": [
            {"id": "a", "name": "HealthAutoExport-b.json", "mimeType": "application/json", "modifiedTime": "2026-01-12T05:00:00Z"},
            {"id": "sub", "name": "Automations", "mimeType": FOLDER_MIME},
            {"id": "x", "name": "notes.txt", "mimeType": "text/plain"},
        ],
        "sub": [{"id": "b", "name": "HealthAutoExport-a.json", "mimeType": "application/json", "modifiedTime": "2026-01-11T05:00:00Z"}],
    }
    files = DriveSource({}, "root", service=_Svc(tree)).list_json()
    assert [f.id for f in files] == ["b", "a"]
    assert files[0].state_key == "drive:b"
