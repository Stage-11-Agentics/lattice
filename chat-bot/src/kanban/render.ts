import { writeFileSync, readFileSync, unlinkSync, mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execSync } from "node:child_process";

/**
 * Render a Mermaid diagram string to a PNG buffer.
 *
 * Strategy:
 * 1. Try mermaid.ink API (no local deps needed)
 * 2. Fall back to local mmdc if available
 */
export async function renderMermaidToPng(
  mermaidCode: string,
): Promise<Buffer | null> {
  // Strategy 1: mermaid.ink hosted API
  const pngFromApi = await renderViaMermaidInk(mermaidCode);
  if (pngFromApi) return pngFromApi;

  // Strategy 2: local mmdc (requires puppeteer/chrome)
  return renderViaMmdc(mermaidCode);
}

/** Use mermaid.ink to render diagram to PNG via HTTP */
async function renderViaMermaidInk(
  mermaidCode: string,
): Promise<Buffer | null> {
  try {
    const encoded = Buffer.from(mermaidCode, "utf-8").toString("base64url");
    const url = `https://mermaid.ink/img/${encoded}?type=png&bgColor=white&width=1200`;

    const resp = await fetch(url, { signal: AbortSignal.timeout(15_000) });
    if (!resp.ok) {
      console.warn(`[kanban] mermaid.ink returned ${resp.status}`);
      return null;
    }

    const arrayBuf = await resp.arrayBuffer();
    return Buffer.from(arrayBuf);
  } catch (err) {
    console.warn("[kanban] mermaid.ink failed, trying local mmdc:", err);
    return null;
  }
}

/** Use local mmdc CLI to render diagram to PNG */
function renderViaMmdc(mermaidCode: string): Buffer | null {
  const dir = mkdtempSync(join(tmpdir(), "lattice-kanban-"));
  const inputPath = join(dir, "diagram.mmd");
  const outputPath = join(dir, "diagram.png");

  try {
    writeFileSync(inputPath, mermaidCode, "utf-8");

    execSync(
      `npx mmdc -i "${inputPath}" -o "${outputPath}" -b white -w 1200 -H 800 --quiet`,
      {
        encoding: "utf-8",
        timeout: 30_000,
        stdio: "pipe",
      },
    );

    return readFileSync(outputPath);
  } catch (err) {
    console.error("[kanban] mmdc fallback failed:", err);
    return null;
  } finally {
    try {
      unlinkSync(inputPath);
    } catch {}
    try {
      unlinkSync(outputPath);
    } catch {}
    try {
      const { rmdirSync } = require("node:fs");
      rmdirSync(dir);
    } catch {}
  }
}

/** Render and return as base64 string (ready for signal-cli-rest-api) */
export async function renderMermaidToBase64(
  mermaidCode: string,
): Promise<string | null> {
  const png = await renderMermaidToPng(mermaidCode);
  if (!png) return null;
  return png.toString("base64");
}
