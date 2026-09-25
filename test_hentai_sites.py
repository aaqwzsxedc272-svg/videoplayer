"""Offline checks for the four sites named in terminal.txt."""

import json

import hentai_sites as sites


class Fake:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers=None, data=None, timeout=25):
        self.calls.append((method, url, data))
        matches = [(needle, response) for needle, response in self.routes if needle in url]
        if not matches:
            return sites._Resp(404, '', {}, url)
        _needle, response = max(matches, key=lambda item: len(item[0]))
        if callable(response):
            return response(method, url, data)
        return response


def test_hosts():
    assert sites.site_kind('https://www.hanime.tv/videos/hentai/example-1') == 'hanime'
    assert sites.site_kind('hentaihaven.xxx') == 'hentaihaven'
    assert sites.site_kind('https://hentaini.com/h/example/2') == 'hentaini'
    assert sites.site_kind('https://hentaimama.io/episodes/example-episode-1/') == 'hentaimama'
    assert sites.site_kind('https://example.com/watch') == ''
    assert sites.needs_fresh_playback('hanime.tv')
    assert sites.needs_fresh_playback('hentaihaven.xxx')
    assert not sites.needs_fresh_playback('hentaini.com')
    assert not sites.needs_fresh_playback('hentaimama.io')


def test_haven_token_roundtrip():
    payload = {'en': 'cipher', 'iv': 'vector', 'uri': 'https://player.example/'}
    opened = sites.haven_decode_token(sites.haven_encode_token(payload))
    assert opened == payload


def test_hanime_seal_roundtrip():
    try:
        sites._aes()
    except ImportError:
        return
    payload = {'slug': 'example-1', 'directive': 'htv_player_handshake'}
    assert sites.hanime_open(sites.hanime_seal(payload)) == payload


def test_hentaini_plays_hls_and_keeps_known_hosters():
    players = json.dumps([
        {'name': 'HLS', 'url': 'https://cdn.example/series/1/1.m3u8'},
        {'name': 'Yourupload', 'url': 'https://www.yourupload.com/embed/abc'},
        {'name': 'Mega', 'url': 'https://mega.nz/embed/ZmVFkaJI#key'},
        {'name': 'StreamHG', 'url': 'https://streamwish.to/e/pqgib6zsycon'},
        {'name': 'HNI', 'url': 'https://player.example/#hash'},
    ])
    downloads = json.dumps([
        {'url': 'https://1024terabox.com/s/1abc'},
        {'url': 'https://www.mediafire.com/file/abc/video.mp4/file'},
    ])
    payload = json.dumps(['ignored', players, downloads])
    fake = Fake([
        ('/_payload.json', sites._Resp(200, payload, {}, 'https://hentaini.com/h/example/1/_payload.json')),
    ])
    found = sites.resolve('https://hentaini.com/h/example/1', fetch=fake)
    assert found['provider'] == 'hentaini'
    assert found['playback_url'].endswith('1.m3u8')
    assert found['stable'] is True
    mirrors = found['mirrors']
    assert 'https://mega.nz/embed/ZmVFkaJI#key' in mirrors
    assert 'https://streamwish.to/e/pqgib6zsycon' in mirrors
    assert 'https://1024terabox.com/s/1abc' in mirrors
    assert 'https://www.yourupload.com/embed/abc' in mirrors
    assert not any('mediafire' in url for url in mirrors)
    assert 'Episode 1' in found['title']


def test_hentaini_series_page_uses_first_direct_episode():
    episode_1 = json.dumps([
        {'name': 'HLS', 'url': 'https://cdn.example/ep1.m3u8'},
        {'name': 'Mega', 'url': 'https://mega.nz/embed/AAAAAAAA#one'},
    ])
    episode_2 = json.dumps([
        {'name': 'HLS', 'url': 'https://cdn.example/ep2.m3u8'},
    ])
    payload = json.dumps([episode_1, episode_2])
    fake = Fake([
        ('/_payload.json', sites._Resp(200, payload)),
    ])
    found = sites.resolve('https://hentaini.com/h/example', fetch=fake)
    assert found['playback_url'].endswith('ep1.m3u8')
    assert len(found['mirrors']) == 1


