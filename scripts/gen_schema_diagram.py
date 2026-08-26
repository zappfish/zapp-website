#!/usr/bin/env python3
"""Generate the curated schema diagram page for the ZAPP schema docs.

The page replaces linkml's `gen-erdiagram` output, which draws every class and
every attribute and is too dense to read. Here the *structure* is curated in
schema-docs/diagram-layout.yaml while every name and definition is read from the
LinkML schema, so the prose cannot drift away from the model.

The layout and the schema are checked against each other and this script exits
non-zero if they disagree -- so adding a class to the schema breaks the build
until someone decides where it belongs.

The page's markup lives in page.md.jinja and its styling in diagram.css, found
in the --assets directory. This script only reads the schema, checks it against
the layout, and hands the result to the template.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from linkml_runtime import SchemaView

TEMPLATE = "page.md.jinja"
STYLESHEET = "diagram.css"

# Levels of nesting get progressively stronger tints in the diagram. One tint per
# level of the story the schema tells: Study > Experiment > Exposure Event >
# Phenotype outcomes.
MAX_TINTED_DEPTH = 4


class LayoutError(Exception):
    """The layout file and the schema disagree."""


def slugify(text):
    """Match the heading anchors mkdocs generates, so in-page links resolve."""
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", slug)


def describe(view, class_name):
    """The class's description from the schema, whitespace normalised."""
    cls = view.get_class(class_name)
    return " ".join((cls.description or "").split()) if cls else ""


class Node:
    """One box in the diagram, always backed by a class in the schema.

    The properties here are what page.md.jinja renders, so the template can stay
    free of logic.
    """

    def __init__(self, class_name, label, children, depth, view):
        self.class_name = class_name
        self.label = label
        self.children = children
        self.depth = depth
        self._view = view

    @property
    def description(self):
        return describe(self._view, self.class_name)

    @property
    def anchor(self):
        return slugify(self.label)

    @property
    def tint(self):
        return min(self.depth, MAX_TINTED_DEPTH - 1)

    @property
    def cards(self):
        """Children with nothing nested inside them."""
        return [child for child in self.children if not child.children]

    @property
    def levels(self):
        """Children that contain classes of their own."""
        return [child for child in self.children if child.children]


def parse_layout(entries, view, depth=0):
    """Turn the `diagram:` tree from the layout file into Node objects."""
    nodes = []
    for entry in entries or []:
        if "class" not in entry:
            raise LayoutError(f"layout entry needs a `class`: {entry!r}")
        name = entry["class"]
        cls = view.get_class(name)
        # A label in the layout wins, then the schema's own title, then the bare
        # class name. Anything written here is a stopgap for a missing `title:`.
        label = entry.get("label") or (cls.title if cls else None) or name
        children = parse_layout(entry.get("contains"), view, depth + 1)
        nodes.append(Node(name, label, children, depth, view))
    return nodes


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.children)


def validate(nodes, excluded, view):
    """Fail loudly when the layout no longer describes the schema.

    This is the whole point of generating the page: a class added to the schema
    must be consciously placed in the diagram or consciously left out of it.
    """
    schema_classes = view.all_classes(imports=False)
    concrete = {n for n, c in schema_classes.items() if not c.abstract}

    placed = [node.class_name for node in walk(nodes)]
    placed_set = set(placed)
    excluded_set = set(excluded)
    known = set(schema_classes)

    def listing(names):
        return "\n".join(f"  - {n}" for n in names)

    problems = []

    duplicates = sorted({n for n in placed if placed.count(n) > 1})
    if duplicates:
        problems.append(
            "These classes appear more than once in `diagram:`:\n"
            + listing(duplicates)
        )

    unknown = sorted((placed_set | excluded_set) - known)
    if unknown:
        problems.append(
            "These classes are in the layout but no longer exist in the schema\n"
            "(renamed or removed?). Update diagram-layout.yaml:\n" + listing(unknown)
        )

    both = sorted(placed_set & excluded_set)
    if both:
        problems.append(
            "These classes are both drawn and excluded. Pick one:\n" + listing(both)
        )

    unaccounted = sorted(concrete - placed_set - excluded_set)
    if unaccounted:
        problems.append(
            "These classes are in the schema but unaccounted for in the diagram\n"
            "layout. Add each one to `diagram:` to draw it, or to `excluded:`\n"
            "to leave it out:\n"
            + "\n".join(f"  - {n}: {describe(view, n)[:90]}" for n in unaccounted)
        )

    # The page shows schema descriptions verbatim, so a missing one leaves a hole.
    undescribed = sorted(
        n for n in (placed_set | excluded_set) & known if not describe(view, n)
    )
    if undescribed:
        problems.append(
            "These classes have no `description:` in the schema, so there is\n"
            "nothing to document them with. Add descriptions upstream:\n"
            + listing(undescribed)
        )

    if problems:
        raise LayoutError("\n\n".join(problems))


def render(nodes, excluded, view, intro, assets_dir):
    env = Environment(
        loader=FileSystemLoader(assets_dir),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    page = env.get_template(TEMPLATE).render(
        intro=intro.strip(),
        css=(assets_dir / STYLESHEET).read_text().strip(),
        nodes=nodes,
        # Every level in the diagram becomes a section on the page, in the order
        # the diagram draws them.
        levels=[node for node in walk(nodes) if node.children],
        excluded=[
            {"name": name, "description": describe(view, name)}
            for name in sorted(excluded)
        ],
    )
    return page.rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="LinkML schema YAML")
    parser.add_argument("--layout", required=True, help="diagram layout YAML")
    parser.add_argument("--out", required=True, help="markdown file to write")
    parser.add_argument(
        "--assets",
        help=f"directory holding {TEMPLATE} and {STYLESHEET} "
        "(default: the layout file's directory)",
    )
    args = parser.parse_args()

    layout_path = Path(args.layout)
    assets_dir = Path(args.assets) if args.assets else layout_path.parent
    missing = [n for n in (TEMPLATE, STYLESHEET) if not (assets_dir / n).is_file()]
    if missing:
        print(
            f"{', '.join(missing)} not found in {assets_dir}/ — pass --assets to "
            "point at the directory holding them",
            file=sys.stderr,
        )
        return 1

    layout = yaml.safe_load(layout_path.read_text())
    view = SchemaView(args.schema)

    try:
        nodes = parse_layout(layout.get("diagram"), view)
        excluded = layout.get("excluded") or []
        validate(nodes, excluded, view)
    except LayoutError as err:
        print(f"\n{args.layout} is out of sync with the schema:\n\n{err}\n",
              file=sys.stderr)
        return 1

    page = render(nodes, excluded, view, layout.get("intro", ""), assets_dir)
    Path(args.out).write_text(page)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
