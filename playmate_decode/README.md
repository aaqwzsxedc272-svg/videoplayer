# How playmate.to's stream endpoint was established

Evidence for `_resolve_playmate_source` in `main.py`. Nothing here runs in the
app; it is the reasoning trail, kept so the endpoint can be re-derived when
playmate re-obfuscates their player.

`https://playmate.to/assets/js/player-core.min.js` is
[javascript-obfuscator](https://github.com/javascript-obfuscator/javascript-obfuscator)
output: every string lives in one array (`_0x70d6`), base64-encoded with a
**custom alphabet that starts lowercase**, and the array is cyclically rotated
at load time until a checksum of `parseInt()`s over selected entries matches.

* `player_core_string_array.js` — the 850 array entries, copied verbatim.
* `decode_and_report.js` — the site's own decoder (`_0x4d3c`), plus the
  rotation offset applied directly.

The checksum loop does not converge over a hand-copied array, so the offset was
pinned from two independent anchors instead: `_0x4d3c(0x521)` must be
`"pathname"` and `_0x4d3c(0x4ec)` must be `"split"`, both of which give
**R = 441**. Every other probe agrees (`Content-`+`Type`, `applicat`+`ion/json`,
`streamin`+`g_url`, `videoSub`+`titles`, `default_`+`sub_lang`, `vast_ads`,
`jwplayer`, `hls`, `sub.info`, `android`, `web`).

    $ node decode_and_report.js

With R = 441 the call in the shipped file decodes to:

    apiURL = '/api/s' + (location.search ? '?' + location.search : '')
    fetch(apiURL, {method: 'POST',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({c: filecode, d: detectDevice()})})
      .then(r => r.json())
      .then(data => ({streaming_url: data.sx, title: data.tx,
                      thumbnail: data.ix, vast_ads: data.ax, ...}))

`filecode` is `getFilecodeFromURL()` — the last path segment of the embed URL.
`detectDevice()` returns `'ios' | 'android' | 'web'`. `setupPlayer()` then feeds
`streaming_url` to JW Player as `playlist[0].file` with `type: 'hls'`, so `sx`
*is* the manifest URL, in plaintext. The pako and crypto-js scripts on the page
belong to `flushBeacon()`'s analytics payload, not to the stream.

Cross-checked against the live host, which is what rules out a guessed route:

    GET https://playmate.to/api/s  ->  {"error":"Method not allowed"}