def test_hanime_prefers_the_free_stream():
    assert sites._hanime_playback([
        {'kind': 'premium', 'src': '/premium.m3u8', 'label': '1080p'},
        {'kind': 'normal', 'src': '/1080.m3u8', 'label': '1080p'},
        {'kind': 'normal', 'src': '/hls/3530/token', 'label': ''},
        {'kind': 'normal', 'src': '/720.m3u8', 'label': '720p'},
    ]) == 'https://hanime.tv/720.m3u8'
    assert sites._hanime_playback([
        {'kind': 'normal', 'src': '/1080.m3u8', 'label': '1080p'},
        {'kind': 'normal', 'src': '/hls/3530/token'},
    ]) == 'https://hanime.tv/hls/3530/token'
    assert sites._hanime_is_hls('https://hanime.tv/hls/3530/token')


def test_hanime_one_stream():
    def handshake(method, url, data):
        body = json.loads(data)
        opened = sites.hanime_open(body['token'])
        assert opened['slug'] == 'example-1'
        manifest = sites.hanime_seal({
            'sources': [
                {'kind': 'premium', 'src': '/premium.m3u8', 'label': '1080p'},
                {'kind': 'normal', 'src': '/720.m3u8', 'label': '720p'},
            ],
        })
        return sites._Resp(200, '', {'X-Token': manifest}, url)

    try:
        sites._aes()
    except ImportError:
        return
    fake = Fake([
        ('/videos/hentai/', sites._Resp(200, '<title>Example 1 - hanime.tv</title>')),
        ('/api/v11/handshake', handshake),
    ])
    found = sites.resolve('https://hanime.tv/videos/hentai/example-1', fetch=fake)
    assert found['provider'] == 'hanime'
    assert found['playback_url'] == 'https://hanime.tv/720.m3u8'
    assert found['mirrors'] == []
    assert found['stable'] is False
    assert found['title'] == 'Example 1'


def test_hentaihaven_challenge_is_not_a_video():
    fake = Fake([
        ('hentaihaven.xxx', sites._Resp(
            200, '<title>Un instant…</title><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>')),
    ])
    assert sites.resolve(
        'https://hentaihaven.xxx/watch/example/episode-1/', fetch=fake) is None


def test_hentaihaven_one_stream():
    token = sites.haven_encode_token({
        'en': 'aaa',
        'iv': 'bbb',
        'uri': 'https://player.example',
    })
    watch = (
        '<title>Example Episode 1 - Hentai Haven</title>'
        '<iframe src="https://player.example/player.php?data=blob"></iframe>'
    )
    player = f'<meta name="x-secure-token" content="{token}">'
    api = json.dumps({
        'status': True,
        'data': {'sources': [{'src': 'https://cdn.example/playlist.m3u8'}]},
    })
    fake = Fake([
        ('/watch/example/episode-1', sites._Resp(200, watch)),
        ('player.php', sites._Resp(200, player)),
        ('/api.php', sites._Resp(200, api)),
    ])
    found = sites.resolve(
        'https://hentaihaven.xxx/watch/example/episode-1/', fetch=fake)
    assert found['provider'] == 'hentaihaven'
    assert found['playback_url'] == 'https://cdn.example/playlist.m3u8'
    assert found['mirrors'] == []
    assert found['title'] == 'Example Episode 1'


def test_hentaihaven_show_page_follows_episode():
    token = sites.haven_encode_token({
        'en': 'aaa', 'iv': 'bbb', 'uri': 'https://player.example',
    })
    show = '<a href="https://hentaihaven.xxx/watch/example/episode-2/">Watch</a>'
    episode = '<iframe src="/player.php?data=blob"></iframe><title>Example Episode 2</title>'
    player = f'<meta content="{token}" name="x-secure-token">'
    api = json.dumps({'status': True, 'data': {'sources': [{'src': 'https://cdn.example/ep2.m3u8'}]}})
    fake = Fake([
        ('/watch/example/episode-2', sites._Resp(200, episode)),
        ('/watch/example/', sites._Resp(200, show, {}, 'https://hentaihaven.xxx/watch/example/')),
        ('player.php', sites._Resp(200, player)),
        ('/api.php', sites._Resp(200, api)),
    ])
    found = sites.resolve('https://hentaihaven.xxx/watch/example/', fetch=fake)
    assert found['playback_url'] == 'https://cdn.example/ep2.m3u8'
    assert any('/episode-2' in url for _method, url, _data in fake.calls)


