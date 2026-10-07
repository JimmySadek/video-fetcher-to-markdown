// Browser fallback for fetch_media.py: run this in the page of a video the site will not hand to yt-dlp
// (for example an Instagram reel without a login). It returns the best direct audio and video links the
// page already knows about. Download both at once with curl: the links are signed and expire.
// Instagram serves picture and sound separately (DASH); other sites often give one combined file ("video").
(() => {
  const html = document.documentElement.innerHTML;
  const fromHtml = (html.match(/https:\\?\/\\?\/[^"'\s]+?\.mp4[^"'\s]*/g) || [])
    .map(u => u.split(/\\\\u003C|\\u003C|</)[0]
      .replace(/\\\//g, '/').replace(/\\\\u0026|\\u0026/g, '&').replace(/&amp;/g, '&').replace(/\\+$/, ''));
  const fromNet = performance.getEntriesByType('resource').map(e => e.name)
    .filter(u => /\.mp4/.test(u.split('?')[0]));
  const fromTags = [...document.querySelectorAll('video[src], video source[src]')]
    .map(v => v.src).filter(u => /^https?:/.test(u));
  const strip = u => {
    try { const x = new URL(u); x.searchParams.delete('bytestart'); x.searchParams.delete('byteend'); return x.toString(); }
    catch (e) { return null; }
  };
  const urls = [...new Set([...fromHtml, ...fromNet, ...fromTags].map(strip).filter(Boolean))];
  // Instagram and Facebook describe each rendition in a base64 "efg" parameter (vencode_tag, bitrate).
  const tag = u => {
    try { const e = new URL(u).searchParams.get('efg'); return JSON.parse(atob(e.replace(/-/g, '+').replace(/_/g, '/'))); }
    catch (e) { return {}; }
  };
  const items = urls.map(u => ({ u, t: tag(u) }));
  const isAudio = i => /audio/i.test(i.t.vencode_tag || '');
  const best = list => list.sort((a, b) => (b.t.bitrate || 0) - (a.t.bitrate || 0))[0];
  const audio = best(items.filter(isAudio));
  const video = best(items.filter(i => !isAudio(i)));
  const meta = p => (document.querySelector(`meta[property="${p}"]`) || {}).content || '';
  return {
    found: items.length,
    audio: audio ? audio.u : null,
    video: video ? video.u : null,
    title: meta('og:title') || document.title,
    description: meta('og:description'),
    page: location.href,
  };
})()
