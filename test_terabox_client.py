"""Parser tests for TeraBox share links. No network."""

import terabox_client


def test_surl_from_short_path_strips_routing_marker():
    url = 'https://www.terabox.com/s/1AbCdEfGhIjKl'
    assert terabox_client.surl_from(url) == 'AbCdEfGhIjKl'


def test_surl_from_query():
    url = 'https://www.1024tera.com/sharing/link?surl=AbCdEfGhIj'
    assert terabox_client.surl_from(url) == 'AbCdEfGhIj'


def test_host_match_is_suffix_not_substring():
    assert terabox_client.is_terabox_host('www.terabox.com')
    assert terabox_client.is_terabox_host('1024terabox.com')
    assert terabox_client.is_terabox_host('dm.1024tera.com')
    assert not terabox_client.is_terabox_host('notterabox.com')
    assert not terabox_client.is_terabox_host('example.com')


def test_file_page_keeps_fid_and_name():
    page = terabox_client.file_page_url(
        'https://terabox.app/s/1AbCdEfGhIjKl',
        'AbCdEfGhIjKl',
        '9988',
        'clip.mp4',
    )
    assert terabox_client.fs_id_from(page) == '9988'
    assert terabox_client.file_name_from(page) == 'clip.mp4'
    assert '/s/1AbCdEfGhIjKl' in page


def test_token_from_trampoline():
    html = '<script>fn%28%22abcdef0123456789abcdef%22%29</script>'
    tokens = terabox_client.extract_tokens(html)
    assert tokens.get('jsToken') == 'abcdef0123456789abcdef'


def test_password_from_query_only():
    assert terabox_client.password_from_url(
        'https://www.terabox.com/s/1AbCdEfGhIjKl?pwd=secret'
    ) == 'secret'
    assert terabox_client.password_from_url('https://www.terabox.com/s/1AbCdEfGhIjKl') == ''


def test_thumbnail_is_not_the_film():
    thumb = (
        'https://data.1024tera.com/thumbnail/1d3b6bbb51f5f9ef5f9eb12e5bf08770'
        '?fid=796624567225084&ft=video'
    )
    stream = (
        'https://www.1024tera.com/share/streaming?uk=4399467314851'
        '&shareid=26693364161&type=M3U8_FLV_264_480&fid=796624567225084'
        '&sign=abc&timestamp=1710000000&jsToken=secret'
    )
    assert terabox_client.is_thumbnail_url(thumb)
    assert not terabox_client.is_streaming_url(thumb)
    assert terabox_client.is_streaming_url(stream)
    assert not terabox_client.is_thumbnail_url(stream)
    params = terabox_client.streaming_params_from_url(stream)
    assert params['uk'] == '4399467314851'
    assert params['shareid'] == '26693364161'
    assert params['fid'] == '796624567225084'
    assert params['type'] == 'M3U8_FLV_264_480'


def test_streaming_url_prefers_the_page_player_shape():
    url = terabox_client.build_streaming_url(
        'https://www.1024tera.com',
        {
            'uk': '4399467314851',
            'shareid': '26693364161',
            'sign': 'abc',
            'timestamp': '1710000000',
        },
        '796624567225084',
        'secret-token',
        'M3U8_FLV_264_480',
    )
    assert '/share/streaming?' in url
    assert 'fid=796624567225084' in url
    assert 'type=M3U8_FLV_264_480' in url
    assert 'pwd=' not in url
    assert terabox_client.streaming_type_rank(
        url.replace('M3U8_FLV_264_480', 'M3U8_FLV_264_720')
    ) > terabox_client.streaming_type_rank(url)


def test_short_complete_playlist_is_the_film():
    # Shape only. A finished 30-second playlist is not a one-chunk burst.
    # It is still the guest preview when the listed file is longer.
    text = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://cdn.example/a.ts
#EXTINF:10.0,
https://cdn.example/b.ts
#EXTINF:10.0,
https://cdn.example/c.ts
#EXT-X-ENDLIST
"""
    assert terabox_client.classify_playlist(text) == 'normal'
    prepared = terabox_client.prepare_playback_playlist(text, 'https://www.1024tera.com/share/streaming')
    assert 'https://cdn.example/a.ts' in prepared
    assert '#EXT-X-PLAYLIST-TYPE:VOD' in prepared
    assert '#EXT-X-ENDLIST' in prepared


def test_one_random_chunk_is_not_the_film():
    text = """#EXTM3U
#EXTINF:6.0,
https://data.1024tera.com/abc/file_17_ts/seg.ts?sign=1
#EXT-X-ENDLIST
"""
    assert terabox_client.classify_playlist(text) == 'random_chunk'


def test_complete_indexed_playlist_is_the_film():
    text = """#EXTM3U
