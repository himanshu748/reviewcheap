// Frontend regression: costBanner / markdown builder must not crash when a
// provider/unknown model returns cost.usd == null (available: false).
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

// Provide the global helpers the page defines (esc is used inside costBanner).
global.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// Extract the costBanner function definition and evaluate it in this scope.
const m = html.match(/function costBanner\(cost, durationMs\) \{[\s\S]*?\n\s*\}/);
if (!m) { console.error('FAIL: costBanner function not found'); process.exit(1); }
(0, eval)(m[0]);

// Case 1: unknown model -> usd null, available false. Must not throw, must show n/a.
const out1 = costBanner({ usd: null, available: false, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'custom-provider-model' }, 1200);
if (out1.includes('n/a') === false) {
  console.error('FAIL: unknown model should render "n/a", got:\n' + out1);
  process.exit(1);
}

// Case 2: known model -> normal USD. Must still render.
const out2 = costBanner({ usd: 0.0023, available: true, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'gpt-5.6-terra' }, 1200);
if (out2.includes('$0.0023') === false) {
  console.error('FAIL: known model should render $"d, got:\n' + out2);
  process.exit(1);
}

// Case 3: no cost at all -> empty string.
const out3 = costBanner(null, 1200);
if (out3 !== '') { console.error('FAIL: null cost should return empty string, got:\n' + out3); process.exit(1); }

console.log('PASS: frontend cost banner handles null / unavailable / known / absent cost');
