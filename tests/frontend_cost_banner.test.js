// Frontend regression: costBanner / buildMarkdown must not crash when cost is
// absent, null, or has usd == null (available: false) for unknown providers.
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

// Provide the global helpers the page defines (esc is used inside costBanner).
global.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// Extract a code block starting at `start` (which points at '{') by counting
// braces to the matching close. Handles nested blocks inside template strings.
function extractBlock(src, start) {
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(src.lastIndexOf('{', start), i + 1);
    }
  }
  return null;
}

function extractFunction(src, name) {
  const idx = src.indexOf('function ' + name + '(');
  if (idx < 0) return null;
  const open = src.indexOf('{', idx);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(idx, i + 1);
    }
  }
  return null;
}

function extractConstObject(src, name) {
  const idx = src.indexOf('const ' + name + ' =');
  if (idx < 0) return null;
  const open = src.indexOf('{', idx);
  const block = extractBlock(src, open);
  return block ? eval('(' + block + ')') : null;
}

global.VERDICT_META = extractConstObject(html, 'VERDICT_META');
if (!global.VERDICT_META) { console.error('FAIL: VERDICT_META not found'); process.exit(1); }

const cb = extractFunction(html, 'costBanner');
if (!cb) { console.error('FAIL: costBanner function not found'); process.exit(1); }
(0, eval)(cb);

const bm = extractFunction(html, 'buildMarkdown');
if (!bm) { console.error('FAIL: buildMarkdown function not found'); process.exit(1); }
(0, eval)(bm);

// --- costBanner cases (existing) ---

// Case 1: unknown model -> usd null, available false. Must not throw, must show n/a.
const out1 = costBanner({ usd: null, available: false, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'custom-provider-model' }, 1200);
if (out1.includes('n/a') === false) {
  console.error('FAIL: unknown model should render "n/a", got:\n' + out1);
  process.exit(1);
}

// Case 2: known model -> normal USD. Must still render.
const out2 = costBanner({ usd: 0.0023, available: true, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'gpt-5.6-terra' }, 1200);
if (out2.includes('$0.0023') === false) {
  console.error('FAIL: known model should render "$0.0023", got:\n' + out2);
  process.exit(1);
}

// Case 3: no cost at all -> empty string.
const out3 = costBanner(null, 1200);
if (out3 !== '') { console.error('FAIL: null cost should return empty string, got:\n' + out3); process.exit(1); }

// --- buildMarkdown cases ---

function baseData(cost) {
  const review = {
    verdict: 'APPROVE', health_score: 88, health_grade: 'B+',
    verdict_reason: 'solid', summary: 'summary', scorecard: [],
    process: {}, issues: [], guideline: {}, highlights: [],
  };
  if (cost !== undefined) review.cost = cost;
  return { review, metadata: {}, signals: {} };
}

// Case 4: absent cost -> no crash, no cost footer line.
const out4 = buildMarkdown(baseData(undefined));
if (out4.includes('Review by [ReviewCheap]')) {
  console.error('FAIL: absent cost should omit footer, got:\n' + out4);
  process.exit(1);
}

// Case 5: null cost -> no crash, no cost footer line.
const out5 = buildMarkdown(baseData(null));
if (out5.includes('Review by [ReviewCheap]')) {
  console.error('FAIL: null cost should omit footer, got:\n' + out5);
  process.exit(1);
}

// Case 6: unknown-price model -> usd null, available false -> n/a.
const out6 = buildMarkdown(baseData({ usd: null, available: false, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'custom-provider-model' }));
if (out6.includes('n/a') === false || out6.includes('custom-provider-model') === false) {
  console.error('FAIL: unknown model should render "n/a" with model name, got:\n' + out6);
  process.exit(1);
}

// Case 7: known-price model -> normal USD.
const out7 = buildMarkdown(baseData({ usd: 0.0023, available: true, claude_price_usd: 0.05, tokens_in: 100, tokens_out: 50, model: 'gpt-5.6-terra' }));
if (out7.includes('$0.0023') === false) {
  console.error('FAIL: known model should render "$0.0023", got:\n' + out7);
  process.exit(1);
}

console.log('PASS: frontend cost banner + buildMarkdown handle null / unavailable / known / absent cost');
