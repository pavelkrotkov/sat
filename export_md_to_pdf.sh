#!/usr/bin/env bash
set -euo pipefail

md_path="${1:?usage: export_md_to_pdf.sh INPUT.md [OUTPUT.pdf]}"
pdf_path="${2:-${md_path%.*}.pdf}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
css_path="${CSS_PATH:-$script_dir/pandoc-report.css}"
html_path="$(mktemp "${TMPDIR:-/tmp}/md-to-pdf.XXXXXX.html")"
scale_css="$(mktemp "${TMPDIR:-/tmp}/md-to-pdf-scale.XXXXXX.css")"
trap 'rm -f "$html_path" "$scale_css"' EXIT

chrome_bin="${CHROME_BIN:-}"
for candidate in \
  "$chrome_bin" \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "$(command -v google-chrome 2>/dev/null || true)" \
  "$(command -v chromium 2>/dev/null || true)" \
  "$(command -v chromium-browser 2>/dev/null || true)"; do
  [[ -n "$candidate" && -x "$candidate" ]] && chrome_bin="$candidate" && break
done
[[ -x "$chrome_bin" ]] || { echo "Chrome/Chromium not found. Set CHROME_BIN." >&2; exit 1; }
[[ -f "$css_path" ]] || { echo "CSS file not found: $css_path" >&2; exit 1; }

cat > "$scale_css" <<'EOF'
@media print {
  body {
    zoom: 0.9;
  }
}
EOF

pandoc -s --embed-resources --mathml --css "$css_path" --css "$scale_css" "$md_path" -o "$html_path"
"$chrome_bin" --headless --disable-gpu --no-sandbox --no-pdf-header-footer --print-to-pdf="$pdf_path" "file://$html_path"