def test_hentaimama_mirrors():
    page = '''
    <title>Example Episode 1 – Hentaimama</title>
    <div data-id="55"></div>
    <li class="dooplay_player_option" data-type="movie" data-post="55" data-nume="1">mi-1</li>
    <li class="dooplay_player_option" data-post="55" data-nume="2" data-type="movie">mi-2</li>
    '''
    ajax = json.dumps([
        '<iframe src="https://hentaimama.io/new2.php?p=55"></iframe>',
        '<iframe src="https://streamtape.com/e/abc"></iframe>',
        '<iframe src="https://doodstream.com/e/xyz"></iframe>',
    ])
    player = 'jwplayer("v").setup({file:"https://cdn.example/video.mp4"});'
    fake = Fake([
        ('/episodes/', sites._Resp(200, page)),
        ('admin-ajax.php', sites._Resp(200, ajax)),
        ('new2.php', sites._Resp(200, player)),
    ])
    found = sites.resolve(
        'https://hentaimama.io/episodes/example-episode-1/', fetch=fake)
    assert found['provider'] == 'hentaimama'
    assert found['playback_url'] == 'https://cdn.example/video.mp4'
    assert 'https://streamtape.com/e/abc' in found['mirrors']
    assert 'https://doodstream.com/e/xyz' in found['mirrors']
    assert not any('new2.php' in url for url in found['mirrors'])
    assert found['title'].startswith('Example Episode 1')
    actions = []
    for _method, url, data in fake.calls:
        if 'admin-ajax' in url and isinstance(data, dict):
            actions.append(data.get('action'))
    assert 'get_player_contents' in actions
    assert 'doo_player_ajax' in actions


def test_hentaidoge_expiry_is_the_path_segment():
    fresh = (
        'https://hentaidoge.org/s/1790320241/k7o7dNnksMaVA_BUxqbbGg/'
        'output/S/shoujo-ramune/1/hls/master.m3u8'
    )
    assert sites.signed_path_expiry(fresh) == 1790320241.0
    assert sites.signed_path_expiry(
        'https://cdn.example/s/1790320241/token/master.m3u8') == 0.0
    assert sites.signed_path_expiry(
        'https://hentaidoge.org/output/S/shoujo-ramune/1/hls/master.m3u8') == 0.0


def test_hentaimama_show_page_follows_episode():
    show = '<a href="https://hentaimama.io/episodes/example-episode-1/">Ep 1</a>'
    episode = '<title>Example Episode 1</title><div data-id="9"></div>'
    ajax = json.dumps(['<iframe src="https://voe.sx/e/abc"></iframe>'])
    fake = Fake([
        ('/tvshows/', sites._Resp(200, show)),
        ('/episodes/', sites._Resp(200, episode)),
        ('admin-ajax.php', sites._Resp(200, ajax)),
    ])
    found = sites.resolve(
        'https://hentaimama.io/tvshows/example/', fetch=fake)
    assert found['playback_url'] == ''
    assert found['mirrors'] == ['https://voe.sx/e/abc']


def test_caption_files_are_kept():
    base = 'https://octopusmanifest.org/70460589-357e-413d-a0aa-09ba198623d4/s/'
    tracks = sites.caption_tracks([
        base + 'fr.vtt',
        base + 'en.vtt',
        base + 'en.vtt?x=1',
        'https://cdn.havenclick.com/ads/ad.vtt',
        base + 'zh.vtt',
        'https://www.googletagmanager.com/gtm.js',
    ])
    assert [item['lang'] for item in tracks] == ['en', 'fr', 'zh']
    assert tracks[0]['ext'] == 'vtt'
    assert tracks[0]['url'] == base + 'en.vtt'
    buried = sites.caption_tracks_from_blobs([
        '<track src="' + base + 'en.vtt"> and ' + base + 'ja.vtt',
    ])
    assert [item['lang'] for item in buried] == ['en', 'ja']


def test_split_stream_does_not_take_the_other_episode():
    own = 'https://octopusmanifest.org/77c15e9d-befe-45e0-8eea-d345e09dfa8f/'
    other = 'https://octopusmanifest.org/78ff108c-4e0e-40d2-84f3-f1137d94f86a/'
    video, audio = sites.pick_split_stream([
        own + 'playlist.m3u8',
        other + 'vp_7sop/v.m3u8',
        other + 'snd/a.m3u8',
        own + 'vp_7sop/v.m3u8',
        own + 'snd/a.m3u8',
    ])
    assert video == own + 'vp_7sop/v.m3u8'
    assert audio == own + 'snd/a.m3u8'
    video, audio = sites.pick_split_stream([
        own + 'playlist.m3u8',
        other + 'vp_7sop/v.m3u8',
        other + 'snd/a.m3u8',
    ])
    assert video == own + 'playlist.m3u8'
    assert audio == ''


