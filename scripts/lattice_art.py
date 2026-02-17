#!/usr/bin/env python3
"""Generate lattice ASCII art in various Unicode formats for Twitter/social media.

Braille characters (U+2800-U+28FF) are all the same width in proportional fonts,
making them the only reliable way to do "ASCII art" on Twitter/X.

Each braille character encodes a 2x4 pixel grid:
    Dot 1 (0x01)  Dot 4 (0x08)
    Dot 2 (0x02)  Dot 5 (0x10)
    Dot 3 (0x04)  Dot 6 (0x20)
    Dot 7 (0x40)  Dot 8 (0x80)
"""


def pixels_to_braille(grid: list[list[int]]) -> str:
    """Convert a 2D pixel grid to braille art.

    Grid values: 0 = empty, 1 = filled.
    Grid height must be divisible by 4, width by 2.
    """
    height = len(grid)
    width = len(grid[0]) if grid else 0

    # Pad height to multiple of 4
    while len(grid) % 4 != 0:
        grid.append([0] * width)
    # Pad width to multiple of 2
    if width % 2 != 0:
        for row in grid:
            row.append(0)
        width += 1

    height = len(grid)
    lines = []

    for by in range(0, height, 4):
        line = []
        for bx in range(0, width, 2):
            # Map pixels to braille dots
            code = 0x2800
            if by < height and bx < width and grid[by][bx]:
                code |= 0x01  # dot 1
            if by < height and bx + 1 < width and grid[by][bx + 1]:
                code |= 0x08  # dot 4
            if by + 1 < height and bx < width and grid[by + 1][bx]:
                code |= 0x02  # dot 2
            if by + 1 < height and bx + 1 < width and grid[by + 1][bx + 1]:
                code |= 0x10  # dot 5
            if by + 2 < height and bx < width and grid[by + 2][bx]:
                code |= 0x04  # dot 3
            if by + 2 < height and bx + 1 < width and grid[by + 2][bx + 1]:
                code |= 0x20  # dot 6
            if by + 3 < height and bx < width and grid[by + 3][bx]:
                code |= 0x40  # dot 7
            if by + 3 < height and bx + 1 < width and grid[by + 3][bx + 1]:
                code |= 0x80  # dot 8
            line.append(chr(code))
        lines.append("".join(line))

    return "\n".join(lines)


def make_diamond_lattice(cols: int = 32, rows: int = 24) -> list[list[int]]:
    """Diamond/X lattice pattern — diagonal crossings at regular intervals."""
    grid = [[0] * cols for _ in range(rows)]
    spacing = 8  # Distance between nodes

    for y in range(rows):
        for x in range(cols):
            # Diagonal lines: x+y and x-y modulo spacing
            if (x + y) % spacing == 0 or (x - y) % spacing == 0:
                grid[y][x] = 1
    return grid


def make_grid_lattice(cols: int = 32, rows: int = 24) -> list[list[int]]:
    """Regular grid lattice with nodes at intersections."""
    grid = [[0] * cols for _ in range(rows)]
    h_spacing = 8
    v_spacing = 6

    for y in range(rows):
        for x in range(cols):
            # Horizontal lines
            if y % v_spacing == 0:
                grid[y][x] = 1
            # Vertical lines
            if x % h_spacing == 0:
                grid[y][x] = 1
    return grid


