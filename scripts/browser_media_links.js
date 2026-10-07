// Browser fallback for fetch_media.py: run this in the page of a video the site will not hand to yt-dlp
// (for example an Instagram reel without a login). It returns the best direct audio and video links the
// page already knows about. Download both at once with curl: the links are signed and expire.
// Instagram serves picture and sound separately (DASH); other sites often give one combined file ("video").
// Sites that stream in pieces expose a playlist instead ("stream", .m3u8 or .mpd): pass that URL to
// fetch_media.py with --source-url set to the page, and yt-dlp downloads the pieces.
(() => {
  const html = document.documentElement.innerHTML;
  // Pages embed links JSON-escaped, sometimes twice: \/ for /, & for &, % for %, < for <.
  const unescape = s => s
    .replace(/\\+u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\+\//g, '/')
    .replace(/&amp;/g, '&');
  const clean = u => unescape(u).split(/[<"'\s]/)[0].replace(/\\+$/, '');
  const found = pattern => (html.match(pattern) || []).map(clean);
  const network = performance.getEntriesByType('resource').map(e => e.name);
  const strip = u => {
    try { const x = new URL(u); x.searchParams.delete('bytestart'); x.searchParams.delete('byteend'); return x.toString(); }
    catch (e) { return null; }
  };
  const files = [...new Set([
    ...found(/https:(?:\\*\/){2}[^"'\s]+?\.mp4[^"'\s]*/g),
    ...network.filter(u => /\.mp4$/.test(u.split('?')[0])),
    ...[...document.querySelectorAll('video[src], video source[src]')].map(v => v.src).filter(u => /^https?:/.test(u)),
  ].map(strip).filter(Boolean))];
  // Instagram and Facebook describe each rendition in a base64 "efg" parameter (vencode_tag, bitrate).
  const tag = u => {
    try { const e = new URL(u).searchParams.get('efg'); return JSON.parse(atob(e.replace(/-/g, '+').replace(/_/g, '/'))); }
    catch (e) { return {}; }
  };
  const items = files.map(u => ({ u, t: tag(u) }));
  const isAudio = i => /audio/i.test(i.t.vencode_tag || '');
  const best = list => list.sort((a, b) => (b.t.bitrate || 0) - (a.t.bitrate || 0))[0];
  const audio = best(items.filter(isAudio));
  const video = best(items.filter(i => !isAudio(i)));
  const streams = [...new Set([
    ...found(/https:(?:\\*\/){2}[^"'\s]+?\.(?:m3u8|mpd)[^"'\s]*/g),
    ...network.filter(u => /\.(m3u8|mpd)$/.test(u.split('?')[0])),
  ])];
  const meta = p => (document.querySelector(`meta[property="${p}"]`) || {}).content || '';
  return {
    found: items.length,
    audio: audio ? audio.u : null,
    video: video ? video.u : null,
    stream: streams.find(u => /master|playlist|manifest/i.test(u)) || streams[0] || null,
    title: meta('og:title') || document.title,
    description: meta('og:description'),
    page: location.href,
  };
})()
