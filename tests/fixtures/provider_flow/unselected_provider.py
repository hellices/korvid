"""Fixture: an unselected SpecialFlow module that must never be imported.

If this module is imported, it immediately raises — proving that the
registry only loads the selected entry point.
"""

raise RuntimeError("unselected_provider was imported — registry loaded a non-selected plugin!")
