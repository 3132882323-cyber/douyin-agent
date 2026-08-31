"""Build-edition metadata embedded into the Agent executable.

Public source always defaults to the community edition.  The internal builder
overlays this file in a temporary private staging tree; it never edits the
checkout in place.
"""

BUILD_FLAVOR = "public_community"
COMMERCIAL_MODULES_INCLUDED = False
REDISTRIBUTABLE = True


def build_edition_status() -> dict[str, object]:
    return {
        "edition": "community",
        "build_flavor": BUILD_FLAVOR,
        "commercial_modules_included": COMMERCIAL_MODULES_INCLUDED,
        "redistributable": REDISTRIBUTABLE,
    }
