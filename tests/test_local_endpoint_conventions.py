"""One file owns the local endpoint lifecycle, and this is what says so.

#390 is a socket-ownership bug: an endpoint that closes an accepted
connection before its client has released the client's own transport
strands that transport on the Windows proactor, and the `OSError` it
raises is reported later against an unrelated test. The fix was to decide
the ordering once, in `tests/local_endpoint.py`, and to route every suite
through it.

A fix like that lasts exactly as long as the next hand-written endpoint.
The three lines below are all it takes to reintroduce the bug:

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

So they are read as syntax, not as text. Every tracked module under
`tests/` is parsed, and a module other than the helper that constructs or
subclasses an HTTP server, TLS-wraps a *listening* socket, or reaches
`serve_forever` is named with its line. A module that will not parse is
named too: a file this contract cannot read is one it cannot clear.

Reading syntax is what makes the contract precise enough to live without
an allowlist of exceptions. The shapes below are legitimate and stay
unnamed, because none of them owns an accepted connection:

- a port probe that binds and releases a socket without serving on it;
- a *client-side* `wrap_socket()`, which wraps the client's own socket;
- a fake that merely defines a `wrap_socket()` method;
- a request handler class, including one nested inside a function;
- prose, and source fixtures quoted as strings — such as the ones in this
  module, which are written to temporary files and parsed from there
  precisely so that the contract's own examples are never mistaken for the
  repository's code.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: The one module allowed to stand a local endpoint up. Not a list, and
#: not a pattern: an exception granted to a second file is the drift this
#: contract exists to prevent.
HELPER = "tests/local_endpoint.py"

RULE_SERVER = "builds an HTTP server"
RULE_LISTENER_TLS = "wraps the listening socket in TLS"
RULE_ACCEPT_LOOP = "starts the accept loop"
RULE_UNREADABLE = "cannot be parsed"

#: The stdlib servers, by the dotted path an import resolves to and by the
#: bare name. The resolved path is the precise judgement; the bare name is
#: what a spelling the imports cannot explain is judged by.
_SERVER_PATHS = frozenset({"http.server.HTTPServer", "http.server.ThreadingHTTPServer"})
_SERVER_NAMES = frozenset({"HTTPServer", "ThreadingHTTPServer"})

_WRAP_SOCKET = "wrap_socket"

#: The accept loop, judged wherever it is reached: called outright, or
#: handed to whatever will call it — `threading.Thread(target=...)`,
#: `Thread(group, target)`, `asyncio.to_thread(...)`, an executor, or a
#: `partial()` of any of them. Naming the thread helpers instead would
#: only name the spellings this suite happens to use today.
_ACCEPT_LOOP = "serve_forever"

#: The attribute a `socketserver` server keeps its *listening* socket in.
_LISTENING_SOCKET = "socket"


@dataclass(frozen=True)
class Violation:
    """One line of one module that stands up an endpoint by hand."""

    path: str
    line: int
    rule: str
    detail: str

    def describe(self) -> str:
        """The violation as a path, a line, and what was found there."""
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def _scan_repository(root: Path) -> tuple[Violation, ...]:
    """Judge every tracked test module, in path order."""
    found: list[Violation] = []
    for name in _tracked_test_modules(root):
        found.extend(_scan_file(root / name, name))
    return tuple(found)


def _tracked_test_modules(root: Path) -> tuple[str, ...]:
    """The repository's own test modules, as git knows them.

    Tracked, so an untracked scratch file, a stale `.pyc`, or a virtualenv
    vendored under `tests/` can neither dilute the scan nor fail it.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "tests"],
        check=True,
        capture_output=True,
    )
    names = tuple(
        sorted(name for name in result.stdout.decode("utf-8").split("\0") if name.endswith(".py"))
    )
    assert names, "git listed no tracked test modules, so this scan would clear anything"
    return names


def _scan_file(path: Path, name: str) -> tuple[Violation, ...]:
    """Judge one module, reporting a read failure rather than raising it."""
    if not path.is_file():
        # Tracked but absent from the worktree: a deletion in flight, and a
        # module being deleted cannot reintroduce the lifecycle.
        return ()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return (Violation(name, 1, RULE_UNREADABLE, f"{type(error).__name__}: {error}"),)
    return _scan_source(source, name)


def _scan_source(source: str, name: str) -> tuple[Violation, ...]:
    """Judge one module's syntax, so prose and quoted source are inert.

    Fails closed on a module that will not parse: a file the contract
    cannot read is a file it cannot clear.
    """
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as error:
        return (Violation(name, error.lineno or 1, RULE_UNREADABLE, error.msg),)
    bindings = _bindings(tree)
    found = [violation for node in ast.walk(tree) for violation in _judge(node, bindings, name)]
    return tuple(sorted(found, key=lambda violation: (violation.line, violation.rule)))


