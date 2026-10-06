"""Draws the vk-chain logo, in the interface's own visual language.

The interface is neo-brutalist: two-pixel light borders, almost no corner
rounding, a hard black shadow offset down and right, and three colours doing
all the work - the blue of the centre node, the green of the first ring, the
pink of the second front. The logo is that, reduced to a mark: three nodes
joined into a chain on the dark panel the interface sits on.

    python assets/make_logo.py

Writes logo.png (512 px) and icon.ico (16 to 256 px) next to this file.
Everything is drawn at four times the size and shrunk, which is what makes
the edges clean without a drawing library beyond Pillow.
"""
import pathlib

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent

BG = "#0b0c0e"
PANEL = "#111317"
LIGHT = "#ecebe4"
BLUE = "#7b8cff"
GREEN = "#4fd18b"
PINK = "#ff7ac6"
BLACK = "#000000"

SIZE = 512
SS = 4  # supersampling


def draw() -> Image.Image:
    n = SIZE * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = n / 100.0

    def box(x0, y0, x1, y1):
        return [x0 * u, y0 * u, x1 * u, y1 * u]

    # The hard shadow first, then the panel and its border on top of it.
    d.rounded_rectangle(box(11, 11, 97, 97), radius=3 * u, fill=BLACK)
    d.rounded_rectangle(box(5, 5, 91, 91), radius=3 * u, fill=PANEL, outline=LIGHT, width=int(2.4 * u))

    # Three nodes down a diagonal, joined by links with their own black edge.
    nodes = [(26, 69, 10.5, BLUE), (48, 48, 8.5, GREEN), (70, 27, 10.5, PINK)]
    for (ax, ay, _, _), (bx, by, _, _) in zip(nodes, nodes[1:]):
        d.line([ax * u, ay * u, bx * u, by * u], fill=BLACK, width=int(8.5 * u))
        d.line([ax * u, ay * u, bx * u, by * u], fill=LIGHT, width=int(4.2 * u))
    for x, y, r, colour in nodes:
        # Offset black disc, then the coloured one: the same hard shadow as
        # every button in the interface.
        d.ellipse(box(x - r + 2.2, y - r + 2.2, x + r + 2.2, y + r + 2.2), fill=BLACK)
        d.ellipse(box(x - r, y - r, x + r, y + r), fill=colour, outline=BLACK, width=int(1.8 * u))

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    logo = draw()
    logo.save(HERE / "logo.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    logo.save(HERE / "icon.ico", sizes=sizes)
    print("wrote", HERE / "logo.png", "and", HERE / "icon.ico")


if __name__ == "__main__":
    main()
