"""Measure the Metadata Linker's ranking accuracy against the SHIPPED database.

The linker auto-applies any candidate with signal_count >= 2 (see
MetadataScraperDialog._run_match), so a wrong top candidate is not a cosmetic
problem: it overwrites the row's display name.

For every movie we synthesise the query strings the app really feeds the
matcher, then ask whether match() (limit=1, include_weak=False -- exactly the
auto-apply path) returns that same movie.

Usage: python3 tools_eval_metadata_linker.py [db.json] [sample]
"""
import os
import random
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# metadata_scraper imports PyQt6 at module scope; the ranking logic under test
# needs none of it, so stub it out and the module imports headless.
if 'PyQt6' not in sys.modules:
    class _QStub:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, n):
            return _QStub()

        def __call__(self, *a, **k):
            return _QStub()

    class _QtModule(types.ModuleType):
        def __getattr__(self, n):
            return (lambda *a, **k: _QStub()) if n == 'pyqtSignal' else _QStub

    for _name in ('PyQt6', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui'):
        sys.modules[_name] = _QtModule(_name)

import metadata_scraper as ms


def query_forms(slug, movie):
    """The shapes a real row presents to the matcher."""
    out = {}
    out['display_name'] = ms.format_display_name(movie)
    out['page_url'] = movie.get('url') or ''
    out['slug'] = slug
    vid = str(movie.get('video_id') or '')
    title = movie.get('title') or ''
    if vid and title:
        # what a file host / remote row usually looks like
        out['host_style'] = f"{movie.get('source_site') or 'nubiles-porn'} {vid} {title}"
    models = [m for m in (movie.get('models') or []) if not ms._is_male_performer(m)]
    if models and title:
        out['scene_only'] = f"{title} {models[0]}"
    return {k: v for k, v in out.items() if v and v.strip()}


def main():
    dbfile = sys.argv[1] if len(sys.argv) > 1 else 'nubiles_metadata.json'
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    db = ms.MetadataDB(dbfile)
    t = time.time()
    matcher = ms.TitleMatcher(db)
    build = time.time() - t
    slugs = [s for s in db.movies if db.movies[s].get('meta_fetched')]
    random.seed(1234)
    if len(slugs) > sample:
        slugs = random.sample(slugs, sample)
    print(f"{dbfile}: {db.count()} movies, indexed {len(matcher._records)} in {build:.2f}s, "
          f"evaluating {len(slugs)}\n")

    totals = {}
    for slug in slugs:
        movie = db.movies[slug]
        for kind, q in query_forms(slug, movie).items():
            t = time.time()
            hit = matcher.match(q)
            ms_per = (time.time() - t) * 1000
            st = totals.setdefault(kind, {'n': 0, 'ok': 0, 'dup': 0, 'bad': 0,
                                     'ms': 0.0, 'miss': []})
            st['n'] += 1
            st['ms'] += ms_per
            if hit and hit.get('slug') == slug:
                st['ok'] += 1
            elif hit and str(hit.get('video_id') or '') == str(movie.get('video_id') or '') \
                    and str(hit.get('video_id') or ''):
                # A different DB key for the SAME video: the gallery scraper
                # stores some videos twice under different season/episode
                # suffixes. Semantically the right answer.
                st['dup'] += 1
            else:
                st['bad'] += 1
                if len(st['miss']) < 3:
                    st['miss'].append((q[:64], (hit or {}).get('slug', 'NONE')[:44]))

    grand_n = grand_ok = grand_bad = 0
    for kind in sorted(totals):
        st = totals[kind]
        grand_n += st['n']
        grand_ok += st['ok']
        grand_bad += st['bad']
        print(f"  {kind:13s} {st['ok']:5d}/{st['n']:5d} = {100.0*st['ok']/st['n']:5.1f}%   "
              f"(+{st['dup']} same-video duplicate slug, {st['bad']} genuinely wrong)   "
              f"avg {st['ms']/st['n']:5.1f} ms/query")
        for q, got in st['miss']:
            print(f"        miss  {q!r}\n              -> {got}")
    print(f"\n  OVERALL       {grand_ok:5d}/{grand_n:5d} = {100.0*grand_ok/grand_n:5.1f}%   "
          f"genuinely wrong: {grand_bad} ({100.0*grand_bad/grand_n:.2f}%)")


if __name__ == '__main__':
    main()
