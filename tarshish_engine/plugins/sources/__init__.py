"""Product source plugins.

Add a module with one concrete ``core.interfaces.BaseSource`` subclass (for
example ``amazon.py`` or ``admitad.py``) and reference it from a pipeline by
its module name (``source: "amazon"``). No change to ``core/`` is needed.
"""
