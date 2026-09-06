"""
SEEAQ: SEEA EA Toolkit plugin for QGIS
---------------
QGIS plugin adding a Processing provider with algorithms to build
UN SEEA Ecosystem Accounting extent accounts and change matrices from
two or more time-series ecosystem type layers.

Install by copying this folder into your QGIS profile's `python/plugins`
directory (see README.md), or zip it and use
Plugins > Manage and Install Plugins > Install from ZIP.
"""


def classFactory(iface):
    from .mainPlugin import SeeaqPlugin
    return SeeaqPlugin(iface)
