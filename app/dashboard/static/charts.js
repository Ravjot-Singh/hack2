// Stacked daily bars (Chart.js). Colors come from CSS tokens so light/dark both work.
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const SERIES = {
  sent: { label: "Sent", color: "--series-1" },
  holdout: { label: "Holdout", color: "--series-2" },
  no_offer: { label: "No offer", color: "--series-3" },
  deferred: { label: "Queued (quiet hours)", color: "--series-4" },
};

function stackedBars(canvasId, legendId, data) {
  const el = document.getElementById(canvasId);
  if (!el || !data.labels.length) return;
  const keys = Object.keys(SERIES).filter((k) => data.series[k]);
  const datasets = keys.map((k) => ({
    label: SERIES[k].label,
    data: data.series[k],
    backgroundColor: css(SERIES[k].color),
    borderColor: css("--surface"),
    borderWidth: { top: 2 },
    borderRadius: 4,
    borderSkipped: "bottom",
    barPercentage: 0.6,
    categoryPercentage: 0.8,
  }));
  new Chart(el, {
    type: "bar",
    data: { labels: data.labels, datasets },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { footer: (items) => "Total " + items.reduce((a, i) => a + i.parsed.y, 0).toLocaleString() } },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: css("--muted") }, border: { color: css("--axis") } },
        y: { stacked: true, grid: { color: css("--grid") }, ticks: { color: css("--muted"), precision: 0 }, border: { display: false } },
      },
    },
  });
  const legend = document.getElementById(legendId);
  if (legend) {
    legend.innerHTML = keys
      .map((k) => `<span><i style="background:${css(SERIES[k].color)}"></i>${SERIES[k].label}</span>`)
      .join("");
  }
}

// Charts declare themselves: <canvas data-chart='{"labels": [...], "series": {...}}' data-legend="legendId">
document.querySelectorAll("canvas[data-chart]").forEach(function (c) {
  stackedBars(c.id, c.dataset.legend, JSON.parse(c.dataset.chart));
});
