import { marked } from 'marked';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// From site/src/lib/ -> go up 3 levels to repo root
const REPO_ROOT = resolve(__dirname, '..', '..', '..');

/**
 * Read a markdown file from the repo root and render to HTML.
 * Paths are relative to the Lattice project root.
 */
export function renderMarkdown(relativePath: string): string {
  const fullPath = resolve(REPO_ROOT, relativePath);
  const content = readFileSync(fullPath, 'utf-8');
  return marked.parse(content) as string;
}
