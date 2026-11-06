/* Loudness overview page — read-only.
 *
 * Fetches /api/loudness/status and renders the summary, the per-library
 * breakdown, and the pending list. This page never posts to
 * /api/loudness/analyze and never touches the scheduler: viewing only.
 */

const SAMPLE_LIMIT = 5000;
let librariesLoaded = false;

function setText(id, value) {
  $(id).textContent = value;
}

function renderStats(data) {
  setText("stat-pending", number(data.pending));
  setText("stat-analyzed", number(data.analyzed_in_plex));
  setText("stat-total", number(data.total_audio_streams));
  setText("stat-measured", number(data.measured_by_us));
  setText("stat-published", number(data.published_by_us));
  setText("stat-failed", number(data.failed_by_us));
  setText("stat-skipped", number(data.undecodable_skipped));
}

function renderPerLibrary(data) {
  const rows = Object.entries(data.per_library || {}).sort(
    (a, b) => b[1].pending - a[1].pending,
  );
  $("loudness-per-library").innerHTML = rows
    .map(
      ([name, counts]) =>
        `<tr><td>${esc(name)}</td><td>${number(counts.total)}</td>` +
        `<td>${number(counts.analyzed)}</td><td>${number(counts.pending)}</td></tr>`,
    )
    .join("");
  $("loudness-per-library-empty").hidden = rows.length > 0;
  if (!librariesLoaded) {
    const select = $("loudness-library");
    for (const [name] of rows) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    }
    librariesLoaded = true;
  }
}

function renderPending(data) {
  const sample = data.sample_pending || [];
  $("loudness-pending").innerHTML = sample
    .map(
      (item) =>
        `<tr><td>${esc(item.title)}</td><td>${esc(item.library)}</td>` +
        `<td>${esc(item.codec)}</td><td>${item.stream_index}</td></tr>`,
    )
    .join("");
  $("loudness-pending-empty").hidden = sample.length > 0 || data.pending > 0;
  setText(
    "loudness-pending-note",
    sample.length < data.pending
      ? `(showing ${number(sample.length)} of ${number(data.pending)})`
      : "",
  );
}

async function load() {
  const button = $("loudness-refresh");
  button.disabled = true;
  try {
    const params = new URLSearchParams({ sample_limit: String(SAMPLE_LIMIT) });
    const library = $("loudness-library").value;
    if (library) params.set("library", library);
    const data = await api("/api/loudness/status?" + params.toString());
    renderStats(data);
    renderPerLibrary(data);
    renderPending(data);
    setText(
      "loudness-updated",
      "Updated " +
        new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
    );
  } catch (error) {
    demo(error.message);
  } finally {
    button.disabled = false;
  }
}

$("loudness-refresh").addEventListener("click", load);
$("loudness-library").addEventListener("change", load);
load();