def make_hex_lattice(cols: int = 36, rows: int = 24) -> list[list[int]]:
    """Hexagonal/honeycomb lattice pattern."""
    grid = [[0] * cols for _ in range(rows)]

    hex_w = 8  # Width of hex cell
    hex_h = 6  # Height of hex cell

    for y in range(rows):
        for x in range(cols):
            cell_row = y // hex_h
            local_y = y % hex_h
            offset = (hex_w // 2) if cell_row % 2 == 1 else 0
            local_x = (x + offset) % hex_w

            # Top edge: / and \
            if local_y == 0 and (local_x == 0 or local_x == hex_w - 1):
                grid[y][x] = 1
            # Ascending left edge
            if local_y == 1 and local_x == hex_w - 1:
                grid[y][x] = 1
            if local_y == 1 and local_x == 0:
                grid[y][x] = 1
            # Horizontal top
            if local_y == 0 and 1 <= local_x <= hex_w - 2:
                grid[y][x] = 1
            # Sides
            if local_y in (1, 2, 3, 4) and (local_x == 0):
                grid[y][x] = 1
            # Bottom horizontal
            if local_y == hex_h - 1 and 1 <= local_x <= hex_w - 2:
                grid[y][x] = 1
    return grid


def make_crystal_3d(cols: int = 40, rows: int = 28) -> list[list[int]]:
    """Isometric 3D crystal lattice — cubes in perspective."""
    grid = [[0] * cols for _ in range(rows)]

    # Draw a 3D lattice using simple isometric projection
    # Horizontal lines
    for y_node in range(0, rows, 8):
        for x in range(cols):
            if y_node < rows:
                grid[y_node][x] = 1

    # Vertical lines
    for x_node in range(0, cols, 10):
        for y in range(rows):
            if x_node < cols:
                grid[y][x_node] = 1

    # Diagonal lines (depth)
    for y_start in range(0, rows, 8):
        for x_start in range(0, cols, 10):
            for d in range(min(5, rows - y_start, cols - x_start)):
                if y_start + d < rows and x_start + d < cols:
                    grid[y_start + d][x_start + d] = 1

    return grid


def make_dense_diamond(cols: int = 40, rows: int = 24) -> list[list[int]]:
    """Dense diamond lattice with smaller spacing — looks great as braille."""
    grid = [[0] * cols for _ in range(rows)]
    spacing = 4

    for y in range(rows):
        for x in range(cols):
            if (x + y) % spacing == 0 or (x - y) % spacing == 0:
                grid[y][x] = 1
    return grid


def make_wave_lattice(cols: int = 40, rows: int = 24) -> list[list[int]]:
    """Intersecting sine-like wave lattice."""
    import math
    grid = [[0] * cols for _ in range(rows)]

    for x in range(cols):
        for wave in range(4):
            y = int(rows / 2 + (rows / 3) * math.sin(2 * math.pi * x / cols + wave * math.pi / 4))
            y = max(0, min(rows - 1, y))
            grid[y][x] = 1
            # Also draw vertical connections
            if x % 6 == 0:
                for yy in range(rows):
                    if yy % 4 == 0:
                        grid[yy][x] = 1
    return grid


def make_nodes_and_edges(cols: int = 44, rows: int = 20) -> list[list[int]]:
    """Lattice of nodes (filled circles) connected by edges."""
    grid = [[0] * cols for _ in range(rows)]

    node_spacing_x = 10
    node_spacing_y = 8
    node_radius = 1

    nodes = []
    for ny in range(0, rows, node_spacing_y):
        offset = node_spacing_x // 2 if (ny // node_spacing_y) % 2 == 1 else 0
        for nx in range(offset, cols, node_spacing_x):
            nodes.append((nx, ny))
            # Draw node (small filled area)
            for dy in range(-node_radius, node_radius + 1):
                for dx in range(-node_radius, node_radius + 1):
                    py, px = ny + dy, nx + dx
                    if 0 <= py < rows and 0 <= px < cols:
                        if dx * dx + dy * dy <= node_radius * node_radius + 1:
                            grid[py][px] = 1

    # Draw edges between nearby nodes
    for i, (x1, y1) in enumerate(nodes):
        for x2, y2 in nodes[i + 1:]:
            dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if dist < node_spacing_x * 1.5:
                # Bresenham-ish line
                steps = max(abs(x2 - x1), abs(y2 - y1))
                if steps == 0:
                    continue
                for s in range(steps + 1):
                    t = s / steps
                    px = int(x1 + t * (x2 - x1))
                    py = int(y1 + t * (y2 - y1))
                    if 0 <= py < rows and 0 <= px < cols:
                        grid[py][px] = 1

    return grid


def make_word_lattice(word: str = "LATTICE", cols: int = 56, rows: int = 12) -> list[list[int]]:
    """Render the word with a lattice/grid texture overlaid."""
    # Simple 5x7 bitmap font for the letters we need
    font = {
        'L': [
            "1    ",
            "1    ",
            "1    ",
            "1    ",
            "1    ",
            "1    ",
            "11111",
        ],
        'A': [
            " 111 ",
            "1   1",
            "1   1",
            "11111",
            "1   1",
            "1   1",
            "1   1",
        ],
        'T': [
            "11111",
            "  1  ",
            "  1  ",
            "  1  ",
            "  1  ",
            "  1  ",
            "  1  ",
        ],
        'I': [
            "11111",
            "  1  ",
            "  1  ",
            "  1  ",
            "  1  ",
            "  1  ",
            "11111",
        ],
        'C': [
            " 1111",
            "1    ",
            "1    ",
            "1    ",
            "1    ",
            "1    ",
            " 1111",
        ],
        'E': [
            "11111",
            "1    ",
            "1    ",
            "1111 ",
            "1    ",
            "1    ",
            "11111",
        ],
    }

    # Calculate dimensions
    char_w = 5
    char_h = 7
    spacing = 2
    total_w = len(word) * (char_w + spacing) - spacing

    # Center in grid
    x_off = max(0, (cols - total_w) // 2)
    y_off = max(0, (rows - char_h) // 2)

    grid = [[0] * cols for _ in range(rows)]

    for ci, ch in enumerate(word):
        if ch not in font:
            continue
        bx = x_off + ci * (char_w + spacing)
        for dy, row_str in enumerate(font[ch]):
            for dx, pixel in enumerate(row_str):
                px, py = bx + dx, y_off + dy
                if 0 <= px < cols and 0 <= py < rows and pixel == '1':
                    grid[py][px] = 1

    return grid


# ─── Generate all patterns ───

print("=" * 60)
print("LATTICE ASCII ART — Unicode Braille Edition")
print("All patterns use braille characters (U+2800-U+28FF)")
print("which have UNIFORM WIDTH in proportional fonts (Twitter!)")
print("=" * 60)

print("\n\n▸ 1. DIAMOND LATTICE (compact — perfect for tweets)")
print("-" * 40)
grid = make_dense_diamond(32, 16)
print(pixels_to_braille(grid))

print("\n\n▸ 2. DIAMOND LATTICE (larger — bio/header)")
print("-" * 40)
grid = make_diamond_lattice(40, 24)
print(pixels_to_braille(grid))

print("\n\n▸ 3. GRID LATTICE (structured)")
print("-" * 40)
grid = make_grid_lattice(40, 24)
print(pixels_to_braille(grid))

print("\n\n▸ 4. NODES & EDGES (graph network)")
print("-" * 40)
grid = make_nodes_and_edges(44, 24)
print(pixels_to_braille(grid))

print("\n\n▸ 5. 3D CRYSTAL (isometric)")
print("-" * 40)
grid = make_crystal_3d(40, 24)
print(pixels_to_braille(grid))

print("\n\n▸ 6. \"LATTICE\" WORDMARK (braille text)")
print("-" * 40)
grid = make_word_lattice("LATTICE", 56, 12)
print(pixels_to_braille(grid))

print("\n\n▸ 7. DENSE DIAMOND — TINY (tweet-sized)")
print("-" * 40)
grid = make_dense_diamond(20, 12)
print(pixels_to_braille(grid))

print("\n\n▸ 8. WORDMARK + LATTICE COMBO")
print("-" * 40)
# Text on top, lattice pattern below
text_grid = make_word_lattice("LATTICE", 48, 12)
diamond_grid = make_dense_diamond(48, 12)
# Merge: text takes priority, lattice fills the background
combined = [[0] * 48 for _ in range(24)]
for y in range(12):
    for x in range(48):
        combined[y][x] = text_grid[y][x]
for y in range(12):
    for x in range(48):
        combined[y + 12][x] = diamond_grid[y][x]
print(pixels_to_braille(combined))

print("\n\n▸ 9. WORDMARK WITH LATTICE TEXTURE")
print("-" * 40)
# The word LATTICE, but with a diamond pattern as texture inside the letters
text_grid = make_word_lattice("LATTICE", 56, 12)
diamond_bg = make_dense_diamond(56, 12)
# Invert: show lattice texture WITHIN the word shape
textured = [[0] * 56 for _ in range(12)]
for y in range(12):
    for x in range(56):
        if text_grid[y][x]:
            textured[y][x] = 1  # Solid letters
        elif diamond_bg[y][x]:
            textured[y][x] = 1  # Lattice background
print(pixels_to_braille(textured))

print("\n\n" + "=" * 60)
print("COPY-PASTE READY — all patterns render correctly on Twitter!")
print("=" * 60)
