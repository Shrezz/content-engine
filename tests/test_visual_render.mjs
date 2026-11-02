import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { Resvg } from '@resvg/resvg-js';

for (const source of [
  'flowchart LR\nA[Draft] --> B[Evaluate]\nB --> C[Human review]',
  'sequenceDiagram\nAgent->>Database: process_refund(order_id=12345, amount=49.99)\nDatabase-->>Agent: verified refund record',
]) {
  test(`local renderer produces visible pixels: ${source.split('\n')[0]}`, () => {
    const directory = mkdtempSync(join(tmpdir(), 'content-engine-render-'));
    try {
      const input = join(directory, 'diagram.mmd'), output = join(directory, 'diagram');
      writeFileSync(input, source);
      const result = spawnSync(process.execPath, ['scripts/render-diagram.mjs', input, output], { encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      assert.ok(readFileSync(`${output}.png`).subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')));
      const svg = readFileSync(`${output}.svg`, 'utf8');
      assert.ok(!svg.includes('var(--'), 'SVG must be independent of browser CSS variables');
      assert.ok(!svg.includes('@import'), 'SVG must not load remote fonts');
      const rendered = new Resvg(svg).render();
      const pixels = rendered.pixels;
      let dark = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i] < 180 && pixels[i + 3] > 0) dark++;
      }
      assert.ok(dark > 100, 'A successful render must contain visible diagram content');
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}
