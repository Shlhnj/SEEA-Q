"""
SEEA EA Toolkit
---------------
QGIS plugin adding a Processing provider with algorithms to build
UN SEEA Ecosystem Accounting extent accounts and change matrices from
two or more time-series ecosystem type layers.

Install by copying this folder into your QGIS profile's `python/plugins`
directory (see README.md), or zip it and use
Plugins > Manage and Install Plugins > Install from ZIP.
"""


def classFactory(iface):
    from .plugin import SeeaEaToolkitPlugin
    return SeeaEaToolkitPlugin(iface)
