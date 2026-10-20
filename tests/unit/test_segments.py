# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Technical content is recognised by structure and summarised by type (D-046)."""

import pytest

from continuum.coding import segments
from continuum.coding.segments import readable, segment, split_identifier


@pytest.mark.parametrize(("paste", "expected"), [
    ('Traceback (most recent call last):\n'
     '  File "/usr/lib/python3.12/site-packages/flask/app.py", line 1498, in dispatch\n'
     '    return f()\n'
     '  File "/srv/app/api/views.py", line 88, in get_user\n'
     '    return users[user_id]\n'
     "KeyError: 'user_id'",
     "Python error: KeyError: 'user_id' (at /srv/app/api/views.py:88 in get_user)"),
    ("TypeError: Cannot read properties of undefined (reading 'id')\n"
     "    at getUser (/app/src/users.js:12:15)\n"
     "    at /app/node_modules/express/lib/router/layer.js:95:5",
     "JavaScript error: TypeError: Cannot read properties of undefined (reading 'id') "
     "(at /app/src/users.js:12 in getUser)"),
    ('Exception in thread "main" java.lang.NullPointerException: name is null\n'
     "\tat com.acme.Billing.total(Billing.java:42)\n"
     "\tat com.acme.Main.main(Main.java:7)",
     "Java error: java.lang.NullPointerException: name is null "
     "(at Billing.java:42 in com.acme.Billing.total)"),
])
def test_stack_traces_become_error_signatures_in_the_projects_own_code(paste, expected):
    [seg] = segment(paste)
    assert seg.kind == "traceback"
    assert seg.summary.startswith(expected)


def test_each_kind_of_paste_is_typed_and_summarised():
    text = """Here is everything:
```diff
--- a/api/views.py
+++ b/api/views.py
@@ -85,1 +85,1 @@
-    return users[user_id]
+    return users.get(user_id)
```

| region | users | failing |
|---|---|---|
| DE | 1200 | 40 |

{"status": "error", "code": 500, "request_id": "abc"}

2026-10-19 10:00:01 INFO start
2026-10-19 10:00:02 ERROR db timeout after 30s

$ npm test
$ npm run lint

```ts
export function getUserById(id: string): User { return db.find(id) }
```
Please keep the API backwards compatible."""
    kinds = [(s.kind, s.summary) for s in segment(text) if s.kind != "prose"]
    assert kinds == [
        ("diff", "diff of 1 file(s) (+1/-1): api/views.py"),
        ("table", "table, 1 row(s) x 3 column(s): region, users, failing"),
        ("json", "JSON object with 3 key(s): status, code, request_id"),
        ("log", "log, 2 line(s) (1 INFO, 1 ERROR); first error: 2026-10-19 10:00:02 "
                "ERROR db timeout after 30s"),
        ("shell", "shell session, 2 command(s): npm test; npm run lint"),
        ("code", "ts code, 1 line(s), defines getUserById"),
    ]
    flat = readable(text)
    assert flat.startswith("Here is everything: [pasted diff of 1 file(s)")
    assert flat.endswith("Please keep the API backwards compatible.")   # words kept


def test_prose_is_never_mistaken_for_a_paste():
    for text in ("Use [x] for this.", "Please use the {name} placeholder.",
                 "Make sure the limit is 25 items.", "Errors go to stderr via `log`."):
        assert readable(text) == text
    # prose right after a one-line paste is still the person's words
    assert readable('ok:\n{"a": 1}\nthanks') == "ok: [pasted JSON object with 1 key(s): a] thanks"


def test_identifiers_are_findable_by_their_parts():
    assert split_identifier("getUserById") == ["get", "user", "by", "id"]
    assert split_identifier("HTTPServerError") == ["http", "server", "error"]
    assert split_identifier("api/views.py") == ["api", "views", "py"]
    terms = segments.search_terms("call getUserById in api/views.py with MAX_RETRY_COUNT")
    assert {"get", "user", "views", "retry", "count"} <= set(terms.split())


def test_segmentation_is_fast_on_a_large_paste():
    import time

    big = "\n".join(f"2026-10-19 10:00:{i % 60:02d} INFO request {i} ok" for i in range(20000))
    start = time.perf_counter()
    [seg] = segment(big)
    assert seg.kind == "log" and time.perf_counter() - start < 2.0