#EXTINF:6.0,
https://cdn.example/file_0_ts/a.ts
#EXTINF:6.0,
https://cdn.example/file_1_ts/b.ts
#EXT-X-ENDLIST
"""
    assert terabox_client.classify_playlist(text) == 'normal'


def test_one_segment_at_the_start_with_endlist_is_the_film():
    text = """#EXTM3U
#EXTINF:30.0,
https://cdn.example/file_0_ts/a.ts
#EXT-X-ENDLIST
"""
    assert terabox_client.classify_playlist(text) == 'normal'


def test_local_playlist_server_serves_the_film_text():
    import urllib.request
    url = terabox_client._serve_m3u8(
        '#EXTM3U\n#EXTINF:1.0,\nhttps://cdn.example/a.ts\n#EXT-X-ENDLIST\n'
    )
    assert url.startswith('http://127.0.0.1:')
    with urllib.request.urlopen(url, timeout=3) as response:
        body = response.read().decode('utf-8')
    assert 'https://cdn.example/a.ts' in body
    assert 'thumbnail' not in body


def test_thumbnail_playlist_is_not_the_film():
    text = """#EXTM3U
#EXTINF:1.0,
https://data.1024tera.com/thumbnail/1d3b6bbb51f5f9ef5f9eb12e5bf08770?ft=video
#EXT-X-ENDLIST
"""
    assert terabox_client.classify_playlist(text) == 'thumbnail'


PREVIEW = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://cdn.example/a.ts
#EXTINF:10.0,
https://cdn.example/b.ts
#EXTINF:10.0,
https://cdn.example/c.ts
#EXT-X-ENDLIST
"""


def test_guest_preview_is_not_played_as_the_film():
    assert terabox_client.looks_like_guest_preview(PREVIEW, duration_ms=90 * 60 * 1000)
    assert terabox_client.looks_like_guest_preview(PREVIEW, size_bytes=500 * 1024 * 1024)
    assert terabox_client.looks_like_guest_preview(PREVIEW)
    assert not terabox_client.looks_like_guest_preview(PREVIEW, duration_ms=30000)
    # Six minutes of a ninety-minute file is still not the film.
    long_film = """#EXTM3U
#EXTINF:120.0,
https://cdn.example/a.ts
#EXTINF:120.0,
https://cdn.example/b.ts
#EXTINF:120.0,
https://cdn.example/c.ts
#EXT-X-ENDLIST
"""
    assert terabox_client.looks_like_guest_preview(long_film, duration_ms=90 * 60 * 1000)
    assert not terabox_client.looks_like_guest_preview(long_film, duration_ms=360000)
    refused = terabox_client._playlist_playback(
        None,
        'https://www.terabox.com/share/streaming?type=M3U8_FLV_264_480',
        PREVIEW,
        'https://www.terabox.com/',
        {},
        duration_ms=90 * 60 * 1000,
        size_bytes=800 * 1024 * 1024,
    )
    assert refused['error_code'] == 'preview'
    assert not refused.get('ok')
    assert not refused.get('playback_url')


def test_login_cookie_is_ndus_only():
    assert terabox_client.has_login_cookie('lang=en; ndus=present')
    assert not terabox_client.has_login_cookie('lang=en; csrfToken=abc')
    assert not terabox_client.has_login_cookie('ndus=')
    assert terabox_client.file_outlasts_preview({'duration_ms': 120000, 'size': 10})
    assert terabox_client.file_outlasts_preview({'duration_ms': 0, 'size': 80 * 1024 * 1024})
    assert not terabox_client.file_outlasts_preview({'duration_ms': 30000, 'size': 4 * 1024 * 1024})


def test_logged_in_share_asks_the_pasted_host_first():
    bases = terabox_client._api_bases(
        'https://www.1024tera.com/s/1abc', prefer_pasted=True)
    assert bases[0] == 'https://www.1024tera.com'
    short = terabox_client._api_bases(
        'https://teraboxlink.com/s/1abc', prefer_pasted=True)
    assert short[0] == 'https://www.terabox.com'


def test_saved_target_reads_the_transfer_path():
    path, fs_id = terabox_client._saved_target({
        'errno': 0,
        'extra': {'list': [{'to': '/Antigravity/film.mp4', 'to_fs_id': 99}]},
    })
    assert path == '/Antigravity/film.mp4'
    assert fs_id == '99'
    path, fs_id = terabox_client._saved_target({
        'errno': 0,
        'info': [{'path': '/Antigravity/film.mp4', 'fs_id': '7'}],
    })
    assert path == '/Antigravity/film.mp4'
    assert fs_id == '7'
