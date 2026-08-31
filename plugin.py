from qgis.core import QgsApplication
from .provider import SeeaEaProvider


class SeeaEaToolkitPlugin:
    """Registers the SEEA EA Processing provider with QGIS."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        self.provider = SeeaEaProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        QgsApplication.processingRegistry().removeProvider(self.provider)
