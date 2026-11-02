import fs from 'node:fs/promises';
import { renderMermaidSVG } from 'beautiful-mermaid';
import { Resvg } from '@resvg/resvg-js';

const [input, output] = process.argv.slice(2);
try {
  // Resvg does not resolve CSS variables or color-mix(). Freeze the theme into
  // concrete colors so the exported SVG and PNG look the same without a browser.
  const colors = {
    bg: '#ffffff', fg: '#243746', _text: '#243746',
    '_text-sec': '#657782', '_text-muted': '#82919b', '_text-faint': '#b4bec5',
    _line: '#8495a1', _arrow: '#315b76', '_node-fill': '#f1f5f8',
    '_node-stroke': '#a7b9c5', '_group-fill': '#ffffff',
    '_group-hdr': '#edf2f6', '_inner-stroke': '#d0dce4', '_key-badge': '#dbe6ee',
  };
  let svg = renderMermaidSVG(await fs.readFile(input, 'utf8'), { bg: colors.bg, fg: colors.fg });
  svg = svg.replace(/<style>[\s\S]*?<\/style>/g, '')
    .replace(/style="--bg:[^"]*"/, 'style="font-family:Arial,sans-serif"')
    .replace(/var\(--([\w-]+)\)/g, (_, key) => {
      if (!colors[key]) throw new Error(`Unsupported diagram color: ${key}`);
      return colors[key];
    });
  // Long sequence labels can extend beyond the layout library's viewBox.
  // Measure actual rendered geometry, then include it in the exported canvas.
  const bounds = new Resvg(svg).getBBox();
  if (!bounds || !bounds.width || !bounds.height) throw new Error('Diagram has no visible content');
  const box = [bounds.x - 24, bounds.y - 24, bounds.width + 48, bounds.height + 48];
  svg = svg.replace(/viewBox="[^"]*"/, `viewBox="${box.join(' ')}"`)
    .replace(/width="[^"]*" height="[^"]*"/, `width="${box[2]}" height="${box[3]}"`)
    .replace(/(<svg[^>]*>)/, `$1<rect x="${box[0]}" y="${box[1]}" width="${box[2]}" height="${box[3]}" fill="#ffffff"/>`);
  await fs.writeFile(`${output}.svg`, svg);
  await fs.writeFile(`${output}.png`, new Resvg(svg, { fitTo: { mode: 'width', value: 1400 } }).render().asPng());
} catch (error) {
  process.stderr.write(`Diagram rendering failed: ${error.message}\n`);
  process.exitCode = 1;
}
