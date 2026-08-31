from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.extent_table import SeeaExtentAlgorithm
from .algorithms.flows_assets import SeeaFlowsAssetsAlgorithm


class SeeaEaProvider(QgsProcessingProvider):

    def id(self):
        return "seea_ea"

    def name(self):
        return "SEEA EA Toolkit"

    def icon(self):
        return QIcon()

    def loadAlgorithms(self):
        self.addAlgorithm(SeeaExtentAlgorithm())
        self.addAlgorithm(SeeaFlowsAssetsAlgorithm())
