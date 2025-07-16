from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDialog, QFileDialog, QProgressDialog
from qgis.core import QgsProject, QgsVectorLayer
from qgis.utils import iface
from qgis.PyQt import uic
import os
import requests
import tempfile
import csv
import mercantile
from shapely.geometry import box, Polygon
from qgis.PyQt.QtCore import Qt
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.core import QgsWkbTypes, QgsRectangle, QgsPointXY
import shapely.geometry
import logging
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape
LOG_PATH = os.path.join(os.path.dirname(__file__), 'ms_buildings_roads.log')
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
from qgis.core import QgsGeometry
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterExtent, QgsProcessingParameterString,
    QgsProcessingParameterRasterDestination, QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsProcessingException
)
from qgis.PyQt.QtCore import QCoreApplication

# Load the .ui file at runtime
FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'form.ui'))

class MSBuildingsRoadsDialog(QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        # Connect the Set Canvas Extent button
        self.btn_extent.clicked.connect(self.set_canvas_extent)
        # Add any additional widgets not in the .ui file (locationComboBox, reloadLocationsButton, etc.)
        # You may need to add these programmatically if not present in the .ui
        # Connect signals and set up logic here
        # Example:
        # self.downloadButton.clicked.connect(self.on_download_clicked)
        # ...

    def set_canvas_extent(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        crsDest = QgsCoordinateReferenceSystem(4326)  # WGS84
        crsSrc = self.iface.mapCanvas().mapSettings().destinationCrs()
        xform = QgsCoordinateTransform(crsSrc, crsDest, QgsProject.instance())
        extent = xform.transformBoundingBox(self.iface.mapCanvas().extent())
        self.spb_west.setValue(int(extent.xMinimum()))
        self.spb_east.setValue(int(extent.xMaximum()))
        self.spb_south.setValue(int(extent.yMinimum()))
        self.spb_north.setValue(int(extent.yMaximum()))

class RectangleMapTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.rubberBand = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubberBand.setColor(Qt.red)
        self.rubberBand.setWidth(1)
        self.reset()

    def reset(self):
        self.startPoint = self.endPoint = None
        self.isEmittingPoint = False
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)

    def canvasPressEvent(self, e):
        self.startPoint = self.toMapCoordinates(e.pos())
        self.endPoint = self.startPoint
        self.isEmittingPoint = True
        self.showRect(self.startPoint, self.endPoint)

    def canvasReleaseEvent(self, e):
        self.isEmittingPoint = False
        r = self.rectangle()
        if r is not None:
            self.callback(r)
        self.reset()

    def canvasMoveEvent(self, e):
        if not self.isEmittingPoint:
            return
        self.endPoint = self.toMapCoordinates(e.pos())
        self.showRect(self.startPoint, self.endPoint)

    def showRect(self, startPoint, endPoint):
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        if startPoint.x() == endPoint.x() or startPoint.y() == endPoint.y():
            return
        rect = QgsRectangle(startPoint, endPoint)
        self.rubberBand.setToGeometry(QgsGeometry.fromRect(rect), None)
        self.rubberBand.show()

    def rectangle(self):
        if self.startPoint is None or self.endPoint is None:
            return None
        if self.startPoint.x() == self.endPoint.x() or self.startPoint.y() == endPoint.y():
            return None
        return QgsRectangle(self.startPoint, self.endPoint)

class MSBuildingsDownloaderAlgorithm(QgsProcessingAlgorithm):
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(QgsProcessingParameterExtent('Extent', 'Define extent to download', defaultValue=None))
        self.addParameter(QgsProcessingParameterString('API_key', 'API key', multiLine=False, defaultValue=''))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, self.tr('Output Raster')))

    def processAlgorithm(self, parameters, context, feedback):
        crs = self.parameterAsExtentCrs(parameters, "Extent", context)
        extent = self.parameterAsExtentGeometry(parameters, "Extent", context).boundingBox()
        if crs.authid() != "EPSG:4326":
            extent = QgsCoordinateTransform(
                crs,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            ).transformBoundingBox(extent)
        # Now use extent.xMinimum(), extent.xMaximum(), etc. for your download logic
        # ... (your download logic here)
        feedback.pushInfo(f"Download extent in WGS84: {extent.yMinimum()}, {extent.xMinimum()}, {extent.yMaximum()}, {extent.xMaximum()}")
        # Placeholder: just return the output path
        return {self.OUTPUT: parameters[self.OUTPUT]}

    def name(self):
        return 'buildings_downloader'

    def displayName(self):
        return self.tr('Buildings Downloader')

    def group(self):
        return self.tr('Buildings Tools')

    def groupId(self):
        return 'buildings_tools'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return MSBuildingsDownloaderAlgorithm()

# Register the algorithm in the plugin
class MSBuildingsRoadsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.provider = None

    def initGui(self):
        from qgis.core import QgsApplication, QgsProcessingProvider
        class CustomProvider(QgsProcessingProvider):
            def id(self):
                return 'msbuildings'
            def name(self):
                return 'MS Buildings & Roads'
            def loadAlgorithms(self):
                self.addAlgorithm(MSBuildingsDownloaderAlgorithm())
        self.provider = CustomProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        self.action = QAction(QIcon(os.path.join(os.path.dirname(__file__), 'icon.png')),
                              "Download MS Buildings/Roads",
                              self.iface.mainWindow())
        self.action.setObjectName("msBuildingsRoadsAction")
        self.action.setWhatsThis("Download Microsoft buildings and/or roads for a specified area")
        self.action.setStatusTip("Download Microsoft buildings and/or roads for a specified area")
        self.iface.addPluginToMenu("&MS Buildings & Roads", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        from qgis.core import QgsApplication
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        self.iface.removePluginMenu("&MS Buildings & Roads", self.action)
        self.iface.removeToolBarIcon(self.action) 