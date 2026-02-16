#!/bin/bash
# Assemble demo.html from wrapper + sections

cat > demo.html << 'HEADER'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lattice Stats Demo — 27 Improvements</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #0f1117;
      --bg-card: #1a1d27;
      --bg-card-hover: #222738;
      --accent: #6366f1;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
      --text-primary: #e2e8f0;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --border: #2d3348;
      --radius: 12px;
      --font: 'Inter', system-ui, -apple-system, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg-primary);
      color: var(--text-primary);
      font-family: var(--font);
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }
    .page-header {
      max-width: 1000px; margin: 0 auto; padding: 3rem 2rem 2rem;
      border-bottom: 1px solid var(--border); margin-bottom: 2rem;
    }
    .page-header h1 { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; margin-bottom: 0.5rem; }
    .page-header h1 .accent { color: var(--accent); }
    .page-header .subtitle { font-size: 0.95rem; color: var(--text-secondary); max-width: 600px; }
    .page-header .meta { margin-top: 1rem; display: flex; gap: 1.5rem; font-size: 0.78rem; color: var(--text-muted); }
    .toc { max-width: 1000px; margin: 0 auto 3rem; padding: 0 2rem; }
    .toc h2 { font-size: 0.75rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); margin-bottom: 1rem; }
    .toc-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem; }
    .toc-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0.75rem; border-radius: 8px; text-decoration: none; color: var(--text-secondary); font-size: 0.82rem; transition: background 0.15s, color 0.15s; }
    .toc-item:hover { background: var(--bg-card); color: var(--text-primary); }
    .toc-num { font-weight: 700; color: var(--accent); font-size: 0.75rem; min-width: 20px; }
    .toc-tag { margin-left: auto; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; padding: 0.15em 0.5em; border-radius: 4px; background: rgba(99,102,241,0.08); color: var(--accent); flex-shrink: 0; }
    .main-content { max-width: 1000px; margin: 0 auto; padding: 0 2rem 4rem; }
    .demo-group { margin-bottom: 3rem; }
    .demo-group-title { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); padding: 1rem 0; border-top: 2px solid var(--border); margin-bottom: 2rem; }
    .demo-item { margin-bottom: 2.5rem; }
    .demo-item-header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.25rem; padding-bottom: 0.75rem; border-bottom: 1px solid var(--border); }
    .demo-item-number { font-size: 1.1rem; color: var(--accent); font-weight: 700; flex-shrink: 0; }
    .demo-item-header h3 { margin: 0; font-size: 1rem; font-weight: 600; color: var(--text-primary); font-family: var(--font); flex: 1; }
    .demo-item-tag { font-size: 0.65rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 0.2em 0.6em; border-radius: 5px; background: rgba(99,102,241,0.1); color: var(--accent); border: 1px solid rgba(99,102,241,0.2); flex-shrink: 0; }
    .section-divider { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); padding: 1.5rem 0 1rem; border-top: 2px solid var(--border); margin-top: 2rem; margin-bottom: 1.5rem; }
    html { scroll-behavior: smooth; }
    @media (max-width: 768px) { .toc-grid { grid-template-columns: 1fr 1fr; } .page-header h1 { font-size: 1.5rem; } }
    @media (max-width: 480px) { .toc-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>

<div class="page-header">
  <h1>Lattice Stats <span class="accent">Demo</span></h1>
  <p class="subtitle">27 proposed improvements to the dashboard stats page. Each section is numbered and self-contained with mock data. Review, pick favorites, and build.</p>
  <div class="meta">
    <span>27 items</span>
    <span>5 categories</span>
    <span>Mock data throughout</span>
  </div>
</div>

<div class="toc">
  <h2>Table of Contents</h2>
  <div class="toc-grid">
    <a class="toc-item" href="#item-1"><span class="toc-num">1</span> Health Banner <span class="toc-tag">Narrative</span></a>
    <a class="toc-item" href="#item-2"><span class="toc-num">2</span> Event Timeline <span class="toc-tag">Activity</span></a>
    <a class="toc-item" href="#item-3"><span class="toc-num">3</span> Executive Summary <span class="toc-tag">Narrative</span></a>
    <a class="toc-item" href="#item-4"><span class="toc-num">4</span> Velocity Sparklines <span class="toc-tag">Charts</span></a>
    <a class="toc-item" href="#item-5"><span class="toc-num">5</span> Cumulative Flow <span class="toc-tag">Charts</span></a>
    <a class="toc-item" href="#item-6"><span class="toc-num">6</span> Cycle + Lead Time <span class="toc-tag">Metrics</span></a>
    <a class="toc-item" href="#item-7"><span class="toc-num">7</span> Status Duration <span class="toc-tag">Analysis</span></a>
    <a class="toc-item" href="#item-8"><span class="toc-num">8</span> Donut Chart <span class="toc-tag">Charts</span></a>
    <a class="toc-item" href="#item-9"><span class="toc-num">9</span> Card Hierarchy <span class="toc-tag">Layout</span></a>
    <a class="toc-item" href="#item-10"><span class="toc-num">10</span> Dark Mode <span class="toc-tag">Polish</span></a>
    <a class="toc-item" href="#item-11"><span class="toc-num">11</span> Clickable Everything <span class="toc-tag">Polish</span></a>
    <a class="toc-item" href="#item-12"><span class="toc-num">12</span> Time Range <span class="toc-tag">Polish</span></a>
    <a class="toc-item" href="#item-13"><span class="toc-num">13</span> Collapsible Sections <span class="toc-tag">Polish</span></a>
    <a class="toc-item" href="#item-14"><span class="toc-num">14</span> Workload Balance <span class="toc-tag">Distribution</span></a>
    <a class="toc-item" href="#item-15"><span class="toc-num">15</span> Risk Stack <span class="toc-tag">Risk</span></a>
    <a class="toc-item" href="#item-16"><span class="toc-num">16</span> Burndown <span class="toc-tag">Charts</span></a>
    <a class="toc-item" href="#item-17"><span class="toc-num">17</span> Activity Heatmap <span class="toc-tag">Calendar</span></a>
    <a class="toc-item" href="#item-18"><span class="toc-num">18</span> Animations <span class="toc-tag">Interaction</span></a>
    <a class="toc-item" href="#item-19"><span class="toc-num">19</span> Empty States <span class="toc-tag">UX</span></a>
    <a class="toc-item" href="#item-20"><span class="toc-num">20</span> Export / Share <span class="toc-tag">Feature</span></a>
    <a class="toc-item" href="#item-21"><span class="toc-num">21</span> KPI Deltas <span class="toc-tag">Data</span></a>
    <a class="toc-item" href="#item-22"><span class="toc-num">22</span> WIP Severity <span class="toc-tag">Alert</span></a>
    <a class="toc-item" href="#item-23"><span class="toc-num">23</span> Bottleneck Detection <span class="toc-tag">Insight</span></a>
    <a class="toc-item" href="#item-24"><span class="toc-num">24</span> Dependency Chains <span class="toc-tag">Graph</span></a>
    <a class="toc-item" href="#item-25"><span class="toc-num">25</span> Human vs Agent <span class="toc-tag">Comparison</span></a>
    <a class="toc-item" href="#item-26"><span class="toc-num">26</span> Recommended Actions <span class="toc-tag">Feature</span></a>
    <a class="toc-item" href="#item-27"><span class="toc-num">27</span> Baseline / Goals <span class="toc-tag">Charts</span></a>
  </div>
</div>

<div class="main-content">
<div class="section-divider">Section 1 &mdash; Hero, Cards &amp; Narrative</div>
HEADER

# Append sections
cat section-1.html >> demo.html

cat >> demo.html << 'DIV2'
<div class="section-divider">Section 2 &mdash; Activity &amp; People</div>
DIV2
cat section-2.html >> demo.html

cat >> demo.html << 'DIV3'
<div class="section-divider">Section 3 &mdash; Charts &amp; Visualization</div>
DIV3
cat section-3.html >> demo.html

cat >> demo.html << 'DIV4'
<div class="section-divider">Section 4 &mdash; Metrics &amp; Risk</div>
DIV4
cat section-4.html >> demo.html

cat >> demo.html << 'DIV5'
<div class="section-divider">Section 5 &mdash; Interactivity &amp; Actions</div>
DIV5
cat section-5.html >> demo.html

cat >> demo.html << 'FOOTER'

</div><!-- /main-content -->
</body>
</html>
FOOTER

echo "demo.html assembled: $(wc -c < demo.html) bytes, $(wc -l < demo.html) lines"
