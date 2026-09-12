"""Execute the REAL functions shipped in main.py against the reported cases.

Nothing here re-implements the logic: each function body is lifted verbatim
out of main.py by AST and exec'd, so a regression in main.py fails these.
"""
import ast
import os
import re
from html import unescape as html_unescape
from urllib.parse import urlparse

SRC = open('main.py', encoding='utf-8').read()
TREE = ast.parse(SRC)


G = {
    're': re, 'os': os, 'urlparse': urlparse,
    'html_unescape': html_unescape, 'print': print,
}


def lift(class_name, func_name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name == func_name:
                    mod = ast.Module(body=[item], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, f'<{class_name}.{func_name}>', 'exec'), G)
                    return G[func_name]
    raise AssertionError(f'{class_name}.{func_name} not found in main.py')


# ── 1. title cleaning ────────────────────────────────────────────────────────
def lift_attr(class_name, attr_name):
    """Exec a class-body assignment from main.py verbatim and return its value."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == attr_name
                        for t in item.targets):
                    mod = ast.Module(body=[item], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    exec(compile(mod, f'<{class_name}.{attr_name}>', 'exec'), G)
                    return G[attr_name]
    raise AssertionError(f'{class_name}.{attr_name} not found in main.py')


banned = lift('VideoPlayer', '_is_banned_stream_title')
banned_fn = banned.__func__ if isinstance(banned, classmethod) else banned
clean = lift('VideoPlayer', '_clean_remote_title')


class TitleStub:
    _BANNED_STREAM_TITLES = lift_attr('VideoPlayer', '_BANNED_STREAM_TITLES')

    @classmethod
    def _is_banned_stream_title(cls, title):
        return banned_fn(cls, title)

    _clean_remote_title = clean


t = TitleStub()
cases = [
    ('Watch Dressage (1986) - Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) | Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) – Erotic Movies', 'Dressage (1986)'),
    ('Watch Dressage (1986) - EroticMV', 'Dressage (1986)'),
    ('Watch Dressage (1986) - eroticmv.com', 'Dressage (1986)'),
    ('Dressage (1986)', 'Dressage (1986)'),
    ('  Watch   Some Film   -   Erotic Movies  ', 'Some Film'),
]
fails = 0
for raw, want in cases:
    got = t._clean_remote_title(raw)
    ok = got == want
    fails += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {raw!r} -> {got!r} (want {want!r})")

# ── 2. seekRelative ──────────────────────────────────────────────────────────
lifted_time_pos = lift('MpvMediaPlayerAdapter', '_mpv_time_pos_ms')
lifted_seek = lift('MpvMediaPlayerAdapter', 'seekRelative')


class FakeMpv:
    def __init__(self, time_pos):
        self.time_pos = time_pos
        self.calls = []

    def get_property(self, name):
        assert name == 'time-pos', name
        return self.time_pos

    def seek(self, seconds, *flags):
        self.calls.append((seconds, flags))

    def command(self, *a):
        pass


class SeekStub:
    _mpv_time_pos_ms = lifted_time_pos
    seekRelative = lifted_seek

    def __init__(self, time_pos_s, duration_ms, file_loaded=True):
        self._mpv = FakeMpv(time_pos_s)
        self._file_loaded = file_loaded
        self._duration_ms = duration_ms
        self._position_ms = int((time_pos_s or 0) * 1000)
        self._pending_seek_ms = 999

    def position(self):
        return int(self._position_ms)

    def duration(self):
        return int(self._duration_ms)

    def setPosition(self, ms):
        self._last_set_position = max(0, int(ms))
        if self._mpv is not None:
            self._mpv.calls.append((self._last_set_position / 1000.0, ('setPosition',)))


def check_seek(label, stub, delta, want_s, want_flags):
    stub.seekRelative(delta)
    got = stub._mpv.calls
    ok = len(got) == 1 and abs(got[0][0] - want_s) < 1e-6 \
        and tuple(got[0][1]) == tuple(want_flags)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: seek{got} (want {want_s}s {want_flags})")
    return 0 if ok else 1


fails += check_seek('right arrow at 100s, dur 1h',
                    SeekStub(100.0, 3600_000), 3000, 103.0, ('absolute', 'exact'))
fails += check_seek('left arrow at 100s, dur 1h',
                    SeekStub(100.0, 3600_000), -3000, 97.0, ('absolute', 'exact'))
fails += check_seek('left arrow at 1.5s clamps to 0',
                    SeekStub(1.5, 3600_000), -3000, 0.0, ('absolute', 'exact'))
fails += check_seek('right arrow past end clamps to duration',
                    SeekStub(3599.0, 3600_000), 3000, 3600.0, ('absolute', 'exact'))
fails += check_seek('left after clicking bar to 1800s',
                    SeekStub(1800.0, 3600_000), -3000, 1797.0, ('absolute', 'exact'))

# time-pos unavailable -> cached position() is the base
s = SeekStub(None, 3600_000)
s._position_ms = 500_000
s.seekRelative(-3000)
got = s._mpv.calls
ok = len(got) == 1 and abs(got[0][0] - 497.0) < 1e-6 and got[0][1] == ('absolute', 'exact')
fails += (not ok)
print(f"  {'PASS' if ok else 'FAIL'}  time-pos None falls back to position(): {got}")

# exact rejected -> keyframe ABSOLUTE fallback, never 'relative'
s = SeekStub(100.0, 3600_000)


def _raise(seconds, *flags):
    if 'exact' in flags:
        raise RuntimeError('mpv: exact unsupported')
    s._mpv.calls.append((seconds, flags))


s._mpv.seek = _raise
s.seekRelative(3000)
ok = len(s._mpv.calls) == 1 and abs(s._mpv.calls[0][0] - 103.0) < 1e-6 \
    and s._mpv.calls[0][1] == ('absolute',)
fails += (not ok)
print(f"  {'PASS' if ok else 'FAIL'}  exact unsupported -> absolute fallback: {s._mpv.calls}")

# mpv absent -> setPosition path, still absolute target
s = SeekStub(100.0, 3600_000)
s._mpv = None
s.seekRelative(3000)
ok = getattr(s, '_last_set_position', None) == 103000
fails += (not ok)
_last = getattr(s, '_last_set_position', None)
print(f"  {'PASS' if ok else 'FAIL'}  no-mpv path -> setPosition({_last}) (want 103000)")

print()
print('FAILURES:', fails)
raise SystemExit(1 if fails else 0)
