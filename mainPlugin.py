from qgis.core import QgsApplication
try:
    from qgis.PyQt.QtWidgets import QAction  # Qt5-based QGIS
except ImportError:
    from qgis.PyQt.QtGui import QAction  # Qt6-based QGIS

from .provider import SeeaEaProvider


class SeeaqPlugin:
    """
    Registers the SEEA EA Processing provider with QGIS, and adds a
    "SEEA EA Toolkit" submenu under the Plugins menu with one entry per
    algorithm. Each entry opens that algorithm's normal Processing
    parameter dialog (the same dialog you'd get double-clicking it in
    the Processing Toolbox) via processing.execAlgorithmDialog() -- no
    separate UI to build or keep in sync.
    """

    MENU_NAME = "&SEEAQ"

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []

    def initGui(self):
        self.provider = SeeaqProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        self._add_menu_action(
            "1. Extent Account + Change Matrix...",
            f"{self.provider.id()}:seea_extent",
        )
        self._add_menu_action(
            "2. Ecosystem Service Flow + Asset Account...",
            f"{self.provider.id()}:seea_flows_assets",
        )

    def _add_menu_action(self, label, algorithm_id):
        action = QAction(label, self.iface.mainWindow())
        action.triggered.connect(lambda checked=False, aid=algorithm_id: self._run(aid))
        self.iface.addPluginToMenu(self.MENU_NAME, action)
        self.actions.append(action)

    @staticmethod
    def _run(algorithm_id):
        import processing
        processing.execAlgorithmDialog(algorithm_id)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.MENU_NAME, action)
        self.actions = []
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
