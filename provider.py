from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.extent_table import SeeaExtentAlgorithm
from .algorithms.flows_assets import SeeaFlowsAssetsAlgorithm


class SeeaqProvider(QgsProcessingProvider):

    def id(self):
        return "seeaq"

    def name(self):
        return "SEEAQ"

    def icon(self):
        return QIcon()

    def loadAlgorithms(self):
        self.addAlgorithm(SeeaExtentAlgorithm())
        self.addAlgorithm(SeeaFlowsAssetsAlgorithm())
