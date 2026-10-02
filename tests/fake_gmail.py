"""Minimal fake of the Gmail API client surface used by inbox_ops.

Records every mutation so tests can assert exactly what would hit Gmail.
"""


class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class _FakeLabels:
    def __init__(self, svc):
        self.svc = svc

    def list(self, userId):
        return _Exec({"labels": list(self.svc.labels_data)})

    def create(self, userId, body):
        new = {"id": f"L{len(self.svc.labels_data) + 1}", "name": body["name"]}
        self.svc.labels_data.append(new)
        return _Exec(new)


class _FakeMessages:
    def __init__(self, svc):
        self.svc = svc

    def list(self, userId, **kwargs):
        return _Exec({"messages": [{"id": mid} for mid in self.svc.message_meta]})

    def get(self, userId, id, **kwargs):
        return {"_get_id": id}

    def batchModify(self, userId, body):
        self.svc.batch_modify_calls.append(body)
        return _Exec({})

    def send(self, userId, body):
        self.svc.send_calls.append(body)
        return _Exec({"id": "sent"})


class _FakeBatch:
    """Mimics Gmail batch semantics: individual items can fail via callback
    exception while the batch itself succeeds."""

    def __init__(self, svc):
        self.svc = svc
        self.items = []

    def add(self, request, callback):
        self.items.append((request["_get_id"], callback))

    def execute(self):
        for mid, cb in self.items:
            if mid in self.svc.fail_once:
                self.svc.fail_once.discard(mid)
                cb(mid, None, Exception("rate limited"))
            else:
                cb(mid, self.svc.message_meta[mid], None)


class _FakeFilters:
    def __init__(self, svc):
        self.svc = svc

    def list(self, userId):
        return _Exec({"filter": list(self.svc.existing_filters)})

    def create(self, userId, body):
        self.svc.created_filters.append(body)
        return _Exec({"id": f"F{len(self.svc.created_filters)}"})

    def delete(self, userId, id):
        self.svc.deleted_filters.append(id)
        return _Exec({})


class _FakeSettings:
    def __init__(self, svc):
        self.svc = svc

    def filters(self):
        return _FakeFilters(self.svc)


class _FakeUsers:
    def __init__(self, svc):
        self.svc = svc

    def labels(self):
        return _FakeLabels(self.svc)

    def messages(self):
        return _FakeMessages(self.svc)

    def settings(self):
        return _FakeSettings(self.svc)


class FakeGmailService:
    def __init__(self, labels=None, existing_filters=None, message_meta=None, fail_once=None):
        self.labels_data = list(labels or [])
        self.existing_filters = list(existing_filters or [])
        self.message_meta = dict(message_meta or {})   # gmail_id -> raw get() response
        self.fail_once = set(fail_once or [])          # ids that fail their first batch item
        self.batch_modify_calls = []
        self.send_calls = []
        self.created_filters = []
        self.deleted_filters = []

    def users(self):
        return _FakeUsers(self)

    def new_batch_http_request(self):
        return _FakeBatch(self)
