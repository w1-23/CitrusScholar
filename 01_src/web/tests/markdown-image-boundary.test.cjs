'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser} = require('./frontend-harness.cjs');

const filePreview = '/api/v2/files/' + 'a'.repeat(32) + '/preview';
const vectorPreview = '/api/v2/figure-workbench/projects/' + 'b'.repeat(32) + '/preview/svg?revision=2';
const origin = 'https://application.example';
const render = () => {
  const {ctx} = browser('app.js', {localMarkdown: true});
  ctx.location.origin = origin;
  ctx.URL = URL;
  return ctx.renderMarkdownSafe;
};

test('minimizes automatic images to authenticated product previews', () => {
  const output = render()([
    '![Original figure](' + filePreview + ')',
    '![Editable figure](' + origin + vectorPreview + ')',
    '![Not a preview](/api/v2/files/' + 'a'.repeat(32) + '/download)',
    '![Extra parameter](' + filePreview + '?unexpected=1)',
    '![Unrelated same origin](/assets/citrus-mark.svg)',
  ].join('\n\n'));
  assert.equal((output.match(/<img\b/g) || []).length, 2);
  assert.ok(output.includes('src="' + filePreview + '"'));
  assert.ok(output.includes('src="' + vectorPreview + '"'));
  assert.match(output, /<p>Not a preview<\/p>/);
  assert.match(output, /<p>Unrelated same origin<\/p>/);
});

test('external images are explicit links with no referrer, including reference syntax', () => {
  const external = 'https://images.example/paper-figure.png';
  const output = render()([
    '![Remote figure](' + external + ')',
    '![Remote reference][ref]\n\n[ref]: https://images.example/further-figure.png',
    '<img src="https://images.example/raw-figure.png" alt="raw document markup">',
    '[Ordinary reference](https://references.example/paper)',
  ].join('\n\n'));
  assert.doesNotMatch(output, /<img\b/);
  assert.ok(output.includes('href="' + external + '"'));
  assert.match(output, /查看外部图片：Remote reference/);
  assert.match(output, /rel="noopener noreferrer" referrerpolicy="no-referrer"/);
  assert.match(output, /&lt;img src=/);
  assert.match(output, /<a href="https:\/\/references\.example\/paper">Ordinary reference<\/a>/);
});

test('safe standalone and fenced SVG still render while literal examples are preserved', () => {
  const {ctx} = browser('app.js', {localMarkdown: true});
  ctx.location.origin = origin;
  ctx.URL = URL;
  const fixture = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>';
  const seen = [];
  ctx.renderSvgBlock = raw => { seen.push(raw); return '<figure>Sanitized SVG fixture</figure>'; };
  const output = ctx.renderMarkdownSafe(['Figure follows:', fixture, '', '```svg', fixture, '```', '',
    '```html', fixture, '```'].join('\n'));
  assert.match(output, /<p>Figure follows:<\/p>/);
  assert.equal((output.match(/<figure>/g) || []).length, 2);
  assert.deepEqual(seen, [fixture, fixture]);
  assert.match(output, /<code class="lang-html">&lt;svg/);
});
