"""
Veri-Fact Desktop
=================

A 100% offline, native desktop application (tkinter) that verifies numeric
claims found in text/files against a "source of truth" database.

There is NO web server, NO localhost, NO browser here. Everything runs inside
one Python process that draws a native OS window.

Package layout
--------------
    config_manager.py : reads/writes config.json (external DB connection info)
    database.py       : dual data source layer (external SQL -> local SQLite)
    nlp_engine.py     : extracts numbers + metric names + timeframes from text
    file_parsers.py   : turns .txt/.log/.csv/.xlsx/.json into "claim candidates"
    verifier.py       : compares claimed values vs database truth (GREEN/YELLOW/RED)
    exporters.py      : writes results to .csv / .xlsx (color coded) / .json
    gui.py            : the tkinter user interface

Everything is launched by ``main.py`` in the parent folder.
"""

__version__ = "1.0.0"
APP_NAME = "Veri-Fact Desktop"
