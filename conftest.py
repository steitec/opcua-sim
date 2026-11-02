import sys


def pytest_configure(config):
    if sys.platform != "win32":
        return
    # A connection is closed and the event loop ends before the proactor has finished closing it,
    # so its transport and socket are collected unfinished. That happens when a loop ends, not while
    # a server runs. Only on Windows: elsewhere these warnings still fail the tests.
    for pattern in ("<function _ProactorBasePipeTransport.__del__", "<socket.socket"):
        config.addinivalue_line(
            "filterwarnings",
            f"ignore:Exception ignored in. {pattern}:pytest.PytestUnraisableExceptionWarning")
