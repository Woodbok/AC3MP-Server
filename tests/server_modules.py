"""The game server as one namespace, for the self-tests.

The server is a package of modules (ac3server/); a test wants to reach any of its functions and tables by name, and
to change a setting or swap a function for the length of a check. `load()` returns an object that does both:

    ps = server_modules.load()
    ps.GAME_SESSIONS.clear()              # read: the module that defines the name
    ps.NOTIFY_GROUP_EVENTS = False        # a setting: set in server_config, where the server reads it
    ps.send_notification = fake           # anything else: set in every server module that holds the name

Names are the package's own: a helper used across modules has no leading underscore (party_of, not _party_of).
"""
import contextlib
import importlib
import io
import os
import pkgutil
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class ServerNamespace:
    def __init__(self, modules, config):
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_config", config)

    def __getattr__(self, name):
        # the defining module first (a function's or class's __module__), then any module that has it
        holders = [m for m in self._modules if hasattr(m, name)]
        for m in holders:
            value = getattr(m, name)
            if getattr(value, "__module__", None) == m.__name__:
                return value
        if holders:
            return getattr(holders[0], name)
        if hasattr(self._config, name):
            return getattr(self._config, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if hasattr(self._config, name):
            setattr(self._config, name, value)
            return
        holders = [m for m in self._modules if hasattr(m, name) and not isinstance(getattr(m, name), types.ModuleType)]
        if not holders:
            raise AttributeError(f"no server module has {name!r}")
        for m in holders:
            setattr(m, name, value)


def load(quiet=True):
    """A fresh copy of the server - every table empty, as at start-up - as one namespace. Its modules are imported
    anew on every call (quietly: building the game settings prints); server_config is shared, as in the server."""
    for name in [n for n in sys.modules if n == "ac3server" or n.startswith("ac3server.")]:
        del sys.modules[name]
    out = io.StringIO() if quiet else sys.stdout
    with contextlib.redirect_stdout(out):
        import ac3server
        modules = [importlib.import_module(info.name)
                   for info in pkgutil.walk_packages(ac3server.__path__, "ac3server.")
                   if not info.name.endswith("__main__")]
    import server_config
    return ServerNamespace(modules, server_config)
