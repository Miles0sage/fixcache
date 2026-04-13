#!/usr/bin/env python3
"""
Build bench/corpus_v2.jsonl — 123 real-shape error samples across 13 classes.

Samples are synthetic-but-realistic (modeled on real GitHub issues).
No real paths, hostnames, usernames, or secrets. Safe to ship.

Run:
    python bench/build_corpus_v2.py
"""
import json
from pathlib import Path

SAMPLES = []

def add(cls: str, text: str):
    SAMPLES.append({"class": cls, "text": text.strip()})

# ── 1. py-module-not-found (10 samples) ──────────────────────────────────────
add("py-module-not-found", """
Traceback (most recent call last):
  File "train.py", line 4, in <module>
    import sklearn
ModuleNotFoundError: No module named 'sklearn'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "app.py", line 1, in <module>
    import pandas
ModuleNotFoundError: No module named 'pandas'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "server.py", line 3, in <module>
    import fastapi
ModuleNotFoundError: No module named 'fastapi'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "ingest.py", line 2, in <module>
    import numpy
ModuleNotFoundError: No module named 'numpy'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "pipeline.py", line 5, in <module>
    import torch
ModuleNotFoundError: No module named 'torch'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "client.py", line 1, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "worker.py", line 2, in <module>
    import redis
ModuleNotFoundError: No module named 'redis'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "migrate.py", line 3, in <module>
    import alembic
ModuleNotFoundError: No module named 'alembic'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "test_api.py", line 1, in <module>
    import pytest
ModuleNotFoundError: No module named 'pytest'
""")
add("py-module-not-found", """
Traceback (most recent call last):
  File "scrape.py", line 2, in <module>
    import bs4
ModuleNotFoundError: No module named 'bs4'
""")

# ── 2. py-attribute-none (10 samples) ────────────────────────────────────────
add("py-attribute-none", """
Traceback (most recent call last):
  File "handler.py", line 42, in process
    result = self.client.send(payload)
AttributeError: 'NoneType' object has no attribute 'send'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "model.py", line 18, in predict
    output = self.model.forward(x)
AttributeError: 'NoneType' object has no attribute 'forward'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "db.py", line 33, in query
    rows = self.conn.execute(sql)
AttributeError: 'NoneType' object has no attribute 'execute'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "router.py", line 12, in dispatch
    return self.handler.handle(req)
AttributeError: 'NoneType' object has no attribute 'handle'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "cache.py", line 7, in get
    return self.store.get(key)
AttributeError: 'NoneType' object has no attribute 'get'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "auth.py", line 55, in verify
    token = self.signer.sign(payload)
AttributeError: 'NoneType' object has no attribute 'sign'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "pipeline.py", line 28, in run
    data = self.loader.load(path)
AttributeError: 'NoneType' object has no attribute 'load'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "worker.py", line 9, in execute
    job = self.queue.pop()
AttributeError: 'NoneType' object has no attribute 'pop'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "stream.py", line 63, in write
    self.buffer.write(chunk)
AttributeError: 'NoneType' object has no attribute 'write'
""")
add("py-attribute-none", """
Traceback (most recent call last):
  File "config.py", line 14, in load
    return self.parser.read(path)
AttributeError: 'NoneType' object has no attribute 'read'
""")

# ── 3. py-import-error (9 samples) ───────────────────────────────────────────
add("py-import-error", """
Traceback (most recent call last):
  File "app.py", line 3, in <module>
    from mypackage import utils
ImportError: cannot import name 'utils' from 'mypackage'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "server.py", line 1, in <module>
    from api import router
ImportError: cannot import name 'router' from 'api'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "train.py", line 5, in <module>
    from models import Transformer
ImportError: cannot import name 'Transformer' from 'models'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "cli.py", line 2, in <module>
    from commands import run
ImportError: cannot import name 'run' from 'commands'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "worker.py", line 4, in <module>
    from tasks import process_job
ImportError: cannot import name 'process_job' from 'tasks'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "test_core.py", line 1, in <module>
    from core import Engine
ImportError: cannot import name 'Engine' from 'core'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "main.py", line 6, in <module>
    from config import Settings
ImportError: cannot import name 'Settings' from 'config'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "migrate.py", line 3, in <module>
    from db import Session
ImportError: cannot import name 'Session' from 'db'
""")
add("py-import-error", """
Traceback (most recent call last):
  File "scheduler.py", line 2, in <module>
    from jobs import nightly_report
ImportError: cannot import name 'nightly_report' from 'jobs'
""")