def _bindings(tree: ast.AST) -> dict[str, str]:
    """What each imported name in this module actually refers to.

    `import http.server as web`, `from http.server import HTTPServer as
    Listener` and `from http import server` all end up pointing at the same
    stdlib path, which is what lets the contract judge the thing rather
    than the spelling.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                bindings[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _spelling(node: ast.expr) -> str | None:
    """The dotted name as it is written, or `None` when it is not one."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _resolve(node: ast.expr, bindings: dict[str, str]) -> str | None:
    """The dotted name with its root replaced by whatever was imported."""
    spelling = _spelling(node)
    if spelling is None:
        return None
    root, _, rest = spelling.partition(".")
    target = bindings.get(root)
    if target is None:
        return spelling
    return f"{target}.{rest}" if rest else target


def _judge(node: ast.AST, bindings: dict[str, str], name: str) -> Iterator[Violation]:
    """Everything one node has to answer for."""
    if isinstance(node, ast.ClassDef):
        yield from _judge_bases(node, bindings, name)
    elif isinstance(node, ast.Call):
        yield from _judge_call(node, bindings, name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        yield from _judge_assignment(node, name)


def _judge_bases(node: ast.ClassDef, bindings: dict[str, str], name: str) -> Iterator[Violation]:
    """A server subclass is a second lifecycle, whoever constructs it."""
    for base in node.bases:
        if _is_server(base, bindings):
            yield Violation(
                name,
                node.lineno,
                RULE_SERVER,
                f"class {node.name} extends {_named(base, bindings)}",
            )


def _judge_call(node: ast.Call, bindings: dict[str, str], name: str) -> Iterator[Violation]:
    """A constructed server, and an accept loop started outside the helper."""
    if _is_server(node.func, bindings):
        yield Violation(name, node.lineno, RULE_SERVER, f"{_named(node.func, bindings)}(...)")
    if _is_accept_loop(node.func):
        yield Violation(name, node.lineno, RULE_ACCEPT_LOOP, f".{_ACCEPT_LOOP}() is run from here")
        return
    # Only the call's *own* arguments, never the tree beneath them: a
    # `partial(server.serve_forever)` handed to a thread is one accept
    # loop, and it is named where it is bound rather than twice.
    if any(_is_accept_loop(argument) for argument in _arguments(node)):
        yield Violation(
            name,
            node.lineno,
            RULE_ACCEPT_LOOP,
            f".{_ACCEPT_LOOP} is handed to {_named(node.func, bindings)}",
        )


def _arguments(node: ast.Call) -> Iterator[ast.expr]:
    """Everything this call is passed, positionally or by keyword."""
    yield from node.args
    for keyword in node.keywords:
        yield keyword.value


def _judge_assignment(node: ast.Assign | ast.AnnAssign, name: str) -> Iterator[Violation]:
    """TLS on the *listening* socket, which is not TLS on an accepted one.

    Wrapping the listener makes `accept()` hand a refused connection to
    `ssl.SSLSocket._create`, which closes it — the endpoint closing first,
    which is #390.
    """
    value = node.value
    if not isinstance(value, ast.Call) or _tail(value.func) != _WRAP_SOCKET:
        return
    wrapped = value.args[0] if value.args else None
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if _is_listening_socket(wrapped) or any(_is_listening_socket(target) for target in targets):
        yield Violation(
            name,
            node.lineno,
            RULE_LISTENER_TLS,
            f"{_WRAP_SOCKET}() is applied to a server's .{_LISTENING_SOCKET}",
        )


def _is_server(node: ast.expr, bindings: dict[str, str]) -> bool:
    resolved = _resolve(node, bindings)
    if resolved is None:
        return False
    return resolved in _SERVER_PATHS or resolved.rsplit(".", 1)[-1] in _SERVER_NAMES


def _named(node: ast.expr, bindings: dict[str, str]) -> str:
    """How the node is written, and what it resolves to when they differ."""
    spelling = _spelling(node)
    if spelling is None:
        return "a call"
    resolved = _resolve(node, bindings)
    return spelling if resolved in (None, spelling) else f"{spelling} ({resolved})"


def _is_accept_loop(node: ast.expr) -> bool:
    """Whether this expression *is* the accept loop, rather than reaches one."""
    return isinstance(node, ast.Attribute) and node.attr == _ACCEPT_LOOP


def _is_listening_socket(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == _LISTENING_SOCKET


def _tail(node: ast.expr) -> str | None:
    """The last segment of a dotted name: the method or class being used."""
    spelling = _spelling(node)
    return None if spelling is None else spelling.rsplit(".", 1)[-1]


def _module(tmp_path: Path, source: str, name: str = "fixture_endpoint.py") -> Path:
    """Write one source fixture to its own module, and say where it landed."""
    path = tmp_path / name
    path.write_text(dedent(source).lstrip(), encoding="utf-8")
    return path


def _scan(tmp_path: Path, source: str) -> tuple[Violation, ...]:
    """Parse one source fixture the way the repository scan parses a module."""
    name = "fixture_endpoint.py"
    return _scan_file(_module(tmp_path, source, name), f"tests/{name}")


def _rules(violations: tuple[Violation, ...]) -> list[str]:
    return [violation.rule for violation in violations]


def test_a_stdlib_http_server_construction_is_named(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        """
        from http.server import ThreadingHTTPServer

        def endpoint(handler):
            return ThreadingHTTPServer(("127.0.0.1", 0), handler)
        """,
    )

    assert _rules(violations) == [RULE_SERVER]
    assert violations[0].line == 4
    assert "ThreadingHTTPServer" in violations[0].detail


def test_a_dotted_http_server_construction_is_named(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        """
        import http.server

        def endpoint(handler):
            return http.server.HTTPServer(("127.0.0.1", 0), handler)
        """,
    )

    assert _rules(violations) == [RULE_SERVER]
    assert "http.server.HTTPServer" in violations[0].detail


def test_an_aliased_http_server_import_is_named(tmp_path: Path) -> None:
    """An alias renames the spelling, not the lifecycle it stands up."""
    violations = _scan(
        tmp_path,
        """
        import http.server as web
        from http.server import HTTPServer as Listener

        def aliased_module(handler):
            return web.ThreadingHTTPServer(("127.0.0.1", 0), handler)

        def aliased_name(handler):
            return Listener(("127.0.0.1", 0), handler)
        """,
    )

    assert _rules(violations) == [RULE_SERVER, RULE_SERVER]
    assert [violation.line for violation in violations] == [5, 8]


def test_a_server_subclass_is_named(tmp_path: Path) -> None:
    """Subclassing the server is how a second lifecycle gets written."""
    violations = _scan(
        tmp_path,
        """
        from http.server import ThreadingHTTPServer

        class MyServer(ThreadingHTTPServer):
            daemon_threads = True
        """,
    )

    assert _rules(violations) == [RULE_SERVER]
    assert violations[0].line == 3


def test_tls_wrapping_the_listening_socket_is_named(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        """
        def secure(server, context):
            server.socket = context.wrap_socket(server.socket, server_side=True)
        """,
    )

    assert _rules(violations) == [RULE_LISTENER_TLS]
    assert violations[0].line == 2


def test_a_thread_started_on_serve_forever_is_named(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        """
        import threading

        def start(server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        """,
    )

    assert _rules(violations) == [RULE_ACCEPT_LOOP]
    assert violations[0].line == 4


def test_a_positional_thread_target_on_serve_forever_is_named(tmp_path: Path) -> None:
    """`Thread(group, target)` starts the same accept loop as `target=`."""
    violations = _scan(
        tmp_path,
        """
        from threading import Thread

        def start(server):
            Thread(None, server.serve_forever).start()
        """,
    )

    assert _rules(violations) == [RULE_ACCEPT_LOOP]
    assert violations[0].line == 4


def test_an_accept_loop_wrapped_in_a_partial_or_lambda_is_named(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        """
        import asyncio
        import threading
        from functools import partial

        def start(server):
            threading.Thread(target=partial(server.serve_forever, poll_interval=0.05)).start()

        def start_lambda(server):
            threading.Thread(target=lambda: server.serve_forever()).start()

        async def start_worker(server):
            await asyncio.to_thread(server.serve_forever)
        """,
    )

    assert _rules(violations) == [RULE_ACCEPT_LOOP] * 3
    assert [violation.line for violation in violations] == [6, 9, 12]


def test_running_the_accept_loop_without_a_thread_helper_is_named(tmp_path: Path) -> None:
    """The accept loop is the violation, not the spelling that starts it.

    An executor, or a call made straight from a worker function, runs the
    same loop that `threading.Thread` would.
    """
    violations = _scan(
        tmp_path,
        """
        def start(loop, server):
            return loop.run_in_executor(None, server.serve_forever)

        def run(server):
            server.serve_forever(poll_interval=0.05)
        """,
    )

    assert _rules(violations) == [RULE_ACCEPT_LOOP] * 2
    assert [violation.line for violation in violations] == [2, 5]


def test_every_prohibited_form_in_a_module_is_named_with_its_line(tmp_path: Path) -> None:
    """The scan reports all of them, in file order, so one run is enough."""
    violations = _scan(
        tmp_path,
        """
        import http.server
        import threading

        def endpoint(handler, context):
            server = http.server.HTTPServer(("127.0.0.1", 0), handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server
        """,
    )

    assert _rules(violations) == [RULE_SERVER, RULE_LISTENER_TLS, RULE_ACCEPT_LOOP]
    assert [violation.line for violation in violations] == [5, 6, 7]
    assert [violation.describe() for violation in violations] == [
        f"tests/fixture_endpoint.py:{violation.line}: {violation.rule}: {violation.detail}"
        for violation in violations
    ]


def test_a_port_probe_that_serves_nothing_is_not_named(tmp_path: Path) -> None:
    """Binding a port to learn it is free starts no endpoint."""
    violations = _scan(
        tmp_path,
        """
        import socket

        def unreachable_endpoint():
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            return f"http://127.0.0.1:{port}"
        """,
    )

    assert violations == ()


def test_a_client_side_tls_wrap_is_not_named(tmp_path: Path) -> None:
    """The client wraps its own socket; nobody's accepted peer is involved."""
    violations = _scan(
        tmp_path,
        """
        import socket
        import ssl

        def connect(port, cafile):
            raw = socket.create_connection(("127.0.0.1", port))
            context = ssl.create_default_context(cafile=str(cafile))
            client = context.wrap_socket(raw, server_hostname="127.0.0.1")
            return client
        """,
    )

    assert violations == ()


def test_a_fake_that_defines_wrap_socket_is_not_named(tmp_path: Path) -> None:
    """Defining the method is not calling it on a listener."""
    violations = _scan(
        tmp_path,
        """
        class UnexpectedHandshakeContext:
            def __init__(self):
                self.wrapped = None

            def wrap_socket(self, raw, **_):
                self.wrapped = UnexpectedHandshakeSocket(raw)
                return self.wrapped
        """,
    )

    assert violations == ()


def test_a_handler_class_nested_in_a_function_is_not_named(tmp_path: Path) -> None:
    """Handlers are what the helper is *given*; they are not a lifecycle."""
    violations = _scan(
        tmp_path,
        """
        from http.server import BaseHTTPRequestHandler

        from tests.local_endpoint import KeepAliveHandler, served_endpoint

        def endpoint(payload):
            class Handler(KeepAliveHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(payload)

            class Raw(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

            return served_endpoint(Handler)
        """,
    )

    assert violations == ()


def test_prose_and_quoted_source_are_not_named(tmp_path: Path) -> None:
    """The scan reads syntax, so a module may still talk about the bug."""
    violations = _scan(
        tmp_path,
        '''
        """Why threading.Thread(target=server.serve_forever) is forbidden."""

        PROGRAM = """
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        """
        ''',
    )

    assert violations == ()


def test_a_tracked_module_that_cannot_be_parsed_fails_the_scan(tmp_path: Path) -> None:
    """Fail closed: a module the scan cannot read is a module it cannot clear."""
    violations = _scan(
        tmp_path,
        """
        def endpoint(
        """,
    )

    assert _rules(violations) == [RULE_UNREADABLE]
    assert violations[0].line >= 1


def test_the_scan_reads_only_the_test_modules_git_tracks() -> None:
    """The scan's input comes from git, so scratch files cannot dilute it."""
    tracked = _tracked_test_modules(REPOSITORY_ROOT)

    assert HELPER in tracked
    assert "tests/test_local_endpoint.py" in tracked
    assert all(name.startswith("tests/") and name.endswith(".py") for name in tracked)
    assert len(tracked) > 50, "git listed too few test modules for the scan to mean anything"


def test_the_helper_is_where_the_lifecycle_actually_lives() -> None:
    """A canary: the scan is only meaningful if it can still see the real thing."""
    found = {violation.rule for violation in _scan_file(REPOSITORY_ROOT / HELPER, HELPER)}

    assert found == {RULE_SERVER, RULE_ACCEPT_LOOP}


def test_only_the_endpoint_helper_stands_up_a_local_endpoint() -> None:
    violations = [
        violation for violation in _scan_repository(REPOSITORY_ROOT) if violation.path != HELPER
    ]

    assert violations == [], "\n".join(
        [
            f"{len(violations)} test module line(s) stand up a local endpoint by hand;"
            f" {HELPER} owns that lifecycle for the whole suite (#390):",
            *[violation.describe() for violation in violations],
        ]
    )
