// Sagamore — minimal progressive enhancement. No build step, no framework.

// Keep the "polled N ago" stamp honest without a reload.
function tickFreshness() {
  document.querySelectorAll('[data-since]').forEach(el => {
    const then = parseFloat(el.dataset.since) * 1000;
    if (!then) return;
    const s = Math.max(0, (Date.now() - then) / 1000);
    el.textContent =
      s < 90 ? `${Math.round(s)}s ago` :
      s < 5400 ? `${Math.round(s / 60)}m ago` :
      `${Math.round(s / 3600)}h ago`;
    // If the poller itself has stalled, say so rather than showing a stale page silently.
    el.classList.toggle('stale-flag', s > 300);
  });
}
setInterval(tickFreshness, 15000);
tickFreshness();

// Refresh the page when the data behind it has actually moved on.
let lastPolled = null;
async function checkForNewData() {
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    const j = await r.json();
    if (lastPolled && j.polled_at && j.polled_at !== lastPolled) location.reload();
    lastPolled = j.polled_at;
  } catch (e) { /* offline: leave the page as-is rather than blanking it */ }
}
setInterval(checkForNewData, 60000);
checkForNewData();

// Manual refresh. Hits the same collector the scheduled poll uses, then reloads —
// the button must never show data gathered a different way from the page around it.
const refreshBtn = document.getElementById('refresh');
if (refreshBtn) {
  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    refreshBtn.classList.add('busy');
    try {
      const r = await fetch('/api/refresh', { method: 'POST', cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      location.reload();
    } catch (e) {
      // Say so rather than silently leaving a dead button: a refresh that failed and
      // a refresh that returned identical data look the same from the outside.
      refreshBtn.classList.remove('busy');
      refreshBtn.disabled = false;
      refreshBtn.title = 'Refresh failed — collector unreachable';
      refreshBtn.textContent = '↻ failed';
    }
  });
}