# ── 4. py-syntax-invalid (9 samples) ─────────────────────────────────────────
add("py-syntax-invalid", """
  File "app.py", line 14
    def run(self
           ^
SyntaxError: '(' was never closed
""")
add("py-syntax-invalid", """
  File "config.py", line 8
    return {key: value
           ^
SyntaxError: '{' was never closed
""")
add("py-syntax-invalid", """
  File "handler.py", line 22
    if x == 1
             ^
SyntaxError: expected ':'
""")
add("py-syntax-invalid", """
  File "models.py", line 5
    class Foo
             ^
SyntaxError: expected ':'
""")
add("py-syntax-invalid", """
  File "routes.py", line 31
    async def fetch(url)
                       ^
SyntaxError: expected ':'
""")
add("py-syntax-invalid", """
  File "tasks.py", line 17
    result = [x for x in items
                               ^
SyntaxError: '[' was never closed
""")
add("py-syntax-invalid", """
  File "schema.py", line 9
    fields = (
             ^
SyntaxError: '(' was never closed
""")
add("py-syntax-invalid", """
  File "runner.py", line 44
    print("done"
               ^
SyntaxError: '(' was never closed
""")
add("py-syntax-invalid", """
  File "seed.py", line 3
    data = [
           ^
SyntaxError: '[' was never closed
""")

# ── 5. py-type-not-subscriptable (8 samples) ─────────────────────────────────
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "router.py", line 11, in dispatch
    handler = registry[key]
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "parser.py", line 7, in parse
    value = result[0]
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "config.py", line 19, in get
    return settings['key']
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "db.py", line 34, in fetch
    row = cursor[0]
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "api.py", line 28, in handle
    data = response['data']
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "pipeline.py", line 52, in step
    chunk = batch[idx]
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "cache.py", line 15, in lookup
    entry = store['token']
TypeError: 'NoneType' object is not subscriptable
""")
add("py-type-not-subscriptable", """
Traceback (most recent call last):
  File "loader.py", line 6, in read
    record = dataset[i]
TypeError: 'NoneType' object is not subscriptable
""")

# ── 6. node-cannot-find-module (10 samples) ───────────────────────────────────
add("node-cannot-find-module", """
Error: Cannot find module 'express'
Require stack:
- /app/server.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
    at Module.require (node:internal/modules/cjs/loader:1113:19)
    at require (node:internal/modules/helpers:103:18)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'lodash'
Require stack:
- /app/utils.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'axios'
Require stack:
- /app/client.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'dotenv'
Require stack:
- /app/config.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'mongoose'
Require stack:
- /app/db.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'jsonwebtoken'
Require stack:
- /app/auth.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'redis'
Require stack:
- /app/cache.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'pg'
Require stack:
- /app/db/pool.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'bcrypt'
Require stack:
- /app/security.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
""")
add("node-cannot-find-module", """
Error: Cannot find module 'multer'
Require stack:
- /app/upload.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)
    at Function.Module._load (node:internal/modules/cjs/loader:885:27)
""")

# ── 7. node-undefined-not-fn (8 samples) ─────────────────────────────────────
add("node-undefined-not-fn", """
TypeError: app.listen is not a function
    at Object.<anonymous> (/app/server.js:12:5)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
    at Object.Module._extensions..js (node:internal/modules/cjs/loader:1422:10)
""")
add("node-undefined-not-fn", """
TypeError: router.use is not a function
    at Object.<anonymous> (/app/routes/index.js:8:8)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")