def test_split_playlist_uses_the_video_not_the_master():
    base = 'https://octopusmanifest.org/70460589-357e-413d-a0aa-09ba198623d4/'
    video, audio = sites.pick_split_stream([
        base + 'playlist.m3u8',
        'https://www.googletagmanager.com/gtm.js?id=1',
        base + 'playlist_vp9.m3u8',
        base + 'vp_7sop/v.m3u8',
        base + 'snd/a.m3u8',
        'https://cdn.havenclick.com/ads/ad.mp4',
        'https://hentaihaven.xxx/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=abc',
    ])
    assert video == base + 'vp_7sop/v.m3u8'
    assert audio == base + 'snd/a.m3u8'


def test_cleared_page_is_read_and_the_check_is_not():
    token = sites.haven_encode_token({
        'en': 'aaa', 'iv': 'bbb', 'uri': 'https://player.example',
    })
    watch = (
        '<title>Example Episode 1 - Hentai Haven</title>'
        '<iframe src="https://player.example/player.php?data=blob"></iframe>'
    )
    player = f'<meta name="x-secure-token" content="{token}">'
    api = json.dumps({
        'status': True,
        'data': {'sources': [{'src': 'https://cdn.example/playlist.m3u8'}]},
    })
    inner = Fake([
        ('player.php', sites._Resp(200, player)),
        ('/api.php', sites._Resp(200, api)),
    ])
    found = sites.resolve(
        'https://hentaihaven.xxx/watch/example/episode-1/',
        fetch=sites.fetch_using_page(
            'https://hentaihaven.xxx/watch/example/episode-1/', watch, inner))
    assert found['playback_url'] == 'https://cdn.example/playlist.m3u8'
    assert not any('episode-1' in url for _method, url, _data in inner.calls)
    challenge = Fake([
        ('hentaihaven.xxx', sites._Resp(200, '<title>should not be used</title>')),
    ])
    blocked = sites.fetch_using_page(
        'https://hentaihaven.xxx/watch/example/episode-1/',
        '<title>Just a moment...</title>',
        challenge)
    page = blocked('GET', 'https://hentaihaven.xxx/watch/example/episode-1/')
    assert 'should not be used' in page.text


def test_capture_ignores_the_cloudflare_page():
    blob = (
        b'https://hentaihaven.xxx/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'
        b'?ray=a406da0a4d1f026a '
        b'https://cdn.example/playlist.m3u8?token=abc '
        b'<meta name="x-secure-token" content="sha512-token"> '
        b'src="//player.example/player.php?data=blobdata"'
    )
    found = sites.urls_from_capture(blob)
    assert found['media'] == ['https://cdn.example/playlist.m3u8?token=abc']
    assert found['token'] == 'sha512-token'
    assert found['players'] == ['https://player.example/player.php?data=blobdata']
    sites.resolve(
        'https://hentaihaven.xxx/watch/example/episode-1/',
        fetch=Fake([
            ('hentaihaven.xxx', sites._Resp(200, '<title>Just a moment...</title>')),
        ]))
    assert sites.last_block == 'cloudflare'


def main():
    tests = [
        test_hosts,
        test_haven_token_roundtrip,
        test_hanime_seal_roundtrip,
        test_hentaini_plays_hls_and_keeps_known_hosters,
        test_hentaini_series_page_uses_first_direct_episode,
        test_hanime_prefers_the_free_stream,
        test_hanime_one_stream,
        test_caption_files_are_kept,
        test_split_stream_does_not_take_the_other_episode,
        test_split_playlist_uses_the_video_not_the_master,
        test_cleared_page_is_read_and_the_check_is_not,
        test_hentaihaven_challenge_is_not_a_video,
        test_capture_ignores_the_cloudflare_page,
        test_hentaihaven_one_stream,
        test_hentaihaven_show_page_follows_episode,
        test_hentaimama_mirrors,
        test_hentaidoge_expiry_is_the_path_segment,
        test_hentaimama_show_page_follows_episode,
    ]
    for test in tests:
        test()
        print('ok', test.__name__)
    print(f'{len(tests)} passed')


if __name__ == '__main__':
    main()