add("node-undefined-not-fn", """
TypeError: db.connect is not a function
    at Object.<anonymous> (/app/database.js:5:4)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")
add("node-undefined-not-fn", """
TypeError: client.query is not a function
    at Object.<anonymous> (/app/db/pool.js:17:12)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")
add("node-undefined-not-fn", """
TypeError: handler.handle is not a function
    at processRequest (/app/middleware.js:23:14)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-undefined-not-fn", """
TypeError: cache.get is not a function
    at Object.<anonymous> (/app/services/cache.js:9:16)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")
add("node-undefined-not-fn", """
TypeError: queue.push is not a function
    at Object.<anonymous> (/app/workers/job.js:31:11)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")
add("node-undefined-not-fn", """
TypeError: logger.info is not a function
    at Object.<anonymous> (/app/utils/log.js:6:8)
    at Module._compile (node:internal/modules/cjs/loader:1364:14)
""")

# ── 8. node-read-prop-undefined ── WEAK POINT (10 samples, adversarial) ───────
# Bug: no targeted redactor for `(reading '<prop>')` — each prop name = different hash
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'id')
    at getUserById (/app/controllers/user.js:14:22)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'name')
    at renderProfile (/app/views/profile.js:7:31)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'token')
    at verifyAuth (/app/middleware/auth.js:22:18)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'status')
    at handleResponse (/app/services/api.js:45:29)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'data')
    at parseResult (/app/utils/parser.js:12:16)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'email')
    at sendNotification (/app/services/mailer.js:33:24)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'userId')
    at createSession (/app/auth/session.js:18:20)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'role')
    at checkPermission (/app/middleware/rbac.js:9:27)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'length')
    at validateInput (/app/validators/schema.js:41:18)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
""")
add("node-read-prop-undefined", """
TypeError: Cannot read properties of undefined (reading 'message')
    at handleError (/app/middleware/errors.js:6:23)
    at Layer.handle_error (/app/node_modules/express/lib/router/layer.js:71:5)
""")

# ── 9. go-undefined (9 samples) ───────────────────────────────────────────────
add("go-undefined", """
./main.go:14:9: undefined: DatabaseClient
""")
add("go-undefined", """
./server.go:31:5: undefined: Router
""")
add("go-undefined", """
./handler.go:8:18: undefined: RequestContext
""")
add("go-undefined", """
./worker.go:22:12: undefined: JobQueue
""")
add("go-undefined", """
./auth.go:44:6: undefined: TokenValidator
""")
add("go-undefined", """
./config.go:7:9: undefined: Settings
""")
add("go-undefined", """
./api.go:55:14: undefined: ResponseWriter
""")
add("go-undefined", """
./cache.go:18:8: undefined: RedisClient
""")
add("go-undefined", """
./migrate.go:3:9: undefined: MigrationRunner
""")

# ── 10. rust-unused-import ── WEAK POINT (10 samples, adversarial) ────────────
# Bug: _pick_final_line grabs caret underline instead of warning line
add("rust-unused-import", """
warning: unused import: `std::collections::HashMap`
 --> src/main.rs:3:5
  |
3 | use std::collections::HashMap;
  |     ^^^^^^^^^^^^^^^^^^^^^^^^^
  |
  = note: `#[warn(unused_imports)]` on by default
""")
add("rust-unused-import", """
warning: unused import: `std::io::Write`
 --> src/lib.rs:1:5
  |
1 | use std::io::Write;
  |     ^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `log::debug`
 --> src/worker.rs:2:5
  |
2 | use log::debug;
  |     ^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `serde::Deserialize`
 --> src/models.rs:4:5
  |
4 | use serde::Deserialize;
  |     ^^^^^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `tokio::time::sleep`
 --> src/scheduler.rs:1:5
  |
1 | use tokio::time::sleep;
  |     ^^^^^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `std::sync::Arc`
 --> src/state.rs:6:5
  |
6 | use std::sync::Arc;
  |     ^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `anyhow::Context`
 --> src/error.rs:2:5
  |
2 | use anyhow::Context;
  |     ^^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `reqwest::Client`
 --> src/http.rs:1:5
  |
1 | use reqwest::Client;
  |     ^^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `std::path::PathBuf`
 --> src/fs.rs:3:5
  |
3 | use std::path::PathBuf;
  |     ^^^^^^^^^^^^^^^^^^
""")
add("rust-unused-import", """
warning: unused import: `tracing::instrument`
 --> src/trace.rs:1:5
  |
1 | use tracing::instrument;
  |     ^^^^^^^^^^^^^^^^^^^
""")

# ── 11. cuda-oom ── WEAK POINT (10 samples, adversarial) ─────────────────────
# Bug: _NUMBER strips 3+ digit numbers only — "9.00 GiB", "8.00 GiB" survive unredacted
# These samples vary the memory values to expose the broken redactor
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 20.00 MiB. GPU 0 has a total capacity of 79.19 GiB of which 9.00 MiB is free. Including non-PyTorch memory, this process has 79.13 GiB memory in use. Of the allocated memory 77.33 GiB is allocated by PyTorch, and 1.05 GiB is reserved by PyTorch but unallocated.
""")
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 16.00 MiB. GPU 0 has a total capacity of 31.47 GiB of which 16.62 MiB is free. Including non-PyTorch memory, this process has 31.44 GiB memory in use. Of the allocated memory 31.11 GiB is allocated by PyTorch, and 39.01 MiB is reserved by PyTorch but unallocated.
""")
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 8.00 GiB. GPU 0 has a total capacity of 39.59 GiB of which 337.19 MiB is free. Process 641349 has 39.25 GiB memory in use. Of the allocated memory 16.49 GiB is allocated by PyTorch, and 19.59 GiB is reserved by PyTorch but unallocated.
""")
add("cuda-oom", """
RuntimeError: CUDA out of memory. Tried to allocate 2.44 GiB
""")
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 50.00 MiB. GPU 0 has a total capacity of 7.67 GiB of which 23.44 MiB is free.
""")
add("cuda-oom", """
OutOfMemoryError: CUDA out of memory. Tried to allocate 162.00 MiB. GPU 0 has a total capacity of 14.56 GiB of which 29.81 MiB is free. Including non-PyTorch memory, this process has 14.53 GiB memory in use.
""")
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 56.00 GiB. GPU 1 has a total capacity of 23.56 GiB of which 20.66 GiB is free. Including non-PyTorch memory, this process has 2.66 GiB memory in use.
""")
add("cuda-oom", """
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 50.51 GiB. GPU 1 total: 178.36 GiB | Free: 44.84 GiB | PyTorch allocated: 130.43 GiB
""")
add("cuda-oom", """
CUDA out of memory. Tried to allocate 1.96 GiB. GPU 0 has a total capacity of 23.53 GiB of which 14.12 MiB is free.
""")
add("cuda-oom", """
OutOfMemoryError: CUDA out of memory. Tried to allocate 222.41 GiB (GPU 0; 23.56 GiB total capacity; 583.94 MiB already allocated; 22.66 GiB free; 612.00 MiB reserved in total by PyTorch)
""")

# ── 12. linker-undefined-reference (9 samples) ───────────────────────────────
add("linker-undefined-reference", """
/usr/bin/ld: /tmp/ccXXXXXX.o: in function `main':
main.c:(.text+0x1a): undefined reference to `sqlite3_open'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/server.o: in function `start_server':
server.c:(.text+0x44): undefined reference to `SSL_new'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/crypto.o: in function `hash_password':
crypto.c:(.text+0x2c): undefined reference to `EVP_DigestInit'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/net.o: in function `resolve_host':
net.c:(.text+0x18): undefined reference to `curl_easy_init'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/compress.o: in function `deflate_data':
compress.c:(.text+0x30): undefined reference to `deflateInit'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/image.o: in function `load_png':
image.c:(.text+0x54): undefined reference to `png_create_read_struct'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/audio.o: in function `play_sound':
audio.c:(.text+0x22): undefined reference to `SDL_OpenAudio'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/db.o: in function `connect_pg':
db.c:(.text+0x10): undefined reference to `PQconnectdb'
collect2: error: ld returned 1 exit status
""")
add("linker-undefined-reference", """
/usr/bin/ld: build/xml.o: in function `parse_doc':
xml.c:(.text+0x3c): undefined reference to `xmlParseFile'
collect2: error: ld returned 1 exit status
""")

# ── 13. net-connection-refused (9 samples) ───────────────────────────────────
add("net-connection-refused", """
Error: connect ECONNREFUSED 127.0.0.1:5432
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1555:16)
""")
add("net-connection-refused", """
Error: connect ECONNREFUSED 127.0.0.1:6379
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1555:16)
""")
add("net-connection-refused", """
Error: connect ECONNREFUSED 0.0.0.0:8080
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1555:16)
""")
add("net-connection-refused", """
requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8000): Max retries exceeded with url: /api/health (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x...>: Failed to establish a new connection: [Errno 111] Connection refused'))
""")
add("net-connection-refused", """
requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=5000): Max retries exceeded with url: /health (Caused by NewConnectionError('Failed to establish a new connection: [Errno 111] Connection refused'))
""")
add("net-connection-refused", """
dial tcp 127.0.0.1:9200: connect: connection refused
""")
add("net-connection-refused", """
dial tcp 0.0.0.0:3306: connect: connection refused
""")
add("net-connection-refused", """
java.net.ConnectException: Connection refused (Connection refused)
    at java.net.PlainSocketImpl.socketConnect(Native Method)
    at java.net.AbstractPlainSocketImpl.doConnect(AbstractPlainSocketImpl.java:350)
""")
add("net-connection-refused", """
grpc: error while dialing dial tcp 127.0.0.1:50051: connect: connection refused
""")

# ── 14. shell-permission-denied (8 samples) ──────────────────────────────────
add("shell-permission-denied", """
bash: /usr/local/bin/deploy.sh: Permission denied
""")
add("shell-permission-denied", """
bash: ./run.sh: Permission denied
""")
add("shell-permission-denied", """
-bash: /opt/app/start.sh: Permission denied
""")
add("shell-permission-denied", """
sh: 1: /entrypoint.sh: Permission denied
""")
add("shell-permission-denied", """
/bin/sh: /scripts/migrate.sh: Permission denied
""")
add("shell-permission-denied", """
bash: ./scripts/build.sh: Permission denied
""")
add("shell-permission-denied", """
zsh: permission denied: /usr/local/bin/fixcache
""")
add("shell-permission-denied", """
bash: /home/deploy/restart.sh: Permission denied
""")

# ── 15. file-not-found (9 samples) ───────────────────────────────────────────
add("file-not-found", """
FileNotFoundError: [Errno 2] No such file or directory: '/app/config/settings.yaml'
""")
add("file-not-found", """
FileNotFoundError: [Errno 2] No such file or directory: '/data/models/checkpoint.pt'
""")
add("file-not-found", """
FileNotFoundError: [Errno 2] No such file or directory: '/etc/app/secrets.json'
""")
add("file-not-found", """
open /app/data/seed.sql: no such file or directory
""")
add("file-not-found", """
open /config/nginx.conf: no such file or directory
""")
add("file-not-found", """
ENOENT: no such file or directory, open '/app/.env'
""")
add("file-not-found", """
ENOENT: no such file or directory, open '/usr/local/share/app/schema.json'
""")
add("file-not-found", """
error: could not load config: open /home/runner/config.toml: no such file or directory
""")
add("file-not-found", """
error: template not found: /app/templates/email/welcome.html
""")

# ── 16. shell-command-not-found (4 samples, keep lean) ───────────────────────
add("shell-command-not-found", """
bash: docker: command not found
""")
add("shell-command-not-found", """
bash: kubectl: command not found
""")
add("shell-command-not-found", """
bash: git: command not found
""")
add("shell-command-not-found", """
bash: python3: command not found
""")


# ── Write output ──────────────────────────────────────────────────────────────
out = Path(__file__).parent / "corpus_v2.jsonl"
with open(out, "w") as f:
    for sample in SAMPLES:
        f.write(json.dumps(sample) + "\n")

# Stats
from collections import Counter
counts = Counter(s["class"] for s in SAMPLES)
print(f"Written {len(SAMPLES)} samples to {out}")
print(f"{len(counts)} classes:\n")
for cls, n in sorted(counts.items()):
    print(f"  {n:3d}  {cls}")
